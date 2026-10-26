"""Resolve physical columns using schema paths and the actual unit-folding policy."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path


class ColumnDictionary:
    def __init__(self, directory=None):
        self.directory = Path(
            directory or Path(__file__).resolve().parents[1] / "docs/column_descriptions"
        )
        self.derived = json.loads(
            (self.directory / "derived_columns.en.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def canonical_schema(schema):
        schema = schema.upper()
        schema = {"IFC2X3_TC1": "IFC2X3", "IFC4X3": "IFC4X3_ADD2"}.get(schema, schema)
        if schema not in ("IFC2X3", "IFC4", "IFC4X3_ADD2"):
            raise ValueError("Unsupported schema; do not substitute another version: " + schema)
        return schema

    @lru_cache(maxsize=3)
    def catalog(self, schema):
        schema = self.canonical_schema(schema)
        return json.loads((self.directory / (schema + ".en.json")).read_text(encoding="utf-8"))

    @lru_cache(maxsize=3)
    def names(self, schema):
        return {
            name.removeprefix("Ifc").casefold(): name for name in self.catalog(schema)["entities"]
        }

    @lru_cache(maxsize=None)
    def descendants(self, schema, cls):
        entities = self.catalog(schema)["entities"]
        return (cls,) + tuple(
            child
            for name, info in entities.items()
            if info["parent"] == cls
            for child in self.descendants(schema, name)
        )

    @lru_cache(maxsize=32768)
    def path(self, schema, cls, tail, depth=0):
        if depth > 64:
            return ()
        catalog = self.catalog(schema)
        entity = catalog["entities"].get(cls)
        if not entity:
            return ()
        fields = entity["attributes"]
        if tail in fields:
            return (fields[tail],)
        result = set()
        token, _, rest = tail.partition("_")
        owned = self.names(schema).get(token.casefold())
        if owned and any(
            owned in self.descendants(schema, base)
            for base in (
                "IfcMaterialProperties",
                "IfcProfileProperties",
                "IfcMaterialDefinitionRepresentation",
                "IfcExternalReferenceRelationship",
                "IfcResourceConstraintRelationship",
            )
        ):
            compatible = any(
                cls in self.descendants(schema, target)
                for key in catalog["entities"][owned]["attributes"].values()
                for target in catalog["attribute_definitions"][key]["reference_targets"]
            )
            if compatible:
                if not rest:
                    return ("@entity:" + owned,)
                result.update(self.path(schema, owned, rest, depth + 1))
        for attr, key in fields.items():
            if not tail.startswith(attr + "_"):
                continue
            rest = tail[len(attr) + 1 :]
            for target in catalog["attribute_definitions"][key]["reference_targets"]:
                for candidate in self.descendants(schema, target):
                    result.update(self.path(schema, candidate, rest, depth + 1))
        return tuple(sorted(result))

    def result(self, schema, column, keys, *, prefix=None):
        catalog = self.catalog(schema)
        keys = tuple(sorted(set(keys)))
        if not keys:
            return None
        entries = [
            (
                catalog["entities"][key.removeprefix("@entity:")]
                if key.startswith("@entity:")
                else catalog["attribute_definitions"][key]
            )
            for key in keys
        ]
        if len(keys) > 1:
            return {
                "status": "ambiguous",
                "description": None,
                "source_ids": list(keys),
                "source_urls": sorted({entry["source_url"] for entry in entries}),
                "reason": "Multiple IFC resource classes can produce this flattened field; no source is selected by name similarity.",
            }
        entry = entries[0]
        text = entry["description"]
        if prefix:
            text = prefix + text
        return {
            "status": "resolved",
            "description": text,
            "full_description": entry["full_description"],
            "description_method": entry["description_method"],
            "source_ids": list(keys),
            "source_urls": [entry["source_url"]],
            "official_prose_available": entry["official_prose_available"],
        }

    def _derived(self, text, source="derived_columns.en.json"):
        return {
            "status": "resolved",
            "description": text,
            "full_description": text,
            "description_method": "bibim_implementation_definition",
            "source_ids": [source],
            "source_urls": [],
            "official_prose_available": False,
        }

    def describe(self, schema, view, column, policy=None):
        schema = self.canonical_schema(schema)
        catalog = self.catalog(schema)
        if not view.startswith("View_"):
            raise ValueError("Expected an entity View name")
        cls = "Ifc" + view.removeprefix("View_").removesuffix("CustomPset")
        if cls not in catalog["entities"]:
            return {
                "status": "unmapped",
                "description": None,
                "reason": "Entity does not exist in this schema.",
            }
        policy = policy or {}
        uniform = {
            info["column"]: (name, info["unit"])
            for name, info in policy.get("uniform_units", {}).items()
        }
        tail, unit = uniform.get(column, (column, None))
        if tail in self.derived["exact_columns"]:
            item = self._derived(self.derived["exact_columns"][tail])
        elif view.endswith("CustomPset"):
            if tail in ("GlobalId", "Name"):
                item = self.result(schema, column, self.path(schema, cls, tail))
            else:
                item = {
                    "status": "custom",
                    "description": None,
                    "reason": self.derived["custom_properties"],
                }
        else:
            item = self._standard(schema, cls, tail)
        item = dict(
            item
            or {
                "status": "unmapped",
                "description": None,
                "reason": "No unambiguous schema path or documented BIBIM rule matches this column.",
            }
        )
        item.update(schema=schema, view=view, column=column)
        if unit is not None:
            item.update(unit=unit, logical_column=tail)
            if item.get("description"):
                item["description"] += f" Stored unit: {unit}; values are not rescaled."
        return item

    def _standard(self, schema, cls, tail):
        catalog = self.catalog(schema)
        if tail in self.derived["exact_columns"]:
            return self._derived(self.derived["exact_columns"][tail])
        if tail.endswith("_Unit"):
            source = self._standard(schema, cls, tail[:-5])
            if source and source.get("description"):
                item = dict(source)
                item.update(
                    description="Unit associated with: " + source["description"],
                    description_method="bibim_unit_interpretation",
                )
                return item
            return None
        if tail.startswith("Owner_") and tail.endswith("_UTC"):
            field = tail.removeprefix("Owner_").removesuffix("_UTC")
            if field in ("CreationDate", "LastModifiedDate"):
                return self.result(
                    schema,
                    tail,
                    self.path(schema, "IfcOwnerHistory", field),
                    prefix="Interpreted as an ISO-format UTC timestamp. ",
                )
        # Exact standard Pset/Qto membership, never property-name-only matching.
        for name in sorted(catalog["property_sets"], key=len, reverse=True):
            if tail.startswith(name + "_"):
                prop = tail[len(name) + 1 :]
                item = catalog["property_sets"][name]
                entry = item["properties"].get(prop)
                if entry:
                    return {
                        "status": "resolved",
                        **entry,
                        "source_ids": [name + "." + prop],
                        "source_urls": [item["source_url"]],
                    }
                return None
        if tail.startswith("BaseQuantities_"):
            prop = tail.removeprefix("BaseQuantities_")
            alias = catalog.get("quantity_aliases", {}).get(cls, {}).get(prop)
            if alias:
                entry = self._standard(schema, cls, alias + "_" + prop)
                if entry:
                    entry = dict(entry)
                    entry["description_method"] = "documented_quantity_alias"
                    return entry
            entry = catalog.get("legacy_quantities", {}).get(cls, {}).get(prop)
            if entry:
                return dict(entry, status="resolved")
            return None
        direct = self.result(schema, tail, self.path(schema, cls, tail))
        if direct:
            return direct
        token, _, rest = tail.partition("_")
        explicit = self.names(schema).get(token.casefold())
        if explicit and explicit in self.descendants(schema, "IfcTypeObject"):
            found = self._standard(schema, explicit, rest)
            if found:
                found = dict(found)
                if found.get("description"):
                    found["description"] = f"Assigned {explicit}: " + found["description"]
                return found
        for prefix, target in (
            ("Owner_Application_", "IfcApplication"),
            ("LastModified_Application_", "IfcApplication"),
            ("Owner_Person_", "IfcPerson"),
            ("LastModifiedBy_Person_", "IfcPerson"),
            ("Owner_", "IfcOwnerHistory"),
        ):
            if tail.startswith(prefix):
                return self.result(schema, tail, self.path(schema, target, tail[len(prefix) :]))
        if tail.startswith("Representation_"):
            rest = tail.removeprefix("Representation_")
            if rest.startswith("Style_"):
                leaf = rest.removeprefix("Style_")
                candidates = set()
                for target in ("IfcPresentationStyleAssignment", "IfcPresentationStyle"):
                    if target in catalog["entities"]:
                        for subclass in self.descendants(schema, target):
                            candidates.update(self.path(schema, subclass, leaf))
                return self.result(schema, tail, candidates)
            resource, _, leaf = rest.partition("_")
            target = self.names(schema).get(resource.casefold())
            if target:
                return self.result(schema, tail, self.path(schema, target, leaf))
        if tail.startswith("Placement_") and schema == "IFC4X3_ADD2":
            field = tail.removeprefix("Placement_")
            if field in ("DistanceAlong", "OffsetLateral", "OffsetVertical", "OffsetLongitudinal"):
                return self.result(
                    schema,
                    tail,
                    self.path(schema, "IfcPointByDistanceExpression", field),
                    prefix="Explicit linear-placement parameter: ",
                )
        # Schema-defined property entities with enum-qualified panels, etc.
        if token in catalog["entities"] and token.startswith("Ifc"):
            candidates = set(self.path(schema, token, rest))
            entity = catalog["entities"][token]
            qualifiers = {
                value
                for key in entity["attributes"].values()
                for value in catalog["attribute_definitions"][key]["allowed_enum_values"]
            }
            for value in qualifiers:
                if rest.startswith(value + "_"):
                    candidates.update(self.path(schema, token, rest[len(value) + 1 :]))
            found = self.result(schema, tail, candidates)
            if found:
                return found
        # Relationship roles come from the converter, not benchmark model names.
        for role in sorted(
            self.derived["relationship_roles"], key=lambda r: len(r["role"]), reverse=True
        ):
            name = role["role"]
            if tail == name:
                return self._derived(
                    role["description"] + " Targets are stored as an object or JSON array."
                )
            if not tail.startswith(name + "_"):
                continue
            leaf = tail[len(name) + 1 :]
            if leaf in ("Count", "Names"):
                text = self.derived["patterns"]["<role>_" + leaf]
                return self._derived(role["description"] + " " + text)
            relation = catalog["entities"].get(role["relationship"])
            if relation:
                key = relation["attributes"].get(role["target_attribute"])
                if key:
                    candidates = set()
                    for target in catalog["attribute_definitions"][key]["reference_targets"]:
                        for subclass in self.descendants(schema, target):
                            candidates.update(self.path(schema, subclass, leaf))
                    found = self.result(schema, tail, candidates, prefix=name + " target: ")
                    if found:
                        return found
        # Unlisted relationship roles follow the schema's actual inverse name.
        for name, inverse in catalog["entities"][cls]["inverse_attributes"].items():
            if tail == name:
                return self._derived(
                    f"Objects linked by inverse {cls}.{name}, defined by {inverse['target_entity']}.{inverse['forward_attribute']} referencing the current object."
                )
            if tail.startswith(name + "_"):
                rest = tail[len(name) + 1 :]
                target = inverse["target_entity"]
                candidates = {
                    key
                    for subclass in self.descendants(schema, target)
                    for key in self.path(schema, subclass, rest)
                }
                endpoint, _, summary = rest.rpartition("_")
                if summary in ("Count", "Names"):
                    keys = {
                        key
                        for subclass in self.descendants(schema, target)
                        for key in self.path(schema, subclass, endpoint)
                    }
                    if any(
                        catalog["attribute_definitions"].get(key, {}).get("reference_targets")
                        for key in keys
                    ):
                        return self._derived(
                            f"Inverse {cls}.{name}, endpoint {endpoint}: "
                            + self.derived["patterns"]["<role>_" + summary]
                        )
                found = self.result(schema, tail, candidates, prefix=f"Inverse {cls}.{name}: ")
                if found:
                    return found
        return None
