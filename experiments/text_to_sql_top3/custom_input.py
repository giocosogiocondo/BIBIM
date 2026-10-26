"""Prepare new questions for the existing live routing/SQL/answer workflow."""
import csv
import json
from pathlib import Path

from experiments.view_selection_ablation.common import (
    resolve, read_json, write_json, file_pin, file_hash, digest_json,
)
from experiments.view_selection_ablation.gguf import read_metadata, label_pool
from .schema import column_inventory


def prepare_custom(config, log=print):
    if config["routing_mode"] != "live":
        raise ValueError("Custom questions require live routing")
    target = Path(config["prepared_directory"])
    if target.exists():
        raise FileExistsError("Prepared directory already exists; use check or a new directory")
    csv_path = resolve(config["question_csv"])
    catalog = resolve(config.get("description_catalog", "docs/view_descriptions/routing_options.en.json"))
    descriptions = read_json(catalog)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Question IDs must be unique and nonempty")
    inventories, cases, schemas = {}, [], {}
    for row in rows:
        if not row["id"].strip() or not row["question"].strip():
            raise ValueError("Empty question or ID")
        key = row["project"] + "/" + row["ifc_model"]
        if key not in config["database_map"]:
            raise ValueError("No database configured for " + key)
        if key not in inventories:
            path = resolve(config["database_map"][key])
            build_path = path.with_suffix(".build.json")
            build = read_json(build_path)
            names = sorted(build["views"])
            groups = sorted({n.removesuffix("CustomPset") for n in names})
            schema = build["schema"]
            if not groups or any(g not in descriptions[schema] for g in groups):
                raise ValueError("Missing View descriptions for " + key)
            inventory = {"database": file_pin(path), "build": file_pin(build_path, full_hash=True),
                         "schema": schema, "physical_views": names, "groups": groups,
                         "descriptions": {g: descriptions[schema][g] for g in groups}}
            inventories[key] = inventory
            schemas[key] = column_inventory(inventory)
            log("Prepared schema: " + key)
        cases.append({"id": row["id"], "question": row["question"],
                      "project": row["project"], "ifc_model": row["ifc_model"],
                      "category": row.get("category", "information_retrieval"),
                      "inventory_key": key, "run_eligible": True,
                      "historical_reference_eligible": False, "reference_view_sets": [],
                      "reference_exclusion_reasons": ["new_question_no_reviewed_reference"],
                      "ground_truth_for_review_only": row.get("ground_truth", "")})
    backend = read_json(resolve(config["backend_config"]))
    for k in ("model_path", "llama_server", "cuda_dll_directory"):
        backend[k] = str(resolve(backend[k]))
    log("Reading local GGUF label metadata (no model loading)...")
    metadata = read_metadata(Path(backend["model_path"]))
    labels = label_pool(metadata, max(2, max(len(i["groups"]) for i in inventories.values())))
    log("Pinning local model and server files...")
    model = file_pin(backend["model_path"], full_hash=True)
    server = file_pin(backend["llama_server"], full_hash=True)
    target.mkdir(parents=True)
    write_json(target / "inventory.json", inventories)
    write_json(target / "schemas.json", schemas)
    write_json(target / "labels.json", labels)
    write_json(target / "reference_queries.json", {})
    profile = resolve("experiments/view_selection_ablation/decisio_prompt_profile.json")
    write_json(target / "routing_profile.json", read_json(profile))
    with (target / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for case in cases:
            stream.write(json.dumps(case, ensure_ascii=False) + "\n")
    files = ["inventory.json", "schemas.json", "labels.json", "reference_queries.json",
             "routing_profile.json", "cases.jsonl"]
    manifest = {"format": "BIBIM-top3-column-names-sql/1", "input_mode": "custom_live",
                "question_scope": "csv_project_ifc_model_only",
                "routing_condition": config["routing_condition"], "routing_repeat": config["routing_repeat"],
                "questions": len(cases), "databases": len(inventories), "historical_reference_questions": 0,
                "custom_input_config": {k: config[k] for k in ("question_csv", "database_map", "backend_config")},
                "input_sources": [file_pin(p, full_hash=True) for p in
                                  (csv_path, catalog, resolve(config["backend_config"]), profile)],
                "model": model, "server": server, "base_server_config": backend,
                "frozen_files": {n: file_hash(target / n) for n in files},
                "model_loaded": False, "inference_executed": False,
                "context_policy": "Existing v3.4.5 live Decisio routing, SQL and answer workflow; no reference answers in model inputs"}
    manifest["fingerprint"] = digest_json(manifest)
    write_json(target / "manifest.json", manifest)
    return manifest
