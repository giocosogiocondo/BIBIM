"""Data-driven columns for entity and entity CustomPset views."""

from __future__ import annotations

import hashlib

IDENTITY = ("ifc_id", "ifc_class", "GlobalId")


def value_groups(columns):
    """Move a measurement and its unit together, independent of input order."""
    columns = list(dict.fromkeys(columns))
    names, visited = set(columns), set()
    for col in columns:
        if col in visited:
            continue
        base = col[:-5] if col.endswith("_Unit") and col[:-5] in names else col
        group = [base] + ([base + "_Unit"] if base + "_Unit" in names else [])
        visited.update(group)
        yield base, group


def is_empty(value):
    """Empty text/containers are absent; zero and False are real IFC values."""
    if value is None or isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, dict):
        return all(is_empty(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(is_empty(v) for v in value)
    return False


def uniform_unit_label(value):
    """Return an exact, known label; mixed or partly unknown unit arrays stay separate."""
    if isinstance(value, str):
        return value if value.strip() and not any(ord(c) < 32 for c in value) else None
    if isinstance(value, (list, tuple)) and value:
        labels = [uniform_unit_label(item) for item in value]
        if labels[0] is not None and all(label == labels[0] for label in labels):
            return labels[0]
    return None


def fold_uniform_units(rows, columns):
    """Encode a uniform measurement unit in the name without changing its values."""
    columns = list(dict.fromkeys(columns))
    occupied = {name.casefold() for name in columns}
    folded = {}
    for base, group in value_groups(columns):
        if len(group) != 2 or base in IDENTITY or base == "etc":
            continue
        unit_column = group[1]
        label, present, valid = None, False, True
        for row in rows:
            value, unit = row.get(base), row.get(unit_column)
            known = uniform_unit_label(unit)
            if not is_empty(value):
                present = True
                if known is None:
                    valid = False
                    break
            if not is_empty(unit):
                if known is None or label is not None and known != label:
                    valid = False
                    break
                label = known
        if not valid or not present or label is None:
            continue
        desired = base + "_" + label
        if desired.casefold() in occupied:
            suffix = hashlib.sha256((base + "\0" + label).encode()).hexdigest()[:10]
            desired += "_" + suffix
            while desired.casefold() in occupied:
                desired += "_"
        occupied.add(desired.casefold())
        folded[base] = {"column": desired, "unit_column": unit_column, "unit": label}
    if not folded:
        return columns, rows, folded
    removed = {info["unit_column"] for info in folded.values()}
    final = [
        folded[key]["column"] if key in folded else key for key in columns if key not in removed
    ]
    renamed = [
        {
            folded[key]["column"] if key in folded else key: value
            for key, value in row.items()
            if key not in removed
        }
        for row in rows
    ]
    return final, renamed, folded


def compact_entity_columns(rows, columns, *, limit=2000, protected=()):
    """Fold uniform units, remove empty fields, pack <=10%/overflow into etc."""
    columns, rows, uniform_units = fold_uniform_units(rows, columns)
    identity = [c for c in IDENTITY if c in columns]
    if limit < len(identity) + 1:
        raise ValueError(
            "SQLite column limit cannot hold identity plus etc; no data was discarded."
        )
    counts = dict.fromkeys(columns, 0)
    for row in rows:
        for key, value in row.items():
            if key in counts and not is_empty(value):
                counts[key] += 1
    protected = set(identity) | {c for c in protected if counts.get(c, 0) > 0}
    kept, moved, empty = set(identity), set(), set()
    groups = []
    for base, group in value_groups(c for c in columns if c != "etc"):
        if any(c in protected for c in group):
            kept.update(group)
        elif counts[base] == 0:
            empty.update(group)
        elif counts[base] * 10 <= len(rows):
            moved.update(group)
        else:
            kept.update(group)
            groups.append((counts[base], base.casefold(), base, group))
    existing = "etc" in columns
    for _, _, _, group in sorted(groups):
        if len(kept) + int(existing or bool(moved)) <= limit:
            break
        kept.difference_update(group)
        moved.update(group)
    final = [c for c in columns if c in kept]
    if moved or existing:
        final.append("etc")
    if len(final) > limit:
        raise ValueError("Identity plus etc exceeds SQLite column limit; no data was discarded.")
    packed = []
    for row in rows:
        output = {key: value for key, value in row.items() if key in kept}
        if moved or existing:
            details = dict(row.get("etc") or {})
            for key, value in row.items():
                if key in moved and not is_empty(value):
                    if key in details and details[key] != value:
                        raise ValueError(f"etc key collision: {key}")
                    details[key] = value
            output["etc"] = details or None
        packed.append(output)
    return (
        final,
        packed,
        {
            "empty_columns": sorted(empty),
            "etc_columns": sorted(moved),
            "coverage_denominator": len(rows),
            "coverage_threshold": 0.1,
            "uniform_units": uniform_units,
        },
    )
