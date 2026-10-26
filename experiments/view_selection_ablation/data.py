from __future__ import annotations

from collections import Counter
from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3

from .common import HERE, check_pin, digest_json, file_hash, file_pin, read_json, write_json
from .gguf import label_pool, read_metadata


def inventory_databases(root, descriptions):
    inventories = {}
    for path in sorted(Path(root).rglob("*.sqlite")):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        build_path = path.with_suffix(".build.json")
        build = read_json(build_path)
        schema = build["schema"]
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            names = sorted(r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name GLOB 'View_*'"
            ))
        groups = sorted({name.removesuffix("CustomPset") for name in names})
        if any(name not in descriptions[schema] for name in groups):
            raise ValueError(f"Description missing for {relative}")
        key = relative.with_suffix("").as_posix()
        inventories[key] = {"database": file_pin(path), "build": file_pin(build_path, full_hash=True),
                            "schema": schema, "physical_views": names, "groups": groups,
                            "descriptions": {name: descriptions[schema][name] for name in groups}}
    return inventories


def compile_reference(path, sql, declared):
    """Compile an old reference against current Views; do not execute its query."""
    if not sql or not sql.lstrip().upper().startswith(("SELECT", "WITH")):
        return "Reference is not a SELECT/WITH statement"
    declared = set(declared)
    reads = set()
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.execute("PRAGMA query_only=ON")

        def authorize(action, first, second, database, context):
            if action == sqlite3.SQLITE_READ:
                if first.startswith("View_"):
                    reads.add(first)
                if context and context.startswith("View_"):
                    reads.add(context)
                if first.startswith("_sv_") and not second and not context:
                    reads.add("View_" + first.removeprefix("_sv_"))
                return sqlite3.SQLITE_OK
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        con.set_authorizer(authorize)
        try:
            con.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        except sqlite3.Error as exc:
            return str(exc)
    normalized = {name.removesuffix("CustomPset") for name in reads}
    if normalized != declared:
        return "Compiled View reads differ from the stored reference: " + str(sorted(normalized))
    return None


