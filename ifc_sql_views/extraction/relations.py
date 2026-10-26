"""RelationExtraction: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from collections import defaultdict
from functools import lru_cache
from ..rules import relation_role
from ..values import unique, collapse


class RelationExtraction:
    @lru_cache(maxsize=8192)
    def endpoint(self, iid, resources=False):
        cls = self.db.nodes[iid]["ifc_class"]
        if not self.db.is_a(cls, "IfcObjectDefinition"):
            return self.resource(iid)
        out = self.direct(iid)
        out.update(self.flat_properties(iid))
        # Other occurrences are object boundaries. Their own views expose their
        # remaining relationships without multiplying the initiating object's row.
        if resources and self.db.is_a(cls, "IfcTypeObject"):
            for attr, ids in self.db.edges.get(iid, {}).items():
                if attr in ("HasPropertySets",):
                    continue
                out[attr] = collapse([self.resource(t) for t in ids])
            for rid, role_attr in self.db.incoming.get(iid, []):
                if (
                    self.db.is_a(self.db.nodes[rid]["ifc_class"], "IfcRelAssociates")
                    and role_attr == "RelatedObjects"
                ):
                    rule = relation_role(self.db, self.db.nodes[rid]["ifc_class"], role_attr)
                    if rule:
                        role, target_attr, _ = rule
                        out[role] = collapse(
                            [self.resource(t) for t in self.db.refs(rid, target_attr)]
                        )
        return out

    def relations(self, iid, provenance=None):
        node = self.db.nodes[iid]
        buckets = defaultdict(list)
        for rid, entry_attr in self.db.incoming.get(iid, []):
            rel = self.db.nodes[rid]
            cls = rel["ifc_class"]
            if self.db.is_a(cls, "IfcRelDefinesByProperties"):
                continue
            inverse = self.db.inverse_name(node["ifc_class"], cls, entry_attr)
            rule = relation_role(self.db, cls, entry_attr)
            if rule:
                role, endpoint_attr, single = rule
                target_attrs = [endpoint_attr]
            else:
                role, single = inverse, False
                target_attrs = [
                    a for a in self.db.edges.get(rid, {}) if a not in (entry_attr, "OwnerHistory")
                ]
            # Non-relationship inverse holders also contain useful data (e.g.
            # IfcMaterialProperties). They are processed rather than ignored.
            if not self.db.is_a(cls, "IfcRelationship"):
                target_attrs = []
                target_data = self.resource(rid)
                buckets[role].append((rid, rid, target_data, False))
                continue
            for attr in target_attrs:
                for position, target in enumerate(self.db.refs(rid, attr)):
                    if target == iid:
                        continue
                    alias = role if rule else role + "_" + attr
                    item = self.endpoint(target, resources=alias == "Type")
                    buckets[alias].append((rid, target, item, single))
                    link = {
                        "element_id": iid,
                        "Role": alias,
                        "relationship_id": rid,
                        "target_id": target,
                        "target_class": self.db.nodes[target]["ifc_class"],
                        "Position": position,
                    }
                    key = tuple(link.values())
                    if key not in self._link_keys:
                        self._link_keys.add(key)
                        self.links.append(link)
            # The relation's own values (including connection geometry refs) are
            # queryable through View_Relationships; never flatten them over Name.
        out = {}
        for role, entries in buckets.items():
            targets = unique([e[2] for e in entries])
            if all(e[3] for e in entries) and len(targets) == 1:
                endpoint_sources = {}
                target_ids = {entry[1] for entry in entries}
                if provenance is not None and len(target_ids) == 1:
                    target = next(iter(target_ids))
                    cls = self.db.nodes[target]["ifc_class"]
                    if self.db.is_a(cls, "IfcObjectDefinition"):
                        semantic_role = (
                            "relation",
                            role,
                            tuple(sorted({(e[0], e[1]) for e in entries})),
                        )
                        endpoint_sources.update(
                            self._direct_provenance(target, self.direct(target), semantic_role)
                        )
                        if role == "Type" and self.db.is_a(cls, "IfcTypeObject"):
                            # Effective inherited values and the explicit Type
                            # endpoint are two presentations of the same shared
                            # property. Overrides have different definition IDs.
                            endpoint_sources.update(self._flat_property_provenance(target))
                for key, value in targets[0].items():
                    out[role + "_" + key] = value
                    if key in endpoint_sources:
                        provenance[role + "_" + key] = endpoint_sources[key]
            else:
                out[role] = targets
                out[role + "_Count"] = len({e[1] for e in entries})
                if any(e[3] for e in entries) and len(targets) > 1:
                    self.db.note("multiple_single_role_targets", iid, role)
                # Names are a useful compact retrieval field, while the objects
                # retain associations between names, properties, and IDs.
                if any(t.get("Name") for t in targets):
                    out[role + "_Names"] = [t.get("Name") for t in targets]
                if len(targets) == 1 and not self.db.is_a(
                    targets[0]["ifc_class"], "IfcObjectDefinition"
                ):
                    # Frequent singleton associations are filterable without
                    # JSON SQL, while the array preserves the role's cardinality.
                    for key, value in targets[0].items():
                        if not isinstance(value, (dict, list)):
                            out[role + "_" + key] = value
        return out

    def relation_summary(self, iid):
        """Record relationship endpoints without copying their Psets or resources."""
        buckets = defaultdict(list)
        node = self.db.nodes[iid]
        for rid, entry_attr in self.db.incoming.get(iid, []):
            cls = self.db.nodes[rid]["ifc_class"]
            if not self.db.is_a(cls, "IfcRelationship") or self.db.is_a(
                cls, "IfcRelDefinesByProperties"
            ):
                continue
            rule = relation_role(self.db, cls, entry_attr)
            if rule:
                role, attr, single = rule
                targets = [(role, attr)]
            else:
                role = self.db.inverse_name(node["ifc_class"], cls, entry_attr)
                single = False
                targets = [
                    (role + "_" + attr, attr)
                    for attr in self.db.edges.get(rid, {})
                    if attr not in (entry_attr, "OwnerHistory")
                ]
            for role, attr in targets:
                for position, target in enumerate(self.db.refs(rid, attr)):
                    if target == iid:
                        continue
                    buckets[role].append((target, single))
                    link = {
                        "element_id": iid,
                        "Role": role,
                        "relationship_id": rid,
                        "target_id": target,
                        "target_class": self.db.nodes[target]["ifc_class"],
                        "Position": position,
                    }
                    key = tuple(link.values())
                    if key not in self._link_keys:
                        self._link_keys.add(key)
                        self.links.append(link)
        out = {}
        for role, entries in buckets.items():
            ids = sorted({target for target, _ in entries})
            if len(ids) == 1 and (
                all(single for _, single in entries)
                or not self.db.is_a(self.db.nodes[ids[0]]["ifc_class"], "IfcObjectDefinition")
            ):
                out[role + "_ifc_id"] = ids[0]
                values = self.direct(ids[0])
                for name in ("Name", "PredefinedType", "Elevation", "Elevation_Unit"):
                    if name in values:
                        out[role + "_" + name] = values[name]
            else:
                out[role + "_Count"] = len(ids)
                names = [self.db.nodes[target].get("Name") for target in ids]
                if any(names):
                    out[role + "_Names"] = names
        return out

    def storeys(self, iid):
        result = set(self.spatial_ancestors(iid, "IfcBuildingStorey", True))
        if len(result) > 1:
            self.db.note("multiple_storeys", iid, targets=sorted(result))
        return result

    def contained_spaces(self, iid):
        return self.spatial_ancestors(iid, "IfcSpace", False)

    @lru_cache(maxsize=8192)
    def spatial_ancestors(self, iid, target_class, include_self=False):
        """Explicit containment/decomposition only; no references or geometry inference."""
        pending, seen, result = [iid], set(), set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if (include_self or current != iid) and self.db.is_a(
                self.db.nodes[current]["ifc_class"], target_class
            ):
                result.add(current)
                continue
            for rid, attr in self.db.incoming.get(current, []):
                rule = relation_role(self.db, self.db.nodes[rid]["ifc_class"], attr)
                if rule and rule[0] in ("Container", "AggregateParent", "NestParent"):
                    pending.extend(self.db.refs(rid, rule[1]))
        return frozenset(result)

    def boundary_spaces(self, iid):
        result = set()
        for rid, attr in self.db.incoming.get(iid, ()):
            if attr == "RelatedBuildingElement" and self.db.is_a(
                self.db.nodes[rid]["ifc_class"], "IfcRelSpaceBoundary"
            ):
                result.update(self.db.refs(rid, "RelatingSpace"))
        return sorted(result)
