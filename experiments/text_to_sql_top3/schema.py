from __future__ import annotations

from contextlib import closing
import csv
import json
import math
from pathlib import Path
import sqlite3

from experiments.view_selection_ablation.common import (
    check_pin,
    digest_json,
    file_hash,
    file_pin,
    read_json,
    resolve,
    write_json,
)
from experiments.view_selection_ablation.data import load_prepared
from experiments.view_selection_ablation.evaluation import rank_options

HERE = Path(__file__).resolve().parent


def retrieval_schedule(config):
    first = config["top_k"]
    return config.get("retrieval_limits", [first] + [k for k in (3, 6) if k > first])


def load_config(path=None):
    config = read_json(path or HERE / "config.json")
    for key in ("routing_run", "prepared_directory", "results_directory"):
        config[key] = str(resolve(config[key]))
    if config["routing_mode"] not in ("saved", "live"):
        raise ValueError("routing_mode must be saved or live")
    if config["routing_condition"] not in ("name_only", "name_description"):
        raise ValueError("Unknown routing condition")
    if type(config["top_k"]) is not int or config["top_k"] < 1 or config["routing_repeat"] < 0:
        raise ValueError("top_k must be a positive integer and routing_repeat nonnegative")
    if config["max_new_tokens"] < 1 or config["context_tokens"] <= config["max_new_tokens"]:
        raise ValueError("Context must leave room for both input and output")
    if config["sql_timeout_seconds"] <= 0 or config["max_result_rows"] < 1:
        raise ValueError("Invalid SQL execution limits")
    if config.get("column_context", "names_only") not in ("names_only", "statistics"):
        raise ValueError("column_context must be names_only or statistics")
    if config.get("column_context") == "statistics":
        config["statistics_directory"] = str(resolve(config["statistics_directory"]))
        if config.get("statistics_timeout_seconds", 600) <= 0:
            raise ValueError("Invalid statistics timeout")
    if type(config.get("column_descriptions", False)) is not bool:
        raise ValueError("column_descriptions must be a boolean")
    if config.get("column_descriptions", False):
        config["column_dictionary_directory"] = str(
            resolve(config.get("column_dictionary_directory", "docs/column_descriptions"))
        )
    if config.get("answer_stage"):
        limits = retrieval_schedule(config)
        if (
            not isinstance(limits, list)
            or not limits
            or limits[0] != config["top_k"]
            or any(type(k) is not int or k < 1 for k in limits)
            or any(b <= a for a, b in zip(limits, limits[1:]))
        ):
            raise ValueError(
                "retrieval_limits must start at top_k and contain strictly increasing positive integers"
            )
        if (
            not 0
            < config.get("answer_max_new_tokens", config["max_new_tokens"])
            < config["context_tokens"]
        ):
            raise ValueError("Invalid answer output budget")
    if type(config.get("inspection_enabled", False)) is not bool:
        raise ValueError("inspection_enabled must be boolean")
    for key in ('view_scores_context', 'sql_feedback_enabled', 'inspection_repair_enabled'):
        if type(config.get(key, False)) is not bool:
            raise ValueError(key+' must be boolean')
    rewrites = config.get('main_sql_max_rewrites', 0)
    if type(rewrites) is not int or not 0 <= rewrites <= 1:
        raise ValueError('main_sql_max_rewrites must be zero or one per question')
    if rewrites and (not config.get('sql_feedback_enabled') or not config.get('answer_stage')):
        raise ValueError('Main SQL rewrite requires answer-stage SQL feedback')
    if config.get('inspection_repair_enabled') and not config.get('inspection_enabled'):
        raise ValueError('Inspection repair requires inspection_enabled')
    if config.get("inspection_enabled", False):
        if not config.get("answer_stage"):
            raise ValueError("Inspection requires the answer-stage workflow")
        cap = config.get("inspection_max_queries", 3)
        if type(cap) is not int or not 0 <= cap <= 3:
            raise ValueError("inspection_max_queries must be between zero and three per question")
        for key, default in (("inspection_max_rows", 50), ("inspection_max_new_tokens", 512)):
            if type(config.get(key, default)) is not int or config.get(key, default) < 1:
                raise ValueError("Invalid " + key)
        if config.get("inspection_timeout_seconds", 10) <= 0:
            raise ValueError("Invalid inspection timeout")
    for key in ('single_pass', 'statistics_on_demand'):
        if type(config.get(key, False)) is not bool:
            raise ValueError(key+' must be boolean')
    if config.get('single_pass'):
        from .single_pass import validate_single_pass
        validate_single_pass(config)
    return config


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def physical_views(groups, inventory):
    available = set(inventory["physical_views"])
    result = []
    for group in groups:
        if group not in inventory["groups"]:
            raise ValueError("Retrieved group is outside the question's database: " + group)
        for view in (group, group + "CustomPset"):
            if view in available:
                result.append(view)
    return result


