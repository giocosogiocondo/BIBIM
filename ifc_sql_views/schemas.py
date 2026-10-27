"""Supported schema releases, version metadata, and JSON manifest output."""

from __future__ import annotations

import json
from pathlib import Path
import ifcopenshell

VERSION = "3.4.7.2"
BUILD_VERSION = f"BIBIM-{VERSION}"
IMPORT_PREPROCESSOR = f"{BUILD_VERSION}-typed-IFC-import"
CUSTOM_IDENTIFIERS = ("ifc_id", "ifc_class", "GlobalId", "Name")
SUPPORTED_SCHEMAS = ("IFC2X3", "IFC4", "IFC4X3_ADD2")


def schema_name(value):
    name = str(value or "").upper()
    if name in ("IFC2X3", "IFC2X3_TC1"):
        return "IFC2X3"
    if name in ("IFC4", "IFC4_ADD2", "IFC4_ADD2_TC1"):
        return "IFC4"
    if name in ("IFC4X3", "IFC4X3_ADD2"):
        actual = ifcopenshell.schema_by_name(name).name()
        if actual == "IFC4X3_ADD2":
            return actual
    raise ValueError(
        f"Unsupported schema: {name}. Expected IFC2X3, IFC4 or IFC4X3_ADD2 (4.3.2.0)."
    )


def template_schema(value):
    return "IFC4X3" if schema_name(value) == "IFC4X3_ADD2" else schema_name(value)


def write_json(path, data, *, allow_nan=True):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=allow_nan),
        encoding="utf-8",
    )
    return target
