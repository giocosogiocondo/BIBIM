"""PropertyExtraction: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from collections import defaultdict
from functools import lru_cache
from ..reader import unpack
from ..values import unique, collapse, wrapped_measure


class PropertyExtraction:
    @lru_cache(maxsize=None)
    def type_ids(self, iid):
        out = []
        for rid, attr in self.db.incoming.get(iid, []):
            if attr == "RelatedObjects" and self.db.is_a(
                self.db.nodes[rid]["ifc_class"], "IfcRelDefinesByType"
            ):
                out.extend(self.db.refs(rid, "RelatingType"))
        ids = tuple(sorted(set(out)))
        if len(ids) > 1:
            self.db.note("multiple_types", iid, targets=list(ids))
        return ids

    @lru_cache(maxsize=None)
    def set_ids(self, iid):
        out = list(self.db.refs(iid, "HasPropertySets"))
        for rid, attr in self.db.incoming.get(iid, []):
            if attr == "RelatedObjects" and self.db.is_a(
                self.db.nodes[rid]["ifc_class"], "IfcRelDefinesByProperties"
            ):
                out.extend(self.db.refs(rid, "RelatingPropertyDefinition"))
        return tuple(sorted(set(out)))

    def property_value(self, pid, set_name, path, active=frozenset(), source_path=()):
        if pid in active:
            self.db.note("property_cycle", pid)
            return []
        if len(active) >= 128:
            self.db.note("property_depth_limit", pid)
            return []
        n = self.db.nodes[pid]
        cls = n["ifc_class"]
        name = str(n.get("Name") or f"Unnamed_{pid}")
        path = (*path, name)
        source_path = (*source_path, pid)
        active = active | {pid}
        if cls in ("IfcComplexProperty", "IfcPhysicalComplexQuantity"):
            attr = "HasProperties" if cls == "IfcComplexProperty" else "HasQuantities"
            result = []
            for child in self.db.refs(pid, attr):
                result.extend(self.property_value(child, set_name, path, active, source_path))
            return result
        measure = self.catalog.measure(set_name, "_".join(path))
        unit_ids = self.db.refs(pid, "Unit")
        explicit = unit_ids[0] if unit_ids else None
        value = None
        source_attrs = None
        if cls == "IfcPropertySingleValue":
            source_attrs = ("NominalValue",)
            raw = n.get("NominalValue")
            measure = wrapped_measure(raw) or measure
            value = unpack(raw)
        elif cls == "IfcPropertyEnumeratedValue":
            source_attrs = ("EnumerationValues", "EnumerationReference")
            measure = wrapped_measure(n.get("EnumerationValues")) or measure
            values = unpack(n.get("EnumerationValues"))
            value = values[0] if isinstance(values, list) and len(values) == 1 else values
            enum_ids = self.db.refs(pid, "EnumerationReference")
            if enum_ids and not explicit:
                enum_units = self.db.refs(enum_ids[0], "Unit")
                explicit = enum_units[0] if enum_units else None
        elif cls == "IfcPropertyListValue":
            source_attrs = ("ListValues",)
            measure = wrapped_measure(n.get("ListValues")) or measure
            value = unpack(n.get("ListValues"))
        elif cls == "IfcPropertyBoundedValue":
            source_attrs = ("LowerBoundValue", "UpperBoundValue", "SetPointValue")
            tags = {
                wrapped_measure(n.get(k))
                for k in ("LowerBoundValue", "UpperBoundValue", "SetPointValue")
            } - {None}
            if len(tags) == 1:
                measure = next(iter(tags))
            value = {
                k: unpack(n.get(k))
                for k in ("LowerBoundValue", "UpperBoundValue", "SetPointValue")
                if k in n
            }
        elif cls == "IfcPropertyTableValue":
            source_attrs = (
                "DefiningValues",
                "DefinedValues",
                "Expression",
                "CurveInterpolation",
                "DefiningUnit",
                "DefinedUnit",
            )
            value = {
                k: unpack(n.get(k)) if k.endswith("Values") else n.get(k)
                for k in ("DefiningValues", "DefinedValues", "Expression", "CurveInterpolation")
                if k in n
            }
            for key in ("DefiningUnit", "DefinedUnit"):
                u = self.db.refs(pid, key)
                value[key] = self.units.for_value(
                    wrapped_measure(n.get(key.replace("Unit", "Values"))), u[0] if u else None
                )
        elif cls == "IfcPropertyReferenceValue":
            source_attrs = ("PropertyReference",)
            refs = self.db.refs(pid, "PropertyReference")
            value = collapse([self.resource(x) for x in refs])
        elif self.db.is_a(cls, "IfcPhysicalSimpleQuantity"):
            mapping = {
                "LengthValue": "IfcLengthMeasure",
                "AreaValue": "IfcAreaMeasure",
                "VolumeValue": "IfcVolumeMeasure",
                "WeightValue": "IfcMassMeasure",
                "TimeValue": "IfcTimeMeasure",
                "CountValue": "IfcCountMeasure",
            }
            for attr, quantity_measure in mapping.items():
                if attr in n:
                    source_attrs = (attr,)
                    value = unpack(n[attr])
                    measure = quantity_measure
                    break
        else:
            value = self.resource(pid)
        # Unknown resource-backed property classes intentionally have no token.
        # A matching output value alone does not prove matching IFC semantics.
        source = None
        if source_attrs is not None:
            raw_types = tuple(
                (attr, self._attribute_type(cls, attr), wrapped_measure(n.get(attr)))
                for attr in source_attrs
            )
            unit_refs = tuple(
                (attr, tuple(self.db.refs(pid, attr)))
                for attr in ("Unit", "DefiningUnit", "DefinedUnit", "EnumerationReference")
            )
            source = (
                source_path,
                cls,
                raw_types,
                measure,
                self._unit_source(measure, explicit),
                unit_refs,
            )
        return [
            {
                "name": "_".join(path),
                "path": list(path),
                "value": value,
                "unit": self.units.for_value(measure, explicit),
                "kind": "Quantity" if self.db.is_a(cls, "IfcPhysicalQuantity") else "Property",
                "definition_id": pid,
                "_source": source,
            }
        ]

    @lru_cache(maxsize=None)
    def own_properties(self, iid):
        found = []
        for sid in self.set_ids(iid):
            s = self.db.nodes[sid]
            name = str(s.get("Name") or s["ifc_class"])
            attrs = (
                "Quantities"
                if self.db.is_a(s["ifc_class"], "IfcElementQuantity")
                else "HasProperties"
            )
            ids = self.db.refs(sid, attrs)
            if ids:
                for pid in ids:
                    for value in self.property_value(pid, name, ()):
                        found.append({**value, "set": name, "set_id": sid})
            elif s["ifc_class"] not in ("IfcPropertySet", "IfcElementQuantity"):
                # IFC2X3 has statically defined property-set entities as well.
                measured = self.direct(sid)
                for attr, value in self.db.scalar_attributes(sid).items():
                    if attr not in {
                        "ifc_id",
                        "ifc_class",
                        "GlobalId",
                        "OwnerHistory_id",
                        "Name",
                        "Description",
                    }:
                        source = self._direct_provenance(sid, {attr: value}, ("property",)).get(
                            attr
                        )
                        found.append(
                            {
                                "name": attr,
                                "path": [attr],
                                "value": value,
                                "unit": measured.get(attr + "_Unit"),
                                "kind": "Property",
                                "definition_id": sid,
                                "set": name,
                                "set_id": sid,
                                "_source": source,
                            }
                        )
        return tuple(found)

    @lru_cache(maxsize=None)
    def effective_properties(self, iid):
        grouped = defaultdict(list)
        for tid in self.type_ids(iid):
            for p in self.own_properties(tid):
                # HasPropertySets carries the actual shared properties in both
                # schemas. Template classification is not a data deletion rule.
                grouped[(p["set"], tuple(p["path"]))].append(p)
        own = defaultdict(list)
        for p in self.own_properties(iid):
            own[(p["set"], tuple(p["path"]))].append(p)
        grouped.update(own)
        out = []
        for key, props in sorted(grouped.items()):
            variants = unique([(p["value"], p["unit"]) for p in props])
            if len(variants) > 1:
                self.db.note("conflicting_property_values", iid, key[0] + "." + ".".join(key[1]))
            out.extend(props)
        return tuple(out)
