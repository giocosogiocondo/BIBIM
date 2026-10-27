"""Coordinate raw DB reading, interpretation, writing, verification and publishing."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
import ifcopenshell
from .catalog import Catalog
from .column_policy import is_empty
from .extractor import Extractor
from .integrity import check_sqlite_integrity, check_unique_view_rows
from .lifecycle import clear_method_caches
from .reader import Database
from .schemas import BUILD_VERSION, CUSTOM_IDENTIFIERS, write_json
from .semantic import Semantic
from .storage import Writer, copy_raw_tables


def canonical_class(db, cls):
    if cls.endswith(("StandardCase", "ElementedCase", "Varying")):
        parent = db.entities[cls].supertype()
        if parent and not parent.is_abstract():
            return parent.name()
    return cls


def select_occurrences(db, classes):
    groups = {}
    if classes:
        for cls in classes:
            if (
                cls not in db.entities
                or not db.is_a(cls, "IfcObjectDefinition")
                or db.is_a(cls, "IfcTypeObject")
            ):
                raise ValueError(
                    f"Unsupported occurrence class for {db.version}: {cls}; types/styles are folded into occurrences"
                )
            groups[cls] = db.ids(cls)
    else:
        for cls in sorted(db.by_class):
            if db.is_a(cls, "IfcObjectDefinition") and not db.is_a(cls, "IfcTypeObject"):
                groups.setdefault(canonical_class(db, cls), []).extend(db.by_class[cls])
    return groups


class Progress:
    def __init__(self, log):
        self.log = log
        self.last = time.perf_counter()

    def tick(self, message):
        if time.perf_counter() - self.last >= 2:
            self.log(message)
            self.last = time.perf_counter()


def interpret_attributes(semantic, groups, progress):
    ids = sorted({iid for values in groups.values() for iid in values})
    attributes = {}
    for position, iid in enumerate(ids, 1):
        attributes[iid] = semantic.object_attributes(iid)
        semantic.reserve_attributes(attributes[iid])
        progress.tick(f"Attribute·위치·소유자·타입 해석: {position:,}/{len(ids):,}개 객체")
    return attributes


def write_entity_pairs(writer, semantic, groups, attributes, progress):
    for cls, ids in sorted(groups.items()):
        ids = sorted(set(ids))
        if not ids:
            continue
        label = cls.removeprefix("Ifc")
        semantic.begin_class()
        progress.log(f"View_{label}: {len(ids):,}개 객체 / 모든 표준 Pset·Qto 및 Custom 추출")
        standard_rows, custom_rows = [], []
        has_custom = False
        for position, iid in enumerate(ids, 1):
            standard, custom, _ = semantic.rows(iid, attributes[iid])
            has_custom |= any(
                not is_empty(value)
                for key, value in custom.items()
                if key not in CUSTOM_IDENTIFIERS
            )
            standard_rows.append(standard)
            custom_rows.append(custom)
            progress.tick(f"View_{label}: {position:,}/{len(ids):,}개 객체")
        writer.write(
            "View_" + label, standard_rows, ("ifc_id", "ifc_class", "GlobalId"), matrix=True
        )
        if has_custom:
            writer.write(
                "View_" + label + "CustomPset", custom_rows, CUSTOM_IDENTIFIERS, matrix=True
            )


def create_report(db, catalog, writer, source, destination, started):
    return {
        "version": BUILD_VERSION,
        "layout": "entity-pairs",
        "column_policy": "All interpreted attributes and effective standard Pset/Qto in entity views; only ifc_id/ifc_class/GlobalId/Name plus custom properties/quantities in entity CustomPset views; uniform measurement units encoded in column names without value conversion; constant values retained; all-empty removed; <=10% and SQLite overflow packed into etc; no 200-column cap",
        "custom_attributes": list(CUSTOM_IDENTIFIERS),
        "coordinate_system": "IFC model coordinates; cumulative LocalPlacement translations and rotations; native project length unit; no CRS/MapConversion or tessellation",
        "typing": "Type/style attributes folded into occurrences; uninstantiated definitions remain in raw tables",
        "spatial_membership": "Explicit containment/aggregate/nest ancestry only; multiple storeys/spaces retained; BoundarySpaces is a distinct-target JSON array; no geometric inference; normal sparse-column policy applies",
        "material_members": "Resource reference aggregates preserve original membership, repetition and LIST order; SET display order is not construction order",
        "source_header": {
            "stored": bool(db.file_header),
            "public_view": "View_Project",
            "fields": list(db.source_fields),
            "legacy_fallback": "metadata.schema and retained metadata.mvd description lists; absent FILE_NAME values are not inferred",
        },
        "input": str(source),
        "output": str(destination),
        "schema": db.version,
        "ifcopenshell": ifcopenshell.version,
        "template_file": catalog.template_path.name,
        "template_sha256": catalog.fingerprint,
        "legacy_quantity_sha256": (
            hashlib.sha256(catalog.legacy_path.read_bytes()).hexdigest()
            if db.version == "IFC2X3"
            else None
        ),
        "value_units": "original; explicit property unit overrides project unit",
        "raw_tables_included": True,
        "raw_nodes": len(db.nodes),
        "views": writer.views,
        "diagnostics": db.diagnostics,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def build(input_path, output_path, *, schema=None, classes=None, overwrite=False, log=None):
    log = log or (lambda message: None)
    source, destination = Path(input_path).resolve(), Path(output_path).resolve()
    if source == destination:
        raise ValueError("Output must be a different file from input")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {destination}; use --overwrite explicitly")
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    log("원시 DB 읽기 및 EXPRESS 참조 인덱스 생성...")
    db = Database(source, schema)
    log(f"참조 인덱스 완료: {len(db.nodes):,}개 엔티티")
    temporary = writer = catalog = extractor = semantic = None
    try:
        catalog = Catalog(db)
        extractor = Extractor(db, catalog)
        semantic = Semantic(extractor)
        groups = select_occurrences(db, classes)
        handle, temporary = tempfile.mkstemp(
            prefix=destination.stem + ".", suffix=".building.sqlite", dir=destination.parent
        )
        os.close(handle)
        writer = Writer(temporary)
        progress = Progress(log)
        attributes = interpret_attributes(semantic, groups, progress)
        write_entity_pairs(writer, semantic, groups, attributes, progress)
        log("원시 IFC 테이블 보존 및 DB 무결성 검사...")
        copy_raw_tables(writer, db)
        writer.con.commit()
        check_sqlite_integrity(writer.con)
        check_unique_view_rows(writer.con, writer.views)
        report = create_report(db, catalog, writer, source, destination, started)
        writer.close()
        writer = None
        os.replace(temporary, destination)
        temporary = None
        write_json(destination.with_suffix(".build.json"), report)
        return report
    finally:
        if writer:
            writer.close()
        clear_method_caches(semantic, extractor, catalog, db)
        db.close()
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Generate document-driven IFC object views from an IFC file or raw Ifc2Sql SQLite DB"
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--schema", choices=("IFC2X3", "IFC4", "IFC4X3", "IFC4X3_ADD2"))
    parser.add_argument(
        "--classes", nargs="+", help="Optional entity classes; default: all object classes present"
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if args.input.suffix.lower() == ".ifc":
        from .conversion import convert

        report = convert(
            args.input,
            args.output,
            schema=args.schema,
            classes=args.classes,
            overwrite=args.overwrite,
        )
    else:
        report = build(
            args.input,
            args.output,
            schema=args.schema,
            classes=args.classes,
            overwrite=args.overwrite,
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
    return 0
