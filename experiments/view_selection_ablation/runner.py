from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from .backend import DecisioAPI, HTTP, LlamaDecisionPort, ManagedServer
from .common import HERE, CONDITIONS, check_pin, digest_json, file_hash, read_json, write_json
from .data import load_prepared, review_labels
from .evaluation import make_report, rank_options
from .prompts import chat_messages, condition_order, ordered_options, systemone_request


def select_cases(cases, limit, seed):
    cases = [case for case in cases if case["run_eligible"]]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        cases = sorted(cases, key=lambda c: digest_json([seed, c["id"], "sample"]))[:limit]
    return sorted(cases, key=lambda c: int(c["id"]))


def plan(config, *, limit=None, repeats=None):
    manifest, inventories, cases, labels = load_prepared(config)
    selected = select_cases(cases, limit, config["seed"])
    approved, excluded = review_labels(config, inventories, cases)
    n = config["repeats"] if repeats is None else repeats
    if n < 1:
        raise ValueError("repeats must be positive")
    profile = read_json(HERE / "decisio_prompt_profile.json")
    example = None
    if selected:
        case = selected[0]
        inventory = inventories[case["inventory_key"]]
        options = ordered_options(case, inventory, config["seed"], 0)
        example = {condition: {
            "messages": chat_messages(case["question"], options, inventory["descriptions"], condition,
                                      labels, config["instructions"], profile),
            "systemone_request": systemone_request(case["question"], options, inventory["descriptions"],
                                                   condition, config["instructions"])}
            for condition in CONDITIONS}
    return {"prepared_fingerprint": manifest["fingerprint"], "model": manifest["model"],
            "backend": config["backend"], "model_loaded": False, "inference_executed": False,
            "questions_available": len(cases), "selected_questions": len(selected), "repeats": n,
            "planned_requests": len(selected) * n * 2,
            "historical_reference_questions": sum(c["historical_reference_eligible"] and c["id"] not in excluded and c["id"] not in approved for c in selected),
            "manual_semantic_gold_questions": sum(c["id"] in approved for c in selected),
            "maximum_options": max((len(inventories[c["inventory_key"]]["groups"]) for c in selected), default=0),
            "example": example}


def hardware_snapshot():
    result = {}
    try:
        response = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version,power.limit", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result["nvidia_smi"] = response.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        result["nvidia_smi"] = "unavailable"
    return result


