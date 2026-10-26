"""SQLite writing, type affinity and raw-table preservation; no IFC traversal."""

import json
import math
import sqlite3
from .reader import quote
from .column_policy import compact_entity_columns
from .file_header import TABLE as FILE_HEADER_TABLE


def validate_columns(name, columns, limit):
    """SQLite identifiers are case-insensitive; never silently overwrite a field."""
    case_names = {}
    for column in columns:
        folded = column.casefold()
        if folded in case_names and case_names[folded] != column:
            raise ValueError(
                f"SQLite column name collision in {name}: {column} / {case_names[folded]}"
            )
        case_names[folded] = column
    if len(columns) > limit:
        raise ValueError(
            f"{name} needs {len(columns)} columns, exceeding SQLite's {limit}. No columns were discarded."
        )


def infer_sql_type(actual, hint="TEXT"):
    """Mixed numeric/text cells must retain BLOB affinity and labels like '007'."""
    if actual and all(isinstance(value, (bool, int)) for value in actual):
        return "INTEGER"
    if actual and all(isinstance(value, (bool, int, float)) for value in actual):
        return "REAL"
    if actual and any(isinstance(value, (bool, int, float)) for value in actual):
        return "BLOB"
    return hint if not actual else "TEXT"


def sql_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite IFC value cannot be published without data loss")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if isinstance(value, bool):
        return int(value)
    return value


class Writer:
    def __init__(self, path):
        self.con = sqlite3.connect(path, uri=True)
        self.views = {}

    def write(self, name, rows, required=(), hints=None, *, matrix=False):
        columns = dict.fromkeys(required)
        for row in rows:
            for key in row:
                columns.setdefault(key, None)
        if not columns:
            return
        limit = self.con.getlimit(sqlite3.SQLITE_LIMIT_COLUMN)
        policy = None
        if matrix:
            columns, rows, policy = compact_entity_columns(
                rows, columns, limit=limit, protected=required
            )
        validate_columns(name, columns, limit)
        types = {}
        for col in columns:
            actual = [row[col] for row in rows if row.get(col) is not None]
            types[col] = infer_sql_type(actual, (hints or {}).get(col, "TEXT"))
        table = "_sv_" + name.removeprefix("View_")
        ddl = ",".join(quote(col) + " " + types[col] for col in columns)
        self.con.execute(f"CREATE TABLE {quote(table)} ({ddl})")
        if rows:
            marks = ",".join("?" for _ in columns)
            self.con.executemany(
                f"INSERT INTO {quote(table)} VALUES ({marks})",
                (tuple(sql_value(row.get(c)) for c in columns) for row in rows),
            )
        if "ifc_id" in columns:
            self.con.execute(
                f"CREATE UNIQUE INDEX {quote(table + '_id')} ON {quote(table)} (ifc_id)"
            )
        elif "element_id" in columns:
            self.con.execute(
                f"CREATE INDEX {quote(table + '_element')} ON {quote(table)} (element_id)"
            )
        public = list(columns)
        selection = ",".join(quote(col) for col in public)
        self.con.execute(f"CREATE VIEW {quote(name)} AS SELECT {selection} FROM {quote(table)}")
        self.views[name] = {
            "rows": len(rows),
            "columns": public,
            "sql_types": {col: types[col] for col in public},
        }
        if policy is not None:
            self.views[name]["column_policy"] = policy

    def close(self):
        self.con.close()


def copy_raw_tables(writer, db):
    """Preserve the plain IFC database; only entity-pair views are public."""
    uri = db.path.as_uri() + "?mode=ro"
    writer.con.execute("ATTACH DATABASE ? AS raw_source", (uri,))
    names = sorted((db.tables & db.entities.keys()) | (db.tables & {"metadata", "id_map", FILE_HEADER_TABLE}))
    for name in names:
        ddl = writer.con.execute(
            "SELECT sql FROM raw_source.sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()[0]
        writer.con.execute(ddl)
        writer.con.execute(
            f"INSERT INTO main.{quote(name)} SELECT * FROM raw_source.{quote(name)}"
        )
    if "id_map" not in db.tables:
        writer.con.execute("CREATE TABLE id_map (ifc_id INTEGER PRIMARY KEY, ifc_class TEXT)")
        writer.con.executemany(
            "INSERT INTO id_map VALUES (?,?)",
            ((iid, node["ifc_class"]) for iid, node in db.nodes.items()),
        )
    writer.con.commit()
    writer.con.execute("DETACH DATABASE raw_source")
