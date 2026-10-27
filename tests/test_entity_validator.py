import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import ifcopenshell
from ifc_sql_views import convert
from validate_entity_views import validate, same


class IndependentEntityValidatorTest(unittest.TestCase):
    def make(self, directory, version):
        model = ifcopenshell.file(schema=version)
        unit = model.create_entity(
            "IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE"
        )
        assignment = model.create_entity("IfcUnitAssignment", Units=[unit])
        model.create_entity(
            "IfcProject",
            GlobalId=ifcopenshell.guid.new(),
            Name="Project",
            UnitsInContext=assignment,
        )
        beams = [
            model.create_entity("IfcBeam", GlobalId=ifcopenshell.guid.new(), Name=f"Beam {i}")
            for i in range(30)
        ]
        [
            model.create_entity(
                "IfcDoor", GlobalId=ifcopenshell.guid.new(), Name=f"Door {i}", OverallHeight=2000.0
            )
            for i in range(20)
        ]
        props = [
            model.create_entity(
                "IfcPropertySingleValue",
                Name="Code",
                NominalValue=model.create_entity("IfcLabel", "007"),
            ),
            model.create_entity(
                "IfcPropertySingleValue",
                Name="Zero",
                NominalValue=model.create_entity("IfcLengthMeasure", 0.0),
            ),
            model.create_entity(
                "IfcPropertySingleValue",
                Name="False",
                NominalValue=model.create_entity("IfcBoolean", False),
            ),
        ]
        pset = model.create_entity(
            "IfcPropertySet", GlobalId=ifcopenshell.guid.new(), Name="Company", HasProperties=props
        )
        model.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId=ifcopenshell.guid.new(),
            RelatedObjects=beams[:1],
            RelatingPropertyDefinition=pset,
        )
        source, db = Path(directory) / "model.ifc", Path(directory) / "model.sqlite"
        model.write(str(source))
        convert(source, db, log=lambda _: None)
        return source, db, beams[0].id()

    def test_both_versions_sparse_json_false_zero_and_no_input_changes(self):
        for version in ("IFC2X3", "IFC4"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                source, db, _ = self.make(directory, version)
                before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, db)]
                result = validate(source, db, Path(directory) / "audit", ["IfcBeam", "IfcDoor"])
                self.assertEqual([s["matched_objects"] for s in result["entities"]], [30, 20])
                for entity in result["entities"]:
                    self.assertEqual(entity["value_agreement_percent"], 100.0)
                    self.assertFalse(
                        entity["missing_ids"] or entity["extra_ids"] or entity["duplicate_ids"]
                    )
                self.assertEqual(
                    before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, db)]
                )

    def test_intentional_corruption_missing_object_and_extra_duplicate_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, db, iid = self.make(directory, "IFC4")
            with closing(sqlite3.connect(db)) as con:
                con.execute("UPDATE _sv_Beam SET Name=? WHERE ifc_id=?", ("WRONG", iid))
                etc = json.loads(
                    con.execute(
                        "SELECT etc FROM _sv_BeamCustomPset WHERE ifc_id=?", (iid,)
                    ).fetchone()[0]
                )
                etc["Company_Code"] = "WRONG"
                con.execute(
                    "UPDATE _sv_BeamCustomPset SET etc=? WHERE ifc_id=?", (json.dumps(etc), iid)
                )
                con.execute("DELETE FROM _sv_Door WHERE ifc_id=(SELECT MIN(ifc_id) FROM _sv_Door)")
                con.execute("DROP VIEW View_Beam")
                con.execute(
                    "CREATE VIEW View_Beam AS SELECT * FROM _sv_Beam UNION ALL SELECT * FROM _sv_Beam WHERE ifc_id="
                    + str(iid)
                )
                con.commit()
            result = validate(source, db, Path(directory) / "audit", ["IfcBeam", "IfcDoor"])
            beam, door = result["entities"]
            self.assertEqual(len(beam["duplicate_ids"]), 1)
            self.assertGreaterEqual(beam["values"]["mismatched"], 2)
            self.assertEqual(len(door["missing_ids"]), 1)
            self.assertIn(
                "Company_Code",
                {
                    r["field"].split(".")[-1]
                    for r in result["details"]
                    if r["status"] == "mismatch"
                },
            )

    def test_numeric_looking_labels_remain_strings(self):
        self.assertFalse(same("007", 7, 1e-6))
        self.assertTrue(same(False, 0, 1e-6))
        self.assertTrue(same([0, False], "[0,false]", 1e-6))

    def test_origin_with_invalid_own_direction_and_rotated_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            source, db, iid = self.make(directory, "IFC4")
            model = ifcopenshell.open(str(source))
            z = model.create_entity("IfcDirection", DirectionRatios=[0.0, 0.0, 1.0])
            x = model.create_entity("IfcDirection", DirectionRatios=[0.0, 1.0, 0.0])
            zero = model.create_entity("IfcDirection", DirectionRatios=[0.0, 0.0, 0.0])
            p = model.create_entity("IfcCartesianPoint", Coordinates=[100.0, 200.0, 0.0])
            q = model.create_entity("IfcCartesianPoint", Coordinates=[10.0, 20.0, 5.0])
            parent_axis = model.create_entity(
                "IfcAxis2Placement3D", Location=p, Axis=z, RefDirection=x
            )
            axis = model.create_entity(
                "IfcAxis2Placement3D", Location=q, Axis=z, RefDirection=zero
            )
            parent = model.create_entity("IfcLocalPlacement", RelativePlacement=parent_axis)
            placement = model.create_entity(
                "IfcLocalPlacement", PlacementRelTo=parent, RelativePlacement=axis
            )
            model.by_id(iid).ObjectPlacement = placement
            model.write(str(source))
            convert(source, db, overwrite=True, log=lambda _: None)
            result = validate(source, db, Path(directory) / "audit", ["IfcBeam"])
            self.assertEqual(result["entities"][0]["categories"]["Placement"]["matched"], 3)
            self.assertFalse([d for d in result["details"] if d["category"] == "Placement"])

    def test_collision_suffix_and_compound_latitude_are_not_false_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            source, db, iid = self.make(directory, "IFC4")
            model = ifcopenshell.open(str(source))
            angle = model.create_entity("IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN")
            assignment = model.by_type("IfcUnitAssignment")[0]
            assignment.Units = (*assignment.Units, angle)
            model.create_entity(
                "IfcSite",
                GlobalId=ifcopenshell.guid.new(),
                Name="Site",
                RefLatitude=[40, 20, 30],
                RefLongitude=[-86, 0, 0],
            )
            pset = model.by_type("IfcPropertySet")[0]
            lower = model.create_entity(
                "IfcPropertySingleValue",
                Name="code",
                NominalValue=model.create_entity("IfcLabel", "lowercase value"),
            )
            pset.HasProperties = (*pset.HasProperties, lower)
            model.write(str(source))
            convert(source, db, overwrite=True, log=lambda _: None)
            result = validate(source, db, Path(directory) / "audit", ["IfcBeam", "IfcSite"])
            for entity in result["entities"]:
                self.assertEqual(entity["value_agreement_percent"], 100.0)
                self.assertEqual(entity["values"].get("missing", 0), 0)
                self.assertEqual(entity["values"].get("mismatched", 0), 0)
