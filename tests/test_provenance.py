import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ifcopenshell

from ifc_sql_views.catalog import Catalog
from ifc_sql_views.extractor import Extractor
from ifc_sql_views.reader import Database, quote


def provenance_fixture(path, configure=None):
    schema = ifcopenshell.schema_by_name("IFC4")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (schema TEXT)")
    con.execute("INSERT INTO metadata VALUES ('IFC4')")
    created = set()

    def add(cls, iid, **values):
        names = [a.name() for a in schema.declaration_by_name(cls).all_attributes()]
        if cls not in created:
            con.execute(
                f"CREATE TABLE {quote(cls)} (ifc_id INTEGER PRIMARY KEY, "
                + ",".join(quote(n) for n in names)
                + ")"
            )
            created.add(cls)
        row = [iid] + [
            json.dumps(values.get(n)) if isinstance(values.get(n), (dict, list)) else values.get(n)
            for n in names
        ]
        con.execute(f"INSERT INTO {quote(cls)} VALUES (" + ",".join("?" for _ in row) + ")", row)

    add("IfcDoor", 1, GlobalId="door-1", Name="Door 1", OverallWidth=900)
    add("IfcDoor", 2, GlobalId="door-2", Name="Door 2", OverallWidth=900)
    add(
        "IfcDoorType",
        3,
        GlobalId="door-type",
        Name="Shared",
        HasPropertySets=[10],
        PredefinedType="DOOR",
        OperationType="SINGLE_SWING_LEFT",
        ParameterTakesPrecedence=False,
    )
    add("IfcRelDefinesByType", 4, RelatedObjects=[1, 2], RelatingType=3)
    add("IfcPropertySet", 10, Name="Pset_DoorCommon", HasProperties=[11, 12])
    add(
        "IfcPropertySingleValue",
        11,
        Name="FireRating",
        NominalValue={"type": "IfcLabel", "value": "EI60"},
    )
    add(
        "IfcPropertySingleValue",
        12,
        Name="IsExternal",
        NominalValue={"type": "IfcBoolean", "value": False},
    )
    add("IfcSIUnit", 20, UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE")
    add("IfcUnitAssignment", 21, Units=[20])
    add("IfcProject", 22, GlobalId="project", UnitsInContext=21)
    add("IfcElementQuantity", 30, Name="BaseQuantities", Quantities=[31])
    add("IfcQuantityLength", 31, Name="Width", LengthValue=900, Unit=20)
    add("IfcRelDefinesByProperties", 32, RelatedObjects=[1, 2], RelatingPropertyDefinition=30)
    add("IfcBuildingStorey", 50, GlobalId="storey", Name="Level 1")
    add("IfcRelContainedInSpatialStructure", 51, RelatedElements=[1, 2], RelatingStructure=50)
    if configure:
        configure(add, con)
    con.commit()
    con.close()


class ProvenanceTest(unittest.TestCase):
    def extractor(self, configure=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        source = Path(folder.name) / "raw.sqlite"
        provenance_fixture(source, configure)
        db = Database(source)
        extractor = Extractor(db, Catalog(db))

        def close():
            for obj in (extractor, db):
                for name in dir(type(obj)):
                    method = getattr(type(obj), name, None)
                    if hasattr(method, "cache_clear"):
                        method.cache_clear()
            db.close()

        self.addCleanup(close)
        return extractor

    def test_shared_type_properties_and_quantity_aliases_have_identical_sources(self):
        extractor = self.extractor()
        for iid in (1, 2):
            row, provenance = extractor.row_with_provenance(iid)
            self.assertEqual(row, extractor.row(iid))
            self.assertEqual(
                provenance["Pset_DoorCommon_FireRating"],
                provenance["Type_Pset_DoorCommon_FireRating"],
            )
            self.assertEqual(
                provenance["Pset_DoorCommon_IsExternal"],
                provenance["Type_Pset_DoorCommon_IsExternal"],
            )
            self.assertEqual(
                provenance["BaseQuantities_Width"], provenance["Qto_DoorBaseQuantities_Width"]
            )
            self.assertEqual(
                provenance["BaseQuantities_Width_Unit"],
                provenance["Qto_DoorBaseQuantities_Width_Unit"],
            )
            self.assertIn("IfcLabel", repr(provenance["Pset_DoorCommon_FireRating"]))
            self.assertIn("('explicit', 20)", repr(provenance["BaseQuantities_Width"]))
            self.assertTrue(all(isinstance(hash(token), int) for token in provenance.values()))
            self.assertFalse(any("_source" in key or "provenance" in key for key in row))

    def test_equal_occurrence_override_is_a_different_original_property(self):
        def configure(add, con):
            add("IfcPropertySet", 40, Name="Pset_DoorCommon", HasProperties=[41])
            add(
                "IfcPropertySingleValue",
                41,
                Name="FireRating",
                NominalValue={"type": "IfcLabel", "value": "EI60"},
            )
            add("IfcRelDefinesByProperties", 42, RelatedObjects=[1], RelatingPropertyDefinition=40)

        extractor = self.extractor(configure)
        first, sources = extractor.row_with_provenance(1)
        self.assertEqual(
            first["Pset_DoorCommon_FireRating"], first["Type_Pset_DoorCommon_FireRating"]
        )
        self.assertNotEqual(
            sources["Pset_DoorCommon_FireRating"], sources["Type_Pset_DoorCommon_FireRating"]
        )
        _, second_sources = extractor.row_with_provenance(2)
        self.assertEqual(
            second_sources["Pset_DoorCommon_FireRating"],
            second_sources["Type_Pset_DoorCommon_FireRating"],
        )

    def test_shared_type_provenance_is_cached_without_losing_contributors(self):
        extractor = self.extractor()
        with patch.object(extractor, "flat_properties", wraps=extractor.flat_properties) as flat:
            _, first = extractor.row_with_provenance(1)
            first_items = extractor._flat_property_provenance(3)
            _, second = extractor.row_with_provenance(2)
            second_items = extractor._flat_property_provenance(3)
            provenance_calls = [
                call
                for call in flat.call_args_list
                if call.args[0] == 3 and call.kwargs.get("provenance") is not None
            ]
            self.assertEqual(len(provenance_calls), 1)
        self.assertIs(first_items, second_items)
        self.assertIsInstance(first_items, tuple)
        self.assertEqual(
            first["Type_Pset_DoorCommon_FireRating"], second["Type_Pset_DoorCommon_FireRating"]
        )
        self.assertGreaterEqual(extractor._flat_property_provenance.cache_info().hits, 3)

    def test_same_entity_with_different_relation_meanings_cannot_merge(self):
        extractor = self.extractor()
        row, sources = extractor.row_with_provenance(1)
        self.assertEqual(row["Container_Name"], row["Storey_Name"])
        self.assertNotEqual(sources["Container_Name"], sources["Storey_Name"])
        self.assertNotEqual(sources["Name"], sources["Type_Name"])

    def test_same_property_reused_in_different_sets_has_different_meaning(self):
        def configure(add, con):
            add("IfcPropertySet", 40, Name="Alternate", HasProperties=[11])
            add(
                "IfcRelDefinesByProperties",
                42,
                RelatedObjects=[1, 2],
                RelatingPropertyDefinition=40,
            )

        extractor = self.extractor(configure)
        row, sources = extractor.row_with_provenance(1)
        self.assertEqual(row["Custom_Alternate_FireRating"], row["Pset_DoorCommon_FireRating"])
        self.assertNotEqual(
            sources["Custom_Alternate_FireRating"], sources["Pset_DoorCommon_FireRating"]
        )

    def test_collapsed_values_retain_every_original_contributor(self):
        def configure(add, con):
            add("IfcPropertySet", 40, Name="Repeated", HasProperties=[41, 43])
            add(
                "IfcPropertySingleValue",
                41,
                Name="Flag",
                NominalValue={"type": "IfcBoolean", "value": False},
            )
            add(
                "IfcPropertySingleValue",
                43,
                Name="Flag",
                NominalValue={"type": "IfcBoolean", "value": False},
            )
            add("IfcRelDefinesByProperties", 42, RelatedObjects=[1], RelatingPropertyDefinition=40)

        row, sources = self.extractor(configure).row_with_provenance(1)
        self.assertIs(row["Custom_Repeated_Flag"], False)
        self.assertEqual(len(sources["Custom_Repeated_Flag"][1]), 2)
        self.assertNotEqual(
            sources["Custom_Repeated_Flag"][1][0], sources["Custom_Repeated_Flag"][1][1]
        )

    def test_unproven_resource_expansion_has_no_token(self):
        def configure(add, con):
            add("IfcCartesianPoint", 60, Coordinates=[0, 0, 0])
            add("IfcAxis2Placement3D", 61, Location=60)
            add("IfcLocalPlacement", 62, RelativePlacement=61)
            con.execute("UPDATE IfcDoor SET ObjectPlacement=62 WHERE ifc_id=1")

        row, sources = self.extractor(configure).row_with_provenance(1)
        self.assertIn("ObjectPlacement_RelativePlacement", row)
        self.assertNotIn("ObjectPlacement_RelativePlacement", sources)
        self.assertIn("ObjectPlacement_id", sources)


if __name__ == "__main__":
    unittest.main()
