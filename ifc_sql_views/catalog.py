from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.pset
from ifcopenshell.util.pset import PsetQto
from .schemas import template_schema


class Catalog:
    """Versioned EXPRESS and buildingSMART Pset/Qto template catalog."""

    def __init__(self, db):
        self.db = db
        self.template_path = (
            Path(ifcopenshell.util.pset.__file__).parent
            / "schema"
            / PsetQto.templates_path[template_schema(db.version)]
        )
        # Explicit templates avoid PsetQto's implicit mutation/backport of IFC4 QTO policies.
        model = ifcopenshell.open(str(self.template_path))
        self.psets = PsetQto(db.version, templates=[model])
        self.templates = {x.Name: x for x in model.by_type("IfcPropertySetTemplate")}
        self.fingerprint = hashlib.sha256(self.template_path.read_bytes()).hexdigest()
        self.legacy_path = Path(__file__).parent / "data" / "ifc2x3_quantities.json"
        self.legacy = (
            json.loads(self.legacy_path.read_text(encoding="utf-8"))["entities"]
            if db.version == "IFC2X3"
            else {}
        )

    @lru_cache(maxsize=None)
    def applicable(self, cls: str):
        # Union conditional templates for a stable class view, even when no
        # occurrence of a particular PredefinedType is present in this model.
        predefined = {""}
        for a in self.db.attributes(cls):
            if a.name() == "PredefinedType":
                t = a.type_of_attribute().declared_type()
                if hasattr(t, "enumeration_items"):
                    predefined.update(t.enumeration_items())
        found = {}
        for value in sorted(predefined):
            for template in self.psets.get_applicable(cls, value, schema=self.db.version):
                found[template.Name] = template
        return tuple(found[k] for k in sorted(found))

    def property_paths(self, template, prefix=()):
        for prop in template.HasPropertyTemplates or ():
            path = (*prefix, prop.Name)
            if prop.is_a("IfcComplexPropertyTemplate"):
                yield from self.property_paths(prop, path)
            else:
                yield path, prop

    def property_templates(self, template):
        for path, prop in self.property_paths(template):
            yield "_".join(path), prop

    @lru_cache(maxsize=None)
    def columns(self, cls):
        out = {}
        for template in self.applicable(cls):
            for path, prop in self.property_paths(template):
                name = "_".join(path)
                out[template.Name + "_" + name] = {
                    "set": template.Name,
                    "property": name,
                    "path": path,
                    "template_type": prop.TemplateType,
                    "measure": prop.PrimaryMeasureType,
                }
        quantity_measures = {
            "Length": "Length",
            "Area": "Area",
            "Volume": "Volume",
            "Weight": "Mass",
            "Count": "Count",
            "Time": "Time",
        }
        for base, definition in self.legacy.items():
            if self.db.is_a(cls, base):
                for name, kind in definition["quantities"].items():
                    out["BaseQuantities_" + name] = {
                        "set": "BaseQuantities",
                        "property": name,
                        "path": (name,),
                        "template_type": "Q_" + kind.removeprefix("IfcQuantity").upper(),
                        "measure": "Ifc"
                        + quantity_measures[kind.removeprefix("IfcQuantity")]
                        + "Measure",
                    }
        return out

    @lru_cache(maxsize=None)
    def standard_set(self, cls, set_name, path, kind, set_class):
        """Recognize documented paths, including schema-defined property sets."""
        if set_class not in ("IfcPropertySet", "IfcElementQuantity"):
            if self.db.is_a(set_class, "IfcPropertySetDefinition"):
                attrs = {a.name() for a in self.db.attributes(set_class)}
                name = path[0].removesuffix("_id") if len(path) == 1 else ""
                return set_class if name in attrs else None
            return None
        template = self.templates.get(set_name)
        if template:
            for defined_path, prop in self.property_paths(template):
                is_quantity = prop.TemplateType.startswith("Q_")
                if tuple(path) == defined_path and is_quantity == (kind == "Quantity"):
                    return set_name
        if set_name == "BaseQuantities" and kind == "Quantity" and len(path) == 1:
            name = path[0]
            if self.db.version in ("IFC4", "IFC4X3_ADD2") and self.quantity_alias(cls, name):
                return set_name
            if "BaseQuantities_" + name in self.columns(cls):
                return set_name
        return None

    @lru_cache(maxsize=None)
    def measure(self, set_name, property_name):
        template = self.templates.get(set_name)
        if template:
            for name, prop in self.property_templates(template):
                if name == property_name:
                    return prop.PrimaryMeasureType
        return None

    def quantity_alias(self, cls, property_name):
        # Legacy exporter naming in an IFC4 DB. Only map an exact quantity name
        # when the applicable standard templates provide one unambiguous target.
        candidates = [
            m["set"]
            for m in self.columns(cls).values()
            if m["set"].startswith("Qto_") and m["property"] == property_name
        ]
        return candidates[0] if len(set(candidates)) == 1 else None
