"""Serialize original IFC instances into typed raw SQLite tables."""

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
import ifcopenshell
from bibim_config import IMPORT_PREPROCESSOR
from .reader import quote
from .schemas import schema_name
from .file_header import store_header


def serialise(value):
    if isinstance(value, ifcopenshell.entity_instance):
        # Entity tags disambiguate EXPRESS SELECT references from numeric values.
        return {
            "type": value.is_a(),
            "value": value.id() if value.id() else serialise(value.wrappedValue),
        }
    if isinstance(value, (tuple, list)):
        return [serialise(item) for item in value]
    return value


def cell(value):
    encoded = serialise(value)
    return (
        json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))
        if isinstance(encoded, (dict, list))
        else encoded
    )


def raw_database(model, destination):
    groups = defaultdict(list)
    for entity in model:
        groups[entity.is_a()].append(entity)
    with closing(sqlite3.connect(destination)) as con:
        con.execute("CREATE TABLE metadata (preprocessor TEXT, schema TEXT, mvd TEXT)")
        con.execute(
            "INSERT INTO metadata VALUES (?, ?, ?)",
            (
                IMPORT_PREPROCESSOR,
                schema_name(model.schema_identifier),
                json.dumps(model.header.file_description.description),
            ),
        )
        store_header(con, model)
        con.execute("CREATE TABLE id_map (ifc_id INTEGER PRIMARY KEY, ifc_class TEXT)")
        for cls, entities in sorted(groups.items()):
            columns = [entities[0].attribute_name(i) for i in range(len(entities[0]))]
            # No affinity coercion: mixed SELECT cells retain JSON tags and values.
            ddl = ", ".join(quote(name) + " BLOB" for name in columns)
            con.execute(
                f"CREATE TABLE {quote(cls)} (ifc_id INTEGER PRIMARY KEY, {ddl}, inverses TEXT)"
            )
            marks = ",".join("?" for _ in range(len(columns) + 2))
            con.executemany(
                f"INSERT INTO {quote(cls)} VALUES ({marks})",
                (
                    [
                        entity.id(),
                        *[cell(v) for v in entity],
                        json.dumps(sorted(x.id() for x in model.get_inverse(entity))),
                    ]
                    for entity in entities
                ),
            )
            con.executemany("INSERT INTO id_map VALUES (?, ?)", ((e.id(), cls) for e in entities))
        con.commit()
    return sum(map(len, groups.values()))
