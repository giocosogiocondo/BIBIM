from __future__ import annotations

from collections import Counter
from contextlib import closing
import re
import sqlite3
import time
from pathlib import Path

from experiments.view_selection_ablation.common import check_pin

TOKEN = re.compile(
    r"--[^\r\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[^\s]",
    re.S,
)


def validate_statement(sql):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("No SQL query")
    tokens = []
    for match in TOKEN.finditer(sql):
        token = match.group()
        if token.startswith(("--", "/*", "'")):
            continue
        if token.startswith(('"', "`", "[")):
            value = token[1:-1].replace(token[0] * 2, token[0])
            if value.lower().startswith("_sv_"):
                raise ValueError("Raw backing tables cannot be explicit SQL identifiers")
            tokens.append((value, True))
        else:
            if token.lower().startswith("_sv_"):
                raise ValueError("Raw backing tables cannot be explicit SQL identifiers")
            tokens.append((token, False))
    if not tokens or tokens[0][1] or tokens[0][0].upper() not in ("SELECT", "WITH"):
        raise ValueError("Only a SELECT/WITH query is allowed")
    semicolons = [i for i, t in enumerate(tokens) if t == (";", False)]
    if semicolons and semicolons != [len(tokens) - 1]:
        raise ValueError("Only one SQL statement is allowed")


def execute(path_pin, allowed_views, sql, *, timeout=30, max_rows=2000):
    """Only selected public Views can expose data; the physical database stays read-only."""
    started = time.perf_counter()
    result = {
        "columns": [],
        "rows": [],
        "error": None,
        "truncated": False,
        "row_count": None,
        "rows_saved": 0,
        "actual_reads": [],
    }
    reads = set()
    allowed = {name.casefold(): name for name in allowed_views}
    backings = {
        "_sv_" + name.removeprefix("View_").casefold(): name.casefold() for name in allowed_views
    }
    try:
        validate_statement(sql)
        check_pin(path_pin)
        with closing(
            sqlite3.connect(Path(path_pin["path"]).resolve().as_uri() + "?mode=ro", uri=True)
        ) as con:
            con.execute("PRAGMA query_only=ON")
            con.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)

            def authorize(action, first, second, database, context):
                if action == sqlite3.SQLITE_READ:
                    reads.add((database, first, context))
                    if database not in (None, "main"):
                        return sqlite3.SQLITE_DENY
                    if database is None:  # SQLite's generated CTE COUNT(*) authorization.
                        return sqlite3.SQLITE_OK
                    table, origin = first.casefold(), (context or "").casefold()
                    if table in allowed or table in ("json_each", "json_tree"):
                        return sqlite3.SQLITE_OK
                    if table in backings and (
                        origin == backings[table] or (not second and not context)
                    ):
                        return sqlite3.SQLITE_OK
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_FUNCTION:
                    return (
                        sqlite3.SQLITE_DENY
                        if (second or "").lower() in ("load_extension", "readfile", "writefile")
                        else sqlite3.SQLITE_OK
                    )
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            con.set_authorizer(authorize)
            deadline = time.monotonic() + timeout
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            cursor = con.execute(sql)
            result["columns"] = [column[0] for column in cursor.description]
            fetched = cursor.fetchmany(max_rows + 1)
            result["rows"] = [list(row) for row in fetched[:max_rows]]
            result["truncated"] = len(fetched) > max_rows
            result["rows_saved"] = len(result["rows"])
            result["row_count"] = None if result["truncated"] else len(result["rows"])
    except (OSError, ValueError, sqlite3.Error) as exc:
        result["error"] = str(exc)
    finally:
        result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        result["actual_reads"] = sorted([list(r) for r in reads], key=str)
    return result


def result_agreement(generated, reference):
    """Strict unordered row-multiset agreement, preserving column order and duplicates."""
    if reference["error"] or reference["truncated"]:
        return None
    if generated is None or generated["error"] or generated["truncated"]:
        return False
    if len(generated["columns"]) != len(reference["columns"]):
        return False
    return Counter(tuple(r) for r in generated["rows"]) == Counter(
        tuple(r) for r in reference["rows"]
    )
