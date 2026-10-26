"""ResourceExtraction: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from functools import lru_cache
from ..reader import unpack
from ..values import collapse


class ResourceExtraction:
    def direct(self, iid):
        data = self.db.scalar_attributes(iid)
        for a in self.db.attributes(self.db.nodes[iid]["ifc_class"]):
            if data.get(a.name()) is None:
                continue
            typ = a.type_of_attribute()
            declared = typ.declared_type() if hasattr(typ, "declared_type") else None
            measure = declared.name() if hasattr(declared, "name") else None
            unit = self.units.for_value(measure)
            if unit:
                data[a.name() + "_Unit"] = unit
        return data

    def resource(self, iid, active=frozenset()):
        if not active:
            return self._root_resource(iid)
        return self._resource(iid, active)

    @lru_cache(maxsize=2048)
    def _root_resource(self, iid):
        # Only cache root expansions: cycle boundaries depend on the active path.
        return self._resource(iid, frozenset())

    def _resource(self, iid, active):
        """Expand resource attributes. Object/relationship endpoints are explicit boundaries.

        Repeated nodes remain ID references. Every raw node/reference also exists
        in the queryable resource/reference views, so no cycle edge is discarded.
        """
        if iid in active:
            self.db.note("resource_cycle", iid)
            return {"ifc_id": iid, "ifc_class": self.db.nodes[iid]["ifc_class"]}
        if len(active) >= 128:
            self.db.note("resource_depth_limit", iid)
            return self.direct(iid)
        node = self.db.nodes[iid]
        out = self.direct(iid)
        cls = node["ifc_class"]
        if self.db.is_a(cls, "IfcObjectDefinition") or self.db.is_a(cls, "IfcRelationship"):
            return out
        active = active | {iid}
        for attr, ids in self.db.edges.get(iid, {}).items():
            if not ids:
                continue
            # Geometry topology is represented by references into View_Resources,
            # rather than recursively duplicating mesh vertices into every beam.
            if self.db.is_a(cls, "IfcRepresentation") or self.db.is_a(
                cls, "IfcRepresentationItem"
            ):
                continue
            data = [self.resource(target, active) for target in ids]
            raw = unpack(node.get(attr))
            out[attr] = data if isinstance(raw, list) else data[0]
        # Resource-owned definitions (e.g. material/profile properties) are inverse
        # links, too. Do not expand associations back out to other object consumers.
        for sid, attribute in self._owned_incoming.get(iid, []):
            sc = self.db.nodes[sid]["ifc_class"]
            # Definition inverses belong to the resource; consumer inverses do
            # not. A shared material must never pull all its building elements
            # into the current beam's row.
            owned = (
                "IfcMaterialProperties",
                "IfcProfileProperties",
                "IfcMaterialDefinitionRepresentation",
                "IfcExternalReferenceRelationship",
                "IfcResourceConstraintRelationship",
            )
            if any(self.db.is_a(sc, base) for base in owned) and sid not in active:
                key = self.db.inverse_name(cls, sc, attribute)
                if self.db.is_a(sc, "IfcResourceLevelRelationship"):
                    details = self.direct(sid)
                    for attr, targets in self.db.edges.get(sid, {}).items():
                        if attr != attribute:
                            details[attr] = collapse(
                                [self.resource(t, active | {sid}) for t in targets]
                            )
                    out.setdefault(key, []).append(details)
                else:
                    out.setdefault(key, []).append(self.resource(sid, active))
        return out
