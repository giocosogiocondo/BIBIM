"""PlacementInterpretation: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from functools import lru_cache
import json
import math
from ..reader import unpack
from ..geometry_math import multiply, orthogonal_axes


class PlacementInterpretation:
    def vector(self, iid, attr, default=None):
        target = self.first(iid, attr)
        if target is None:
            return default
        node = self.db.nodes[target]
        value = unpack(
            node.get("Coordinates")
            if node["ifc_class"] == "IfcCartesianPoint"
            else node.get("DirectionRatios")
        )
        if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
            raise ValueError("Invalid coordinate vector")
        vector = tuple(float(x) for x in value) + ((0.0,) if len(value) == 2 else ())
        if not all(math.isfinite(x) for x in vector):
            raise ValueError("Nonfinite coordinate vector")
        return vector

    def axis_matrix(self, iid):
        node = self.db.nodes[iid]
        if node["ifc_class"] not in ("IfcAxis2Placement2D", "IfcAxis2Placement3D"):
            raise ValueError("Unsupported axis placement")
        origin = self.vector(iid, "Location")
        if origin is None:
            raise ValueError("Missing location")
        x, y, z = orthogonal_axes(
            self.vector(iid, "Axis", (0.0, 0.0, 1.0)),
            self.vector(iid, "RefDirection", (1.0, 0.0, 0.0)),
        )
        return tuple((x[i], y[i], z[i], origin[i]) for i in range(3)) + ((0.0, 0.0, 0.0, 1.0),)

    def _placement(self, iid, active):
        if iid in active:
            self.db.note("placement_cycle", iid)
            return None
        if len(active) >= 128:
            self.db.note("placement_depth_limit", iid)
            return None
        node = self.db.nodes[iid]
        if node["ifc_class"] not in ("IfcLocalPlacement", "IfcLinearPlacement"):
            self.db.note("unsupported_placement", iid, placement_class=node["ifc_class"])
            return None
        relative = self.first(iid, "RelativePlacement")
        if relative is None:
            self.db.note("invalid_placement", iid)
            return None
        try:
            if node["ifc_class"] == "IfcLinearPlacement":
                cartesian = self.first(iid, "CartesianPosition")
                if cartesian is not None:
                    local = self.axis_matrix(cartesian)
                else:
                    from ..linear import axis_matrix

                    local = axis_matrix(self, relative)
            else:
                local = self.axis_matrix(relative)
        except (TypeError, ValueError, OverflowError):
            self.db.note("invalid_placement", iid)
            return None
        parent = self.first(iid, "PlacementRelTo")
        if parent is None:
            if node.get("PlacementRelTo") is not None:
                self.db.note("invalid_placement_parent", iid)
                return None
            return local
        parent_matrix = self._placement(parent, active | {iid})
        return multiply(parent_matrix, local) if parent_matrix is not None else None

    @lru_cache(maxsize=None)
    def placement(self, iid):
        return self._placement(iid, frozenset())

    def placement_origin(self, iid):
        """Recover an origin without inventing an invalid local orientation.

        A parent must still have a complete valid frame: its rotation transforms
        this local location, even when this placement's own rotation is invalid.
        """
        node = self.db.nodes[iid]
        if node["ifc_class"] not in ("IfcLocalPlacement", "IfcLinearPlacement"):
            return None
        if node["ifc_class"] == "IfcLinearPlacement":
            cartesian = self.first(iid, "CartesianPosition")
            relative = cartesian or self.first(iid, "RelativePlacement")
        else:
            relative = self.first(iid, "RelativePlacement")
        if relative is None:
            return None
        try:
            if self.db.nodes[relative]["ifc_class"] == "IfcAxis2PlacementLinear":
                from ..linear import point_frame

                location = self.first(relative, "Location")
                origin = (
                    tuple(row[3] for row in point_frame(self, location)[:3]) if location else None
                )
            elif self.db.nodes[relative]["ifc_class"] in (
                "IfcAxis2Placement2D",
                "IfcAxis2Placement3D",
            ):
                origin = self.vector(relative, "Location")
            else:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        if origin is None:
            return None
        parent = self.first(iid, "PlacementRelTo")
        if parent is None:
            return origin if node.get("PlacementRelTo") is None else None
        matrix = self.placement(parent)
        if matrix is None:
            return None
        result = tuple(
            sum(matrix[i][k] * origin[k] for k in range(3)) + matrix[i][3] for i in range(3)
        )
        return result if all(math.isfinite(x) for x in result) else None

    def placement_fields(self, iid):
        result = {"Placement_Type": self.db.nodes[iid]["ifc_class"]}
        if result["Placement_Type"] == "IfcLinearPlacement":
            relative = self.first(iid, "RelativePlacement")
            location = self.first(relative, "Location") if relative else None
            if location:
                node = self.db.nodes[location]
                for field in (
                    "DistanceAlong",
                    "OffsetLateral",
                    "OffsetVertical",
                    "OffsetLongitudinal",
                ):
                    value = node.get(field)
                    if value is not None:
                        result["Placement_" + field] = unpack(value)
                        raw = (
                            json.loads(value)
                            if isinstance(value, str) and value.startswith("{")
                            else value
                        )
                        if (
                            field != "DistanceAlong"
                            or isinstance(raw, dict)
                            and raw.get("type") == "IfcLengthMeasure"
                        ):
                            unit = self.units.for_value("IfcLengthMeasure")
                            if unit:
                                result["Placement_" + field + "_Unit"] = unit
                curve = self.first(location, "BasisCurve")
                if curve:
                    result["Placement_BasisCurveType"] = self.db.nodes[curve]["ifc_class"]
        matrix = self.placement(iid)
        if matrix is None:
            origin = self.placement_origin(iid)
            if origin is None:
                return result
            result.update({"Placement_" + axis: origin[index] for index, axis in enumerate("XYZ")})
            unit = self.units.for_value("IfcLengthMeasure")
            if unit:
                result["Placement_Unit"] = unit
            return result
        for index, axis in enumerate("XYZ"):
            result["Placement_" + axis] = matrix[index][3]
            for column, name in enumerate(("XAxis", "YAxis", "ZAxis")):
                result[f"Placement_{name}_{axis}"] = matrix[index][column]
        unit = self.units.for_value("IfcLengthMeasure")
        if unit:
            result["Placement_Unit"] = unit
        return result
