"""IndependentSource: independent validation responsibility, sharing the Validator context."""

from __future__ import annotations
from collections import defaultdict
import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.util.unit
from .values import empty, native, unit_label


class IndependentSource:
    def properties(self, obj):
        """Independent forward/inverse traversal; occurrence overrides type by set/path."""

        def own(target):
            sets = {s.id(): s for s in getattr(target, "HasPropertySets", ()) or ()}
            for rel in self.model.get_inverse(target):
                if rel.is_a("IfcRelDefinesByProperties") and target in (rel.RelatedObjects or ()):
                    definition = rel.RelatingPropertyDefinition
                    if definition is None:
                        continue
                    definitions = (
                        definition if isinstance(definition, (list, tuple)) else (definition,)
                    )
                    for s in definitions:
                        if s.id():
                            sets[s.id()] = s
                        else:
                            sets.update({x.id(): x for x in s.wrappedValue})
            result = defaultdict(list)

            def leaf(p, path, active):
                path = (*path, p.Name or f"Unnamed_{p.id()}")
                key = (s.Name or s.is_a(), path)
                kind = "Quantity" if p.is_a("IfcPhysicalQuantity") else "Property"
                try:
                    if p.id() in active:
                        raise ValueError("cyclic property")
                    if p.is_a("IfcComplexProperty") or p.is_a("IfcPhysicalComplexQuantity"):
                        for child in (
                            getattr(p, "HasProperties", None)
                            or getattr(p, "HasQuantities", None)
                            or ()
                        ):
                            leaf(child, path, active | {p.id()})
                        return
                    if p.is_a("IfcPropertySingleValue"):
                        value = native(p.NominalValue)
                    elif p.is_a("IfcPropertyListValue"):
                        value = native(p.ListValues)
                    elif p.is_a("IfcPropertyEnumeratedValue"):
                        value = native(p.EnumerationValues)
                        if isinstance(value, list) and len(value) == 1:
                            value = value[0]
                    elif p.is_a("IfcPropertyBoundedValue"):
                        value = {
                            k: native(getattr(p, k, None))
                            for k in ("LowerBoundValue", "UpperBoundValue", "SetPointValue")
                            if k in p.get_info()
                        }
                    elif p.is_a("IfcPhysicalSimpleQuantity"):
                        value = native(p[3])
                    else:
                        raise ValueError("unsupported structured property")
                    unit = None
                    if p.is_a("IfcPropertySingleValue") or p.is_a("IfcPhysicalSimpleQuantity"):
                        unit = unit_label(
                            ifcopenshell.util.unit.get_property_unit(p, self.model, use_cache=True)
                        )
                    result[key].append((kind, value, unit, None))
                except (ValueError, AttributeError, TypeError, RuntimeError) as exc:
                    result[key].append((kind, None, None, str(exc)))

            for s in sorted(sets.values(), key=lambda x: x.id()):
                if s.is_a("IfcPropertySet") or s.is_a("IfcElementQuantity"):
                    for p in (
                        getattr(s, "HasProperties", None) or getattr(s, "Quantities", None) or ()
                    ):
                        leaf(p, (), frozenset())
                else:
                    result[(s.Name or s.is_a(), (s.is_a(),))].append(
                        (
                            "Property",
                            None,
                            None,
                            "static property set not yet independently decoded",
                        )
                    )
            return result

        effective = defaultdict(list)
        types = {
            r.RelatingType.id(): r.RelatingType
            for r in self.model.get_inverse(obj)
            if r.is_a("IfcRelDefinesByType")
            and obj in (r.RelatedObjects or ())
            and r.RelatingType is not None
        }
        for t in sorted(types.values(), key=lambda x: x.id()):
            for key, values in own(t).items():
                effective[key].extend(values)
        effective.update(own(obj))
        return effective, list(types.values())

    def scalars(self, obj, prefix=""):
        declared = self.schema.declaration_by_name(obj.is_a()).all_attributes()
        for index in range(len(obj)):
            name = obj.attribute_name(index)
            value = obj[index]
            if empty(value):
                continue
            try:
                v = native(value)
            except ValueError:
                yield prefix + name, None, None, "entity reference not independently interpreted"
                continue
            measure = (
                value.is_a()
                if isinstance(value, ifcopenshell.entity_instance)
                else str(declared[index].type_of_attribute())
            )
            kind = (
                None
                if "CompoundPlaneAngleMeasure" in measure
                else next(
                    (
                        u
                        for word, u in [
                            ("Length", "LENGTHUNIT"),
                            ("Area", "AREAUNIT"),
                            ("Volume", "VOLUMEUNIT"),
                            ("PlaneAngle", "PLANEANGLEUNIT"),
                        ]
                        if word + "Measure" in measure
                    ),
                    None,
                )
            )
            yield prefix + name, v, self.units.get(kind), None
