import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import test_entity_validator as fixtures
from validate_all_entity_views import discover, run, source_ifc


class BatchEntityValidatorTest(unittest.TestCase):
    def test_batch_continues_and_reports_good_bad_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "databases"
            helper = fixtures.IndependentEntityValidatorTest()
            for name in ("good", "bad"):
                folder = root / name
                folder.mkdir(parents=True)
                source, db, iid = helper.make(folder, "IFC4")
                if name == "bad":
                    with closing(sqlite3.connect(db)) as con:
                        con.execute("UPDATE _sv_Beam SET Name=? WHERE ifc_id=?", ("WRONG", iid))
                        con.commit()
            legacy = root / "legacy.sqlite"
            with closing(sqlite3.connect(legacy)) as con:
                con.execute("CREATE TABLE example (x)")
                con.commit()
            legacy.with_suffix(".build.json").write_text(
                json.dumps(dict(layout="relational", input=str(root / "good/model.ifc"))),
                encoding="utf8",
            )
            self.assertEqual(len(discover(root)), 3)
            code = run(root, base / "result")
            self.assertEqual(code, 1)
            summary = json.loads((base / "result/batch_summary.json").read_text(encoding="utf8"))
            self.assertEqual(summary["databases"], 3)
            self.assertEqual(summary["statuses"].get("passed_with_unverified"), 1)
            self.assertEqual(summary["statuses"].get("differences"), 1)
            self.assertEqual(summary["statuses"].get("unsupported_layout"), 1)

    def test_source_manifest_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "test.ifc"
            source.write_text("fixture", encoding="utf8")
            first, second = root / "raw.sqlite", root / "preview.sqlite"
            first.with_suffix(".build.json").write_text(
                json.dumps(dict(input=str(source))), encoding="utf8"
            )
            second.with_suffix(".build.json").write_text(
                json.dumps(dict(input=str(first))), encoding="utf8"
            )
            self.assertEqual(source_ifc(second)[0], source.resolve())
