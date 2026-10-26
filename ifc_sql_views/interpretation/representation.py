"""RepresentationInterpretation: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from collections import defaultdict
from ..values import collapse


class RepresentationInterpretation:
    def representation_fields(self, iid):
        reps = self.db.refs(iid, "Representations")
        if self.db.is_a(self.db.nodes[iid]["ifc_class"], "IfcRepresentation"):
            reps = [iid]
        fields = defaultdict(list)
        visited = set()

        def visit(rep):
            if rep in visited:
                return
            visited.add(rep)
            node = self.db.nodes[rep]
            for name in ("Name", "Description", "RepresentationIdentifier", "RepresentationType"):
                if node.get(name) is not None:
                    key = {
                        "RepresentationIdentifier": "Identifier",
                        "RepresentationType": "Type",
                    }.get(name, name)
                    fields["Representation_" + key].append(node[name])
            items = self.db.refs(rep, "Items")
            fields["Representation_ItemTypes"].extend(self.db.nodes[i]["ifc_class"] for i in items)
            for item in items:
                cls = self.db.nodes[item]["ifc_class"]
                for styled, attr in self.db.incoming.get(item, ()):
                    if attr != "Item" or self.db.nodes[styled]["ifc_class"] != "IfcStyledItem":
                        continue
                    for style in self.db.refs(styled, "Styles"):
                        values = self.flattened(
                            "Representation_Style", self.resolved_resource(style)
                        )
                        for key, value in values.items():
                            fields[key].append(value)
                if cls == "IfcMappedItem":
                    mapping = self.first(item, "MappingSource")
                    target = self.first(mapping, "MappedRepresentation") if mapping else None
                    if target:
                        visit(target)
                    continue
                # Only explicit dimensions/profiles are read; no tessellation or inferred Qto.
                direct = self.extractor.direct(item)
                for name in ("Depth", "Radius", "SemiAxis1", "SemiAxis2", "XDim", "YDim", "ZDim"):
                    if direct.get(name) is not None:
                        key = "Representation_" + cls.removeprefix("Ifc") + "_" + name
                        fields[key].append(direct[name])
                        if direct.get(name + "_Unit"):
                            fields[key + "_Unit"].append(direct[name + "_Unit"])
                for profile in self.db.refs(item, "SweptArea"):
                    details = self.resolved_resource(profile)
                    for key, value in self.flattened(
                        "Representation_"
                        + self.db.nodes[profile]["ifc_class"].removeprefix("Ifc"),
                        details,
                    ).items():
                        fields[key].append(value)

        for rep in reps:
            visit(rep)
        return {key: collapse(values) for key, values in fields.items()}
