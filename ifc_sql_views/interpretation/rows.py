"""EntityRowComposition: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from collections import defaultdict
import hashlib
import json
import re
from ..schemas import CUSTOM_IDENTIFIERS
from ..values import without_references


class EntityRowComposition:
    def field_name(self, key, desired):
        if key in self.names:
            return self.names[key]
        folded = desired.casefold()
        names = self.key_maps.setdefault(key[0], dict(self.keys))
        if any(c in names and names[c] != key for c in (folded, folded + "_unit")):
            desired += "_" + hashlib.sha256(repr(key).encode()).hexdigest()[:10]
            self.db.note("column_name_collision", 0, desired)
        names[desired.casefold()] = key
        names[desired.casefold() + "_unit"] = key
        self.names[key] = desired
        return desired

    def begin_class(self):
        self.names.clear()
        self.key_maps.clear()

    def reserve_attributes(self, attributes):
        for name in attributes:
            self.keys[name.casefold()] = ("attribute", name)

    def rows(self, iid, attributes=None):
        attributes = self.object_attributes(iid) if attributes is None else attributes
        self.reserve_attributes(attributes)
        standard = dict(attributes)
        custom = {key: attributes[key] for key in CUSTOM_IDENTIFIERS if key in attributes}
        values = {"standard": defaultdict(list), "custom": defaultdict(list)}
        properties = self.extractor.effective_properties(iid)
        present = {(p["set"], p["name"]) for p in properties}
        for p in properties:
            cls = self.db.nodes[p["set_id"]]["ifc_class"]
            canonical = self.extractor.catalog.standard_set(
                self.db.nodes[iid]["ifc_class"], p["set"], tuple(p["path"]), p["kind"], cls
            )
            family = "standard" if canonical else "custom"
            set_name = canonical or re.sub(r"^(?:Pset|Qto)_", "", p["set"], flags=re.IGNORECASE)
            qualifier = ""
            if canonical in ("IfcDoorPanelProperties", "IfcWindowPanelProperties"):
                position = self.db.nodes[p["set_id"]].get("PanelPosition")
                if position and position != "NOTDEFINED":
                    qualifier = str(position)
                    set_name += "_" + qualifier
            desired = set_name + "_" + p["name"]
            key = (family, canonical or p["set"], p["kind"], qualifier, tuple(p["path"]))
            name = self.field_name(key, desired)
            value = without_references(p["value"])
            values[family][name].append((value, p["unit"]))
            if (
                family == "standard"
                and p["set"] == "BaseQuantities"
                and self.db.version in ("IFC4", "IFC4X3_ADD2")
            ):
                alias = self.extractor.catalog.quantity_alias(
                    self.db.nodes[iid]["ifc_class"], p["name"]
                )
                if alias and (alias, p["name"]) not in present:
                    alias_name = self.field_name(
                        ("standard", alias, p["kind"], "", tuple(p["path"])),
                        alias + "_" + p["name"],
                    )
                    values[family][alias_name].append((value, p["unit"]))
        for family, destination in (("standard", standard), ("custom", custom)):
            for key, pairs in values[family].items():
                distinct = []
                seen = set()
                for value, unit in pairs:
                    token = json.dumps([value, unit], sort_keys=True, ensure_ascii=False)
                    if token not in seen:
                        seen.add(token)
                        distinct.append((value, unit))
                destination[key] = (
                    distinct[0][0] if len(distinct) == 1 else [p[0] for p in distinct]
                )
                if any(p[1] is not None for p in distinct):
                    destination[key + "_Unit"] = (
                        distinct[0][1] if len(distinct) == 1 else [p[1] for p in distinct]
                    )
        return standard, custom, attributes
