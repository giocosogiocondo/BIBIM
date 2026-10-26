from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

import ifcopenshell
from ifcopenshell import ifcopenshell_wrapper as W
from .schemas import schema_name
from .file_header import read_header, project_fields


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def unpack(value: Any) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value

    def unwrap(item):
        if isinstance(item, dict) and "type" in item and "value" in item:
            return unwrap(item["value"])
        if isinstance(item, list):
            return [unwrap(v) for v in item]
        return item

    # Decode the serialized cell once. An IfcLabel containing 'true' or '7'
    # remains text after its typed wrapper is removed.
    return unwrap(value)


def integers(value: Any):
    value = unpack(value)
    if isinstance(value, int) and not isinstance(value, bool):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from integers(item)


class Database:
    """Read raw Ifc2Sql tables; existing views and flattened helper tables are ignored."""

    def __init__(self, path: Path, schema_override: str | None = None):
        self.path = Path(path).resolve()
        self.con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.diagnostics: list[dict] = []
        self._diagnostic_keys = set()
        self.tables = {
            r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = schema_override
        self.source_schema = None
        metadata = {}
        if "metadata" in self.tables:
            row = self.con.execute('SELECT * FROM metadata LIMIT 1').fetchone()
            metadata = dict(row) if row else {}
            self.source_schema = metadata.get("schema")
            version = version or self.source_schema
        self.file_header = read_header(self.con, self.tables)
        self.source_fields = project_fields(self.file_header, self.source_schema, metadata.get("mvd"))
        self.version = schema_name(version)
        self.schema = ifcopenshell.schema_by_name(self.version)
        self.entities = {
            d.name(): d for d in self.schema.declarations() if isinstance(d, W.entity)
        }
        self.nodes: dict[int, dict] = {}
        self.by_class: dict[str, list[int]] = defaultdict(list)
        self.edges: dict[int, dict[str, list[int]]] = defaultdict(dict)
        self.incoming: dict[int, list[tuple[int, str]]] = defaultdict(list)
        seen_edges = set()
        for name in sorted(self.tables & self.entities.keys()):
            for row in self.con.execute(f"SELECT * FROM {quote(name)}"):
                record = dict(row)
                if "ifc_id" not in record:
                    raise ValueError(f"{name} has no ifc_id column; unsupported DB layout")
                iid = int(record["ifc_id"])
                if iid in self.nodes:
                    raise ValueError(
                        f"Repeated STEP ID {iid}; expanded Ifc2Sql rows are not supported"
                    )
                self.nodes[iid] = {"ifc_id": iid, "ifc_class": name, **record}
                self.by_class[name].append(iid)
        if not self.nodes:
            raise ValueError("No raw IFC entity rows found")
        # A reference index derived from declared forward attributes also works if
        # inverses were omitted or contain stale/incorrect IDs in the source DB.
        for iid, row in self.nodes.items():
            for attr, allowed, mixed in self.reference_attributes(row["ifc_class"]):
                raw = row.get(attr)
                if raw is None:
                    continue
                targets = list(integers(raw))
                if mixed:
                    targets = self.mixed_references(raw, iid, attr, allowed)
                refs = []
                for target in targets:
                    if target not in self.nodes:
                        self.note("missing_reference", iid, attr, target=target)
                        continue
                    target_class = self.nodes[target]["ifc_class"]
                    if not any(self.is_a(target_class, cl) for cl in allowed):
                        self.note("reference_type_mismatch", iid, attr, target=target)
                        continue
                    refs.append(target)
                    edge = (iid, attr)
                    if (target, *edge) not in seen_edges:
                        self.incoming[target].append(edge)
                        seen_edges.add((target, *edge))
                self.edges[iid][attr] = refs

    def mixed_references(self, raw, iid, attr, allowed):
        # SQLite affinity can erase the distinction between a typed numeric value
        # and a reference in an EXPRESS SELECT. Require an explicit entity tag;
        # never decide solely because a same-numbered STEP instance exists.
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return []
        refs, ambiguous = [], []

        def visit(value):
            if isinstance(value, dict) and "type" in value:
                if value["type"] in self.entities:
                    refs.extend(integers(value.get("value")))
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, int) and not isinstance(value, bool):
                if value in self.nodes and any(
                    self.is_a(self.nodes[value]["ifc_class"], c) for c in allowed
                ):
                    ambiguous.append(value)

        visit(raw)
        if ambiguous:
            self.note("ambiguous_select", iid, attr, candidates=ambiguous)
        return refs

    def close(self):
        self.con.close()

    def note(self, code, iid, attribute=None, **details):
        item = {"code": code, "ifc_id": iid, **details}
        if attribute:
            item["attribute"] = attribute
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in self._diagnostic_keys:
            self._diagnostic_keys.add(key)
            self.diagnostics.append(item)

    @lru_cache(maxsize=None)
    def is_a(self, cls: str, base: str) -> bool:
        declaration = self.entities.get(cls)
        while declaration:
            if declaration.name() == base:
                return True
            declaration = declaration.supertype()
        return False

    @lru_cache(maxsize=None)
    def attributes(self, cls: str):
        return self.entities[cls].all_attributes()

    @lru_cache(maxsize=None)
    def reference_attributes(self, cls: str):
        result = []
        for attr in self.attributes(cls):
            text = str(attr.type_of_attribute())
            allowed = tuple(dict.fromkeys(re.findall(r"<entity (\w+)", text)))
            if allowed:
                mixed = "<select " in text and bool(
                    re.search(r"<(?:real|integer|number|string|boolean|logical)>", text)
                )
                result.append((attr.name(), allowed, mixed))
        return tuple(result)

    @lru_cache(maxsize=None)
    def inverse_name(self, target_class, source_class, attribute):
        names = []
        for inv in self.entities[target_class].all_inverse_attributes():
            if inv.attribute_reference().name() == attribute and self.is_a(
                source_class, inv.entity_reference().name()
            ):
                names.append(inv.name())
        return names[0] if names else f"{source_class}_{attribute}"

    def refs(self, iid: int, attr: str) -> list[int]:
        return self.edges.get(iid, {}).get(attr, [])

    def scalar_attributes(self, iid: int) -> dict:
        node = self.nodes[iid]
        ref_names = {x[0]: x[2] for x in self.reference_attributes(node["ifc_class"])}
        out = {"ifc_id": iid, "ifc_class": node["ifc_class"]}
        for a in self.attributes(node["ifc_class"]):
            name = a.name()
            value = node.get(name)
            if name in ref_names:
                if ref_names[name]:
                    out[name] = unpack(value)
                    out[name + "_id"] = self.refs(iid, name) or None
                else:
                    out[name + "_id"] = unpack(value)
            else:
                kind = str(a.type_of_attribute())
                # IFC strings that happen to look like numbers or JSON stay strings.
                out[name] = (
                    unpack(value)
                    if any(t in kind for t in ("<select", "<list", "<set", "<array", "<bag"))
                    else value
                )
        return out

    def ids(self, base: str):
        return sorted(
            iid for cls, ids in self.by_class.items() if self.is_a(cls, base) for iid in ids
        )
