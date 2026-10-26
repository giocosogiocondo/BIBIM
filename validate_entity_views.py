"""Independent IFC -> public View validation CLI and compatibility API."""

import argparse
from pathlib import Path
from bibim_validation.values import quote, empty, native, same, collapse, canonical, unit_label
from bibim_validation.validator import Validator
from bibim_validation.runner import validate, has_failures

__all__ = [
    "quote",
    "empty",
    "native",
    "same",
    "collapse",
    "canonical",
    "unit_label",
    "Validator",
    "validate",
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("db", type=Path)
    parser.add_argument(
        "--entities", nargs="+", help="e.g. IfcBeam IfcDoor; default all occurrence classes"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--tolerance", type=float, default=1e-6, help="Absolute tolerance in native units"
    )
    args = parser.parse_args()
    result = validate(args.ifc, args.db, args.out, args.entities, args.tolerance)
    failed = has_failures(result)
    raise SystemExit(1 if failed else 0)
