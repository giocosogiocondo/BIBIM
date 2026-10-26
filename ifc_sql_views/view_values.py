"""Remove internal reference keys from interpreted public values."""


def without_references(value):
    """Remove generated reference keys inside an interpreted resource value."""
    if isinstance(value, dict):
        return {
            k: without_references(v)
            for k, v in value.items()
            if k != "ifc_id" and not k.endswith("_id") and not k.startswith("_")
        }
    if isinstance(value, (tuple, list)):
        return [without_references(v) for v in value]
    return value
