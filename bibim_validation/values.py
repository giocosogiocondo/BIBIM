"""Independent source value semantics; deliberately separate from converter logic."""

from __future__ import annotations
import json
import math
from pathlib import Path
import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.util.unit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "outputs"
IDENTITY_4X4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def write_json(path, data, *, allow_nan=True):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=allow_nan),
        encoding="utf-8",
    )
    return target


def matrix_is_finite(matrix) -> bool:
    try:
        return all(math.isfinite(float(cell)) for row in matrix for cell in row)
    except (TypeError, ValueError):
        return False


def transform_point(matrix, coordinates) -> tuple[float, float, float]:
    """Transform a 2D/3D point by a 4x4 homogeneous placement matrix without numpy."""
    coords = list(coordinates)
    if len(coords) == 2:
        coords.append(0.0)
    if len(coords) != 3:
        raise ValueError("Invalid point coordinate length")
    vec = [float(coords[0]), float(coords[1]), float(coords[2]), 1.0]
    if not matrix_is_finite(matrix) or not all(math.isfinite(x) for x in vec):
        raise ValueError("invalid parent frame or location")
    rows = [[float(cell) for cell in row] for row in matrix]
    xyz = tuple(sum(rows[i][j] * vec[j] for j in range(4)) for i in range(3))
    if not all(math.isfinite(x) for x in xyz):
        raise ValueError("invalid parent frame or location")
    return xyz


def quote(s):
    return '"' + s.replace('"', '""') + '"'


def empty(v):
    if v is None or isinstance(v, str) and not v.strip():
        return True
    if isinstance(v, (list, tuple, dict)):
        return all(empty(x) for x in (v.values() if isinstance(v, dict) else v))
    return False


def native(v):
    if isinstance(v, ifcopenshell.entity_instance):
        if v.id():
            raise ValueError("Entity reference requires a separate interpretation")
        return native(v.wrappedValue)
    if isinstance(v, (list, tuple)):
        return [native(x) for x in v]
    return v


def same(a, b, tolerance):
    # JSON containers are decoded only when a container is expected: '007' stays text.
    if isinstance(a, (list, dict)) and isinstance(b, str):
        try:
            b = json.loads(b)
        except ValueError:
            return False
    if isinstance(a, dict):
        return (
            isinstance(b, dict)
            and a.keys() == b.keys()
            and all(same(a[k], b[k], tolerance) for k in a)
        )
    if isinstance(a, list):
        return (
            isinstance(b, (list, tuple))
            and len(a) == len(b)
            and all(same(x, y, tolerance) for x, y in zip(a, b))
        )
    if isinstance(a, bool):
        return isinstance(b, (bool, int)) and b == int(a)
    if isinstance(a, (int, float)):
        return isinstance(b, (int, float)) and math.isclose(a, b, abs_tol=tolerance, rel_tol=1e-9)
    return type(a) is type(b) and a == b


def collapse(values):
    unique = []
    tokens = set()
    for value in values:
        token = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if token not in tokens:
            unique.append(value)
            tokens.add(token)
    return unique[0] if len(unique) == 1 else unique


def canonical(obj, schema):
    name = obj.is_a()
    if name.endswith(("StandardCase", "ElementedCase", "Varying")):
        parent = schema.declaration_by_name(name).supertype()
        if parent and not parent.is_abstract():
            return parent.name()
    return name


def unit_label(u):
    if u is None:
        return None
    if u.is_a("IfcSIUnit"):
        symbols = {
            "METRE": "m",
            "SQUARE_METRE": "m2",
            "CUBIC_METRE": "m3",
            "GRAM": "g",
            "SECOND": "s",
            "RADIAN": "rad",
            "STERADIAN": "sr",
            "KELVIN": "K",
            "DEGREE_CELSIUS": "degC",
            "NEWTON": "N",
            "PASCAL": "Pa",
            "JOULE": "J",
            "WATT": "W",
            "AMPERE": "A",
            "VOLT": "V",
            "HERTZ": "Hz",
            "OHM": "ohm",
            "FARAD": "F",
            "COULOMB": "C",
            "HENRY": "H",
            "SIEMENS": "S",
            "TESLA": "T",
            "WEBER": "Wb",
            "LUX": "lx",
            "LUMEN": "lm",
            "CANDELA": "cd",
            "BECQUEREL": "Bq",
            "GRAY": "Gy",
            "SIEVERT": "Sv",
            "MOLE": "mol",
        }
        return ifcopenshell.util.unit.prefix_symbols.get(u.Prefix, u.Prefix or "") + symbols.get(
            u.Name, u.Name
        )
    if u.is_a("IfcDerivedUnit"):
        return "*".join(f"{unit_label(e.Unit)}^{e.Exponent}" for e in u.Elements)
    return getattr(u, "Name", None) or getattr(u, "Currency", None)
