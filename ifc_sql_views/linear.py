"""Native-unit linear referencing for explicit line/polyline/circle curves.

Other curve representations remain in raw tables and are diagnosed, never
approximated by tessellation. The caller handles parent placement transforms.
"""

import json
import math
from .reader import unpack


def _distance(node):
    raw = node.get("DistanceAlong")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or raw.get("type") not in (
        "IfcLengthMeasure",
        "IfcParameterValue",
    ):
        raise ValueError("DistanceAlong requires a retained typed measure")
    distance = float(unpack(raw))
    if not math.isfinite(distance):
        raise ValueError("Nonfinite distance")
    return distance, raw["type"]


def _sample_line(semantic, curve_id, distance, measure):
    from .geometry_math import normalized

    db = semantic.db
    origin = semantic.vector(curve_id, "Pnt")
    direction_id = semantic.first(curve_id, "Dir")
    if origin is None or direction_id is None:
        raise ValueError("Incomplete line")
    tangent = normalized(semantic.vector(direction_id, "Orientation"))
    if measure == "IfcParameterValue":
        magnitude = float(db.nodes[direction_id]["Magnitude"])
        if not math.isfinite(magnitude) or magnitude <= 0:
            raise ValueError("Invalid line magnitude")
        distance *= magnitude
    return tuple(origin[k] + distance * tangent[k] for k in range(3)), tangent


def _sample_polyline(semantic, curve_id, distance, measure):
    from .geometry_math import normalized

    if measure != "IfcLengthMeasure":
        raise ValueError("Polyline parameter convention is not implemented")
    points = []
    for pid in semantic.db.refs(curve_id, "Points"):
        value = unpack(semantic.db.nodes[pid].get("Coordinates"))
        if not isinstance(value, list) or len(value) not in (2, 3):
            raise ValueError("Invalid polyline point")
        points.append(tuple(float(v) for v in value) + ((0.0,) if len(value) == 2 else ()))
    if distance < 0 or len(points) < 2:
        raise ValueError("Distance outside polyline")
    remaining = distance
    for a, b in zip(points, points[1:]):
        delta = tuple(b[k] - a[k] for k in range(3))
        size = math.sqrt(sum(v * v for v in delta))
        if size < 1e-12:
            continue
        if remaining <= size + 1e-9:
            tangent = normalized(delta)
            return tuple(a[k] + remaining * tangent[k] for k in range(3)), tangent
        remaining -= size
    raise ValueError("Distance outside polyline")


def _sample_circle(semantic, curve_id, distance, measure, iid):
    from .geometry_math import normalized

    radius = float(semantic.db.nodes[curve_id]["Radius"])
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("Invalid circle radius")
    angle = distance / radius if measure == "IfcLengthMeasure" else distance
    position = semantic.first(curve_id, "Position")
    if position is None:
        raise ValueError("Missing circle position")
    matrix = semantic.axis_matrix(position)
    if abs(matrix[0][2]) > 1e-9 or abs(matrix[1][2]) > 1e-9 or matrix[2][2] < 1.0 - 1e-9:
        semantic.db.note("unsupported_linear_curve_orientation", iid, curve_class="IfcCircle")
        raise ValueError("Tilted circle lateral/vertical conventions are not implemented")
    local = (radius * math.cos(angle), radius * math.sin(angle), 0.0)
    origin = tuple(sum(matrix[i][k] * local[k] for k in range(3)) + matrix[i][3] for i in range(3))
    local_tangent = (-math.sin(angle), math.cos(angle), 0.0)
    tangent = normalized(
        tuple(sum(matrix[i][k] * local_tangent[k] for k in range(3)) for i in range(3))
    )
    return origin, tangent


def point_frame(semantic, iid):
    from .geometry_math import normalized, cross

    node = semantic.db.nodes[iid]
    if node["ifc_class"] != "IfcPointByDistanceExpression":
        raise ValueError("Linear location must be IfcPointByDistanceExpression")
    distance, measure = _distance(node)
    curve_id = semantic.first(iid, "BasisCurve")
    if curve_id is None:
        raise ValueError("Missing basis curve")
    cls = semantic.db.nodes[curve_id]["ifc_class"]
    if cls == "IfcLine":
        origin, tangent = _sample_line(semantic, curve_id, distance, measure)
    elif cls == "IfcPolyline":
        origin, tangent = _sample_polyline(semantic, curve_id, distance, measure)
    elif cls == "IfcCircle":
        origin, tangent = _sample_circle(semantic, curve_id, distance, measure, iid)
    else:
        semantic.db.note("unsupported_linear_curve", iid, curve_class=cls)
        raise ValueError("Unsupported basis curve: " + cls)
    # Positive lateral is left of tangent; vertical stays normal to tangent in
    # the tangent/global-Z plane. A vertical tangent cannot define that frame.
    left = normalized(cross((0.0, 0.0, 1.0), tangent))
    up = normalized(cross(tangent, left))
    offsets = [
        float(node.get(a) or 0.0)
        for a in ("OffsetLongitudinal", "OffsetLateral", "OffsetVertical")
    ]
    if not all(math.isfinite(v) for v in (*origin, *offsets)):
        raise ValueError("Nonfinite linear location")
    origin = tuple(
        origin[k] + offsets[0] * tangent[k] + offsets[1] * left[k] + offsets[2] * up[k]
        for k in range(3)
    )
    return tuple((tangent[i], left[i], up[i], origin[i]) for i in range(3)) + (
        (0.0, 0.0, 0.0, 1.0),
    )


def axis_matrix(semantic, iid):
    from .geometry_math import orthogonal_axes, multiply

    if semantic.db.nodes[iid]["ifc_class"] != "IfcAxis2PlacementLinear":
        raise ValueError("Invalid linear relative placement")
    location = semantic.first(iid, "Location")
    if location is None:
        raise ValueError("Missing linear location")
    base = point_frame(semantic, location)
    x, y, z = orthogonal_axes(
        semantic.vector(iid, "Axis", (0.0, 0.0, 1.0)),
        semantic.vector(iid, "RefDirection", (1.0, 0.0, 0.0)),
    )
    rotation = tuple((x[i], y[i], z[i], 0.0) for i in range(3)) + ((0.0, 0.0, 0.0, 1.0),)
    return multiply(base, rotation)