def column_inventory(inventory):
    """Read schema metadata only: no values, JSON keys, statistics, or descriptions."""
    result = {}
    with closing(
        sqlite3.connect(
            Path(inventory["database"]["path"]).resolve().as_uri() + "?mode=ro", uri=True
        )
    ) as con:
        con.execute("PRAGMA query_only=ON")
        for view in inventory["physical_views"]:
            columns = [row[1] for row in con.execute("PRAGMA table_info(" + quote(view) + ")")]
            if not columns:
                raise ValueError("Empty or missing View schema: " + view)
            result[view] = columns
    return result


def saved_routes(path, cases, inventories, condition, repeat):
    by_id = {case["id"]: case for case in cases}
    found = {}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record["condition"] != condition or record["repeat"] != repeat:
                continue
            qid = record["id"]
            if qid in found or qid not in by_id or record["status"] != "ok":
                raise ValueError("Duplicate, unknown, or failed saved route: " + qid)
            case = by_id[qid]
            expected = set(inventories[case["inventory_key"]]["groups"])
            options, ranking = record["options"], record["ranking"]
            probabilities = record["score"]["probabilities"]
            if (
                record["project"] != case["project"]
                or len(options) != len(expected)
                or set(options) != expected
                or len(ranking) != len(expected)
                or set(ranking) != expected
                or set(probabilities) != expected
            ):
                raise ValueError("Saved route database scope differs from the CSV: " + qid)
            values = list(probabilities.values())
            if not all(math.isfinite(p) and p >= 0 for p in values) or not math.isclose(
                sum(values), 1, abs_tol=1e-5
            ):
                raise ValueError("Invalid saved routing probabilities: " + qid)
            if rank_options(probabilities) != ranking:
                raise ValueError("Saved routing rank differs from probabilities: " + qid)
            found[qid] = {
                "ranking": ranking,
                "probabilities": probabilities,
                "elapsed_ms": record["score"]["elapsed_ms"],
                "source_record_sha256": digest_json(record),
            }
    if set(found) != set(by_id):
        raise ValueError("Saved routing does not cover every benchmark question")
    return found


