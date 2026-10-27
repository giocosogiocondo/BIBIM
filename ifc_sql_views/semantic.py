"""Public semantic facade combining placement, resource, attribute and row roles."""

from .geometry_math import IDENTITY_MATRIX, multiply, normalized, cross
from .schemas import CUSTOM_IDENTIFIERS
from .values import without_references
from .interpretation.placement import PlacementInterpretation
from .interpretation.resources import ResourceInterpretation
from .interpretation.owner import OwnerInterpretation
from .interpretation.representation import RepresentationInterpretation
from .interpretation.attributes import ObjectAttributes
from .interpretation.rows import EntityRowComposition

__all__ = [
    "Semantic",
    "CUSTOM_IDENTIFIERS",
    "IDENTITY_MATRIX",
    "multiply",
    "normalized",
    "cross",
    "without_references",
]


class Semantic(
    PlacementInterpretation,
    ResourceInterpretation,
    OwnerInterpretation,
    RepresentationInterpretation,
    ObjectAttributes,
    EntityRowComposition,
):
    def __init__(self, extractor):
        self.extractor = extractor
        self.db = extractor.db
        self.units = extractor.units
        self.names = {}
        self.keys = {}
        self.key_maps = {}
