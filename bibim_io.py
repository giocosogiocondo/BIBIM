"""Consistent artifact encoding; no IFC interpretation or validation semantics."""

import json
from pathlib import Path


def write_json(path, data, *, allow_nan=True):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=allow_nan),
        encoding="utf-8",
    )
