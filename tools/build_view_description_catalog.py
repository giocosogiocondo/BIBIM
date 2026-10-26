"""Build or check the English source-based View description catalog; never run an LLM.

This standalone documentation tool does not import or run the IFC-to-DB pipeline.
Only --databases opens existing SQLite files, and those connections are read-only.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse

import ifcopenshell
import ifcopenshell.util.doc


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "view_descriptions"
REVISION = "2026-10-17.1"
SCHEMAS = {
    "IFC2X3": ("2x3 TC1 (2.3.0.1)", "ifc2x3"),
    "IFC4": ("4 ADD2 TC1 (4.0.2.1)", "ifc4"),
    "IFC4X3_ADD2": ("4.3 ADD2 (4.3.2.0)", "ifc4x3"),
}
MAX_WORDS = 35


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def as_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def ancestors(entity):
    result = []
    while entity:
        result.append(entity.name())
        entity = entity.supertype()
    return result


def canonical(entity):
    """Match BIBIM 3.4.5 default grouping; explicit --classes keeps its label."""
    parent = entity.supertype()
    if (
        entity.name().endswith(("StandardCase", "ElementedCase", "Varying"))
        and parent
        and not parent.is_abstract()
    ):
        return parent.name()
    return entity.name()


def view_name(entity_name):
    return "View_" + entity_name.removeprefix("Ifc")


def clean_first_sentence(text):
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("*", "").replace("_", "").replace("~", "")
    text = " ".join(text.split()).lstrip("<")
    text = re.sub(
        r"^Definition (?:from|form|of) [^:]+(?::\d{4})?:\s*", "", text
    )
    for abbreviation in ("e.g.", "i.e.", "etc."):
        text = text.replace(abbreviation, abbreviation.replace(".", "\u00a7"))
    text = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)[0].replace("\u00a7", ".")
    return text if text.endswith((".", "!", "?")) else text + "."


def source_url(schema, name, original):
    if schema == "IFC4X3_ADD2":
        return (
            "https://standards.buildingsmart.org/IFC/RELEASE/IFC4_3/HTML/lexical/"
            + name
            + ".htm"
        )
    if not original:
        raise ValueError(f"Missing original source URL: {schema}.{name}")
    return original


def build():
    overrides_path = TARGET / "summary_overrides.en.json"
    overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
    source_base = Path(ifcopenshell.util.doc.__file__).parent / "schema"
    manifest = {
        "catalog_revision": REVISION,
        "language": "en",
        "provider": "IfcOpenShell bundled buildingSMART documentation cache",
        "provider_version": ifcopenshell.__version__,
        "authority": "buildingSMART International",
        "authority_url": "https://standards.buildingsmart.org/",
        "source_access": (
            "Descriptions are based on pinned bundled documentation, with official "
            "entity source URLs. The build does not download or certify every live page. "
            "The cached IFC4X3 documentation uses a historical documentation hostname; "
            "the source_url field points to the current RELEASE publication."
        ),
        "summary_overrides_sha256": sha256(overrides_path.read_bytes()),
        "schemas": {},
        "additional_source_checks": overrides["additional_source_checks"],
    }
    catalogs = {}
    stats = {}
    all_names = set()
    used_overrides = set()
    for schema_name, (release, stem) in SCHEMAS.items():
        schema = ifcopenshell.schema_by_name(schema_name)
        path = source_base / (stem + "_entities.json")
        docs = json.loads(path.read_text(encoding="utf-8"))
        definitions = {}
        grouped = defaultdict(list)
        schema_shape = []
        excluded_types = 0
        for entity in sorted(schema.entities(), key=lambda item: item.name()):
            lineage = ancestors(entity)
            schema_shape.append(
                [entity.name(), bool(entity.is_abstract()), lineage[1:],
                 [attribute.name() for attribute in entity.all_attributes()]]
            )
            if "IfcObjectDefinition" not in lineage:
                continue
            if "IfcTypeObject" in lineage:
                excluded_types += 1
                continue
            name = entity.name()
            all_names.add(name)
            doc = docs.get(name, {})
            original = doc.get("description", "").strip()
            if not original:
                raise ValueError(f"Missing source definition: {schema_name}.{name}")
            description = overrides["by_schema"].get(schema_name, {}).get(name)
            method = "source_based_paraphrase"
            if description is not None:
                used_overrides.add((schema_name, name))
            elif name in overrides["common"]:
                description = overrides["common"][name]
                used_overrides.add(("common", name))
            else:
                description = clean_first_sentence(original)
                method = "short_source_sentence"
                if len(description.split()) > 25:
                    raise ValueError(f"Needs a concise paraphrase: {schema_name}.{name}")
            if not 3 <= len(description.split()) <= MAX_WORDS:
                raise ValueError(f"Invalid summary length: {schema_name}.{name}")
            if not description.endswith((".", "!", "?")):
                raise ValueError(f"Incomplete summary: {schema_name}.{name}")
            if re.search(r"[\uac00-\ud7a3]|\*\*|~~|<[^>]+>", description):
                raise ValueError(f"Unexpected language or markup: {schema_name}.{name}")
            default_view = None if entity.is_abstract() else view_name(canonical(entity))
            definitions[name] = {
                "description": description,
                "description_method": method,
                "is_abstract": bool(entity.is_abstract()),
                "supertypes": lineage[1:],
                "default_view": default_view,
                "explicit_class_view": view_name(name),
                "source_url": source_url(schema_name, name, doc.get("spec_url")),
                "cached_source_url": doc.get("spec_url"),
                "cached_definition_sha256": sha256(original.encode("utf-8")),
            }
            if default_view:
                grouped[canonical(entity)].append(name)
        views = {}
        for entity_name, members in sorted(grouped.items()):
            definition = definitions[entity_name]
            name = view_name(entity_name)
            views[name] = {
                "entity": entity_name,
                "description": definition["description"],
                "description_method": definition["description_method"],
                "main_view": name,
                "custom_view": name + "CustomPset",
                "custom_view_is_conditional": True,
                "included_ifc_classes": sorted(members),
                "source_url": definition["source_url"],
            }
        manifest["schemas"][schema_name] = {
            "release": release,
            "compiled_schema": schema.name(),
            "documentation_file": path.name,
            "documentation_sha256": sha256(path.read_bytes()),
            "compiled_entity_shape_sha256": sha256(as_json(schema_shape).encode("utf-8")),
        }
        catalogs[schema_name] = {
            "catalog_revision": REVISION,
            "language": "en",
            "schema": schema_name,
            "release": release,
            "description_scope": "IFC entity semantics only; no claim of actual field availability",
            "default_view_grouping": "BIBIM 3.4.5 entity-pairs default class selection",
            "views": views,
            "class_definitions": definitions,
        }
        stats[schema_name] = {
            "default_view_groups": len(views),
            "concrete_occurrence_classes": sum(not d["is_abstract"] for d in definitions.values()),
            "abstract_reference_classes": sum(d["is_abstract"] for d in definitions.values()),
            "folded_class_aliases": {
                name: d["default_view"]
                for name, d in definitions.items()
                if d["default_view"] and d["default_view"] != view_name(name)
            },
            "excluded_type_and_style_classes": excluded_types,
            "summary_methods": dict(Counter(d["description_method"] for d in definitions.values())),
        }
    unused = set(overrides["common"]) - all_names
    if unused:
        raise ValueError(f"Unknown summary override names: {sorted(unused)}")
    for schema_name, entries in overrides["by_schema"].items():
        unknown = set(entries) - catalogs[schema_name]["class_definitions"].keys()
        if unknown:
            raise ValueError(f"Unknown schema override names: {schema_name}: {sorted(unknown)}")
    return catalogs, manifest, stats


def markdown(catalogs, stats):
    lines = [
        "# English IFC View description catalog", "",
        "Source-based English descriptions for BIBIM 3.4.5 View selection. "
        "These are not buildingSMART-authored SQL View definitions.", "",
        "Use the description for an ablation condition only after selecting the correct "
        "IFC schema and filtering to View groups actually present in the database. "
        "A main View and its conditional CustomPset companion count as one option.", "",
        "| Schema | Release | Default View groups | Concrete classes | Abstract reference classes |",
        "|---|---|---:|---:|---:|",
    ]
    for schema_name, catalog in catalogs.items():
        s = stats[schema_name]
        lines.append(
            f"| {schema_name} | {catalog['release']} | {len(catalog['views'])} | "
            f"{s['concrete_occurrence_classes']} | {s['abstract_reference_classes']} |"
        )
    for schema_name, catalog in catalogs.items():
        lines.extend(["", f"## {schema_name}", "", "| View group | English description |",
                      "|---|---|"])
        for name, definition in catalog["views"].items():
            lines.append(
                f"| [{name}]({definition['source_url']}) | "
                + definition["description"].replace("|", "\\|") + " |"
            )
        aliases = stats[schema_name]["folded_class_aliases"]
        if aliases:
            lines.extend(["", "Default folded classes:", ""])
            lines.extend(f"- `{name}` -> `{target}`" for name, target in aliases.items())
    lines.extend([
        "", "## Reference classes and explicit class selection", "",
        "Each schema JSON also contains class_definitions for all non-type "
        "IfcObjectDefinition classes, including abstract supertypes. Abstract classes "
        "are reference entries, not default routing candidates. An explicitly requested "
        "--classes View uses explicit_class_view and that class's description. A folded "
        "specialization also retains its own reference definition.", "",
        "Type/style classes, relationships, property sets, quantity sets, geometry resources, "
        "and enumerations are not independent routing candidates in this layout.", "",
    ])
    return "\n".join(lines)


def check_databases(folder, catalogs):
    records = []
    for path in sorted(folder.rglob("*.sqlite")):
        relative = path.relative_to(folder)
        if any(part.startswith(".") for part in relative.parts):
            continue
        report_path = path.with_suffix(".build.json")
        if not report_path.is_file():
            raise ValueError(f"Missing schema manifest: {path}")
        schema = json.loads(report_path.read_text(encoding="utf-8"))["schema"]
        if schema not in catalogs:
            raise ValueError(f"Unknown database schema: {schema}")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name GLOB 'View_*'"
            )]
        groups = {name.removesuffix("CustomPset") for name in names}
        known = catalogs[schema]["views"]
        unknown = sorted(groups - known.keys())
        orphan_customs = sorted(name for name in names if name.endswith("CustomPset")
                               and name.removesuffix("CustomPset") not in names)
        if unknown or orphan_customs:
            raise ValueError(f"Uncovered View groups: {relative}: {unknown}; {orphan_customs}")
        records.append({"database": relative.as_posix(), "schema": schema,
                        "physical_views": len(names), "view_groups": len(groups),
                        "uncovered_groups": unknown})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify files without writing them")
    parser.add_argument("--databases", type=Path, help="Read-only coverage check of existing outputs")
    args = parser.parse_args()
    catalogs, manifest, stats = build()
    outputs = {schema + ".en.json": as_json(catalog) for schema, catalog in catalogs.items()}
    outputs["routing_options.en.json"] = as_json({
        schema: {name: entry["description"] for name, entry in catalog["views"].items()}
        for schema, catalog in catalogs.items()
    })
    outputs["source_manifest.json"] = as_json(manifest)
    outputs["CATALOG_EN.md"] = markdown(catalogs, stats)
    if args.check:
        for name, content in outputs.items():
            path = TARGET / name
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                raise ValueError(f"Catalog is stale or differs from pinned inputs: {path}")
    else:
        for name, content in outputs.items():
            (TARGET / name).write_text(content, encoding="utf-8", newline="\n")
    records = check_databases(args.databases.resolve(), catalogs) if args.databases else []
    audit = {
        "passed": True,
        "catalog_revision": REVISION,
        "schemas": stats,
        "total_versioned_default_view_groups": sum(len(c["views"]) for c in catalogs.values()),
        "unique_default_view_names_across_schemas": len({n for c in catalogs.values() for n in c["views"]}),
        "database_coverage": records,
        "databases_checked": len(records),
        "physical_views_checked": sum(r["physical_views"] for r in records),
        "uncovered_view_groups": 0,
        "files_sha256": {name: sha256(text.encode("utf-8")) for name, text in outputs.items()},
        "limits": [
            "Source URLs are provenance references, not a claim that every live page was fetched.",
            "The catalog describes IFC semantics, not property presence in a particular database.",
            "Default routing candidates exclude abstract classes and fold types/styles and specialized aliases.",
            "No model inference, SQL generation, IFC conversion, or evaluation benchmark was run.",
        ],
    }
    if not args.check:
        (TARGET / "validation.json").write_text(as_json(audit), encoding="utf-8", newline="\n")
    print(as_json({key: value for key, value in audit.items()
                   if key not in ("database_coverage", "files_sha256")}))


if __name__ == "__main__":
    main()
