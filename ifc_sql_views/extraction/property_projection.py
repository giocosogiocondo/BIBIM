"""PropertyProjection: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
import json
import hashlib
from collections import defaultdict
from ..values import collapse


class PropertyProjection:
    def column_name(self, set_name, property_name, path=None):
        key = (set_name, tuple(path or [property_name]))
        if key in self._column_names:
            return self._column_names[key]
        prefix = (
            "" if set_name in self.catalog.templates or set_name == "BaseQuantities" else "Custom_"
        )
        name = prefix + set_name + "_" + property_name
        folded = name.casefold()
        if any(
            c in self._column_keys and self._column_keys[c] != key
            for c in (folded, folded + "_unit")
        ):
            name += "_" + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:10]
            self.db.note("column_name_collision", 0, name)
        self._column_keys[name.casefold()] = key
        self._column_keys[name.casefold() + "_unit"] = key
        self._column_names[key] = name
        return name

    def flat_properties(self, iid, record_details=False, provenance=None):
        values = defaultdict(list)
        sources = defaultdict(list) if provenance is not None else None
        properties = self.effective_properties(iid)
        present = {(p["set"], p["name"]) for p in properties}
        for p in properties:
            key = self.column_name(p["set"], p["name"], p["path"])
            values[key].append(p["value"])
            token = (
                (p["kind"], p["set_id"], p["set"], tuple(p["path"]), p["_source"])
                if p.get("_source") is not None
                else None
            )
            if sources is not None:
                sources[key].append(("value", token) if token is not None else None)
            if p["unit"]:
                values[key + "_Unit"].append(p["unit"])
                if sources is not None:
                    sources[key + "_Unit"].append(("unit", token) if token is not None else None)
            if (
                p["kind"] == "Quantity"
                and p["set"] == "BaseQuantities"
                and self.db.version in ("IFC4", "IFC4X3_ADD2")
            ):
                alias = self.catalog.quantity_alias(self.db.nodes[iid]["ifc_class"], p["name"])
                if alias and (alias, p["name"]) not in present:
                    alias_key = self.column_name(alias, p["name"])
                    values[alias_key].append(p["value"])
                    if sources is not None:
                        sources[alias_key].append(("value", token) if token is not None else None)
                    if p["unit"]:
                        values[alias_key + "_Unit"].append(p["unit"])
                        if sources is not None:
                            sources[alias_key + "_Unit"].append(
                                ("unit", token) if token is not None else None
                            )
            if record_details:
                row = {
                    "element_id": iid,
                    "SetName": p["set"],
                    "PropertyName": p["name"],
                    "ColumnName": key,
                    "Value": p["value"],
                    "Unit": p["unit"],
                    "Kind": p["kind"],
                }
                set_view = self.catalog.standard_set(
                    self.db.nodes[iid]["ifc_class"],
                    p["set"],
                    tuple(p["path"]),
                    p["kind"],
                    self.db.nodes[p["set_id"]]["ifc_class"],
                )
                row["_SetView"] = set_view
                if set_view:
                    canonical = self.column_name(set_view, p["name"], p["path"])
                    prefix = (
                        (
                            ""
                            if set_view in self.catalog.templates or set_view == "BaseQuantities"
                            else "Custom_"
                        )
                        + set_view
                        + "_"
                    )
                    row["_SetColumn"] = canonical.removeprefix(prefix)
                else:
                    row["_SetColumn"] = None
                serial = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if serial not in self._value_keys:
                    self._value_keys.add(serial)
                    self.values.append(row)
        if sources is not None:
            for key, tokens in sources.items():
                if tokens and all(token is not None for token in tokens):
                    # Retain every contributor, even if collapse deduplicated
                    # equal values from distinct IFC property definitions.
                    provenance[key] = ("property-cell", tuple(tokens))
        return {k: collapse(v) for k, v in values.items()}
