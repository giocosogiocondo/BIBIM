"""Independent source value semantics; deliberately separate from converter logic."""

from __future__ import annotations
import json
import math
import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.util.unit


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
            "KELVIN": "K",
            "DEGREE_CELSIUS": "degC",
            "NEWTON": "N",
            "PASCAL": "Pa",
            "JOULE": "J",
            "WATT": "W",
            "AMPERE": "A",
            "VOLT": "V",
            "HERTZ": "Hz",
        }
        return ifcopenshell.util.unit.prefix_symbols.get(u.Prefix, u.Prefix or "") + symbols.get(
            u.Name, u.Name
        )
    if u.is_a("IfcDerivedUnit"):
        return "*".join(f"{unit_label(e.Unit)}^{e.Exponent}" for e in u.Elements)
    return getattr(u, "Name", None) or getattr(u, "Currency", None)
