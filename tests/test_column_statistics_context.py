"""Exact profiles, cache validity and prompt propagation; no real model inference."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_top3_text_to_sql as base_tests
from experiments.text_to_sql_top3.generation import SQLGenerator, messages
from experiments.text_to_sql_top3.profiles import load_profiles, profile_view, render_statistics
from experiments.text_to_sql_top3.runner import plan, process_case, run
from experiments.text_to_sql_top3.schema import column_inventory
from experiments.text_to_sql_top3.workflow import retrieve_and_answer
from experiments.view_selection_ablation.common import file_pin


class ColumnStatisticsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = base_tests.SQLBaselineTest(
            methodName="test_companion_expansion_and_database_membership"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = {
            **self.fixture.config,
            "column_context": "statistics",
            "statistics_directory": str(self.fixture.root / "profiles"),
        }

    def profiles(self):
        return load_profiles(
            self.config,
            {"p/arc": self.fixture.inventory},
            {"p/arc": self.fixture.schemas},
            [self.fixture.case],
            log=lambda _: None,
        )

    def make_values_view(self):
        with closing(sqlite3.connect(self.fixture.db)) as con:
            con.execute(
                "CREATE TABLE values_fixture(ifc_id INTEGER, GlobalId TEXT, flag, nullable TEXT, etc TEXT, long_text TEXT)"
            )
            rows = [
                (i, "ID" + str(i), value, None, '{"HIDDEN_JSON_KEY":1}', "x" * 300)
                for i, value in enumerate([0, 0, 0, 1, 1, "false", 0.0, None], 1)
            ]
            con.executemany("INSERT INTO values_fixture VALUES (?,?,?,?,?,?)", rows)
            con.execute("CREATE VIEW View_Values AS SELECT * FROM values_fixture")
            con.execute("CREATE VIEW View_Empty AS SELECT * FROM values_fixture WHERE 0")
            con.commit()
        inventory = {
            **self.fixture.inventory,
            "database": file_pin(self.fixture.db, full_hash=True),
            "physical_views": ["View_Values", "View_Empty"],
        }
        return inventory, column_inventory(inventory)

    def test_actual_storage_types_nulls_top3_and_counts_are_exact(self):
        inventory, schemas = self.make_values_view()
        profile = profile_view(inventory, "View_Values", schemas["View_Values"])
        self.assertEqual(profile["row_count"], 8)
        flag = profile["columns"]["flag"]
        self.assertEqual(flag["data_type"], "INTEGER|REAL|TEXT")
        self.assertEqual(flag["null_count"], 1)
        self.assertEqual(
            flag["top_values"],
            [{"count": 3, "value": 0}, {"count": 2, "value": 1}, {"count": 1, "value": 0.0}],
        )
        self.assertEqual(profile["columns"]["nullable"]["data_type"], "UNKNOWN")
        self.assertEqual(profile["columns"]["nullable"]["null_count"], 8)

    def test_spilled_and_memory_frequencies_produce_identical_results(self):
        inventory, schemas = self.make_values_view()
        regular = profile_view(inventory, "View_Values", schemas["View_Values"])
        spilled = profile_view(inventory, "View_Values", schemas["View_Values"], spill_entries=1)
        self.assertEqual(regular, spilled)

    def test_identifier_and_etc_values_are_not_exposed_and_long_values_are_previews(self):
        inventory, schemas = self.make_values_view()
        profile = profile_view(inventory, "View_Values", schemas["View_Values"])
        text = render_statistics("View_Values", schemas["View_Values"], profile)
        for name in ("ifc_id", "GlobalId", "etc"):
            self.assertEqual(profile["columns"][name]["top_values"], [])
        self.assertNotIn("HIDDEN_JSON_KEY", text)
        long = profile["columns"]["long_text"]["top_values"][0]
        self.assertTrue(long["truncated"])
        self.assertEqual(long["count"], 8)
        self.assertNotIn("value", long)
        self.assertEqual(len(long["preview"]), 160)

    def test_empty_view_reports_zero_rows_and_zero_nulls(self):
        inventory, schemas = self.make_values_view()
        profile = profile_view(inventory, "View_Empty", schemas["View_Empty"])
        self.assertEqual(profile["row_count"], 0)
        self.assertTrue(
            all(c["null_count"] == 0 and not c["top_values"] for c in profile["columns"].values())
        )

    def test_schema_mismatch_and_outside_view_are_rejected(self):
        with self.assertRaises(ValueError):
            profile_view(self.fixture.inventory, "View_Door", ["invented"])
        with self.assertRaises(ValueError):
            profile_view(self.fixture.inventory, "IfcDoor", ["step_id"])

    def test_statistics_messages_contain_only_selected_view_values_and_no_gold_or_json_keys(self):
        profiles, _ = self.profiles()
        chat = messages("Count doors", ["View_Door"], self.fixture.schemas, profiles["p/arc"])
        text = json.dumps(chat)
        self.assertIn("Door A", text)
        self.assertIn("rows=2", text)
        self.assertEqual(chat[1]["content"].count("rows="), 1)
        self.assertIn("REAL", text)
        for value in (
            "Room A",
            "Custom_Width",
            "SECRET_KEY",
            "SECRET_DESCRIPTION",
            "SECRET_ANSWER",
        ):
            self.assertNotIn(value, text)
        self.assertIn("not evidence of absence", text)

    def test_profile_cache_is_reused_and_fingerprints_are_stable(self):
        first, pins = self.profiles()
        with patch("experiments.text_to_sql_top3.profiles.profile_view") as scan:
            second, other_pins = self.profiles()
            scan.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(pins, other_pins)
        self.assertEqual(file_pin(self.fixture.db, full_hash=True), self.fixture.pin)

    def test_tampered_cache_and_changed_db_pin_are_rejected(self):
        self.profiles()
        path = next(Path(self.config["statistics_directory"]).glob("*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["profile"]["row_count"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.profiles()
        with closing(sqlite3.connect(self.fixture.db)) as con:
            con.execute("INSERT INTO _sv_Door VALUES (9,'Changed',9,NULL)")
            con.commit()
        with self.assertRaises(ValueError):
            self.profiles()

    def test_sql_generation_uses_the_same_statistics_as_the_saved_prompt(self):
        profiles, _ = self.profiles()
        client = Mock()
        client.request.side_effect = [
            {"prompt": "APPLIED"},
            {"tokens": [1]},
            {"content": "SELECT COUNT(*) FROM View_Door", "tokens_predicted": 5},
        ]
        generator = SQLGenerator(client, self.config)
        generator.generate("Count doors", ["View_Door"], self.fixture.schemas, profiles["p/arc"])
        chat = client.request.call_args_list[0].args[1]["messages"]
        self.assertEqual(
            chat, messages("Count doors", ["View_Door"], self.fixture.schemas, profiles["p/arc"])
        )

    def test_missing_statistics_do_not_silently_fall_back_to_names(self):
        generator = Mock()
        record = process_case(
            self.fixture.case,
            self.config,
            self.fixture.inventory,
            self.fixture.schemas,
            generator,
            None,
            None,
        )
        generator.generate.assert_not_called()
        self.assertEqual(record["status"], "generation_error")

    def test_plan_builds_profiles_without_loading_a_model(self):
        self.fixture.prepare_fixture()
        with patch("experiments.text_to_sql_top3.runner.ManagedServer") as server:
            result = plan(self.config)
            server.assert_not_called()
        self.assertEqual(result["column_context"], "statistics")
        self.assertEqual(result["profile_views"], 4)
        self.assertIn("Door A", json.dumps(result["example_messages"]))

    def test_top1_plan_ignores_old_frozen_top3_selection_and_counts_distinct_rounds(self):
        self.fixture.prepare_fixture()
        config = {**self.config, "answer_stage": True, "top_k": 1, "retrieval_limits": [1, 3, 6]}
        with patch("experiments.text_to_sql_top3.runner.ManagedServer") as server:
            result = plan(config)
            server.assert_not_called()
        self.assertEqual(result["initial_top_k"], 1)
        self.assertEqual(result["maximum_saved_route_prompt"]["selected_groups"], ["View_Door"])
        self.assertNotIn("Room A", json.dumps(result["example_messages"]))
        self.assertEqual(result["sql_generation_requests_max"], 2)
        self.assertEqual(result["profile_views"], 4)

    def test_expansion_includes_profiles_for_newly_retrieved_views(self):
        inventory = self.fixture.inventory
        ranking = ["View_Space", "View_Wall", "View_Door"]
        profiles, _ = self.profiles()
        generator = Mock()
        generator.generate.return_value = self.fixture.generation()
        answerer = Mock()
        answerer.generate.side_effect = [
            {"decision": {"answerable": False, "answer": "", "reason": "Need doors"}},
            {"decision": {"answerable": True, "answer": "2 doors", "reason": ""}},
        ]
        # Smaller cuts isolate context propagation without additional fake entities.
        config = {**self.config, "retrieval_limits": [1, 3]}
        result = retrieve_and_answer(
            "Count doors",
            ranking,
            config,
            inventory,
            self.fixture.schemas,
            generator,
            answerer,
            profiles["p/arc"],
        )
        self.assertNotIn("Door A", json.dumps(result["attempts"][0]["messages"]))
        self.assertIn("Door A", json.dumps(result["attempts"][1]["messages"]))
        self.assertEqual(generator.generate.call_args.kwargs["profiles"], profiles["p/arc"])

    def test_mocked_run_saves_profile_identity_and_exact_context(self):
        self.fixture.prepare_fixture()
        generator = Mock()
        generator.generate.return_value = self.fixture.generation()
        server = Mock()
        server.__enter__ = Mock(return_value=SimpleNamespace(command=["MOCKED"]))
        server.__exit__ = Mock(return_value=False)
        with (
            patch("experiments.text_to_sql_top3.runner.ManagedServer", return_value=server),
            patch("experiments.text_to_sql_top3.runner.HTTP") as http,
            patch("experiments.text_to_sql_top3.runner.SQLGenerator", return_value=generator),
            patch("experiments.text_to_sql_top3.runner.hardware_snapshot", return_value={}),
        ):
            http.return_value.request.return_value = {}
            summary = run(self.config)
        self.assertTrue(summary["complete"])
        output = next(Path(self.config["results_directory"]).iterdir())
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["column_profiles"]["p/arc"]), 4)
        record = json.loads((output / "records.jsonl").read_text(encoding="utf-8"))
        self.assertIn("Door A", json.dumps(record["messages"]))
        self.assertEqual(file_pin(self.fixture.db, full_hash=True), self.fixture.pin)


if __name__ == "__main__":
    unittest.main()