def prepare(config, *, log=print):
    target = Path(config["prepared_directory"])
    if (target / "manifest.json").exists():
        raise FileExistsError("Prepared data already exists. Use check, or choose a new prepared_directory.")
    catalog = read_json(config["description_catalog"])
    inventories = inventory_databases(Path(config["databases"]), catalog)
    references = {str(row["id"]): row for row in read_json(config["reference_json"])}
    judgments = {str(row["id"]): row for row in read_json(config["judgments_json"])}
    investigations = {str(row["id"]): row["investigation"]
                      for row in read_json(config["investigations_json"])}
    with Path(config["question_csv"]).open(encoding="utf-8-sig", newline="") as stream:
        questions = list(csv.DictReader(stream))
    cases, reviews = [], []
    counts = Counter()
    for question in questions:
        qid = str(question["id"])
        reference = references.get(qid, {})
        judgment = judgments.get(qid, {})
        key = question["project"] + "/" + question["ifc_model"]
        inventory = inventories.get(key)
        if reference.get("question") != question["question"]:
            raise ValueError(f"Reference question does not match benchmark question {qid}")
        reasons = []
        groups = sorted({v.removesuffix("CustomPset") for v in reference.get("referenced_views", [])})
        if not inventory:
            reasons.append("missing_current_database")
        cross = bool(reference.get("attached_databases")) or any("." in v for v in groups)
        if cross:
            reasons.append("historical_sql_has_other_database_attachments_or_references")
        if not groups:
            reasons.append("no_reference_views")
        missing = sorted(set(groups) - set(inventory["groups"])) if inventory else groups
        if missing:
            reasons.append("reference_view_missing_in_current_database")
        compile_error = None
        if inventory and groups and not cross and not missing:
            compile_error = compile_reference(inventory["database"]["path"], reference.get("sql"), groups)
            if compile_error:
                reasons.append("reference_sql_does_not_compile_with_current_views")
        if investigations.get(qid, {}).get("first_difference") == "view_to_sql":
            reasons.append("prior_audit_found_sql_selection_error")
        trusted = bool(reference.get("status") == "reviewed" and judgment.get("correct") is True
                       and reference.get("answerability") == "yes")
        if not trusted:
            reasons.append("legacy_reference_not_fully_answerable_and_correct")
        primary = not reasons
        # Benchmark scope comes from the CSV, never from historical SQL attachments.
        # An unsuitable SQL reference disables automatic scoring, not inference.
        run_eligible = inventory is not None
        counts["questions"] += 1
        counts["run_eligible"] += run_eligible
        counts["historical_reference_subset"] += primary
        counts.update("reason:" + reason for reason in reasons)
        cases.append({
            "id": qid, "question": question["question"], "project": question["project"],
            "ifc_model": question["ifc_model"], "category": question["category"],
            "inventory_key": key, "run_eligible": run_eligible,
            "reference_view_sets": [groups] if groups and not cross and not missing else [],
            "historical_reference_eligible": primary, "reference_exclusion_reasons": reasons,
            "reference_compile_error": compile_error,
            "reference_basis": "historical reviewed SQL View set, not independently annotated minimal routing gold",
        })
        reviews.append({"id": qid, "status": "pending", "accepted_view_sets": [],
                        "suggested_reference_view_sets": cases[-1]["reference_view_sets"], "notes": ""})
        if len(cases) % 100 == 0:
            log(f"Prepared reference/inventory metadata for {len(cases)}/{len(questions)} questions...")
    model = Path(config["model_path"])
    log("Reading GGUF metadata without loading model tensors...")
    metadata = read_metadata(model)
    maximum = max(len(inv["groups"]) for inv in inventories.values())
    labels = label_pool(metadata, max(maximum, 2))
    compact_metadata = {key: value for key, value in metadata.items()
                        if key not in ("tokenizer.ggml.tokens", "tokenizer.chat_template")}
    log("Hashing the downloaded GGUF for reproducibility; this is a file read, not inference...")
    model_pin = file_pin(model, full_hash=True)
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "inventory.json", inventories)
    write_json(target / "labels.json", labels)
    write_json(target / "model_metadata.json", compact_metadata)
    write_json(target / "label_review.json", reviews)
    with (target / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for case in cases:
            stream.write(json.dumps(case, ensure_ascii=False, allow_nan=False) + "\n")
    inputs = {key: file_pin(config[key], full_hash=True) for key in (
        "question_csv", "reference_json", "judgments_json", "investigations_json", "description_catalog")}
    inputs["decisio_profile"] = file_pin(HERE / "decisio_prompt_profile.json", full_hash=True)
    frozen = {name: file_hash(target / name) for name in (
        "inventory.json", "labels.json", "model_metadata.json", "cases.jsonl")}
    manifest = {
        "format": "BIBIM-view-name-ablation/1", "prepared_only": True, "model_inference_executed": False,
        "question_scope": "csv_project_ifc_model_only",
        "model": model_pin, "server": file_pin(config["llama_server"], full_hash=True),
        "inputs": inputs, "frozen_files": frozen, "counts": dict(counts),
        "databases": len(inventories), "physical_views": sum(len(i["physical_views"]) for i in inventories.values()),
        "maximum_options": maximum, "conditions": ["name_only", "name_description"],
        "manual_semantic_gold_approved": 0,
        "notes": ["A main View and CustomPset companion are one option.",
                  "Only database View inventories are read; no reference SELECT is executed.",
                  "Legacy SQL is compiled with EXPLAIN QUERY PLAN only.",
                  "Historical-reference agreement is not a claim of semantic routing accuracy.",
                  "Each question uses only the database specified by its CSV project and ifc_model.",
                  "Historical SQL attachments do not exclude questions or expand their candidate database scope."],
    }
    manifest["fingerprint"] = digest_json(manifest)
    write_json(target / "manifest.json", manifest)
    log(json.dumps({key: manifest[key] for key in ("counts", "databases", "physical_views", "maximum_options")}, indent=2))
    return manifest


def load_prepared(config):
    folder = Path(config["prepared_directory"])
    manifest = read_json(folder / "manifest.json")
    fingerprint = manifest["fingerprint"]
    if digest_json({k: v for k, v in manifest.items() if k != "fingerprint"}) != fingerprint:
        raise ValueError("Prepared manifest fingerprint is invalid")
    if manifest.get("question_scope") != "csv_project_ifc_model_only":
        raise ValueError("Prepared data uses the obsolete scope policy; prepare a new dataset")
    for name, expected in manifest["frozen_files"].items():
        if file_hash(folder / name) != expected:
            raise ValueError("Frozen prepared file changed: " + name)
    for key, pin in manifest["inputs"].items():
        configured = HERE / "decisio_prompt_profile.json" if key == "decisio_profile" else Path(config[key])
        if configured.resolve() != Path(pin["path"]):
            raise ValueError("Configured input differs from prepared input: " + key)
        check_pin(pin, full_hash=True)
    check_pin(manifest["model"])
    if str(Path(config["model_path"]).resolve()) != manifest["model"]["path"]:
        raise ValueError("Configured model differs from prepared model")
    inventory = read_json(folder / "inventory.json")
    for key, item in inventory.items():
        expected_database = (Path(config["databases"]) / (key + ".sqlite")).resolve()
        if expected_database != Path(item["database"]["path"]):
            raise ValueError("Configured database root differs from prepared inventory")
        check_pin(item["database"])
        check_pin(item["build"], full_hash=True)
    cases = [json.loads(line) for line in (folder / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line]
    return manifest, inventory, cases, read_json(folder / "labels.json")


def review_labels(config, inventory, cases):
    path = Path(config["prepared_directory"]) / "label_review.json"
    by_id = {case["id"]: case for case in cases}
    result = {}
    excluded = set()
    seen = set()
    for record in read_json(path):
        qid = str(record["id"])
        if qid in seen or qid not in by_id:
            raise ValueError("Duplicate or unknown reviewed label ID: " + qid)
        seen.add(qid)
        if record["status"] not in ("pending", "approved", "excluded"):
            raise ValueError("Invalid review status: " + qid)
        if record["status"] == "excluded":
            excluded.add(qid)
            continue
        if record["status"] != "approved":
            continue
        case = by_id[qid]
        if not case["run_eligible"]:
            raise ValueError("Cannot approve a case outside this experiment's scope: " + qid)
        options = set(inventory[case["inventory_key"]]["groups"])
        sets = record["accepted_view_sets"]
        if not sets or not all(isinstance(s, list) and s and set(s) <= options and len(s) == len(set(s)) for s in sets):
            raise ValueError("Approved labels require valid, nonempty accepted View sets: " + qid)
        result[qid] = sets
    return result, excluded
