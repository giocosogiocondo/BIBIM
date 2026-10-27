import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import ifcopenshell

from ifc_sql_views import build, convert
from ifc_sql_views.column_policy import compact_entity_columns
from test_provenance import provenance_fixture
from test_documented_views import fixture


class ColumnIntegrationTest(unittest.TestCase):
    def test_uniform_custom_angle_unit_is_in_name_and_supports_sql_aggregation(self):
        def configure(add, con):
            add("IfcConversionBasedUnit", 60, UnitType="PLANEANGLEUNIT", Name="Degree")
            for iid, pid, sid, rid, value in ((1, 61, 62, 63, 0), (2, 64, 65, 66, 15.0)):
                add(
                    "IfcPropertySingleValue",
                    pid,
                    Name="Column Slant Angle",
                    NominalValue={"type": "IfcPlaneAngleMeasure", "value": value},
                    Unit=60,
                )
                add("IfcPropertySet", sid, Name="ArchiCADProperties", HasProperties=[pid])
                add(
                    "IfcRelDefinesByProperties",
                    rid,
                    RelatedObjects=[iid],
                    RelatingPropertyDefinition=sid,
                )

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "out.sqlite"
            provenance_fixture(source, configure)
            report = build(source, output)
            column = "ArchiCADProperties_Column Slant Angle_Degree"
            info = report["views"]["View_DoorCustomPset"]
            self.assertIn(column, info["columns"])
            self.assertNotIn("ArchiCADProperties_Column Slant Angle_Unit", info["columns"])
            with closing(sqlite3.connect(output)) as con:
                result = con.execute(
                    f'SELECT SUM("{column}"),AVG("{column}") FROM View_DoorCustomPset'
                ).fetchone()
                self.assertEqual(result, (15.0, 7.5))
                self.assertEqual(
                    con.execute(
                        "SELECT Name FROM IfcConversionBasedUnit WHERE ifc_id=60"
                    ).fetchone()[0],
                    "Degree",
                )

    def test_nonfinite_property_does_not_replace_existing_output(self):
        def configure(add, con):
            add(
                "IfcPropertySingleValue",
                40,
                Name="Invalid",
                NominalValue={"type": "IfcReal", "value": float("nan")},
            )
            con.execute(
                "UPDATE IfcPropertySet SET HasProperties=? WHERE ifc_id=10",
                (json.dumps([11, 12, 40]),),
            )

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "out.sqlite"
            provenance_fixture(source, configure)
            output.write_bytes(b"existing output")
            with self.assertRaisesRegex(ValueError, "Non-finite"):
                build(source, output, overwrite=True)
            self.assertEqual(output.read_bytes(), b"existing output")

    def test_grid_placement_is_reported_and_does_not_invent_xyz(self):
        def configure(add, con):
            add("IfcCartesianPoint", 60, Coordinates=[0, 0])
            add("IfcCartesianPoint", 61, Coordinates=[10, 0])
            add("IfcPolyline", 62, Points=[60, 61])
            add("IfcCartesianPoint", 63, Coordinates=[0, 10])
            add("IfcPolyline", 64, Points=[60, 63])
            add("IfcGridAxis", 65, AxisTag="A", AxisCurve=62, SameSense=True)
            add("IfcGridAxis", 66, AxisTag="1", AxisCurve=64, SameSense=True)
            add(
                "IfcVirtualGridIntersection", 67, IntersectingAxes=[65, 66], OffsetDistances=[0, 0]
            )
            add("IfcGridPlacement", 68, PlacementLocation=67)
            add("IfcGrid", 69, Name="Grid", UAxes=[65], VAxes=[66])
            con.execute("UPDATE IfcDoor SET ObjectPlacement=68 WHERE ifc_id=1")

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "out.sqlite"
            provenance_fixture(source, configure)
            r = build(source, output)
            self.assertNotIn("Placement_X", r["views"]["View_Door"]["columns"])
            self.assertIn("unsupported_placement", {d["code"] for d in r["diagnostics"]})

    def test_legacy_and_explicit_qto_share_the_same_column_across_objects(self):
        def configure(add, con):
            add("IfcElementQuantity", 40, Name="Qto_DoorBaseQuantities", Quantities=[41])
            add("IfcQuantityLength", 41, Name="Width", LengthValue=650, Unit=20)
            add("IfcRelDefinesByProperties", 42, RelatedObjects=[1], RelatingPropertyDefinition=40)

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "out.sqlite"
            provenance_fixture(source, configure)
            r = build(source, output)
            columns = r["views"]["View_Door"]["columns"]
            self.assertEqual(
                [c for c in columns if c.startswith("Qto_DoorBaseQuantities_Width")],
                ["Qto_DoorBaseQuantities_Width_mm"],
            )
            with closing(sqlite3.connect(output)) as con:
                rows = con.execute(
                    "SELECT Qto_DoorBaseQuantities_Width_mm FROM View_Door ORDER BY ifc_id"
                ).fetchall()
                self.assertEqual(rows, [(650,), (900,)])

    def test_all_standard_sets_and_qto_are_in_the_entity_view(self):
        def configure(add, con):
            add("IfcPropertySet", 40, Name="Pset_ManufacturerTypeInformation", HasProperties=[41])
            add(
                "IfcPropertySingleValue",
                41,
                Name="Manufacturer",
                NominalValue={"type": "IfcLabel", "value": "Factory A"},
            )
            add(
                "IfcRelDefinesByProperties",
                42,
                RelatedObjects=[1, 2],
                RelatingPropertyDefinition=40,
            )
            add(
                "IfcPropertySet", 43, Name="Pset_EnvironmentalImpactIndicators", HasProperties=[44]
            )
            add(
                "IfcPropertySingleValue",
                44,
                Name="FunctionalUnitReference",
                NominalValue={"type": "IfcLabel", "value": "one door"},
            )
            add(
                "IfcRelDefinesByProperties",
                45,
                RelatedObjects=[1, 2],
                RelatingPropertyDefinition=43,
            )

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "views.sqlite"
            provenance_fixture(source, configure)
            report = build(source, output)
            with closing(sqlite3.connect(output)) as con:
                row = con.execute(
                    "SELECT Pset_ManufacturerTypeInformation_Manufacturer,Pset_EnvironmentalImpactIndicators_FunctionalUnitReference,Qto_DoorBaseQuantities_Width_mm,DoorType_OperationType FROM View_Door WHERE ifc_id=1"
                ).fetchone()
                self.assertEqual(row, ("Factory A", "one door", 900, "SINGLE_SWING_LEFT"))
            self.assertNotIn("View_DoorType", report["views"])

    def test_noncommon_concrete_and_packing_sets_are_not_excluded_from_doors(self):
        def configure(add, con):
            add("IfcPropertySet", 40, Name="Pset_ConcreteElementGeneral", HasProperties=[41])
            add(
                "IfcPropertySingleValue",
                41,
                Name="ConcreteCover",
                NominalValue={"type": "IfcLengthMeasure", "value": 25},
            )
            add(
                "IfcRelDefinesByProperties",
                42,
                RelatedObjects=[1, 2],
                RelatingPropertyDefinition=40,
            )
            add("IfcPropertySet", 43, Name="Pset_PackingInstructions", HasProperties=[44])
            add(
                "IfcPropertySingleValue",
                44,
                Name="SpecialInstructions",
                NominalValue={"type": "IfcText", "value": "Handle carefully"},
            )
            add(
                "IfcRelDefinesByProperties",
                45,
                RelatedObjects=[1, 2],
                RelatingPropertyDefinition=43,
            )

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "out.sqlite"
            provenance_fixture(source, configure)
            build(source, output)
            with closing(sqlite3.connect(output)) as con:
                row = con.execute(
                    "SELECT Pset_ConcreteElementGeneral_ConcreteCover_mm,Pset_PackingInstructions_SpecialInstructions FROM View_Door WHERE ifc_id=1"
                ).fetchone()
                self.assertEqual(row, (25, "Handle carefully"))

    def test_world_placement_owner_and_representation_are_resolved(self):
        def configure(add, con):
            add("IfcCartesianPoint", 60, Coordinates=[10, 20, 30])
            add("IfcDirection", 61, DirectionRatios=[0, 1, 0])
            add("IfcAxis2Placement3D", 62, Location=60, RefDirection=61)
            add("IfcLocalPlacement", 63, RelativePlacement=62)
            add("IfcCartesianPoint", 64, Coordinates=[2, 0, 0])
            add("IfcAxis2Placement3D", 65, Location=64)
            add("IfcLocalPlacement", 66, RelativePlacement=65, PlacementRelTo=63)
            add("IfcPerson", 67, GivenName="Ada", FamilyName="Lovelace")
            add("IfcOrganization", 68, Name="BIM Team")
            add("IfcPersonAndOrganization", 69, ThePerson=67, TheOrganization=68)
            add(
                "IfcApplication",
                70,
                ApplicationDeveloper=68,
                Version="2",
                ApplicationFullName="Example CAD",
                ApplicationIdentifier="CAD",
            )
            add(
                "IfcOwnerHistory",
                71,
                OwningUser=69,
                OwningApplication=70,
                CreationDate=0,
                ChangeAction="ADDED",
            )
            add(
                "IfcRectangleProfileDef",
                72,
                ProfileType="AREA",
                ProfileName="Rectangle",
                XDim=300,
                YDim=400,
            )
            add("IfcCartesianPoint", 73, Coordinates=[0, 0, 0])
            add("IfcAxis2Placement3D", 74, Location=73)
            add("IfcDirection", 75, DirectionRatios=[0, 0, 1])
            add(
                "IfcExtrudedAreaSolid",
                76,
                SweptArea=72,
                Position=74,
                ExtrudedDirection=75,
                Depth=2000,
            )
            add(
                "IfcShapeRepresentation",
                77,
                RepresentationIdentifier="Body",
                RepresentationType="SweptSolid",
                Items=[76],
            )
            add("IfcProductDefinitionShape", 78, Representations=[77])
            con.execute(
                "UPDATE IfcDoor SET ObjectPlacement=66,OwnerHistory=71,Representation=78 WHERE ifc_id=1"
            )

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "out.sqlite"
            provenance_fixture(source, configure)
            r = build(source, output)
            with closing(sqlite3.connect(output)) as con:
                row = con.execute(
                    "SELECT Placement_X,Placement_Y,Placement_Z,Owner_Name,Owner_Organization,Representation_Type,Representation_RectangleProfileDef_XDim_mm FROM View_Door WHERE ifc_id=1"
                ).fetchone()
                self.assertEqual(
                    row, (10.0, 22.0, 30.0, "Ada Lovelace", "BIM Team", "SweptSolid", 300)
                )
            self.assertFalse(
                set(r["views"]["View_Door"]["columns"])
                & {"ObjectPlacement_id", "OwnerHistory_id", "Representation_id"}
            )

    def test_ifc2x3_door_style_is_folded_into_door(self):
        def configure(add, con):
            add("IfcDoor", 200, Name="Door", GlobalId="door")
            add(
                "IfcDoorStyle",
                201,
                Name="Style A",
                GlobalId="style",
                OperationType="SINGLE_SWING_LEFT",
                ConstructionType="WOOD",
                ParameterTakesPrecedence=True,
                Sizeable=False,
            )
            add("IfcRelDefinesByType", 202, RelatedObjects=[200], RelatingType=201)

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "out.sqlite"
            fixture(source, "IFC2X3", configure)
            r = build(source, output)
            with closing(sqlite3.connect(output)) as con:
                row = con.execute(
                    "SELECT Doorstyle_Name,Doorstyle_OperationType,Doorstyle_Sizeable FROM View_Door"
                ).fetchone()
                self.assertEqual(row, ("Style A", "SINGLE_SWING_LEFT", 0))
            self.assertNotIn("View_DoorStyle", r["views"])

    def test_empty_and_exact_ten_percent_fields_are_packed_not_lost(self):
        rows = [
            {
                "ifc_id": i,
                "ifc_class": "IfcDoor",
                "GlobalId": str(i),
                "Name": "Door",
                "Blank": "   ",
            }
            for i in range(10)
        ]
        rows[0].update(Rare=0, Rare_Unit="mm", Flag=False)
        rows[1]["Dense"] = 1
        rows[2]["Dense"] = 2
        columns = list(dict.fromkeys(k for row in rows for k in row))
        columns, packed, policy = compact_entity_columns(rows, columns)
        self.assertNotIn("Blank", columns)
        self.assertNotIn("Rare", columns)
        self.assertIn("Dense", columns)
        self.assertEqual(packed[0]["etc"], {"Rare_mm": 0, "Flag": False})
        self.assertEqual(policy["coverage_denominator"], 10)

    def test_thousands_of_custom_properties_overflow_without_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            m = ifcopenshell.file(schema="IFC4")
            wall = m.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name="Wall")
            props = [
                m.create_entity(
                    "IfcPropertySingleValue",
                    Name=f"Field{i}",
                    NominalValue=m.create_entity("IfcInteger", i),
                )
                for i in range(2100)
            ]
            pset = m.create_entity(
                "IfcPropertySet",
                GlobalId=ifcopenshell.guid.new(),
                Name="PSet_Revit_Identity Data",
                HasProperties=props,
            )
            m.create_entity(
                "IfcRelDefinesByProperties",
                GlobalId=ifcopenshell.guid.new(),
                RelatedObjects=[wall],
                RelatingPropertyDefinition=pset,
            )
            source, output = Path(folder) / "test.ifc", Path(folder) / "out.sqlite"
            m.write(str(source))
            report = convert(source, output, log=lambda _: None)
            self.assertLessEqual(len(report["views"]["View_WallCustomPset"]["columns"]), 2000)
            with closing(sqlite3.connect(output)) as con:
                con.row_factory = sqlite3.Row
                row = dict(con.execute("SELECT * FROM View_WallCustomPset").fetchone())
                row.update(json.loads(row.pop("etc") or "{}"))
                properties = {
                    k: v for k, v in row.items() if k.startswith("Revit_Identity Data_Field")
                }
                self.assertEqual(len(properties), 2100)
                self.assertEqual(set(properties.values()), set(range(2100)))

    def test_custom_extensions_and_quantities_share_entity_custom_view(self):
        def configure(add, con):
            add(
                "IfcPropertySingleValue",
                40,
                Name="CompanyNote",
                NominalValue={"type": "IfcLabel", "value": "extra"},
            )
            con.execute(
                "UPDATE IfcPropertySet SET HasProperties=? WHERE ifc_id=10",
                (json.dumps([11, 12, 40]),),
            )
            add("IfcElementQuantity", 41, Name="CompanyQty", Quantities=[42])
            add("IfcQuantityCount", 42, Name="Count", CountValue=0)
            add(
                "IfcRelDefinesByProperties",
                43,
                RelatedObjects=[1, 2],
                RelatingPropertyDefinition=41,
            )

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "raw.sqlite", Path(folder) / "out.sqlite"
            provenance_fixture(source, configure)
            r = build(source, output)
            with closing(sqlite3.connect(output)) as con:
                row = con.execute(
                    "SELECT DoorCommon_CompanyNote,CompanyQty_Count FROM View_DoorCustomPset WHERE ifc_id=1"
                ).fetchone()
                self.assertEqual(row, ("extra", 0))
            self.assertNotIn("View_CustomQuantities", r["views"])

    def test_dense_view_is_not_limited_to_two_hundred_columns(self):
        rows = [
            {
                "ifc_id": 1,
                "ifc_class": "IfcWall",
                "GlobalId": "g",
                **{f"Field{i}": i for i in range(300)},
            }
        ]
        columns, packed, _ = compact_entity_columns(rows, list(rows[0]))
        self.assertEqual(len(columns), 303)
        self.assertNotIn("etc", columns)


if __name__ == "__main__":
    unittest.main()
