"""Reproducible schema-wide dictionary generation; no benchmark or model input."""

from __future__ import annotations

from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import re

import ifcopenshell
import ifcopenshell.util.doc as doc
from ifcopenshell.util.pset import PsetQto

from bibim_config import VERSION
from ifc_sql_views.schemas import template_schema

REVISION = "3.4.5.1"
SCHEMAS = {
    "IFC2X3": ("2x3 TC1 (2.3.0.1)", "ifc2x3"),
    "IFC4": ("4 ADD2 TC1 (4.0.2.1)", "ifc4"),
    "IFC4X3_ADD2": ("4.3 ADD2 (4.3.2.0)", "ifc4x3"),
}
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs/column_descriptions"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"~~.*?~~", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return " ".join(text.replace("*", "").replace("_", "").split())


def opening(text):
    for abbreviation in ("e.g.", "i.e.", "etc."):
        text = text.replace(abbreviation, abbreviation.replace(".", "\u00a7"))
    return re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)[0].replace("\u00a7", ".")


def reference_targets(typ):
    if hasattr(typ, "type_of_element"):
        return reference_targets(typ.type_of_element())
    if hasattr(typ, "declared_type"):
        return reference_targets(typ.declared_type())
    if hasattr(typ, "select_list"):
        return sorted({name for item in typ.select_list() for name in reference_targets(item)})
    return [typ.name()] if hasattr(typ, "all_attributes") else []


def enum_values(typ):
    if hasattr(typ, "type_of_element"):
        return enum_values(typ.type_of_element())
    if hasattr(typ, "declared_type"):
        return enum_values(typ.declared_type())
    return list(typ.enumeration_items()) if hasattr(typ, "enumeration_items") else []


def type_name(typ):
    if hasattr(typ, "type_of_element"):
        return "aggregate of " + type_name(typ.type_of_element())
    if hasattr(typ, "declared_type"):
        return type_name(typ.declared_type())
    return typ.name() if hasattr(typ, "name") else str(typ)


def url(version, name, cached=None):
    if version == "IFC4X3_ADD2":
        return (
            "https://standards.buildingsmart.org/IFC/RELEASE/IFC4_3/HTML/lexical/" + name + ".htm"
        )
    return (
        cached
        or {
            "IFC2X3": "https://standards.buildingsmart.org/IFC/RELEASE/IFC2x3/TC1/EXPRESS/IFC2X3_TC1.exp",
            "IFC4": "https://standards.buildingsmart.org/IFC/RELEASE/IFC4/ADD2_TC1/EXPRESS/IFC4_ADD2_TC1.exp",
        }[version]
    )


def description(raw, fallback):
    full = clean(raw)
    if not full:
        return {
            "description": fallback,
            "full_description": None,
            "description_method": "schema_signature",
            "official_prose_available": False,
        }
    return {
        "description": opening(full),
        "full_description": full,
        "description_method": "official_opening_sentence",
        "official_prose_available": True,
        "cached_text_sha256": digest(raw.encode("utf-8")),
    }


