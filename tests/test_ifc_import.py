import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import ifcopenshell

from benchmark_tools import download, needed
from ifc_sql_views.import_ifc import convert


class IfcImportTest(unittest.TestCase):
    def fixture(self, folder, schema="IFC4"):
        model = ifcopenshell.file(schema=schema)
        unit = model.create_entity(
            "IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE"
        )
        assignment = model.create_entity("IfcUnitAssignment", Units=[unit])
        model.create_entity(
            "IfcProject", GlobalId=ifcopenshell.guid.new(), Name="Test", UnitsInContext=assignment
        )
        wall = model.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name="001")
        label = model.create_entity(
            "IfcPropertySingleValue",
            Name="Code",
            NominalValue=model.create_entity("IfcLabel", "007"),
        )
        length = model.create_entity(
            "IfcPropertySingleValue",
            Name="Width",
            NominalValue=model.create_entity("IfcLengthMeasure", 250.0),
        )
        pset = model.create_entity(
            "IfcPropertySet",
            GlobalId=ifcopenshell.guid.new(),
            Name="Company",
            HasProperties=[label, length],
        )
        model.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId=ifcopenshell.guid.new(),
            RelatedObjects=[wall],
            RelatingPropertyDefinition=pset,
        )
        material = model.create_entity("IfcMaterial", Name="Concrete")
        model.create_entity(
            "IfcRelAssociatesMaterial",
            GlobalId=ifcopenshell.guid.new(),
            RelatedObjects=[wall],
            RelatingMaterial=material,
        )
        point = model.create_entity("IfcCartesianPoint", Coordinates=[1.0, 2.0, 3.0])
        source = Path(folder) / "test.ifc"
        model.write(str(source))
        return source, wall.id(), label.id(), point.id(), len(list(model))

    def test_combined_db_preserves_entities_and_typed_properties(self):
        for schema in ("IFC2X3", "IFC4"):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as folder:
                source, wall, prop, point, count = self.fixture(folder, schema)
                before = hashlib.sha256(source.read_bytes()).hexdigest()
                output = Path(folder) / "result.sqlite"
                report = convert(source, output, log=lambda _: None)
                with closing(sqlite3.connect(output)) as con:
                    self.assertEqual(
                        con.execute("SELECT COUNT(*) FROM id_map").fetchone()[0], count
                    )
                    self.assertEqual(
                        con.execute("SELECT Name FROM IfcWall WHERE ifc_id=?", (wall,)).fetchone()[
                            0
                        ],
                        "001",
                    )
                    name = con.execute(
                        "SELECT Name FROM View_Wall WHERE ifc_id=?", (wall,)
                    ).fetchone()[0]
                    custom = con.execute(
                        "SELECT Company_Code,Company_Width_mm FROM View_WallCustomPset WHERE ifc_id=?",
                        (wall,),
                    ).fetchone()
                    self.assertEqual((name, *custom), ("001", "007", 250.0))
                    self.assertEqual(
                        report["views"]["View_WallCustomPset"]["column_policy"]["uniform_units"][
                            "Company_Width"
                        ]["unit"],
                        "mm",
                    )
                    raw = json.loads(
                        con.execute(
                            "SELECT NominalValue FROM IfcPropertySingleValue WHERE ifc_id=?",
                            (prop,),
                        ).fetchone()[0]
                    )
                    self.assertEqual(raw, {"type": "IfcLabel", "value": "007"})
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM IfcCartesianPoint WHERE ifc_id=?", (point,)
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(report["source_sha256"], before)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
                self.assertTrue(output.with_suffix(".build.json").exists())
                with self.assertRaises(FileExistsError):
                    convert(source, output, log=lambda _: None)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_lfs_pointer_and_corruption_do_not_publish_db(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "bad.ifc", Path(folder) / "result.sqlite"
            for content in (
                b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 42\n",
                b"ISO-10303-21;\x00",
            ):
                source.write_bytes(content)
                with self.assertRaises(ValueError):
                    convert(source, output, log=lambda _: None)
                self.assertFalse(output.exists())

    def test_download_selection(self):
        self.assertTrue(needed("projects/duplex/arc.ifc"))
        self.assertTrue(needed("questions/ifc-bench-v1.csv"))
        self.assertTrue(needed("projects/duplex/license.txt"))
        self.assertFalse(needed("projects/.DS_Store"))
        self.assertFalse(needed("projects/duplex/snapshot.png"))
        self.assertFalse(needed("projects/duplex/document/door.pdf"))
        self.assertTrue(needed("projects/duplex/document/door.pdf", documents=True))

    def test_lfs_download_checks_actual_content_and_reports_commit(self):
        content = b"ISO-10303-21;\nTEST\nEND-ISO-10303-21;"
        digest = hashlib.sha256(content).hexdigest()
        pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(content)}\n".encode()
        blob = hashlib.sha1(b"blob " + str(len(pointer)).encode() + b"\0" + pointer).hexdigest()
        tree = json.dumps(
            {
                "sha": "test-commit",
                "tree": [{"type": "blob", "path": "projects/duplex/arc.ifc", "sha": blob}],
            }
        ).encode()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("benchmark_tools.fetch", side_effect=[tree, pointer, content]),
        ):
            report = download(folder, log=lambda _: None, version="v1")
            self.assertEqual((Path(folder) / "projects/duplex/arc.ifc").read_bytes(), content)
            self.assertEqual(report["commit"], "test-commit")
            self.assertEqual(report["files"][0]["lfs_sha256"], digest)
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("benchmark_tools.fetch", side_effect=[tree, pointer, b"corrupted"]),
        ):
            with self.assertRaises(RuntimeError):
                download(folder, log=lambda _: None, version="v1")
            self.assertFalse((Path(folder) / "projects/duplex/arc.ifc").exists())

    def test_v2_stream_download_and_verified_reuse(self):
        content = b"ISO-10303-21; V2 fixture"
        digest = hashlib.sha256(content).hexdigest()
        entry = {
            "type": "file",
            "path": "projects/demo/arc.ifc",
            "size": len(content),
            "oid": "pointer",
            "lfs": {"oid": digest},
        }
        calls = []

        def response(request, **kwargs):
            calls.append(request.full_url)
            result = io.BytesIO(
                json.dumps([entry]).encode() if "/tree/" in request.full_url else content
            )
            result.headers = {}
            return result

        with (
            tempfile.TemporaryDirectory() as folder,
            patch("benchmark_tools.fetch", return_value=b'{"sha":"commit-v2"}'),
            patch("benchmark_tools.urllib.request.urlopen", side_effect=response),
        ):
            report = download(folder, log=lambda _: None)
            self.assertEqual(report["version"], "v2")
            self.assertTrue(report["complete"])
            self.assertEqual(report["expected_ifc"], 1)
            self.assertEqual((Path(folder) / entry["path"]).read_bytes(), content)
            download(folder, log=lambda _: None)
            self.assertEqual(sum("/resolve/" in url for url in calls), 1)
        self.assertTrue(needed("projects/demo/model_card.md"))
        self.assertTrue(needed("LICENSE"))


if __name__ == "__main__":
    unittest.main()