def run(config, *, limit=None, repeats=None, resume=None):
    manifest, inventories, cases, labels = load_prepared(config)
    selected = select_cases(cases, limit, config["seed"])
    approved, excluded = review_labels(config, inventories, cases)
    profile = read_json(HERE / "decisio_prompt_profile.json")
    n = config["repeats"] if repeats is None else repeats
    if n < 1 or not selected:
        raise ValueError("No cases or invalid repeat count")
    config = {**config, "repeats": n}
    sources = {path.name: file_hash(path) for path in HERE.glob("*.py")}
    label_review_path = Path(config["prepared_directory"]) / "label_review.json"
    signature = digest_json({"prepared": manifest["fingerprint"], "config": config,
                             "ids": [c["id"] for c in selected], "code": sources,
                             "label_review_sha256": file_hash(label_review_path)})
    if resume:
        output = Path(resume).resolve()
        previous = read_json(output / "run_manifest.json")
        if previous["signature"] != signature:
            raise ValueError("Resume configuration, inputs, labels, or code differ from the original run")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f_UTC")
        output = Path(config["results_directory"]) / stamp
        output.mkdir(parents=True, exist_ok=False)
    run_manifest = {"signature": signature, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                    "prepared_fingerprint": manifest["fingerprint"], "config": config,
                    "model": manifest["model"], "server": manifest["server"], "source_hashes": sources,
                    "question_ids": [c["id"] for c in selected], "manual_gold_sha256": file_hash(label_review_path),
                    "hardware": hardware_snapshot(), "model_loaded_during_preparation": False,
                    "conditions": list(CONDITIONS), "sampling": "all eligible questions" if limit is None else "seeded subset",
                    "cache_policy": "local: cache_prompt=false, cache-ram=0, one slot; external API: server-defined",
                    "condition_order": "deterministic counterbalanced AB/BA per question and repeat",
                    "candidate_order": "seeded per question and repeat, identical in A and B",
                    "known_limits": ["Local Q5 GGUF adaptation is not the official Decisio FP8 configuration.",
                                     "Transferred temperature is not validated calibration for this quantization.",
                                     "A/B rankings are evaluated at fixed Top-k; no threshold is tuned on test labels."]}
    if not resume:
        write_json(output / "run_manifest.json", run_manifest)
        write_json(output / "label_review_snapshot.json", read_json(label_review_path))
    records_path = output / "records.jsonl"
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line] if records_path.exists() else []
    completed = {(r["id"], r["repeat"], r["condition"]) for r in records if r.get("status") == "ok"}
    planned = len(selected) * n * 2
    fatal = None
    print(f"Starting {len(selected)} questions x {n} repeats x 2 conditions. Results: {output}", flush=True)
    try:
        managed = ManagedServer(config, output, manifest["server"]) if config["backend"] == "llama_cpp_decisio_port" else nullcontext()
        with managed as server:
            url = f"http://{config['host']}:{config['port']}" if server else config["decisio_api_url"]
            client = HTTP(url, config["http_timeout_seconds"])
            backend = LlamaDecisionPort(client, config, profile, labels) if server else DecisioAPI(client, config)
            preflight = backend.preflight()
            write_json(output / "preflight.json", preflight)
            if server:
                write_json(output / "server_command.json", {"args": server.command})
            # Same independent warm-up for both conditions, excluded from all reported observations.
            warmups = {}
            for condition in CONDITIONS:
                warmups[condition] = backend.score("Find information about doors.", ["View_Door", "View_Wall"],
                                                   {"View_Door": "A door provides controlled access.",
                                                    "View_Wall": "A wall bounds or subdivides spaces."}, condition)
            write_json(output / "warmup.json", warmups)
            consecutive_errors = 0
            with records_path.open("a", encoding="utf-8", newline="\n") as stream:
                for repeat in range(n):
                    for index, case in enumerate(selected, 1):
                        inventory = inventories[case["inventory_key"]]
                        options = ordered_options(case, inventory, config["seed"], repeat)
                        for condition in condition_order(config["seed"], case["id"], repeat):
                            key = (case["id"], repeat, condition)
                            if key in completed:
                                continue
                            record = {"id": case["id"], "repeat": repeat, "condition": condition,
                                      "options": options, "project": case["project"],
                                      "timestamp_utc": datetime.now(timezone.utc).isoformat()}
                            try:
                                score = backend.score(case["question"], options, inventory["descriptions"], condition)
                                record.update(status="ok", score=score, ranking=rank_options(score["probabilities"]))
                                completed.add(key)
                                consecutive_errors = 0
                            except Exception as exc:
                                record.update(status="error", error=f"{type(exc).__name__}: {exc}")
                                consecutive_errors += 1
                            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                            stream.flush()
                            records.append(record)
                            if consecutive_errors >= 3:
                                raise RuntimeError("Stopped after three consecutive failed requests; inspect records.jsonl")
                        print(f"Repeat {repeat + 1}/{n}, question {index}/{len(selected)}, id={case['id']}; "
                              f"saved {len(completed)}/{planned} successful requests", flush=True)
            for item in inventories.values():
                check_pin(item["database"])
            check_pin(manifest["model"])
    except KeyboardInterrupt:
        fatal = "Interrupted by user"
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
    finally:
        report = make_report(output, selected, records, approved, config, planned, fatal=fatal,
                             manually_excluded=excluded)
        print("Report: " + str(output / "REPORT.md"), flush=True)
    if fatal:
        raise RuntimeError(fatal)
    return report
