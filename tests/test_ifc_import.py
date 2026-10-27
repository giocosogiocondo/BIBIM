import hashlib
import json
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import ifcopenshell

from bibim_validation.values import transform_point, unit_label
from experiments.text_to_sql_top3.generation import messages
from experiments.view_selection_ablation.gguf import read_metadata
from ifc_sql_views import Database, convert, strip_step_literals_and_comments


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

    def test_step_comments_and_strings_with_hash_equals_do_not_fail_entity_count(self):
        sample = b"/* comment\n#99999 = FAKE();\n*/\n#1 = IFCPERSON($,'line1\n#88888 = TEXT',$,$,$,$,$,$);\n"
        sanitized = strip_step_literals_and_comments(sample)
        self.assertNotIn(b"#99999", sanitized)
        self.assertNotIn(b"#88888", sanitized)
        self.assertIn(b"#1 =", sanitized)

    def test_expanded_si_units_and_database_context_manager(self):
        model = ifcopenshell.file(schema="IFC4")
        lux = model.create_entity("IfcSIUnit", UnitType="ILLUMINANCEUNIT", Name="LUX")
        kohm = model.create_entity(
            "IfcSIUnit", UnitType="ELECTRICRESISTANCEUNIT", Prefix="KILO", Name="OHM"
        )
        self.assertEqual(unit_label(lux), "lx")
        self.assertEqual(unit_label(kohm), "kohm")
        with tempfile.TemporaryDirectory() as folder:
            source, _, _, _, _ = self.fixture(folder, "IFC4")
            output = Path(folder) / "result.sqlite"
            convert(source, output, log=lambda _: None)
            with Database(output) as db:
                self.assertEqual(db.version, "IFC4")
                self.assertGreater(len(db.nodes), 0)

    def test_pure_python_transform_point_and_configurable_gguf_and_etc_keys(self):
        matrix = (
            (0.0, -1.0, 0.0, 10.0),
            (1.0, 0.0, 0.0, 20.0),
            (0.0, 0.0, 1.0, 30.0),
            (0.0, 0.0, 0.0, 1.0),
        )
        self.assertEqual(transform_point(matrix, (2.0, 3.0)), (7.0, 22.0, 30.0))
        with self.assertRaises(ValueError):
            transform_point(matrix, (float("nan"), 0.0, 0.0))

        with tempfile.TemporaryDirectory() as folder:
            gguf_path = Path(folder) / "custom.gguf"
            key = b"general.architecture"
            val = b"qwen3"
            payload = (
                b"GGUF"
                + struct.pack("<IQQ", 3, 0, 1)
                + struct.pack("<Q", len(key))
                + key
                + struct.pack("<IQ", 8, len(val))
                + val
            )
            gguf_path.write_bytes(payload)
            with self.assertRaises(ValueError):
                read_metadata(gguf_path)
            meta = read_metadata(gguf_path, required_architecture="any")
            self.assertEqual(meta["general.architecture"], "qwen3")

        chat = messages(
            "Find fire rating",
            ["View_Door"],
            {"View_Door": ["ifc_id", "Name", "etc"]},
            etc_keys={"View_Door": ["Pset_DoorCommon_FireRating"]},
        )
        self.assertIn("Sparse etc JSON keys:", chat[1]["content"])
        self.assertIn('"Pset_DoorCommon_FireRating"', chat[1]["content"])


if __name__ == "__main__":
    unittest.main()
