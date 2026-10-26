"""Compatibility API and CLI for the document-driven DB pipeline."""

import argparse
import json
from pathlib import Path
from .pipeline import build, canonical_class
from .storage import Writer, sql_value, copy_raw_tables

__all__ = ["build", "Writer", "sql_value", "canonical_class", "copy_raw_tables", "main"]


def main():
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Generate document-driven IFC object views from a plain Ifc2Sql SQLite DB"
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--schema", choices=("IFC2X3", "IFC4", "IFC4X3", "IFC4X3_ADD2"))
    parser.add_argument(
        "--classes", nargs="+", help="Optional entity classes; default: all object classes present"
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    report = build(
        args.input, args.output, schema=args.schema, classes=args.classes, overwrite=args.overwrite
    )
    print(
        json.dumps(
            {
                "output": report["output"],
                "schema": report["schema"],
                "views": len(report["views"]),
                "diagnostics": len(report["diagnostics"]),
                "elapsed_seconds": report["elapsed_seconds"],
            },
            ensure_ascii=False,
        )
    )
