"""Read-only table/view equivalence checks against the pre-refactor release.

Only the import preprocessor version is normalized in raw metadata. Public
values, names, affinity, IDs and array order must agree. JSON object key order
does not change SQL JSON retrieval and is compared structurally.
"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def equivalent_cell(left, right, allow_json):
    if left == right:
        return True
    if allow_json and isinstance(left, str) and isinstance(right, str):
        try:
            a, b = json.loads(left), json.loads(right)
        except ValueError:
            return False
        if isinstance(a, (dict, list)) and isinstance(b, type(a)):
            return a == b
    return False


def compare_databases(baseline, candidate):
    with (
        closing(sqlite3.connect(Path(baseline).resolve().as_uri() + "?mode=ro", uri=True)) as old,
        closing(sqlite3.connect(Path(candidate).resolve().as_uri() + "?mode=ro", uri=True)) as new,
    ):
        old_objects = dict(
            old.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','view')")
        )
        new_objects = dict(
            new.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','view')")
        )
        assert old_objects == new_objects, "Changed tables/views"
        checked_rows = checked_cells = 0
        reordered = []
        for name, kind in sorted(old_objects.items()):
            before = list(old.execute("PRAGMA table_info(" + quote(name) + ")"))
            after = list(new.execute("PRAGMA table_info(" + quote(name) + ")"))
            before_types = {r[1]: tuple(r[2:]) for r in before}
            after_types = {r[1]: tuple(r[2:]) for r in after}
            assert before_types == after_types, ("Changed column names/types", name)
            if [r[1] for r in before] != [r[1] for r in after]:
                reordered.append(name)
            columns = [r[1] for r in before]
            projection = ",".join(quote(c) for c in columns)
            order = " ORDER BY ifc_id" if "ifc_id" in columns else " ORDER BY rowid"
            a = old.execute("SELECT " + projection + " FROM " + quote(name) + order)
            b = new.execute("SELECT " + projection + " FROM " + quote(name) + order)
            while True:
                left, right = a.fetchmany(512), b.fetchmany(512)
                assert len(left) == len(right), ("Changed row count", name)
                if not left:
                    break
                for x, y in zip(left, right):
                    for column, lval, rval in zip(columns, x, y):
                        if name == "metadata" and column == "preprocessor":
                            lval = lval.replace("BIBIM-3.4.3", "BIBIM-VERSION")
                            rval = rval.replace("BIBIM-3.4.4", "BIBIM-VERSION").replace("BIBIM-3.4.5", "BIBIM-VERSION")
                        assert equivalent_cell(
                            lval, rval, kind == "view" or name.startswith("_sv_")
                        ), (name, column, lval, rval)
                    checked_rows += 1
                    checked_cells += len(columns)
        return dict(
            passed=True,
            tables=sum(k == "table" for k in old_objects.values()),
            views=sum(k == "view" for k in old_objects.values()),
            rows_checked=checked_rows,
            cells_checked=checked_cells,
            reordered_columns=reordered,
        )
