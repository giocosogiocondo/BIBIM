"""Verified short column definitions, prepared before starting the model server."""

from __future__ import annotations

from pathlib import Path

from ifc_column_dictionary import ColumnDictionary
from experiments.view_selection_ablation.common import digest_json, file_hash, read_json, resolve
from .schema import physical_views, retrieval_schedule


def load_descriptions(config, inventories, schemas, cases):
    if not config.get("column_descriptions", False):
        return {}, {}
    directory = resolve(config.get("column_dictionary_directory", "docs/column_descriptions"))
    validation = read_json(directory / "validation.json")
    if validation.get("passed") is not True:
        raise ValueError("Column dictionary validation has not passed")
    required = {}
    limits = retrieval_schedule(config) if config.get("answer_stage") else [config["top_k"]]
    for case in cases:
        key = case["inventory_key"]
        groups = (
            inventories[key]["groups"]
            if config["routing_mode"] == "live"
            else case["saved_route"]["ranking"][: max(limits)]
        )
        required.setdefault(key, set()).update(physical_views(groups, inventories[key]))
    versions = {ColumnDictionary.canonical_schema(inventories[key]["schema"]) for key in required}
    names = ["derived_columns.en.json", "source_manifest.json"] + [
        v + ".en.json" for v in sorted(versions)
    ]
    files = {}
    for name in names:
        actual = file_hash(directory / name)
        if actual != validation["files_sha256"].get(name):
            raise ValueError(
                "Column dictionary differs from its validation record; rebuild it: " + name
            )
        files[name] = actual
    dictionary = ColumnDictionary(directory)
    descriptions = {}
    for key, views in sorted(required.items()):
        build = read_json(inventories[key]["build"]["path"])
        if ColumnDictionary.canonical_schema(build["schema"]) != ColumnDictionary.canonical_schema(
            inventories[key]["schema"]
        ):
            raise ValueError("Dictionary schema differs from the database manifest: " + key)
        descriptions[key] = {}
        for view in sorted(views):
            info = build["views"][view]
            if info["columns"] != schemas[key][view]:
                raise ValueError("Dictionary column inventory differs: " + key + "/" + view)
            fields = {}
            for column in schemas[key][view]:
                entry = dictionary.describe(
                    build["schema"], view, column, info.get("column_policy", {})
                )
                if entry.get("description"):
                    fields[column] = entry["description"]
            descriptions[key][view] = fields
    package = Path(__file__).resolve().parents[2] / "ifc_column_dictionary"
    identity = {
        "catalog_revision": validation["catalog_revision"],
        "files_sha256": files,
        "resolver_sha256": {p.name: file_hash(p) for p in sorted(package.glob("*.py"))},
        "resolved_text_sha256": digest_json(descriptions),
        "views": sum(len(v) for v in descriptions.values()),
        "described_columns": sum(
            len(fields) for views in descriptions.values() for fields in views.values()
        ),
        "style": "short English descriptions; no invented custom or ambiguous definitions",
    }
    identity["fingerprint"] = digest_json(identity)
    return descriptions, identity
