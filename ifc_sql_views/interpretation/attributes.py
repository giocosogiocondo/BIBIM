"""ObjectAttributes: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from collections import defaultdict
from functools import lru_cache
from ..values import collapse, without_references


class ObjectAttributes:
    def attributes(self, iid, *, include_typing=True):
        node = self.db.nodes[iid]
        refs = {a[0] for a in self.db.reference_attributes(node["ifc_class"])}
        result = {
            k: without_references(v)
            for k, v in self.extractor.direct(iid).items()
            if k == "ifc_id" or (not k.endswith("_id") and k not in refs)
        }
        if self.db.is_a(node["ifc_class"], "IfcProject"):
            result.update(self.db.source_fields)
        for attr in sorted(refs):
            if attr in ("HasPropertySets", "RepresentationMaps"):
                continue
            ids = self.db.refs(iid, attr)
            if attr == "ObjectPlacement":
                if ids:
                    result.update(self.placement_fields(ids[0]))
            elif attr == "OwnerHistory":
                if ids:
                    result.update(self.owner_fields(ids[0]))
            elif attr == "Representation":
                if ids:
                    result.update(self.representation_fields(ids[0]))
            else:
                if ids:
                    result.update(
                        self.flattened(attr, collapse([self.resolved_resource(t) for t in ids]))
                    )
                elif any(
                    a[0] == attr and a[2] for a in self.db.reference_attributes(node["ifc_class"])
                ):
                    result.update(self.flattened(attr, self.select_value(iid, attr)))
        if include_typing:
            buckets = defaultdict(lambda: defaultdict(list))
            for tid in self.extractor.type_ids(iid):
                cls = self.db.nodes[tid]["ifc_class"]
                prefix = cls.removeprefix("Ifc")
                if prefix.endswith("Style"):
                    prefix = prefix[:-5] + "style"
                for name, value in self.typing_fields(tid).items():
                    if name not in ("ifc_id", "ifc_class"):
                        buckets[prefix][name].append(value)
            for prefix, fields in buckets.items():
                for name, values in fields.items():
                    result[prefix + "_" + name] = collapse(values)
        return result

    @lru_cache(maxsize=4096)
    def typing_fields(self, iid):
        result = self.attributes(iid, include_typing=False)
        for rid, attr in self.db.incoming.get(iid, ()):
            if attr != "RelatedObjects":
                continue
            cls = self.db.nodes[rid]["ifc_class"]
            for base, reference, prefix in (
                ("IfcRelAssociatesMaterial", "RelatingMaterial", "Material"),
                ("IfcRelAssociatesClassification", "RelatingClassification", "Classification"),
            ):
                if self.db.is_a(cls, base):
                    for target in self.db.refs(rid, reference):
                        result.update(self.flattened(prefix, self.resolved_resource(target)))
        return result

    def relations(self, iid):
        summary = self.extractor.relation_summary(iid)
        result = {
            key: value
            for key, value in summary.items()
            if not key.endswith("_ifc_id") and not key.startswith(("Type_", "BoundarySpaces_"))
        }
        endpoints = defaultdict(list)
        seen_targets = set()
        for link in self.extractor.links[self._link_start :]:
            if link["element_id"] != iid or link["Role"] in ("Type", "BoundarySpaces"):
                continue
            target_key = (link["Role"], link["target_id"])
            if target_key in seen_targets:
                continue
            seen_targets.add(target_key)
            node = self.db.nodes[link["target_id"]]
            if self.db.is_a(node["ifc_class"], "IfcObjectDefinition"):
                details = {
                    key: node.get(key)
                    for key in ("GlobalId", "Name", "PredefinedType", "ObjectType", "LongName")
                }
            else:
                details = self.resolved_resource(link["target_id"])
            endpoints[link["Role"]].append(details)
        for role, targets in endpoints.items():
            # De-duplicate repeated paths by original target ID above. Different
            # targets remain different even when their interpreted values match.
            result.update(self.flattened(role, targets[0] if len(targets) == 1 else targets))
        storeys = sorted(self.extractor.storeys(iid))
        for prefix, ids, fields in (
            ("Storey", storeys, ("GlobalId", "Name", "Elevation")),
            ("ContainedSpace", sorted(self.extractor.contained_spaces(iid)), ("GlobalId", "Name")),
        ):
            if not ids:
                continue
            for field in fields:
                values = [self.db.nodes[target].get(field) for target in ids]
                result[prefix + "_" + field] = values[0] if len(values) == 1 else values
        if storeys:
            if any(self.db.nodes[target].get("Elevation") is not None for target in storeys):
                result["Storey_Elevation_Unit"] = self.units.for_value("IfcLengthMeasure")
        spaces = self.extractor.boundary_spaces(iid)
        result["BoundarySpaces"] = [
            {field: self.db.nodes[target].get(field) for field in ("GlobalId", "Name", "LongName")}
            for target in spaces
        ] or None
        return result

    def object_attributes(self, iid):
        attributes = self.attributes(iid)
        self._link_start = len(self.extractor.links)
        attributes.update(self.relations(iid))
        return attributes
