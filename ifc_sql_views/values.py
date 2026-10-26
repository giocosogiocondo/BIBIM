"""Converter-side value deduplication and retained IFC measure tags."""

import json


def unique(values):
    values = list(values)
    # A singleton cannot contain a duplicate. Large type/material JSON payloads
    # used to be serialized here only to deduplicate a list of one item.
    if len(values) < 2:
        return values
    out, seen = [], set()
    for v in values:
        key = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def collapse(values):
    values = unique(values)
    return values[0] if len(values) == 1 else values or None


def wrapped_measure(raw):
    """Use retained IFC tags; plain numeric values do not identify a measure."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, dict):
        return raw.get("type")
    if isinstance(raw, list):
        types = {wrapped_measure(v) for v in raw}
        return next(iter(types)) if len(types) == 1 else None
    return None
