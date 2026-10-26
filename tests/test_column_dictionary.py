import json
from pathlib import Path
import tempfile
import unittest

import ifcopenshell

from ifc_column_dictionary import ColumnDictionary
from ifc_column_dictionary.build import SCHEMAS, TARGET, clean


class ColumnDictionaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary = ColumnDictionary()

    def test_all_schema_attributes_have_same_version_definitions(self):
        for version in SCHEMAS:
            catalog = self.dictionary.catalog(version)
            schema = ifcopenshell.schema_by_name(version)
            self.assertEqual(set(catalog["entities"]), {e.name() for e in schema.entities()})
            for entity in schema.entities():
                fields = catalog["entities"][entity.name()]["attributes"]
                self.assertEqual(set(fields), {a.name() for a in entity.all_attributes()})
                for key in fields.values():
                    self.assertIn(key, catalog["attribute_definitions"])
                    entry = catalog["attribute_definitions"][key]
                    self.assertTrue(entry["description"])
                    if not entry["official_prose_available"]:
                        self.assertEqual(entry["description_method"], "schema_signature")

    def test_inherited_name_and_type_style_names(self):
        for version in SCHEMAS:
            item = self.dictionary.describe(version, "View_Beam", "Name")
            self.assertEqual(item["source_ids"], ["IfcRoot.Name"])
        for version, column, source in [
            ("IFC2X3", "Doorstyle_OperationType", "IfcDoorStyle.OperationType"),
            ("IFC4", "DoorType_OperationType", "IfcDoorType.OperationType"),
            ("IFC4X3_ADD2", "DoorType_OperationType", "IfcDoorType.OperationType"),
        ]:
            item = self.dictionary.describe(version, "View_Door", column)
            self.assertEqual(item["source_ids"], [source])
            self.assertIn("Assigned", item["description"])

    def test_standard_psets_beyond_common_and_quantity(self):
        for version in SCHEMAS:
            catalog = self.dictionary.catalog(version)
            for set_name, info in catalog["property_sets"].items():
                for prop, entry in info["properties"].items():
                    self.assertTrue(entry["description"], (version, set_name, prop))
            self.assertIn("Pset_DoorCommon", catalog["property_sets"])
            self.assertIn("Pset_ManufacturerTypeInformation", catalog["property_sets"])
        item = self.dictionary.describe("IFC4", "View_Door", "Pset_DoorCommon_FireRating")
        self.assertEqual(item["source_ids"], ["Pset_DoorCommon.FireRating"])
        item = self.dictionary.describe("IFC4", "View_Door", "Qto_DoorBaseQuantities_Width")
        self.assertEqual(item["source_ids"], ["Qto_DoorBaseQuantities.Width"])

    def test_uniform_unit_mapping_uses_manifest_and_preserves_values(self):
        policy = {
            "uniform_units": {
                "OverallHeight": {
                    "column": "OverallHeight_mm",
                    "unit_column": "OverallHeight_Unit",
                    "unit": "mm",
                }
            }
        }
        item = self.dictionary.describe("IFC4", "View_Door", "OverallHeight_mm", policy)
        self.assertEqual(item["source_ids"], ["IfcDoor.OverallHeight"])
        self.assertEqual(item["unit"], "mm")
        self.assertIn("not rescaled", item["description"])
        self.assertEqual(
            self.dictionary.describe("IFC4", "View_Door", "OverallHeight_mm")["status"], "unmapped"
        )

    def test_custom_semantics_never_borrow_official_property(self):
        for column in [
            "Vendor_FireRating",
            "Pset_DoorCommon_FireRating",
            "Unknown_Manufacturer",
            "new field 2027",
        ]:
            item = self.dictionary.describe("IFC4", "View_DoorCustomPset", column)
            self.assertEqual(item["status"], "custom")
            self.assertIsNone(item["description"])
            self.assertNotIn("source_ids", item)

    def test_derived_coordinates_are_not_raw_geographic_coordinates(self):
        item = self.dictionary.describe("IFC4", "View_Beam", "Placement_X")
        self.assertEqual(item["description_method"], "bibim_implementation_definition")
        self.assertIn("parent placements and rotations", item["description"])
        self.assertIn("without CRS", item["description"])
        item = self.dictionary.describe("IFC4", "View_Beam", "Placement_XAxis_Z")
        self.assertIn("Dimensionless", item["description"])

    def test_spatial_fields_keep_explicit_semantics(self):
        for column in ["Storey_Name", "ContainedSpace_Name", "BoundarySpaces"]:
            item = self.dictionary.describe("IFC4", "View_Wall", column)
            self.assertEqual(item["status"], "resolved")
            self.assertEqual(item["description_method"], "bibim_implementation_definition")
        self.assertIn(
            "not necessarily",
            self.dictionary.describe("IFC4", "View_Wall", "BoundarySpaces")["description"],
        )

    def test_nested_application_and_resource_paths(self):
        for version, field, source in [
            ("IFC2X3", "Id", "IfcOrganization.Id"),
            ("IFC4", "Identification", "IfcOrganization.Identification"),
        ]:
            item = self.dictionary.describe(
                version, "View_Beam", "Owner_Application_ApplicationDeveloper_" + field
            )
            self.assertEqual(item["source_ids"], [source])
        item = self.dictionary.describe(
            "IFC4", "View_Beam", "Representation_RectangleProfileDef_XDim"
        )
        self.assertEqual(item["source_ids"], ["IfcRectangleProfileDef.XDim"])

    def test_enum_qualified_schema_property_entity(self):
        item = self.dictionary.describe(
            "IFC2X3", "View_Door", "IfcDoorPanelProperties_LEFT_PanelDepth"
        )
        self.assertEqual(item["source_ids"], ["IfcDoorPanelProperties.PanelDepth"])
        item = self.dictionary.describe(
            "IFC2X3", "View_Door", "IfcDoorPanelProperties_FAKE_PanelDepth"
        )
        self.assertEqual(item["status"], "unmapped")

    def test_relationship_count_is_target_grain(self):
        item = self.dictionary.describe("IFC4", "View_System", "GroupMembers_Count")
        self.assertIn("distinct relationship target IDs", item["description"])
        self.assertIn("IfcRelAssignsToGroup", item["description"])

    def test_unlisted_inverse_endpoint_summary_is_schema_driven(self):
        item = self.dictionary.describe(
            "IFC4", "View_Beam", "HasAssignments_RelatingProcess_Count"
        )
        self.assertEqual(item["status"], "resolved")
        self.assertIn("distinct relationship target IDs", item["description"])
        item = self.dictionary.describe("IFC4", "View_Beam", "HasAssignments_FAKE_Count")
        self.assertEqual(item["status"], "unmapped")

    def test_abstract_type_prefix_and_ambiguous_type_resources(self):
        item = self.dictionary.describe("IFC4", "View_Beam", "TypeProduct_Owner_Name")
        self.assertEqual(item["status"], "resolved")
        self.assertIn("Assigned IfcTypeProduct", item["description"])
        item = self.dictionary.describe("IFC4", "View_Beam", "BeamType_Material_Name")
        self.assertEqual(item["status"], "ambiguous")

    def test_owned_material_definition_is_not_an_unrelated_class(self):
        item = self.dictionary.describe(
            "IFC4", "View_Wall", "Material_MaterialDefinitionRepresentation"
        )
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["source_ids"], ["@entity:IfcMaterialDefinitionRepresentation"])

    def test_ambiguous_resource_provenance_is_not_guessed(self):
        item = self.dictionary.describe("IFC4", "View_Wall", "Material_Name")
        self.assertEqual(item["status"], "ambiguous")
        self.assertIsNone(item["description"])
        self.assertGreater(len(item["source_ids"]), 1)

    def test_version_specific_entity_and_unknown_schema(self):
        self.assertEqual(
            self.dictionary.describe("IFC2X3", "View_Bearing", "Name")["status"], "unmapped"
        )
        self.assertEqual(
            self.dictionary.describe("IFC4X3", "View_Bearing", "Name")["status"], "resolved"
        )
        with self.assertRaises(ValueError):
            self.dictionary.describe("IFC4X2", "View_Door", "Name")

    def test_deletions_in_cached_markup_are_removed(self):
        self.assertEqual(clean("door ~~body of the~~ opening"), "door opening")

    def test_catalog_files_are_external_to_object_rows(self):
        self.assertTrue((TARGET / "source_manifest.json").is_file())
        self.assertTrue((TARGET / "derived_columns.en.json").is_file())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "copy.json"
            path.write_text(json.dumps(self.dictionary.describe("IFC4", "View_Door", "Name")))
            self.assertEqual(json.loads(path.read_text())["source_ids"], ["IfcRoot.Name"])


if __name__ == "__main__":
    unittest.main()
