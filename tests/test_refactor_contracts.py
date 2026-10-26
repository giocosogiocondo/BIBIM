"""Architectural and equivalence safeguards required by this refactoring."""

import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ifc_sql_views.build import Writer, build
from ifc_sql_views.import_ifc import convert, raw_database
from ifc_sql_views.extractor import Extractor, unique
from ifc_sql_views.semantic import Semantic
from compare_refactor_outputs import compare_databases


class RefactorContractsTest(unittest.TestCase):
    def test_public_facades_keep_entrypoints_and_cache_api(self):
        for function in (build, convert, raw_database, unique):
            self.assertTrue(callable(function))
        self.assertTrue(hasattr(Writer, "write"))
        for cls, names in [
            (Extractor, ["effective_properties", "resource", "row_with_provenance", "relations"]),
            (Semantic, ["object_attributes", "rows", "placement", "resolved_resource"]),
        ]:
            self.assertTrue(all(callable(getattr(cls, name)) for name in names))
        self.assertTrue(hasattr(Semantic.placement, "cache_clear"))
        self.assertTrue(hasattr(Extractor._root_resource, "cache_info"))

    def test_independent_validation_does_not_import_converter(self):
        folder = Path(__file__).resolve().parents[1] / "bibim_validation"
        for file in folder.glob("*.py"):
            tree = ast.parse(file.read_text(encoding="utf8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith("ifc_sql_views"), file)
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(n.name.startswith("ifc_sql_views") for n in node.names), file
                    )

    def test_equivalence_checker_detects_corrupted_value(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("before.sqlite", "after.sqlite")]
            for path in paths:
                with closing(sqlite3.connect(path)) as con:
                    con.execute("CREATE TABLE data (ifc_id INTEGER, Name TEXT)")
                    con.execute("INSERT INTO data VALUES (1,'Correct')")
                    con.execute("CREATE VIEW View_Beam AS SELECT * FROM data")
                    con.commit()
            self.assertTrue(compare_databases(*paths)["passed"])
            with closing(sqlite3.connect(paths[1])) as con:
                con.execute("UPDATE data SET Name='Wrong'")
                con.commit()
            with self.assertRaises(AssertionError):
                compare_databases(*paths)
