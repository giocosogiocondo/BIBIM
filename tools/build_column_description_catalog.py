"""Build/check the English schema-wide column dictionary; never load an LLM."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ifc_column_dictionary.build import REVISION, SCHEMAS, TARGET, build_schema, digest, dumps
from ifc_column_dictionary.derived import definitions
from ifc_column_dictionary import ColumnDictionary
import ifcopenshell


def reference_markdown(catalog):
    def cell(text):
        return (text or "").replace("|", "\\|").replace("\n", " ")

    lines = [
        "# " + catalog["schema"] + " English column definitions",
        "",
        "Declared fields are listed once. Inherited attributes link to these definitions in the schema JSON. Missing prose is explicitly marked schema_signature; implementation-derived columns are listed separately.",
        "",
        "## Attributes",
        "",
    ]
    previous = None
    for key, entry in sorted(catalog["attribute_definitions"].items()):
        if entry["entity"] != previous:
            previous = entry["entity"]
            lines += [
                "",
                "### [" + previous + "](" + entry["source_url"] + ")",
                "",
                "| Attribute | IFC type | Description | Method |",
                "|---|---|---|---|",
            ]
        lines.append(
            "| "
            + " | ".join(
                cell(x)
                for x in [
                    entry["attribute"],
                    entry["schema_type_name"],
                    entry["description"],
                    entry["description_method"],
                ]
            )
            + " |"
        )
    lines += ["", "## Standard property and quantity sets", ""]
    for name, entry in sorted(catalog["property_sets"].items()):
        lines += [
            "",
            "### [" + name + "](" + entry["source_url"] + ")",
            "",
            cell(entry["description"]),
            "",
            "| Field | Description | Method |",
            "|---|---|---|",
        ]
        lines += [
            "| "
            + " | ".join(cell(x) for x in [prop, info["description"], info["description_method"]])
            + " |"
            for prop, info in sorted(entry["properties"].items())
        ]
    lines += [
        "",
        "Full cached descriptions, source references and inherited bindings remain in the schema JSON.",
        "",
    ]
    return "\n".join(lines)


def database_catalog(folder, dictionary):
    records, bindings = [], {}
    for path in sorted(folder.rglob("*.sqlite")):
        if any(p.startswith(".") for p in path.relative_to(folder).parts):
            continue
        manifest_path = path.with_suffix(".build.json")
        if not manifest_path.is_file():
            raise ValueError("Missing build manifest: " + str(path))
        build = json.loads(manifest_path.read_text(encoding="utf-8"))
        schema = build["schema"]
        key = path.relative_to(folder).as_posix()
        counts = Counter()
        views = {}
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.execute("PRAGMA query_only=ON")
            actual = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='view' AND name GLOB 'View_*' ORDER BY name"
                )
            ]
            if set(actual) != set(build["views"]):
                raise ValueError("View inventory differs from manifest: " + str(path))
            for view in actual:
                quoted = '"' + view.replace('"', '""') + '"'
                columns = [r[1] for r in con.execute("PRAGMA table_info(" + quoted + ")")]
                info = build["views"][view]
                if columns != info["columns"]:
                    raise ValueError(
                        "Column inventory differs from manifest: " + str(path) + "/" + view
                    )
                fields = {}
                for column in columns:
                    result = dictionary.describe(
                        schema, view, column, info.get("column_policy", {})
                    )
                    fields[column] = {
                        k: v
                        for k, v in result.items()
                        if k not in ("full_description", "source_urls", "schema", "view", "column")
                    }
                    fields[column]["storage"] = {"column": column}
                    if result["status"] == "custom":
                        fields[column][
                            "reason"
                        ] = "Exporter/user-defined property; semantic definition unavailable."
                    counts[result["status"]] += 1
                    if result["status"] == "resolved":
                        counts[result["description_method"]] += 1
                # Record sparse keys from the converter manifest without reading
                # row values or promising keys absent from the actual output.
                sparse = {}
                for name in info.get("column_policy", {}).get("etc_columns", []):
                    item = dictionary.describe(schema, view, name, info.get("column_policy", {}))
                    item = {
                        k: v
                        for k, v in item.items()
                        if k not in ("full_description", "source_urls", "schema", "view", "column")
                    }
                    if item["status"] == "custom":
                        item["reason"] = (
                            "Exporter/user-defined property; semantic definition unavailable."
                        )
                    item["storage"] = {
                        "column": "etc",
                        "json_key": name,
                        "presence": "candidate_from_build_policy; per-row presence not asserted",
                    }
                    sparse[name] = item
                views[view] = {"columns": fields, "etc_fields": sparse}
        records.append(
            {
                "database": key,
                "schema": schema,
                "views": len(actual),
                "columns": sum(len(v["columns"]) for v in views.values()),
                "resolution": dict(counts),
            }
        )
        bindings[key] = {
            "schema": schema,
            "source_database": str(path.resolve()),
            "source_build_sha256": digest(manifest_path.read_bytes()),
            "views": views,
        }
        print(f"Mapped {len(records)} databases: {key}, {len(actual)} Views", flush=True)
    if not records:
        raise ValueError("No generated databases found: " + str(folder))
    return records, bindings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Read-only reproducibility validation"
    )
    parser.add_argument("--databases", type=Path, help="Read-only mapping of real View columns")
    args = parser.parse_args(argv)
    catalogs, sources, stats = {}, {}, {}
    for version in SCHEMAS:
        catalogs[version], sources[version], stats[version] = build_schema(version)
    derived = definitions()
    manifest = {
        "catalog_revision": REVISION,
        "language": "en",
        "provider": "IfcOpenShell bundled buildingSMART documentation and EXPRESS schema",
        "provider_version": ifcopenshell.__version__,
        "schemas": sources,
        "bibim_sources": {
            p.relative_to(ROOT).as_posix(): digest(p.read_bytes())
            for p in [
                *sorted((ROOT / "ifc_column_dictionary").glob("*.py")),
                ROOT / "tools/build_column_description_catalog.py",
                ROOT / "ifc_sql_views/rules.py",
                *sorted((ROOT / "ifc_sql_views/interpretation").glob("*.py")),
                *sorted((ROOT / "ifc_sql_views/extraction").glob("*.py")),
                ROOT / "ifc_sql_views/column_policy.py",
            ]
        },
        "limitations": [
            "Cached official prose is not a live-page completeness guarantee.",
            "Missing prose uses an explicitly labelled schema signature, never another schema's prose.",
            "BIBIM-derived descriptions are implementation definitions, not official buildingSMART SQL columns.",
            "Custom property semantics are not invented.",
            "Opening sentences are deterministic extracts; full descriptions remain available for conditions omitted from the opening.",
        ],
    }
    files = {version + ".en.json": dumps(catalog) for version, catalog in catalogs.items()}
    files.update(
        {version + ".en.md": reference_markdown(catalog) for version, catalog in catalogs.items()}
    )
    files["column_options.en.json"] = dumps(
        {
            version: {
                **{
                    key: item["description"]
                    for key, item in catalog["attribute_definitions"].items()
                },
                **{
                    name + "." + prop: item["description"]
                    for name, pset in catalog["property_sets"].items()
                    for prop, item in pset["properties"].items()
                },
            }
            for version, catalog in catalogs.items()
        }
    )
    files["derived_columns.en.json"] = dumps(derived)
    files["source_manifest.json"] = dumps(manifest)
    lines = [
        "# English column dictionary",
        "",
        "Versioned IFC attributes and standard Pset/Qto fields, including schemas and entities absent from the current database collection.",
        "",
        "| Schema | Entities | Declared attributes | Pset | Qto | Properties/quantities |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for version, s in stats.items():
        lines.append(
            f"| [{version} readable reference]({version}.en.md) / [JSON]({version}.en.json) | {s['entities']} | {s['declared_attributes']} | {s['property_sets']} | {s['quantity_sets']} | {s['properties_and_quantities']} |"
        )
    lines += [
        "",
        "The schema JSON files store each declared Attribute once and link inherited fields through entity maps. Each entry has a short opening sentence, full cached prose when available, method, schema type and source URL. Missing official prose is labelled schema_signature.",
        "",
        "[BIBIM derived-column descriptions](derived_columns.en.json) document placement, owner interpretation, source headers, type/style prefixes and relationship roles. These are implementation definitions. Arbitrary custom meanings are not assigned.",
        "",
        "No model is loaded or inference run by this catalog tool. The generator does not modify conversion or inference configuration.",
        "",
    ]
    files["CATALOG_EN.md"] = "\n".join(lines)
    if args.check:
        for name, text in files.items():
            if (
                not (TARGET / name).is_file()
                or (TARGET / name).read_text(encoding="utf-8") != text
            ):
                raise ValueError("Stale or different dictionary: " + str(TARGET / name))
    else:
        TARGET.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (TARGET / name).write_text(text, encoding="utf-8", newline="\n")
    records = []
    if args.databases:
        records, bindings = database_catalog(args.databases.resolve(), ColumnDictionary(TARGET))
        content = dumps(bindings)
        if args.check:
            path = TARGET / "database_columns.en.json"
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                raise ValueError("Stale or different database-column mappings: " + str(path))
        else:
            (TARGET / "database_columns.en.json").write_text(
                content, encoding="utf-8", newline="\n"
            )
    audit = {
        "passed": True,
        "catalog_revision": REVISION,
        "schemas": stats,
        "databases_checked": len(records),
        "physical_views_checked": sum(r["views"] for r in records),
        "physical_columns_checked": sum(r["columns"] for r in records),
        "database_coverage": records,
        "files_sha256": {name: digest(text.encode()) for name, text in files.items()},
        "model_loaded": False,
        "inference_executed": False,
    }
    if args.databases:
        audit["database_columns_sha256"] = digest(content.encode())
    if not args.check:
        (TARGET / "validation.json").write_text(dumps(audit), encoding="utf-8", newline="\n")
    print(
        dumps({k: v for k, v in audit.items() if k not in ("database_coverage", "files_sha256")})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