def build_schema(version):
    release, stem = SCHEMAS[version]
    schema = ifcopenshell.schema_by_name(version)
    source_root = Path(doc.__file__).parent / "schema"
    entity_path = source_root / (stem + "_entities.json")
    property_path = source_root / (stem + "_properties.json")
    entities_doc = json.loads(entity_path.read_text(encoding="utf-8"))
    properties_doc = json.loads(property_path.read_text(encoding="utf-8"))
    entities, fields = {}, {}
    for entity in sorted(schema.entities(), key=lambda x: x.name()):
        cls = entity.name()
        parent = entity.supertype()
        entity_doc = entities_doc.get(cls, {})
        attributes = {}
        lineage = []
        current = entity
        while current:
            lineage.append(current)
            current = current.supertype()
        for declaring in reversed(lineage):
            owner = declaring.name()
            owner_doc = entities_doc.get(owner, {})
            for attr in declaring.attributes():
                key = owner + "." + attr.name()
                attributes[attr.name()] = key
                if key in fields:
                    continue
                values = enum_values(attr.type_of_attribute())
                label = type_name(attr.type_of_attribute())
                fallback = (
                    f"Predefined category of {owner}; allowed codes follow {label}."
                    if attr.name() == "PredefinedType"
                    else f"{owner}.{attr.name()} attribute; IFC schema type: {label}."
                )
                fields[key] = {
                    "entity": owner,
                    "attribute": attr.name(),
                    "kind": "attribute",
                    **description(owner_doc.get("attributes", {}).get(attr.name(), ""), fallback),
                    "schema_type": str(attr.type_of_attribute()),
                    "optional": bool(attr.optional()),
                    "schema_type_name": label,
                    "reference_targets": reference_targets(attr.type_of_attribute()),
                    "allowed_enum_values": values,
                    "source_url": url(version, owner, owner_doc.get("spec_url")),
                }
        inverses = {}
        for inverse in entity.all_inverse_attributes():
            inverses[inverse.name()] = {
                "target_entity": inverse.entity_reference().name(),
                "forward_attribute": inverse.attribute_reference().name(),
                "description": clean(
                    (doc.get_entity_doc(version, cls) or {})
                    .get("attributes", {})
                    .get(inverse.name(), "")
                )
                or None,
            }
        entities[cls] = {
            **description(entity_doc.get("description", ""), f"IFC schema entity {cls}."),
            "parent": parent.name() if parent else None,
            "abstract": bool(entity.is_abstract()),
            "attributes": attributes,
            "inverse_attributes": inverses,
            "source_url": url(version, cls, entity_doc.get("spec_url")),
        }

    template_path = (
        Path(doc.__file__).parent / "schema" / PsetQto.templates_path[template_schema(version)]
    )
    templates = ifcopenshell.open(str(template_path))
    property_sets = {}
    for name, item in sorted(properties_doc.items()):
        props = {}
        for prop, definition in sorted(item.get("properties", {}).items()):
            raw = definition.get("description", "") if isinstance(definition, dict) else ""
            props[prop] = {
                "property": prop,
                **description(
                    raw,
                    f"{prop} property of {name}; no explanatory prose is present in the pinned documentation cache.",
                ),
            }
        property_sets[name] = {
            "name": name,
            "kind": "quantity_set" if name.startswith("Qto_") else "property_set",
            "description": clean(item.get("description")),
            "properties": props,
            "source_url": url(version, name, item.get("spec_url")),
            "applicable_entity": None,
            "template_type": None,
        }

    def properties(template, prefix=()):
        for prop in template.HasPropertyTemplates or ():
            path = (*prefix, prop.Name)
            if prop.is_a("IfcComplexPropertyTemplate"):
                yield from properties(prop, path)
            else:
                yield path, prop

    for template in sorted(templates.by_type("IfcPropertySetTemplate"), key=lambda t: t.Name):
        name = template.Name
        item = property_sets.setdefault(
            name,
            {
                "name": name,
                "kind": (
                    "quantity_set"
                    if (template.TemplateType or "").startswith("QTO") or name.startswith("Qto_")
                    else "property_set"
                ),
                "description": clean(template.Description),
                "properties": {},
                "source_url": url(version, name),
                "applicable_entity": None,
                "template_type": None,
            },
        )
        item.update(
            applicable_entity=template.ApplicableEntity, template_type=template.TemplateType
        )
        for path, prop in properties(template):
            name_in_set = "_".join(path)
            entry = item["properties"].setdefault(
                name_in_set,
                {
                    "property": name_in_set,
                    **description(
                        prop.Description or "",
                        f"{name_in_set} field of {name}; IFC measure: {prop.PrimaryMeasureType or prop.TemplateType}.",
                    ),
                },
            )
            entry.update(
                path=list(path),
                measure_type=prop.PrimaryMeasureType,
                template_type=prop.TemplateType,
            )
    pset_lookup = PsetQto(version, templates=[templates])
    quantity_aliases = {}
    legacy_quantities = {}
    legacy_path = ROOT / "ifc_sql_views/data/ifc2x3_quantities.json"
    legacy = (
        json.loads(legacy_path.read_text(encoding="utf-8"))["entities"]
        if version == "IFC2X3"
        else {}
    )
    for cls, entity in entities.items():
        lineage = []
        ancestor = cls
        while ancestor:
            lineage.append(ancestor)
            ancestor = entities[ancestor]["parent"]
        if "IfcObjectDefinition" not in lineage:
            continue
        if version == "IFC2X3":
            fields_for_entity = {}
            for owner in reversed(lineage):
                for name, kind in legacy.get(owner, {}).get("quantities", {}).items():
                    fields_for_entity[name] = {
                        "description": f"Documented BaseQuantities.{name} quantity of {owner}, represented as {kind}; use the stored measurement unit.",
                        "full_description": None,
                        "description_method": "schema_quantity_signature",
                        "official_prose_available": False,
                        "source_ids": [owner + ".BaseQuantities." + name],
                        "source_urls": [entities[owner]["source_url"]],
                        "measure_type": kind,
                    }
            if fields_for_entity:
                legacy_quantities[cls] = fields_for_entity
        else:
            predefined = {""}
            key = entity["attributes"].get("PredefinedType")
            if key:
                predefined.update(fields[key]["allowed_enum_values"])
            applicable = {
                t.Name
                for value in sorted(predefined)
                for t in pset_lookup.get_applicable(cls, value, schema=version)
                if (t.TemplateType or "").startswith("QTO") or t.Name.startswith("Qto_")
            }
            candidates = {}
            for name in applicable:
                for prop in property_sets.get(name, {}).get("properties", {}):
                    candidates.setdefault(prop, set()).add(name)
            aliases = {
                prop: next(iter(names))
                for prop, names in sorted(candidates.items())
                if len(names) == 1
            }
            if aliases:
                quantity_aliases[cls] = aliases
    catalog = {
        "format": "BIBIM-column-dictionary/1",
        "catalog_revision": REVISION,
        "bibim_version": VERSION,
        "language": "en",
        "schema": version,
        "release": release,
        "entities": entities,
        "attribute_definitions": fields,
        "property_sets": property_sets,
        "quantity_aliases": quantity_aliases,
        "legacy_quantities": legacy_quantities,
    }
    sources = {
        "release": release,
        "compiled_schema": schema.name(),
        "files": [
            {"name": p.name, "sha256": digest(p.read_bytes())}
            for p in (entity_path, property_path, template_path)
        ],
        "schema_shape_sha256": digest(
            dumps(
                {
                    k: {
                        "parent": v["parent"],
                        "attributes": v["attributes"],
                        "inverse_attributes": v["inverse_attributes"],
                    }
                    for k, v in entities.items()
                }
            ).encode()
        ),
    }
    if legacy:
        sources["legacy_quantity_file"] = {
            "name": str(legacy_path.relative_to(ROOT)),
            "sha256": digest(legacy_path.read_bytes()),
        }
    stats = {
        "entities": len(entities),
        "declared_attributes": len(fields),
        "inherited_attribute_bindings": sum(len(e["attributes"]) for e in entities.values()),
        "attribute_methods": dict(Counter(e["description_method"] for e in fields.values())),
        "property_sets": sum(p["kind"] == "property_set" for p in property_sets.values()),
        "quantity_sets": sum(p["kind"] == "quantity_set" for p in property_sets.values()),
        "properties_and_quantities": sum(len(p["properties"]) for p in property_sets.values()),
        "property_methods": dict(
            Counter(
                e["description_method"]
                for p in property_sets.values()
                for e in p["properties"].values()
            )
        ),
    }
    return catalog, sources, stats
