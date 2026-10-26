"""Recheck reported differences after correcting column mapping, without rewriting results.

This is a targeted review of the original run's IFC-derived expected values.
It is not a fresh full IFC extraction and does not certify units of previously
unmapped fields. Original JSON/CSV/logs are preserved.
"""

import argparse
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from bibim_config import OUTPUT_ROOT
from validate_entity_views import quote, same


def review(folder):
    batch = json.loads((folder / "batch_summary.json").read_text(encoding="utf8"))
    outcomes = []
    for entry in batch["results"]:
        db = OUTPUT_ROOT / entry["database"]
        outcome = dict(database=entry["database"], resolved=[], unresolved=[])
        if entry["status"] == "error":
            metadata = json.loads(db.with_suffix(".build.json").read_text(encoding="utf8"))
            outcome["status"] = (
                "unsupported_layout" if metadata.get("layout") != "entity-pairs" else "error"
            )
            outcome["original_error"] = entry.get("error")
            outcomes.append(outcome)
            continue
        result = json.loads(
            (Path(entry["report_folder"]) / "entity_validation.json").read_text(encoding="utf8")
        )
        resolved_keys = set()
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            views = {r[0] for r in con.execute("select name from sqlite_master where type='view'")}
            manifest = json.loads(db.with_suffix(".build.json").read_text(encoding="utf8"))
            for d in result["details"]:
                if d["status"] == "unverified":
                    continue
                # Compound latitude/longitude is degrees/minutes/seconds, not a
                # scalar project PlaneAngle measure; do not require its SI label.
                if d["status"] == "unit_mismatch" and d["field"] in (
                    "RefLatitude",
                    "RefLongitude",
                ):
                    outcome["resolved"].append(
                        dict(
                            **d,
                            reason="checker incorrectly assigned a project angle unit to compound DMS coordinates",
                        )
                    )
                    resolved_keys.add((d["entity"], d["ifc_id"], "Attribute", d["field"]))
                    continue
                if d["status"] != "missing":
                    outcome["unresolved"].append(d)
                    continue
                base = "View_" + d["entity"].removeprefix("Ifc")
                names = {d["field"], re.sub(r"^(?:Pset|Qto)_", "", d["field"], flags=re.I)}
                candidates = []
                for v in (base, base + "CustomPset"):
                    if v not in views:
                        continue
                    row = con.execute(
                        "select * from " + quote(v) + " where ifc_id=?", (d["ifc_id"],)
                    ).fetchone()
                    if row is None:
                        continue
                    actual = dict(row)
                    actual.update(json.loads(actual.pop("etc", None) or "{}"))
                    folded = (
                        manifest.get("views", {})
                        .get(v, {})
                        .get("column_policy", {})
                        .get("uniform_units", {})
                    )
                    mapping = {k: k for k in actual}
                    mapping.update({k: info["column"] for k, info in folded.items()})
                    for original, column in mapping.items():
                        if column not in actual:
                            continue
                        if any(
                            re.fullmatch(re.escape(name) + r"_[0-9a-f]{10}", original)
                            for name in names
                        ):
                            candidates.append((v, column, actual[column]))
                candidates = list({(v, k): (v, k, a) for v, k, a in candidates}.values())
                if len(candidates) == 1 and same(
                    d["expected"], candidates[0][2], result["tolerance"]
                ):
                    v, k, a = candidates[0]
                    outcome["resolved"].append(
                        dict(
                            **d,
                            reason="value preserved in renamed collision column",
                            actual_column=v + "." + k,
                            actual_value=a,
                            unit_rechecked=False,
                        )
                    )
                    resolved_keys.add((d["entity"], d["ifc_id"], d["category"], d["field"]))
                else:
                    outcome["unresolved"].append(dict(**d, candidates=candidates))
        outcome["resolved_evaluated_fields"] = len(resolved_keys)
        outcome["evaluated_values"] = sum(
            s["values"].get("expected", 0) for s in result["entities"]
        )
        outcome["original_matched_values"] = sum(
            s["values"].get("matched", 0) for s in result["entities"]
        )
        outcome["reviewed_matched_values"] = outcome["original_matched_values"] + len(
            resolved_keys
        )
        outcome["status"] = (
            "unresolved" if outcome["unresolved"] else "no_remaining_reported_differences"
        )
        outcomes.append(outcome)
        print(
            entry["database"],
            len(resolved_keys),
            "resolved",
            len(outcome["unresolved"]),
            "unresolved",
            flush=True,
        )
        del result
    report = dict(
        note=__doc__,
        original_batch=str(folder),
        statuses=dict(Counter(r["status"] for r in outcomes)),
        results=outcomes,
    )
    (folder / "reviewed_differences.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
    )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    args = p.parse_args()
    review(args.folder)
