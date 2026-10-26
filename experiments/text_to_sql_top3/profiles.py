"""Exact, reusable column profiles from public Views; never open a writable source DB."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
import heapq
import json
import math
from pathlib import Path
import sqlite3
import time

from experiments.view_selection_ablation.common import (
    check_pin,
    digest_json,
    read_json,
    write_json,
)
from .schema import physical_views, quote, retrieval_schedule

FORMAT = "BIBIM-column-statistics/1"
TOP_VALUES = 3
MAX_VALUE_CHARACTERS = 160
POLICY = {
    "format": FORMAT,
    "top_values": TOP_VALUES,
    "max_value_characters": MAX_VALUE_CHARACTERS,
    "ties": "count descending, SQLite storage type ascending, value ascending BINARY",
    "excluded_value_columns": ["ifc_id", "globalid", "step_id", "etc"],
    "sampling": False,
}


def context_mode(config):
    return config.get("column_context", "names_only")


def context_policy(config):
    if context_mode(config) == "names_only":
        policy = "Physical View and column names only; no descriptions, types, values, statistics, etc keys, reference SQL or answers"
    else:
        policy = (
            "Physical View/column names; one row count per View; observed SQLite storage types, "
            "exact top-3 non-NULL values with counts, and NULL count per column. "
            "Identifier and etc/JSON payload values are omitted. Long values are marked previews. "
            "No column descriptions, JSON-key inventory, reference SQL or benchmark answers."
        )
    if config.get("column_descriptions", False):
        policy = policy.replace("no descriptions, ", "").replace("No column descriptions, ", "No ")
        policy = policy.replace("column names only", "column names with definitions")
        policy += " Short English IFC/BIBIM column dictionary descriptions are supplied for matched fields; custom meanings and ambiguous source paths are not invented."
    return policy


def _storage_type(value):
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    if isinstance(value, str):
        return "TEXT"
    if isinstance(value, bytes):
        return "BLOB"
    raise ValueError("Unsupported SQLite value type: " + type(value).__name__)


def _top_entry(storage, value, count):
    result = {"count": count}
    if storage == "TEXT" and len(value) > MAX_VALUE_CHARACTERS:
        result.update(preview=value[:MAX_VALUE_CHARACTERS], truncated=True)
    elif storage == "REAL" and not math.isfinite(value):
        result.update(preview=str(value), non_finite=True)
    else:
        result["value"] = value
    return result


def profile_view(inventory, view, columns, *, spill_entries=100_000, timeout=600):
    """One streaming View scan; spill exact frequency counts to a temporary DB if needed."""
    if view not in inventory["physical_views"]:
        raise ValueError("Statistics View is outside the selected database: " + view)
    check_pin(inventory["database"])
    started = time.monotonic()
    uri = Path(inventory["database"]["path"]).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source, closing(sqlite3.connect("")) as spill:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        source.set_progress_handler(lambda: int(time.monotonic() - started > timeout), 10_000)
        info = list(source.execute("PRAGMA table_info(" + quote(view) + ")"))
        if [r[1] for r in info] != columns:
            raise ValueError("Profile column names differ from the frozen schema: " + view)
        result = {
            "row_count": 0,
            "columns": {
                r[1]: {
                    "declared_type": r[2],
                    "observed_types": {},
                    "null_count": 0,
                    "top_values": [],
                }
                for r in info
            },
        }
        frequencies = Counter()
        frequency_bytes = 0
        spilled = False
        excluded = set(POLICY["excluded_value_columns"])
        allow_values = [c.casefold() not in excluded for c in columns]
        # No affinity on value: actual INTEGER/REAL/TEXT encodings stay distinct.
        spill.execute(
            "CREATE TABLE counts (col INTEGER, kind TEXT, value, n INTEGER, "
            "PRIMARY KEY(col,kind,value)) WITHOUT ROWID"
        )

        def flush():
            nonlocal spilled, frequency_bytes
            spill.executemany(
                "INSERT INTO counts VALUES (?,?,?,?) ON CONFLICT(col,kind,value) "
                "DO UPDATE SET n=n+excluded.n",
                ((*key, count) for key, count in frequencies.items()),
            )
            frequencies.clear()
            frequency_bytes = 0
            spilled = True

        metadata = list(result["columns"].values())
        for row in source.execute("SELECT * FROM " + quote(view)):
            result["row_count"] += 1
            for i, value in enumerate(row):
                item = metadata[i]
                if value is None:
                    item["null_count"] += 1
                    continue
                storage = _storage_type(value)
                item["observed_types"][storage] = item["observed_types"].get(storage, 0) + 1
                if allow_values[i] and storage != "BLOB":
                    key = (i, storage, value)
                    if key not in frequencies:
                        frequency_bytes += 160 + (len(value) * 4 if storage == "TEXT" else 8)
                    frequencies[key] += 1
            if len(frequencies) >= spill_entries or frequency_bytes >= 64 * 1024 * 1024:
                flush()
            if time.monotonic() - started > timeout:
                raise TimeoutError("Column profiling timeout: " + view)
        if spilled:
            flush()
            spill.execute("CREATE INDEX rank_counts ON counts(col,n DESC,kind,value)")
        by_column = [[] for _ in columns]
        if not spilled:
            for (col, kind, value), count in frequencies.items():
                by_column[col].append((kind, value, count))
        for i, column in enumerate(columns):
            item = metadata[i]
            item["data_type"] = "|".join(sorted(item["observed_types"])) or "UNKNOWN"
            if not allow_values[i]:
                item["values_omitted"] = (
                    "json_payload" if column.casefold() == "etc" else "identifier"
                )
                continue
            if spilled:
                top = spill.execute(
                    "SELECT kind,value,n FROM counts WHERE col=? "
                    "ORDER BY n DESC,kind,value COLLATE BINARY LIMIT ?",
                    (i, TOP_VALUES),
                ).fetchall()
            else:
                top = heapq.nsmallest(
                    TOP_VALUES,
                    by_column[i],
                    key=lambda x: (-x[2], x[0], x[1]),
                )
            item["top_values"] = [_top_entry(*entry) for entry in top]
        source.rollback()
    check_pin(inventory["database"])
    return result


def profile_identity(inventory, view, columns):
    return {
        "policy": POLICY,
        "database": inventory["database"],
        "view": view,
        "columns": columns,
    }


def load_profiles(config, inventories, schemas, cases, *, log=print):
    """Build missing per-View caches before any LLM starts, then verify exact input identities."""
    if context_mode(config) == "names_only":
        return {}, {}
    directory = Path(config["statistics_directory"])
    directory.mkdir(parents=True, exist_ok=True)
    limits = retrieval_schedule(config) if config.get("answer_stage") else [config["top_k"]]
    required = {}
    for case in cases:
        key = case["inventory_key"]
        groups = (
            inventories[key]["groups"]
            if config["routing_mode"] == "live"
            else case["saved_route"]["ranking"][: max(limits)]
        )
        required.setdefault(key, set()).update(physical_views(groups, inventories[key]))
    profiles, fingerprints = {}, {}
    total = sum(map(len, required.values()))
    completed = 0
    for key, views in sorted(required.items()):
        profiles[key], fingerprints[key] = {}, {}
        for view in sorted(views):
            identity = profile_identity(inventories[key], view, schemas[key][view])
            path = directory / (digest_json(identity) + ".json")
            if path.exists():
                payload = read_json(path)
                body = {k: v for k, v in payload.items() if k != "fingerprint"}
                if payload.get("identity") != identity or digest_json(body) != payload.get(
                    "fingerprint"
                ):
                    raise ValueError("Column statistics cache is corrupt: " + str(path))
            else:
                log(f"Profiling View {completed + 1}/{total}: {key}/{view}")
                payload = {
                    "identity": identity,
                    "profile": profile_view(
                        inventories[key],
                        view,
                        schemas[key][view],
                        timeout=config.get("statistics_timeout_seconds", 600),
                    ),
                }
                payload["fingerprint"] = digest_json(payload)
                temporary = path.with_suffix(".tmp")
                write_json(temporary, payload)
                temporary.replace(path)
            profiles[key][view] = payload["profile"]
            fingerprints[key][view] = payload["fingerprint"]
            completed += 1
        check_pin(inventories[key]["database"])
    return profiles, fingerprints


def render_statistics(view, columns, profile):
    if list(profile["columns"]) != columns:
        raise ValueError("Column profile schema differs: " + view)
    lines = [quote(view) + " | rows=" + str(profile["row_count"])]
    for column in columns:
        item = profile["columns"][column]
        values = [
            [entry.get("value", {k: v for k, v in entry.items() if k != "count"}), entry["count"]]
            for entry in item["top_values"]
        ]
        top = (
            "omitted"
            if "values_omitted" in item or item["data_type"] == "BLOB"
            else json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        )
        lines.append(f'{quote(column)} | {item["data_type"]} | {top} | {item["null_count"]}')
    return "\n".join(lines)
