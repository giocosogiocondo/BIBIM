"""Regression checks for new question IDs and live-only schema previews."""
import unittest

from experiments.text_to_sql_top3.runner import choose_cases, preview_ranking
from experiments.text_to_sql_top3.custom_input import prepare_custom


class CustomSQLInputTest(unittest.TestCase):
    def test_domain_ids_can_be_selected_without_numeric_conversion(self):
        cases = [{"id": key, "run_eligible": True} for key in ("R01", "D02", "B01")]
        config = {"input_mode": "custom_live", "seed": 7}
        self.assertEqual([c["id"] for c in choose_cases(cases, config, ids=["R01", "B01"])],
                         ["B01", "R01"])
        self.assertEqual(len(choose_cases(cases, config, limit=1)), 1)
        with self.assertRaises(ValueError):
            choose_cases(cases, config, ids=["missing"])

    def test_preview_does_not_pretend_to_have_a_saved_route(self):
        case = {"id": "B01"}
        inventory = {"groups": ["View_Bridge", "View_Beam"]}
        self.assertEqual(preview_ranking(case, inventory, {"routing_mode": "live"}),
                         ["View_Beam", "View_Bridge"])
        self.assertNotIn("saved_route", case)
        with self.assertRaises(ValueError):
            preview_ranking(case, inventory, {"routing_mode": "saved"})

    def test_custom_prepare_rejects_saved_routing_before_reading_inputs(self):
        with self.assertRaisesRegex(ValueError, "live routing"):
            prepare_custom({"routing_mode": "saved"})


if __name__ == "__main__":
    unittest.main()
