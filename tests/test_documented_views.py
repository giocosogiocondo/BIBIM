import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import ifcopenshell

from ifc_sql_views import build, Writer
from ifc_sql_views.reader import Database, quote
from ifc_sql_views.extractor import Extractor
from ifc_sql_views.extractor import unique
from unittest.mock import patch
from ifc_sql_views.catalog import Catalog


class SharedResourcePerformanceTest(unittest.TestCase):
    def test_singleton_dedup_does_not_serialize_large_payload(self):
        value = {"Name": "Shared type", "Data": list(range(1000))}
        with patch(
            "ifc_sql_views.extractor.json.dumps",
            side_effect=AssertionError("Unnecessary serialization"),
        ):
            self.assertEqual(unique([value]), [value])
        self.assertEqual(unique([value, dict(value)]), [value])
        self.assertEqual(len(unique([value, {"Name": "Different"}])), 2)

    def test_owner_history_consumer_inverses_are_not_rescanned(self):
        def configure(add, con):
            add("IfcOrganization", 200, Name="Shared organization")
            add("IfcPerson", 201, FamilyName="Tester")
            add("IfcPersonAndOrganization", 202, ThePerson=201, TheOrganization=200)
            add(
                "IfcApplication",
                203,
                ApplicationDeveloper=200,
                Version="1",
                ApplicationFullName="Fixture",
                ApplicationIdentifier="T",
            )
            add(
                "IfcOwnerHistory",
                204,
                OwningUser=202,
                OwningApplication=203,
                ChangeAction="ADDED",
                CreationDate=0,
            )
            for index in range(500):
                add("IfcBeam", 1000 + index, Name=str(index), OwnerHistory=204)

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "shared.sqlite"
            fixture(source, "IFC4", configure)
            db = Database(source)
            extractor = Extractor(db, Catalog(db))
            try:
                self.assertEqual(len(db.incoming[204]), 500)

                class NoRescan(list):
                    def __iter__(self):
                        raise AssertionError("Shared consumer inverses were rescanned")

                db.incoming[204] = NoRescan(db.incoming[204])
                resource = extractor.resource(204)
                self.assertEqual(
                    resource["OwningUser"]["TheOrganization"]["Name"], "Shared organization"
                )
                self.assertEqual(extractor.resource(204), resource)
                self.assertEqual(extractor._root_resource.cache_info().hits, 1)
            finally:
                for obj in (extractor, db):
                    for name in dir(type(obj)):
                        method = getattr(type(obj), name, None)
                        if hasattr(method, "cache_clear"):
                            method.cache_clear()
                db.close()


