"""Relation roles; all unlisted relations use EXPRESS-derived role names.

Keys include the incoming attribute: the same relationship has different
meanings when the current object occupies the opposite endpoint.
"""

RELATION_ROLES = {
    ("IfcRelDefinesByType", "RelatedObjects"): ("Type", "RelatingType", True),
    ("IfcRelDefinesByType", "RelatingType"): ("TypedObjects", "RelatedObjects", False),
    ("IfcRelAggregates", "RelatedObjects"): ("AggregateParent", "RelatingObject", True),
    ("IfcRelAggregates", "RelatingObject"): ("AggregateChildren", "RelatedObjects", False),
    ("IfcRelNests", "RelatedObjects"): ("NestParent", "RelatingObject", True),
    ("IfcRelNests", "RelatingObject"): ("NestChildren", "RelatedObjects", False),
    ("IfcRelContainedInSpatialStructure", "RelatedElements"): (
        "Container",
        "RelatingStructure",
        True,
    ),
    ("IfcRelContainedInSpatialStructure", "RelatingStructure"): (
        "ContainedElements",
        "RelatedElements",
        False,
    ),
    ("IfcRelReferencedInSpatialStructure", "RelatedElements"): (
        "ReferencedStructures",
        "RelatingStructure",
        False,
    ),
    ("IfcRelReferencedInSpatialStructure", "RelatingStructure"): (
        "ReferencedElements",
        "RelatedElements",
        False,
    ),
    ("IfcRelAssociatesMaterial", "RelatedObjects"): ("Material", "RelatingMaterial", False),
    ("IfcRelAssociatesClassification", "RelatedObjects"): (
        "Classification",
        "RelatingClassification",
        False,
    ),
    ("IfcRelAssociatesDocument", "RelatedObjects"): ("Document", "RelatingDocument", False),
    ("IfcRelAssociatesLibrary", "RelatedObjects"): ("Library", "RelatingLibrary", False),
    ("IfcRelAssignsToGroup", "RelatedObjects"): ("Groups", "RelatingGroup", False),
    ("IfcRelAssignsToGroup", "RelatingGroup"): ("GroupMembers", "RelatedObjects", False),
    ("IfcRelVoidsElement", "RelatingBuildingElement"): (
        "Openings",
        "RelatedOpeningElement",
        False,
    ),
    ("IfcRelVoidsElement", "RelatedOpeningElement"): (
        "OpeningHost",
        "RelatingBuildingElement",
        True,
    ),
    ("IfcRelFillsElement", "RelatedBuildingElement"): (
        "FilledOpening",
        "RelatingOpeningElement",
        True,
    ),
    ("IfcRelFillsElement", "RelatingOpeningElement"): (
        "Fillings",
        "RelatedBuildingElement",
        False,
    ),
    ("IfcRelConnectsElements", "RelatingElement"): ("ConnectedTo", "RelatedElement", False),
    ("IfcRelConnectsElements", "RelatedElement"): ("ConnectedFrom", "RelatingElement", False),
    ("IfcRelSpaceBoundary", "RelatingSpace"): (
        "BoundaryElements",
        "RelatedBuildingElement",
        False,
    ),
    ("IfcRelSpaceBoundary", "RelatedBuildingElement"): ("BoundarySpaces", "RelatingSpace", False),
    ("IfcRelConnectsPortToElement", "RelatedElement"): ("Ports", "RelatingPort", False),
    ("IfcRelConnectsPortToElement", "RelatingPort"): ("PortHost", "RelatedElement", True),
    ("IfcRelConnectsPorts", "RelatingPort"): ("ConnectedPortsTo", "RelatedPort", False),
    ("IfcRelConnectsPorts", "RelatedPort"): ("ConnectedPortsFrom", "RelatingPort", False),
}


def relation_role(db, relation_class, incoming_attribute):
    d = db.entities[relation_class]
    while d:
        result = RELATION_ROLES.get((d.name(), incoming_attribute))
        if result:
            return result
        d = d.supertype()
    return None
