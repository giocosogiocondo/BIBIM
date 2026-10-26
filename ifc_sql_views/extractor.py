"""Public extraction facade; internal roles share one graph and provenance state."""

from __future__ import annotations
import json
from .units import Units
from .values import unique, collapse, wrapped_measure
from .extraction.resources import ResourceExtraction
from .extraction.properties import PropertyExtraction
from .extraction.property_projection import PropertyProjection
from .extraction.provenance import ProvenanceTracking
from .extraction.relations import RelationExtraction
from .extraction.rows import LegacyRowProjection

__all__ = ["Extractor", "unique", "collapse", "wrapped_measure", "json"]


class Extractor(
    ResourceExtraction,
    PropertyExtraction,
    PropertyProjection,
    ProvenanceTracking,
    RelationExtraction,
    LegacyRowProjection,
):
    def __init__(self, db, catalog):
        self.db, self.catalog = db, catalog
        self.units = Units(db)
        self.values = []
        self.links = []
        self._value_keys = set()
        self._link_keys = set()
        self._column_keys = {}
        self._column_names = {}
        # Shared OwnerHistory/context resources can have tens of thousands of
        # consumer inverses. Resource expansion only needs owned definitions.
        # Filter that graph once, rather than scan every consumer for every row.
        owners = {
            "IfcMaterialProperties": "Material",
            "IfcProfileProperties": "ProfileDefinition",
            "IfcMaterialDefinitionRepresentation": "RepresentedMaterial",
            "IfcExternalReferenceRelationship": "RelatedResourceObjects",
            "IfcResourceConstraintRelationship": "RelatedResourceObjects",
        }
        owned_attributes = {
            iid: {attr for base, attr in owners.items() if db.is_a(cls, base)}
            for cls, ids in db.by_class.items()
            if any(db.is_a(cls, base) for base in owners)
            for iid in ids
        }
        # A property reused by two definitions is a member, not their owner.
        # Following Properties backwards would mix other materials into this one
        # and create exponentially many paths through otherwise acyclic values.
        self._owned_incoming = {
            iid: [(sid, attr) for sid, attr in incoming if attr in owned_attributes.get(sid, ())]
            for iid, incoming in db.incoming.items()
            if any(attr in owned_attributes.get(sid, ()) for sid, attr in incoming)
        }
        for template in sorted(catalog.templates.values(), key=lambda t: t.Name):
            for path, _ in catalog.property_paths(template):
                self.column_name(template.Name, "_".join(path), path)
