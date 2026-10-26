"""Portable entry point for the original routing/SQL/answer engine."""
import sys
from experiments.text_to_sql_top3.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if not any(a == "--config" or a.startswith("--config=") for a in args):
        args = ["--config", "configs/pipeline.local.json", *args]
    raise SystemExit(main(args))
