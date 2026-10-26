"""CLI for sequential validation of every published database."""

import argparse
from datetime import datetime
from pathlib import Path
from bibim_config import PROJECT_ROOT, OUTPUT_ROOT
from bibim_validation.batch import discover, source_ifc, save, run

__all__ = ["discover", "source_ifc", "save", "run"]

if __name__ == "__main__":
    base = PROJECT_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()
    output = args.out or OUTPUT_ROOT / (
        "entity_validation_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    )
    raise SystemExit(run(args.db_root, output, args.list_only))
