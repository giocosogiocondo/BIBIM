from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .runner import choose_cases, plan, run
from .profiles import context_mode, context_policy, load_profiles
from .descriptions import load_descriptions
from .schema import HERE, load_config, load_prepared_sql, prepare, retrieval_schedule


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local View routing, selected-View SQL and final answers"
    )
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser(
        "prepare", help="Freeze schema names and saved routes; no model loading"
    )
    check = sub.add_parser(
        "check", help="Verify prepared files and database pins; no model loading"
    )
    preview = sub.add_parser("plan", help="Show request counts and an example; no model loading")
    profiling = sub.add_parser(
        "profile", help="Build exact column statistics from read-only Views; no model loading"
    )
    execution = sub.add_parser(
        "run", help="Load Gemma, generate SQL and execute selected-View-only queries"
    )
    for command in (setup, check, preview, profiling, execution):
        command.add_argument("--routing-condition", choices=("name_only", "name_description"))
        command.add_argument("--prepared-directory", type=Path)
        command.add_argument("--column-context", choices=("names_only", "statistics"))
        command.add_argument(
            "--column-descriptions",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Include short IFC/BIBIM column definitions (optional)",
        )
    for command in (preview, profiling, execution):
        command.add_argument("--limit", type=int)
        command.add_argument("--ids", help="Comma-separated benchmark question IDs")
        command.add_argument("--routing-mode", choices=("saved", "live"))
    execution.add_argument("--dry-run", action="store_true")
    execution.add_argument("--resume", type=Path)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.column_context:
        config["column_context"] = args.column_context
    if args.column_descriptions is not None:
        config["column_descriptions"] = args.column_descriptions
    if args.routing_condition:
        if args.routing_condition != config["routing_condition"] and not args.prepared_directory:
            config["prepared_directory"] += "_" + args.routing_condition
        config["routing_condition"] = args.routing_condition
    if args.prepared_directory:
        config["prepared_directory"] = str(args.prepared_directory.resolve())
    if getattr(args, "routing_mode", None):
        config["routing_mode"] = args.routing_mode
    if args.command == "prepare":
        result = prepare(config, log=lambda message: print(message, flush=True))
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "questions",
                        "databases",
                        "historical_reference_questions",
                        "context_policy",
                        "model_loaded",
                        "inference_executed",
                    )
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "profile":
        _, inventories, cases, schemas = load_prepared_sql(config)
        ids = [i.strip() for i in args.ids.split(",")] if args.ids else None
        cases = choose_cases(cases, config, limit=args.limit, ids=ids)
        started = time.perf_counter()
        profiles, fingerprints = load_profiles(
            config, inventories, schemas, cases, log=lambda s: print(s, flush=True)
        )
        print(
            json.dumps(
                {
                    "passed": True,
                    "questions": len(cases),
                    "databases": len(profiles),
                    "views": sum(map(len, fingerprints.values())),
                    "context_policy": context_policy(config),
                    "elapsed_seconds": time.perf_counter() - started,
                    "model_loaded": False,
                    "inference_executed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "check":
        manifest, inventories, cases, schemas = load_prepared_sql(config)
        _, dictionary_identity = load_descriptions(config, inventories, schemas, cases)
        print(
            json.dumps(
                {
                    "passed": True,
                    "questions": len(cases),
                    "model": manifest["model"]["path"],
                    "column_context": context_mode(config),
                    "column_descriptions": config.get("column_descriptions", False),
                    "column_dictionary": dictionary_identity,
                    "initial_top_k": config["top_k"],
                    "retrieval_limits": (
                        retrieval_schedule(config)
                        if config.get("answer_stage")
                        else [config["top_k"]]
                    ),
                    "context_policy": context_policy(config),
                    "model_loaded": False,
                    "inference_executed": False,
                },
                indent=2,
            )
        )
    else:
        ids = [i.strip() for i in args.ids.split(",")] if args.ids else None
        if args.command == "plan" or args.dry_run:
            result = plan(config, limit=args.limit, ids=ids)
            example = result.pop("example_messages")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("\nExample SQL model input (no inference):\n")
            print(json.dumps(example, ensure_ascii=False, indent=2))
        else:
            result = run(config, limit=args.limit, ids=ids, resume=args.resume)
            return 0 if result["complete"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