def fixture(path, version, configure=None):
    schema = ifcopenshell.schema_by_name(version)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (preprocessor TEXT, schema TEXT, mvd TEXT)")
    con.execute("INSERT INTO metadata VALUES (?,?,?)", ("IfcOpenShell-1.0.0", version, ""))
    created = set()

    def add(cls, iid, **values):
        names = [a.name() for a in schema.declaration_by_name(cls).all_attributes()]
        if cls not in created:
            con.execute(
                f"CREATE TABLE {quote(cls)} (ifc_id INTEGER PRIMARY KEY, "
                + ",".join(quote(n) for n in names)
                + ", inverses TEXT)"
            )
            created.add(cls)
        row = (
            [iid]
            + [
                (
                    json.dumps(values.get(n))
                    if isinstance(values.get(n), (list, dict))
                    else values.get(n)
                )
                for n in names
            ]
            + ["[999999]"]
        )
        con.execute(f"INSERT INTO {quote(cls)} VALUES (" + ",".join("?" for _ in row) + ")", row)

    add("IfcBeam", 1, Name="001", GlobalId="beam-one", ObjectPlacement=90)
    add("IfcBeam", 2, Name="Beam two", GlobalId="beam-two")
    add("IfcBeamType", 3, Name="Shared type", HasPropertySets=[31], PredefinedType="BEAM")
    add("IfcElementAssembly", 4, Name="Assembly")
    add("IfcGroup", 5, Name="Group A")
    add("IfcBuildingStorey", 6, Name="Level 1", Elevation=3000)
    add("IfcMaterial", 7, Name="Concrete")
    add("IfcMaterial", 8, Name="Steel")
    add("IfcMaterialList", 9, Materials=[7, 8])
    add("IfcRelDefinesByType", 20, RelatedObjects=[1, 2], RelatingType=3)
    add("IfcRelAggregates", 21, RelatingObject=4, RelatedObjects=[1, 2])
    add("IfcRelAssignsToGroup", 22, RelatedObjects=[1, 2], RelatingGroup=5)
    add("IfcRelContainedInSpatialStructure", 23, RelatedElements=[4], RelatingStructure=6)
    add("IfcRelAssociatesMaterial", 24, RelatedObjects=[1], RelatingMaterial=9)
    add("IfcRelDefinesByProperties", 25, RelatedObjects=[1], RelatingPropertyDefinition=30)
    add("IfcRelDefinesByProperties", 26, RelatedObjects=[1, 2], RelatingPropertyDefinition=32)
    add("IfcRelDefinesByProperties", 27, RelatedObjects=[1], RelatingPropertyDefinition=33)
    add("IfcRelDefinesByProperties", 28, RelatedObjects=[1], RelatingPropertyDefinition=34)
    add("IfcPropertySet", 30, Name="Pset_BeamCommon", HasProperties=[40, 41, 42])
    add("IfcPropertySet", 31, Name="Pset_BeamCommon", HasProperties=[43, 44])
    add("IfcPropertySet", 32, Name="Company", HasProperties=[45, 46, 47, 48, 49, 50, 51])
    add("IfcPropertySet", 33, Name="Company_A", HasProperties=[55])
    add("IfcPropertySet", 34, Name="Company", HasProperties=[56])
    add("IfcPropertySingleValue", 40, Name="FireRating", NominalValue='"90min"')
    add("IfcPropertySingleValue", 41, Name="LoadBearing", NominalValue="true")
    add("IfcPropertySingleValue", 42, Name="Span", NominalValue="2000")
    add("IfcPropertySingleValue", 43, Name="FireRating", NominalValue='"60min"')
    add("IfcPropertySingleValue", 44, Name="IsExternal", NominalValue="false")
    add("IfcPropertySingleValue", 45, Name="NumberMatchingId", NominalValue="7")
    add("IfcPropertyListValue", 46, Name="Sizes", ListValues=[1, 2, 7])
    add(
        "IfcPropertyEnumeratedValue",
        47,
        Name="Status",
        EnumerationValues=[{"type": "IfcLabel", "value": "NEW"}],
    )
    add(
        "IfcPropertyBoundedValue",
        48,
        Name="Range",
        LowerBoundValue="1",
        UpperBoundValue="9",
        Unit=101,
    )
    add("IfcPropertyReferenceValue", 49, Name="Finish", PropertyReference=8)
    add("IfcPropertyTableValue", 50, Name="Curve", DefiningValues=[1, 2], DefinedValues=[3, 4])
    add("IfcComplexProperty", 51, Name="Nested", HasProperties=[52, 53], UsageName="detail")
    add("IfcPropertySingleValue", 52, Name="Code", NominalValue='"A"')
    add("IfcComplexProperty", 53, Name="Cycle", HasProperties=[51], UsageName="cycle")
    add("IfcPropertySingleValue", 55, Name="B", NominalValue='"first"')
    add("IfcPropertySingleValue", 56, Name="A_B", NominalValue='"second"')
    add("IfcQuantityLength", 60, Name="Length", LengthValue=2000, Unit=102)
    add(
        "IfcElementQuantity",
        61,
        Name="Qto_BeamBaseQuantities" if version == "IFC4" else "BaseQuantities",
        Quantities=[60],
    )
    add("IfcRelDefinesByProperties", 62, RelatedObjects=[1], RelatingPropertyDefinition=61)
    add(
        "IfcRelConnectsElements",
        70,
        RelatingElement=1,
        RelatedElement=2,
        Description="physical connection",
    )
    add(
        "IfcRelAssignsToProduct",
        71,
        RelatedObjects=[2],
        RelatingProduct=1,
        Description="generic relation",
    )
    add("IfcCartesianPoint", 89, Coordinates=[1, 2, 7])
    add("IfcLocalPlacement", 90, PlacementRelTo=91, RelativePlacement=92)
    add("IfcLocalPlacement", 91, PlacementRelTo=90, RelativePlacement=92)
    add("IfcAxis2Placement3D", 92, Location=89)
    add("IfcSIUnit", 101, UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE")
    add("IfcSIUnit", 102, UnitType="LENGTHUNIT", Name="METRE")
    add("IfcUnitAssignment", 103, Units=[101])
    add("IfcProject", 104, Name="Project", UnitsInContext=103)
    if configure:
        configure(add, con)
    # Poisoned existing view proves that the generator only reads raw tables.
    con.execute("CREATE VIEW View_Beam AS SELECT 999 AS ifc_id")
    con.commit()
    con.close()


class DocumentedViewsTest(unittest.TestCase):
    def build_case(self, configure=None, version="IFC4", classes=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        source, output = Path(folder.name) / "raw.sqlite", Path(folder.name) / "result.sqlite"
        fixture(source, version, configure)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        report = build(source, output, classes=classes)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        con = sqlite3.connect(output)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        return con, report

    def test_pairs_and_complete_raw_database_for_both_schemas(self):
        for version in ("IFC2X3", "IFC4"):
            with self.subTest(version=version):
                con, report = self.build_case(version=version)
                names = set(report["views"])
                self.assertIn("View_Beam", names)
                self.assertIn("View_BeamCustomPset", names)
                self.assertFalse(
                    names
                    & {
                        "View_BeamType",
                        "View_Properties",
                        "View_Resources",
                        "View_References",
                        "View_Links",
                    }
                )
                self.assertFalse(any(n.startswith(("View_Pset_", "View_Qto_")) for n in names))
                self.assertEqual(con.execute("SELECT COUNT(*) FROM View_Beam").fetchone()[0], 2)
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM id_map").fetchone()[0], report["raw_nodes"]
                )
                self.assertEqual(
                    con.execute(
                        "SELECT PlacementRelTo FROM IfcLocalPlacement WHERE ifc_id=90"
                    ).fetchone()[0],
                    91,
                )
                self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertTrue(
                    all(
                        c == "ifc_id" or not c.endswith("_id")
                        for v in report["views"].values()
                        for c in v["columns"]
                    )
                )

    def test_inherited_properties_occurrence_override_and_false(self):
        con, _ = self.build_case()
        rows = con.execute(
            "SELECT Pset_BeamCommon_FireRating,Pset_BeamCommon_IsExternal FROM View_Beam ORDER BY ifc_id"
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("90min", 0), ("60min", 0)])

    def test_custom_identifiers_only_and_join_recovers_detailed_attributes(self):
        def configure(add, con):
            con.execute("UPDATE IfcBeam SET Tag='B001',ObjectType='Example' WHERE ifc_id=1")
            con.execute("UPDATE IfcLocalPlacement SET PlacementRelTo=NULL WHERE ifc_id=91")

        for version in ("IFC2X3", "IFC4"):
            with self.subTest(version=version):
                con, report = self.build_case(configure, version=version)
                row = dict(
                    con.execute("SELECT * FROM View_BeamCustomPset WHERE ifc_id=1").fetchone()
                )
                self.assertEqual(
                    {k: row[k] for k in ("ifc_id", "ifc_class", "GlobalId", "Name")},
                    {"ifc_id": 1, "ifc_class": "IfcBeam", "GlobalId": "beam-one", "Name": "001"},
                )
                forbidden = {
                    "Tag",
                    "ObjectType",
                    "Placement_X",
                    "Placement_Unit",
                    "Storey_Name",
                    "BeamType_Name",
                    "Material_Materials",
                    "Pset_BeamCommon_FireRating",
                }
                self.assertFalse(forbidden & set(row))
                self.assertFalse(forbidden & set(json.loads(row.get("etc") or "{}")))
                joined = con.execute(
                    "SELECT c.Name,c.Company_NumberMatchingId,b.Tag,b.Placement_X,b.BeamType_Name,b.Storey_Name,b.Pset_BeamCommon_FireRating FROM View_BeamCustomPset c JOIN View_Beam b ON b.ifc_id=c.ifc_id WHERE c.ifc_id=1"
                ).fetchone()
                self.assertEqual(
                    tuple(joined), ("001", 7, "B001", 2.0, "Shared type", "Level 1", "90min")
                )
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM View_BeamCustomPset c JOIN View_Beam b ON b.ifc_id=c.ifc_id"
                    ).fetchone()[0],
                    2,
                )

    def test_identifiers_without_custom_values_do_not_create_custom_view(self):
        def configure(add, con):
            con.execute("UPDATE IfcPropertySet SET HasProperties='[]' WHERE Name LIKE 'Company%'")
            con.execute("DELETE FROM IfcPropertySingleValue WHERE ifc_id=42")

        con, report = self.build_case(configure)
        self.assertNotIn("View_BeamCustomPset", report["views"])

    def test_custom_value_types_complex_properties_and_units(self):
        con, _ = self.build_case()
        r = dict(con.execute("SELECT * FROM View_BeamCustomPset WHERE ifc_id=1").fetchone())
        self.assertEqual(r["Company_NumberMatchingId"], 7)
        self.assertEqual(json.loads(r["Company_Sizes"]), [1, 2, 7])
        self.assertEqual(r["Company_Status"], "NEW")
        self.assertEqual(r["Company_Nested_Code"], "A")
        self.assertIn("Company_Range_mm", r)
        self.assertNotIn("Company_Range_Unit", r)
        self.assertEqual(json.loads(r["Company_Finish"])["Name"], "Steel")
        self.assertNotIn("ifc_id", json.loads(r["Company_Finish"]))
        self.assertFalse(any(k.startswith("Custom_") for k in r))

    def test_collision_preserves_distinct_paths(self):
        con, report = self.build_case()
        row = dict(con.execute("SELECT * FROM View_BeamCustomPset WHERE ifc_id=1").fetchone())
        self.assertEqual(
            {v for k, v in row.items() if k.startswith("Company_A_B")}, {"first", "second"}
        )
        self.assertIn("column_name_collision", {d["code"] for d in report["diagnostics"]})

    def test_placement_cycles_do_not_produce_false_coordinates(self):
        con, report = self.build_case()
        row = dict(con.execute("SELECT * FROM View_Beam WHERE ifc_id=1").fetchone())
        self.assertNotIn("Placement_X", row)
        self.assertIn("placement_cycle", {d["code"] for d in report["diagnostics"]})

    def test_invalid_local_direction_preserves_origin_with_parent_rotation(self):
        def configure(add, con):
            add("IfcCartesianPoint", 501, Coordinates=[100, 200, 0])
            add("IfcCartesianPoint", 502, Coordinates=[10, 20, 5])
            add("IfcDirection", 503, DirectionRatios=[0, 0, 1])
            add("IfcDirection", 504, DirectionRatios=[0, 1, 0])
            add("IfcDirection", 505, DirectionRatios=[0, 0, 0])
            add("IfcAxis2Placement3D", 506, Location=501, Axis=503, RefDirection=504)
            add("IfcAxis2Placement3D", 507, Location=502, Axis=503, RefDirection=505)
            add("IfcLocalPlacement", 508, RelativePlacement=506)
            add("IfcLocalPlacement", 509, PlacementRelTo=508, RelativePlacement=507)
            add("IfcLocalPlacement", 510, PlacementRelTo=509, RelativePlacement=506)
            con.execute("UPDATE IfcBeam SET ObjectPlacement=509 WHERE ifc_id=1")
            con.execute("UPDATE IfcBeam SET ObjectPlacement=510 WHERE ifc_id=2")

        for version in ("IFC2X3", "IFC4"):
            with self.subTest(version=version):
                con, report = self.build_case(configure, version=version)
                row = dict(con.execute("SELECT * FROM View_Beam WHERE ifc_id=1").fetchone())
                self.assertEqual(tuple(row["Placement_" + a] for a in "XYZ"), (80, 210, 5))
                self.assertEqual(row["Placement_Unit"], "mm")
                self.assertFalse(
                    any(
                        k.startswith(("Placement_XAxis_", "Placement_YAxis_", "Placement_ZAxis_"))
                        and row[k] is not None
                        for k in row
                    )
                )
                child = dict(con.execute("SELECT * FROM View_Beam WHERE ifc_id=2").fetchone())
                self.assertTrue(all(child.get("Placement_" + a) is None for a in "XYZ"))
                self.assertIn("invalid_placement", {d["code"] for d in report["diagnostics"]})

    def test_invalid_root_direction_preserves_origin(self):
        def configure(add, con):
            add("IfcCartesianPoint", 501, Coordinates=[10, 20, 5])
            add("IfcDirection", 502, DirectionRatios=[0, 0, 0])
            add("IfcAxis2Placement3D", 503, Location=501, RefDirection=502)
            add("IfcLocalPlacement", 504, RelativePlacement=503)
            con.execute("UPDATE IfcBeam SET ObjectPlacement=504 WHERE ifc_id=1")

        con, _ = self.build_case(configure)
        row = dict(con.execute("SELECT * FROM View_Beam WHERE ifc_id=1").fetchone())
        self.assertEqual(tuple(row["Placement_" + a] for a in "XYZ"), (10, 20, 5))

    def test_typing_and_material_names_are_interpreted(self):
        con, _ = self.build_case()
        row = dict(con.execute("SELECT * FROM View_Beam WHERE ifc_id=1").fetchone())
        self.assertEqual(row["BeamType_Name"], "Shared type")
        self.assertEqual(row["Storey_Name"], "Level 1")
        material = json.loads(row["Material_Materials"])
        self.assertEqual({n["Name"] for n in material}, {"Concrete", "Steel"})
        self.assertFalse(any("ifc_id" in n for n in material))

    def test_static_definition_is_standard_not_custom(self):
        def configure(add, con):
            add("IfcDoorLiningProperties", 110, Name="Lining", LiningDepth=20)
            add(
                "IfcRelDefinesByProperties",
                111,
                RelatedObjects=[1],
                RelatingPropertyDefinition=110,
            )

        con, _ = self.build_case(configure)
        row = con.execute(
            "SELECT IfcDoorLiningProperties_LiningDepth_mm FROM View_Beam WHERE ifc_id=1"
        ).fetchone()
        self.assertEqual(tuple(row), (20,))
        custom = dict(con.execute("SELECT * FROM View_BeamCustomPset WHERE ifc_id=1").fetchone())
        self.assertFalse(any(k.startswith("Lining_") for k in custom))

    def test_conflicting_value_unit_pairs_remain_aligned(self):
        def configure(add, con):
            add("IfcPropertySet", 110, Name="Conflict", HasProperties=[111, 112])
            add("IfcPropertySingleValue", 111, Name="Value", NominalValue="1", Unit=101)
            add("IfcPropertySingleValue", 112, Name="Value", NominalValue="1", Unit=102)
            add(
                "IfcRelDefinesByProperties",
                113,
                RelatedObjects=[1],
                RelatingPropertyDefinition=110,
            )

        con, report = self.build_case(configure)
        row = con.execute(
            "SELECT Conflict_Value,Conflict_Value_Unit FROM View_BeamCustomPset WHERE ifc_id=1"
        ).fetchone()
        self.assertEqual(json.loads(row[0]), [1, 1])
        self.assertEqual(json.loads(row[1]), ["mm", "m"])
        self.assertIn("conflicting_property_values", {d["code"] for d in report["diagnostics"]})

    def test_empty_columns_and_unused_classes_are_absent(self):
        con, report = self.build_case(classes=["IfcWindow"])
        self.assertEqual(report["views"], {})
        con, report = self.build_case()
        self.assertNotIn("Description", report["views"]["View_Beam"]["columns"])
        self.assertNotIn("BaseQuantities_NominalLength", report["views"]["View_Beam"]["columns"])

    def test_standard_case_union_keeps_actual_class(self):
        def configure(add, con):
            add("IfcWall", 110, Name="Wall")
            add("IfcWallStandardCase", 111, Name="Standard wall")

        con, _ = self.build_case(configure)
        self.assertEqual(
            {r[0] for r in con.execute("SELECT ifc_class FROM View_Wall")},
            {"IfcWall", "IfcWallStandardCase"},
        )

    def test_native_labels_and_table_units(self):
        def configure(add, con):
            con.execute(
                "UPDATE IfcPropertySingleValue SET NominalValue=? WHERE ifc_id=45",
                (json.dumps({"type": "IfcLabel", "value": "007"}),),
            )
            values = json.dumps(
                [
                    {"type": "IfcLengthMeasure", "value": 10},
                    {"type": "IfcLengthMeasure", "value": 20},
                ]
            )
            con.execute("UPDATE IfcPropertyListValue SET ListValues=? WHERE ifc_id=46", (values,))
            con.execute(
                "UPDATE IfcPropertyTableValue SET DefiningValues=?,Expression=? WHERE ifc_id=50",
                (values, "true"),
            )

        con, _ = self.build_case(configure)
        row = dict(con.execute("SELECT * FROM View_BeamCustomPset WHERE ifc_id=1").fetchone())
        self.assertEqual(row["Company_NumberMatchingId"], "007")
        self.assertEqual(json.loads(row["Company_Sizes_mm"]), [10, 20])
        self.assertNotIn("Company_Sizes_Unit", row)
        table = json.loads(row["Company_Curve"])
        self.assertEqual(table["DefiningUnit"], "mm")
        self.assertEqual(table["Expression"], "true")

    def test_view_rows_do_not_contain_diagnostic_metadata(self):
        _, report = self.build_case()
        forbidden = {"confidence", "evidence", "source_path", "diagnostics", "reason"}
        for info in report["views"].values():
            self.assertFalse(forbidden & {c.lower() for c in info["columns"]})

    def test_input_output_safety(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        source, output = Path(folder.name) / "raw.sqlite", Path(folder.name) / "out.sqlite"
        fixture(source, "IFC4")
        with self.assertRaises(ValueError):
            build(source, source, overwrite=True)
        build(source, output)
        with self.assertRaises(FileExistsError):
            build(source, output)

    def test_invalid_sqlite_limit_reports_error_without_creating_table(self):
        writer = Writer(":memory:")
        self.addCleanup(writer.close)
        writer.con.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 20)
        with self.assertRaisesRegex(ValueError, "No columns were discarded"):
            writer.write("View_TooWide", [], [f"c{i}" for i in range(21)])
        self.assertFalse(
            writer.con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )


if __name__ == "__main__":
    unittest.main()
