from __future__ import annotations

import argparse
import json
from pathlib import Path

from .common import HERE, load_config
from .data import load_prepared, prepare, review_labels
from .runner import plan, run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare or run the paired View name/description ablation")
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="Read metadata and compile references; do not load a model")
    sub.add_parser("check", help="Verify prepared inputs without model loading or HTTP requests")
    preview = sub.add_parser("plan", help="Show the planned comparison without inference")
    execution = sub.add_parser("run", help="Load the local model and execute both conditions")
    for child in (preview, execution):
        child.add_argument("--limit", type=int)
        child.add_argument("--repeats", type=int)
    execution.add_argument("--dry-run", action="store_true", help="Only print a plan; never start a model server")
    execution.add_argument("--resume", type=Path)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.command == "prepare":
        prepare(config, log=lambda message: print(message, flush=True))
    elif args.command == "check":
        manifest, inventory, cases, labels = load_prepared(config)
        approved, excluded = review_labels(config, inventory, cases)
        print(json.dumps({"passed": True, "questions": len(cases), "databases": len(inventory),
                          "label_codes": len(labels), "manual_gold_approved": len(approved),
                          "manual_gold_excluded": len(excluded),
                          "counts": manifest["counts"], "model_loaded": False,
                          "inference_executed": False}, indent=2))
    elif args.command == "plan" or args.dry_run:
        result = plan(config, limit=args.limit, repeats=args.repeats)
        print(json.dumps({key: value for key, value in result.items() if key != "example"}, indent=2))
        print("\nExample A and B requests (no SQL or answers):\n")
        print(json.dumps(result["example"], ensure_ascii=False, indent=2))
    else:
        report = run(config, limit=args.limit, repeats=args.repeats, resume=args.resume)
        return 0 if report["complete"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
