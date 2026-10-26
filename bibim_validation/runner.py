"""Read-only IFC/DB validation orchestration and CLI outcome classification."""

from __future__ import annotations
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.util.unit
from .validator import Validator
from .reporting import write_entity_reports


def validate(ifc, db, output, entities=None, tolerance=1e-6):
    ifc, db, output = Path(ifc).resolve(), Path(db).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    model = ifcopenshell.open(str(ifc))
    manifest = db.with_suffix(".build.json")
    report = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        validator = Validator(model, con, report, tolerance)
        summaries = validator.run(entities)
    result = dict(
        source_ifc=str(ifc),
        database=str(db),
        schema=model.schema,
        tolerance=tolerance,
        scope="Object ID sets; direct scalar attributes; scalar type fields; effective leaf Pset/Qto; origin XYZ. Reference resources, static Psets and other output fields are explicitly unverified.",
        note="Agreement percentage applies to evaluated fields only, not to all IFC information. Manifest is used only for column renaming; expected values come from the original IFC.",
        entities=summaries,
        details=validator.details,
    )
    write_entity_reports(output, result)
    return result


def has_failures(result):
    return any(
        s["missing_ids"]
        or s["extra_ids"]
        or s["duplicate_ids"]
        or s["values"].get("missing")
        or s["values"].get("mismatched")
        for s in result["entities"]
    ) or any(
        d["status"] in ("conflicting_etc", "extra_or_duplicate_custom_object")
        for d in result["details"]
    )
