import unittest
from ifc_sql_views.column_policy import compact_entity_columns, is_empty


class EntityPolicyTest(unittest.TestCase):
    def test_zero_and_false_are_information(self):
        self.assertFalse(is_empty(0))
        self.assertFalse(is_empty(False))
        self.assertFalse(is_empty([None, 0]))
        self.assertFalse(is_empty({"Flag": False}))

    def test_blank_text_and_nested_null_containers_are_empty(self):
        for value in (None, "", "  ", [], {}, [None, ""], {"Value": None}):
            self.assertTrue(is_empty(value), repr(value))

    def test_identity_is_preserved_even_when_missing(self):
        columns, rows, _ = compact_entity_columns(
            [{"ifc_id": 1, "ifc_class": "IfcDoor", "GlobalId": None}],
            ["ifc_id", "ifc_class", "GlobalId"],
        )
        self.assertEqual(columns, ["ifc_id", "ifc_class", "GlobalId"])

    def test_custom_name_is_kept_when_sparse_but_removed_when_entirely_empty(self):
        rows = [{"ifc_id": i, "Name": None} for i in range(10)]
        rows[0]["Name"] = "Named object"
        columns, packed, _ = compact_entity_columns(rows, ["ifc_id", "Name"], protected=("Name",))
        self.assertEqual(columns, ["ifc_id", "Name"])
        self.assertEqual(packed[0]["Name"], "Named object")
        rows[0]["Name"] = None
        columns, _, _ = compact_entity_columns(rows, ["ifc_id", "Name"], protected=("Name",))
        self.assertEqual(columns, ["ifc_id"])

    def test_value_and_unit_move_together_on_overflow(self):
        rows = [
            {
                "ifc_id": 1,
                "ifc_class": "IfcBeam",
                "GlobalId": "g",
                "Width": 3,
                "Width_Unit": "mm",
                "Name": "B",
            },
            {
                "ifc_id": 2,
                "ifc_class": "IfcBeam",
                "GlobalId": "h",
                "Width": 4,
                "Width_Unit": "m",
                "Name": "C",
            },
        ]
        columns, packed, _ = compact_entity_columns(rows, list(rows[0]), limit=5)
        self.assertLessEqual(len(columns), 5)
        restored = {**packed[0], **(packed[0].get("etc") or {})}
        self.assertEqual(restored["Width"], 3)
        self.assertEqual(restored["Width_Unit"], "mm")
        self.assertEqual("Width" in columns, "Width_Unit" in columns)

    def test_uniform_degree_is_in_name_and_numeric_values_are_unchanged(self):
        base = "ArchiCADProperties_Column Slant Angle"
        rows = [
            {"ifc_id": 1, base: 0, base + "_Unit": "Degree"},
            {"ifc_id": 2, base: 12.5, base + "_Unit": "Degree"},
            {"ifc_id": 3},
        ]
        columns, packed, policy = compact_entity_columns(rows, list(rows[0]))
        self.assertEqual(columns, ["ifc_id", base + "_Degree"])
        self.assertEqual([r.get(base + "_Degree") for r in packed], [0, 12.5, None])
        self.assertEqual(policy["uniform_units"][base]["unit"], "Degree")
        self.assertEqual(rows[0][base + "_Unit"], "Degree")

    def test_mixed_or_unknown_units_are_not_folded(self):
        for second in ("m", None, "", ["mm", None], ["mm", "m"]):
            with self.subTest(unit=second):
                rows = [
                    {"ifc_id": 1, "Width": 1, "Width_Unit": "mm"},
                    {"ifc_id": 2, "Width": 2, "Width_Unit": second},
                ]
                columns, packed, policy = compact_entity_columns(rows, list(rows[0]))
                self.assertIn("Width_Unit", columns)
                self.assertEqual(policy["uniform_units"], {})
                self.assertEqual(packed[1]["Width_Unit"], second)

    def test_uniform_unit_arrays_fold_without_changing_value_arrays(self):
        rows = [{"ifc_id": 1, "Width": [1, 2], "Width_Unit": ["mm", "mm"]}]
        columns, packed, _ = compact_entity_columns(rows, list(rows[0]))
        self.assertEqual(columns, ["ifc_id", "Width_mm"])
        self.assertEqual(packed[0]["Width_mm"], [1, 2])

    def test_unit_name_collision_preserves_both_fields(self):
        rows = [{"ifc_id": 1, "Width": 3, "Width_Unit": "mm", "WIDTH_MM": "original"}]
        columns, packed, policy = compact_entity_columns(rows, list(rows[0]))
        renamed = policy["uniform_units"]["Width"]["column"]
        self.assertTrue(renamed.startswith("Width_mm_"))
        self.assertEqual(packed[0][renamed], 3)
        self.assertEqual(packed[0]["WIDTH_MM"], "original")
        self.assertEqual(len(columns), len({c.casefold() for c in columns}))

    def test_sparse_uniform_measurement_is_preserved_in_etc_with_unit_name(self):
        rows = [{"ifc_id": i} for i in range(10)]
        rows[0].update(Width=0, Width_Unit="mm")
        columns, packed, policy = compact_entity_columns(rows, ["ifc_id", "Width", "Width_Unit"])
        self.assertEqual(columns, ["ifc_id", "etc"])
        self.assertEqual(packed[0]["etc"], {"Width_mm": 0})
        self.assertEqual(policy["etc_columns"], ["Width_mm"])

    def test_constant_values_are_not_moved_just_because_they_repeat(self):
        rows = [{"ifc_id": i, "Setting": False, "Width": 3, "Width_Unit": "mm"} for i in range(10)]
        columns, packed, _ = compact_entity_columns(rows, list(rows[0]))
        self.assertEqual(columns, ["ifc_id", "Setting", "Width_mm"])
        self.assertNotIn("etc", packed[0])

    def test_unit_folding_happens_before_sqlite_overflow(self):
        rows = [
            {
                "ifc_id": 1,
                "ifc_class": "IfcBeam",
                "GlobalId": "g",
                "Width": 3,
                "Width_Unit": "mm",
                "Name": "B",
            }
        ]
        columns, packed, _ = compact_entity_columns(rows, list(rows[0]), limit=5)
        self.assertEqual(len(columns), 5)
        self.assertNotIn("etc", columns)
        self.assertEqual(packed[0]["Width_mm"], 3)

    def test_too_small_limit_fails_without_discarding_identity(self):
        with self.assertRaises(ValueError):
            compact_entity_columns(
                [{"ifc_id": 1, "ifc_class": "IfcBeam", "GlobalId": "g"}],
                ["ifc_id", "ifc_class", "GlobalId"],
                limit=3,
            )

    def test_does_not_mutate_input_rows(self):
        rows = [{"ifc_id": i, "Rare": None} for i in range(10)]
        rows[0]["Rare"] = False
        _, packed, _ = compact_entity_columns(rows, ["ifc_id", "Rare"])
        self.assertIn("Rare", rows[0])
        self.assertNotIn("etc", rows[0])
        self.assertEqual(packed[0]["etc"]["Rare"], False)

    def test_empty_measurement_does_not_leave_a_phantom_unit(self):
        columns, _, _ = compact_entity_columns(
            [{"ifc_id": 1, "Width": None, "Width_Unit": "mm"}], ["ifc_id", "Width", "Width_Unit"]
        )
        self.assertEqual(columns, ["ifc_id"])

    def test_overflow_selection_is_independent_of_column_order(self):
        rows = [{"ifc_id": 1, **{f"f{i}": i for i in range(50)}}]
        a, _, _ = compact_entity_columns(rows, list(rows[0]), limit=10)
        b, _, _ = compact_entity_columns(rows, list(reversed(rows[0])), limit=10)
        self.assertEqual(set(a), set(b))


if __name__ == "__main__":
    unittest.main()
