"""Vocabulary-only size audit; no model inference or SQL execution."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.text_to_sql_top3.descriptions import load_descriptions
from experiments.text_to_sql_top3.context_audit.measure_context import VocabCounter, render_chat
from experiments.text_to_sql_top3.generation import messages
from experiments.text_to_sql_top3.profiles import profile_identity
from experiments.text_to_sql_top3.schema import load_config, load_prepared_sql, physical_views
from experiments.view_selection_ablation.common import (
    digest_json,
    file_hash,
    read_json,
    write_json,
)


def main():
    config = load_config()
    manifest, inventories, cases, schemas = load_prepared_sql(config)
    annotations, dictionary_identity = load_descriptions(config, inventories, schemas, cases)
    profiles = {}
    for key in {c["inventory_key"] for c in cases}:
        profiles[key] = {}
        needed = {
            v
            for c in cases
            if c["inventory_key"] == key
            for v in physical_views(c["saved_route"]["ranking"][:6], inventories[key])
        }
        for view in needed:
            identity = profile_identity(inventories[key], view, schemas[key][view])
            cache = read_json(
                Path(config["statistics_directory"]) / (digest_json(identity) + ".json")
            )
            assert cache["identity"] == identity
            assert cache["fingerprint"] == digest_json(
                {k: v for k, v in cache.items() if k != "fingerprint"}
            )
            profiles[key][view] = cache["profile"]
    dll = Path(manifest["server"]["path"]).parent / "libllama.dll"
    previous_audit = Path(config["prepared_directory"]).parent / "context_audit/SUMMARY.json"
    if previous_audit.is_file():
        assert (
            file_hash(dll) == read_json(previous_audit)["llama_dll_sha256"]
        ), "Tokenizer DLL changed; recheck ABI."
    counter = VocabCounter(Path(manifest["model"]["path"]), dll)
    rows = []
    budget = config["context_tokens"] - config["max_new_tokens"] - 16
    try:
        for case in cases:
            key = case["inventory_key"]
            for limit in (1, 3, 6):
                views = physical_views(case["saved_route"]["ranking"][:limit], inventories[key])
                chat = messages(case["question"], views, schemas[key], profiles[key])
                base = counter.count(render_chat(chat))
                chat = messages(
                    case["question"], views, schemas[key], profiles[key], annotations[key]
                )
                total = counter.count(render_chat(chat))
                rows.append(
                    {
                        "id": case["id"],
                        "database": key,
                        "top_k": limit,
                        "baseline_tokens": base,
                        "dictionary_tokens": total,
                        "added_tokens": total - base,
                        "over_budget": total > budget,
                    }
                )
            if len(rows) % 300 == 0:
                print(f"Measured {len(rows)//3}/{len(cases)} questions x Top1/3/6", flush=True)
        result = {
            "questions": len(cases),
            "input_budget": budget,
            "context_tokens": config["context_tokens"],
            "output_reserve": config["max_new_tokens"],
            "top_k": {},
            "scope": "All observed column statistics retained; only resolved dictionary descriptions are appended. Custom meanings and ambiguous paths are not invented. No etc-key expansion.",
            "model_loaded": False,
            "model_weights_loaded": False,
            "vocabulary_loaded": True,
            "inference_executed": False,
            "prepared_fingerprint": manifest["fingerprint"],
            "script_sha256": file_hash(Path(__file__)),
            "dictionary_source_manifest_sha256": file_hash(
                Path(config["column_dictionary_directory"]) / "source_manifest.json"
            ),
            "dictionary_identity": dictionary_identity,
            "generation_source_sha256": file_hash(
                ROOT / "experiments/text_to_sql_top3/generation.py"
            ),
            "dll_sha256": file_hash(dll),
        }
        for limit in (1, 3, 6):
            subset = [r for r in rows if r["top_k"] == limit]
            values = sorted(r["dictionary_tokens"] for r in subset)
            result["top_k"][str(limit)] = {
                "baseline_mean": statistics.mean(r["baseline_tokens"] for r in subset),
                "mean": statistics.mean(values),
                "median": statistics.median(values),
                "p95": values[__import__("math").ceil(0.95 * len(values)) - 1],
                "max": values[-1],
                "overflow_questions": sum(r["over_budget"] for r in subset),
                "new_overflow_questions": sum(
                    r["over_budget"] and r["baseline_tokens"] <= budget for r in subset
                ),
                "maximum": max(subset, key=lambda r: r["dictionary_tokens"]),
            }
        output = ROOT / "docs/column_descriptions/context_audit"
        output.mkdir(exist_ok=True)
        write_json(output / "SUMMARY.json", result)
        with (output / "by_question.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        counter.close()


if __name__ == "__main__":
    main()