def prepare(config, log=print):
    if config.get("input_mode") == "custom_live":
        from .custom_input import prepare_custom
        return prepare_custom(config, log=log)
    target = Path(config["prepared_directory"])
    if target.exists():
        raise FileExistsError("Prepared directory already exists; use check or a new directory")
    routing_run = Path(config["routing_run"])
    source = read_json(routing_run / "run_manifest.json")
    manifest, inventories, cases, labels = load_prepared(source["config"])
    if source["prepared_fingerprint"] != manifest["fingerprint"]:
        raise ValueError("Routing run used a different prepared dataset")
    if source["config"]["backend"] != "llama_cpp_decisio_port":
        raise ValueError("This single-model baseline expects the local Gemma Decisio port")
    if set(source["question_ids"]) != {case["id"] for case in cases}:
        raise ValueError("Choose a full routing run, not a pilot")
    routes = saved_routes(
        routing_run / "records.jsonl",
        cases,
        inventories,
        config["routing_condition"],
        config["routing_repeat"],
    )
    schemas = {}
    for key, inventory in inventories.items():
        log("Reading column names only: " + key)
        schemas[key] = column_inventory(inventory)
    with Path(source["config"]["question_csv"]).open(encoding="utf-8-sig", newline="") as stream:
        benchmark = {str(row["id"]): row for row in csv.DictReader(stream)}
    references = {str(row["id"]): row for row in read_json(source["config"]["reference_json"])}
    reference_queries = {}
    prepared = []
    for case in cases:
        original = benchmark[case["id"]]
        if (case["question"], case["inventory_key"]) != (
            original["question"],
            original["project"] + "/" + original["ifc_model"],
        ):
            raise ValueError("Case differs from benchmark CSV")
        groups = routes[case["id"]]["ranking"][: config["top_k"]]
        prepared.append(
            {
                **case,
                "saved_route": routes[case["id"]],
                "selected_groups": groups,
                "selected_views": physical_views(groups, inventories[case["inventory_key"]]),
                "ground_truth_for_review_only": original["ground_truth"],
            }
        )
        if case["historical_reference_eligible"]:
            ref = references[case["id"]]
            reference_queries[case["id"]] = {
                "sql": ref["sql"],
                "views": ref["referenced_views"],
                "basis": "historical reviewed SQL replayed on the current CSV-designated database",
            }
    target.mkdir(parents=True)
    write_json(target / "inventory.json", inventories)
    write_json(target / "schemas.json", schemas)
    write_json(target / "labels.json", labels)
    write_json(target / "reference_queries.json", reference_queries)
    profile = (
        Path(__file__).resolve().parents[1] / "view_selection_ablation/decisio_prompt_profile.json"
    )
    write_json(target / "routing_profile.json", read_json(profile))
    with (target / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for case in prepared:
            stream.write(json.dumps(case, ensure_ascii=False) + "\n")
    files = (
        "inventory.json",
        "schemas.json",
        "labels.json",
        "reference_queries.json",
        "routing_profile.json",
        "cases.jsonl",
    )
    result = {
        "format": "BIBIM-top3-column-names-sql/1",
        "question_scope": "csv_project_ifc_model_only",
        "routing_condition": config["routing_condition"],
        "routing_repeat": config["routing_repeat"],
        "questions": len(prepared),
        "databases": len(inventories),
        "historical_reference_questions": len(reference_queries),
        "routing_source": file_pin(routing_run / "records.jsonl", full_hash=True),
        "routing_manifest": file_pin(routing_run / "run_manifest.json", full_hash=True),
        "prepared_source_fingerprint": manifest["fingerprint"],
        "model": source["model"],
        "server": source["server"],
        "base_server_config": source["config"],
        "frozen_files": {name: file_hash(target / name) for name in files},
        "model_loaded": False,
        "inference_executed": False,
        "context_policy": "Physical View and column names only; no descriptions, types, values, statistics, etc keys, reference SQL or answers",
    }
    result["fingerprint"] = digest_json(result)
    write_json(target / "manifest.json", result)
    return result


def load_prepared_sql(config):
    target = Path(config["prepared_directory"])
    manifest = read_json(target / "manifest.json")
    if (
        digest_json({k: v for k, v in manifest.items() if k != "fingerprint"})
        != manifest["fingerprint"]
    ):
        raise ValueError("SQL prepared manifest fingerprint differs")
    if manifest["question_scope"] != "csv_project_ifc_model_only":
        raise ValueError("Unsupported database scope")
    if (manifest["routing_condition"], manifest["routing_repeat"]) != (
        config["routing_condition"],
        config["routing_repeat"],
    ):
        raise ValueError("Prepare a separate dataset for another routing condition/repeat")
    for name, expected in manifest["frozen_files"].items():
        if file_hash(target / name) != expected:
            raise ValueError("SQL prepared input changed: " + name)
    if manifest.get("input_mode") == "custom_live":
        if config.get("input_mode") != "custom_live" or config["routing_mode"] != "live":
            raise ValueError("Custom questions require live routing; no saved routes exist")
        if manifest["custom_input_config"] != {k: config[k] for k in ("question_csv", "database_map", "backend_config")}:
            raise ValueError("Custom question/database configuration changed; prepare a new dataset")
        for pin in manifest["input_sources"]:
            check_pin(pin, full_hash=True)
    else:
        for pin in (manifest["routing_source"], manifest["routing_manifest"]):
            check_pin(pin, full_hash=True)
        if Path(manifest["routing_source"]["path"]).parent != Path(config["routing_run"]):
            raise ValueError("Configured routing run differs from frozen input")
    check_pin(manifest["model"])
    inventories = read_json(target / "inventory.json")
    for inventory in inventories.values():
        check_pin(inventory["database"])
        check_pin(inventory["build"], full_hash=True)
    cases = [
        json.loads(line)
        for line in (target / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(cases) != manifest["questions"] or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Duplicate or missing prepared questions")
    return manifest, inventories, cases, read_json(target / "schemas.json")
