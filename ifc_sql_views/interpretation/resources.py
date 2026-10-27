"""ResourceInterpretation: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from functools import lru_cache
import json
from ..reader import unpack
from ..values import collapse, without_references


class ResourceInterpretation:
    def select_value(self, iid, attr, active=frozenset()):
        raw = self.db.nodes[iid].get(attr)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return raw
        if isinstance(raw, dict) and "type" in raw:
            if raw["type"] in self.db.entities:
                target = raw.get("value")
                return self.resource(target, active) if target in self.db.nodes else None
            return unpack(raw)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            # Untagged mixed numeric/entity SELECTs cannot be interpreted safely.
            if raw in self.db.nodes and raw in self.db.refs(iid, attr):
                return self.resource(raw, active)
            if any(
                d.get("code") == "ambiguous_select"
                and d.get("ifc_id") == iid
                and d.get("attribute") == attr
                for d in self.db.diagnostics
            ):
                return None
        return unpack(raw)

    def resource(self, iid, active=frozenset()):
        """Traverse owned forward attributes; never expand a resource's consumers."""
        if iid in active or len(active) >= 64:
            self.db.note(
                "semantic_resource_cycle" if iid in active else "semantic_resource_depth_limit",
                iid,
            )
            return None
        node = self.db.nodes[iid]
        references = self.db.reference_attributes(node["ifc_class"])
        refs = {a[0] for a in references}
        scalar = self.extractor.direct(iid)
        result = {
            k: without_references(v)
            for k, v in scalar.items()
            if k != "ifc_id" and not k.endswith("_id") and k not in refs
        }
        if self.db.is_a(node["ifc_class"], "IfcObjectDefinition") or self.db.is_a(
            node["ifc_class"], "IfcRelationship"
        ):
            return result
        # A shape graph contains mesh vertices and cycles, rather than scalar object attributes.
        if self.db.is_a(node["ifc_class"], "IfcRepresentation") or self.db.is_a(
            node["ifc_class"], "IfcRepresentationItem"
        ):
            return result
        for attr in refs:
            if self.db.refs(iid, attr) and all(t in active for t in self.db.refs(iid, attr)):
                continue
            if any(a[0] == attr and a[2] for a in references):
                result[attr] = self.select_value(iid, attr, active | {iid})
                continue
            values = [self.resource(target, active | {iid}) for target in self.db.refs(iid, attr)]
            if values:
                # Aggregate positions are source occurrences, not a set of unique
                # interpreted values. Even two equal members (or the same member
                # twice in a LIST) must survive, including singleton aggregates.
                raw = unpack(node.get(attr))
                result[attr] = values if isinstance(raw, list) else collapse(values)
        for source, attribute in self.extractor._owned_incoming.get(iid, ()):
            if source in active:
                continue
            key = self.db.nodes[source]["ifc_class"].removeprefix("Ifc")
            details = self.resource(source, active | {iid})
            if details:
                result.setdefault(key, []).append(details)
        return result

    @lru_cache(maxsize=4096)
    def resolved_resource(self, iid):
        return self.resource(iid)

    def flattened(self, prefix, value):
        if isinstance(value, dict):
            out = {}
            for name, child in value.items():
                if name == "ifc_class":
                    continue
                out.update(self.flattened(prefix + "_" + name, child))
            return out
        return {prefix: value}

    def first(self, iid, attr):
        refs = self.db.refs(iid, attr)
        return refs[0] if refs else None
