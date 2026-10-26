"""ProvenanceTracking: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from functools import lru_cache
from ..units import MEASURE_UNITS


class ProvenanceTracking:
    @lru_cache(maxsize=None)
    def _attribute_type(self, cls, name):
        for attribute in self.db.attributes(cls):
            if attribute.name() == name:
                return str(attribute.type_of_attribute())
        return None

    def _unit_source(self, measure, explicit=None):
        """Identify the original unit, including whether it was explicit."""
        if explicit:
            return ("explicit", explicit)
        key = MEASURE_UNITS.get(measure)
        if not key and measure and measure.startswith("Ifc") and measure.endswith("Measure"):
            key = measure[3:-7].upper() + "UNIT"
        return ("project", key, self.units.project_units.get(key))

    def _direct_provenance(self, iid, data, role):
        node = self.db.nodes[iid]
        attrs = {a.name(): a for a in self.db.attributes(node["ifc_class"])}
        result = {}
        for column in data:
            if column in ("ifc_id", "ifc_class"):
                result[column] = ("identity", role, iid, column)
                continue
            raw_name, form = column, "value"
            if raw_name not in attrs and raw_name.endswith("_id"):
                raw_name, form = raw_name[:-3], "reference"
            if raw_name not in attrs and raw_name.endswith("_Unit"):
                raw_name, form = raw_name[:-5], "unit"
            attribute = attrs.get(raw_name)
            if attribute is None:
                continue
            typ = attribute.type_of_attribute()
            declared = typ.declared_type() if hasattr(typ, "declared_type") else None
            measure = declared.name() if hasattr(declared, "name") else None
            result[column] = (
                "attribute",
                role,
                iid,
                raw_name,
                str(typ),
                form,
                measure,
                self._unit_source(measure),
            )
        return result

    @lru_cache(maxsize=8192)
    def _flat_property_provenance(self, iid):
        # Shared Type endpoints may serve thousands of occurrences. Their
        # effective property sources are stable for this Database, just like the
        # cached endpoint values. Immutable items prevent callers changing a
        # cached mapping while preserving every original contributor token.
        provenance = {}
        self.flat_properties(iid, provenance=provenance)
        return tuple(provenance.items())
