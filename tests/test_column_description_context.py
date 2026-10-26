"""Dictionary-to-actual-model-request integration using synthetic DBs and fake clients."""

import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_top3_text_to_sql as base_tests
from experiments.text_to_sql_top3.descriptions import load_descriptions
from experiments.text_to_sql_top3.generation import SQLGenerator, messages
from experiments.text_to_sql_top3.profiles import load_profiles
from experiments.text_to_sql_top3.runner import plan, process_case, run
from experiments.text_to_sql_top3.schema import load_config
from experiments.text_to_sql_top3.workflow import retrieve_and_answer
from experiments.view_selection_ablation.common import file_pin, write_json


class ColumnDescriptionContextTest(unittest.TestCase):
    def setUp(self):
        self.fixture = base_tests.SQLBaselineTest(
            methodName="test_companion_expansion_and_database_membership"
        )
        self.fixture.setUp()
        self.fixture.inventory["schema"] = "IFC4"
        self.addCleanup(self.fixture.doCleanups)
        self.directory = Path(__file__).resolve().parents[1] / "docs/column_descriptions"
        self.config = {
            **self.fixture.config,
            "column_context": "statistics",
            "column_descriptions": True,
            "column_dictionary_directory": str(self.directory),
            "statistics_directory": str(self.fixture.root / "profiles"),
        }
        write_json(
            self.fixture.build,
            {
                "schema": "IFC4",
                "views": {
                    v: {"columns": cols, "column_policy": {}}
                    for v, cols in self.fixture.schemas.items()
                },
            },
        )
        self.fixture.inventory["build"] = file_pin(self.fixture.build, full_hash=True)
        self.args = (
            self.config,
            {"p/arc": self.fixture.inventory},
            {"p/arc": self.fixture.schemas},
            [self.fixture.case],
        )

    def contexts(self):
        descriptions, identity = load_descriptions(*self.args)
        profiles, _ = load_profiles(*self.args, log=lambda _: None)
        return profiles["p/arc"], descriptions["p/arc"], identity

    def test_current_default_uses_statistics_without_dictionary(self):
        config = load_config()
        self.assertFalse(config['column_descriptions'])
        self.assertEqual(config['column_context'], 'statistics')

    def test_loader_preserves_unknown_custom_meaning_and_actual_field_scope(self):
        _, descriptions, identity = self.contexts()
        self.assertIn("Name", descriptions["View_Door"])
        self.assertNotIn("Custom_Width", descriptions["View_DoorCustomPset"])
        self.assertNotIn("Pressure Drop", descriptions["View_Door"])
        self.assertIn("fingerprint", identity)
        text = json.dumps(descriptions)
        for secret in ("SECRET_KEY", "SECRET_ANSWER", "SECRET_DESCRIPTION", "Door A"):
            self.assertNotIn(secret, text)

    def test_actual_model_request_matches_saved_messages_and_retains_statistics(self):
        profiles, descriptions, _ = self.contexts()
        client = Mock()
        client.request.side_effect = [
            {"prompt": "APPLIED"},
            {"tokens": [1, 2, 3]},
            {
                "content": "SELECT COUNT(*) FROM View_Door;",
                "tokens_predicted": 8,
                "stop_type": "eos",
            },
        ]
        record = process_case(
            self.fixture.case,
            self.config,
            self.fixture.inventory,
            self.fixture.schemas,
            SQLGenerator(client, self.config),
            None,
            None,
            profiles=profiles,
            descriptions=descriptions,
        )
        sent = client.request.call_args_list[0].args[1]["messages"]
        self.assertEqual(record["messages"], sent)
        self.assertEqual(record["status"], "ok")
        text = json.dumps(sent)
        self.assertIn("Column descriptions (English)", text)
        self.assertIn(descriptions["View_Door"]["Name"], text)
        self.assertIn("Door A", text)
        self.assertNotIn("No column descriptions", sent[0]["content"])
        self.assertNotIn("SECRET_ANSWER", text)
        self.assertNotIn("SECRET_KEY", text)

    def test_expansion_adds_only_the_selected_views_descriptions(self):
        profiles, descriptions, _ = self.contexts()
        descriptions["View_Door"]["Name"] = "DOOR_ONLY_DEFINITION"
        config = {**self.config, "answer_stage": True, "top_k": 1, "retrieval_limits": [1, 3]}
        generator = Mock()
        generator.generate.return_value = self.fixture.generation()
        answerer = Mock()
        answerer.generate.side_effect = [
            {"decision": {"answerable": False, "answer": "", "reason": "Need doors"}},
            {"decision": {"answerable": True, "answer": "2 doors", "reason": ""}},
        ]
        result = retrieve_and_answer(
            "Count doors",
            ["View_Space", "View_Wall", "View_Door"],
            config,
            self.fixture.inventory,
            self.fixture.schemas,
            generator,
            answerer,
            profiles,
            descriptions,
        )
        self.assertNotIn("DOOR_ONLY_DEFINITION", json.dumps(result["attempts"][0]["messages"]))
        self.assertIn("DOOR_ONLY_DEFINITION", json.dumps(result["attempts"][1]["messages"]))
        self.assertIs(generator.generate.call_args.kwargs["descriptions"], descriptions)

    def test_disabled_mode_has_no_dictionary_read_and_is_the_original_prompt(self):
        with patch("experiments.text_to_sql_top3.descriptions.ColumnDictionary") as dictionary:
            result = load_descriptions(
                {**self.config, "column_descriptions": False}, *self.args[1:]
            )
            dictionary.assert_not_called()
        self.assertEqual(result, ({}, {}))
        prompt = messages("Count doors", ["View_Door"], self.fixture.schemas)
        self.assertNotIn("Column descriptions (English)", json.dumps(prompt))

    def test_enabled_mode_never_silently_runs_without_descriptions(self):
        profiles, _, _ = self.contexts()
        generator = Mock()
        record = process_case(
            self.fixture.case,
            self.config,
            self.fixture.inventory,
            self.fixture.schemas,
            generator,
            None,
            None,
            profiles=profiles,
        )
        self.assertEqual(record["status"], "generation_error")
        self.assertIn("descriptions are required", record["error"])
        generator.generate.assert_not_called()
        client = Mock()
        with self.assertRaisesRegex(ValueError, "descriptions are required"):
            SQLGenerator(client, self.config).generate(
                "Question", ["View_Door"], self.fixture.schemas
            )
        client.request.assert_not_called()

    def test_dry_plan_provides_dictionary_without_starting_server(self):
        self.fixture.prepare_fixture()
        with patch("experiments.text_to_sql_top3.runner.ManagedServer") as server:
            result = plan(self.config)
            server.assert_not_called()
        self.assertTrue(result["column_descriptions"])
        self.assertTrue(result["column_dictionary"]["fingerprint"])
        self.assertIn("Column descriptions (English)", json.dumps(result["example_messages"]))

    def test_corrupt_dictionary_fails_before_model_startup(self):
        self.fixture.prepare_fixture()
        folder = self.fixture.root / "dictionary"
        folder.mkdir()
        for name in (
            "validation.json",
            "source_manifest.json",
            "derived_columns.en.json",
            "IFC4.en.json",
        ):
            shutil.copyfile(self.directory / name, folder / name)
        with (folder / "IFC4.en.json").open("a", encoding="utf-8") as stream:
            stream.write("\n")
        config = {**self.config, "column_dictionary_directory": str(folder)}
        with patch("experiments.text_to_sql_top3.runner.ManagedServer") as server:
            with self.assertRaisesRegex(ValueError, "dictionary differs"):
                run(config)
            server.assert_not_called()

    def test_run_pins_dictionary_and_rejects_changed_dictionary_on_resume(self):
        self.fixture.prepare_fixture()
        _, descriptions, identity = self.contexts()
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
            result = run(self.config)
        self.assertTrue(result["complete"])
        output = next(Path(self.config["results_directory"]).iterdir())
        manifest = json.loads((output / "run_manifest.json").read_text())
        self.assertEqual(manifest["column_dictionary"]["fingerprint"], identity["fingerprint"])
        self.assertIsNotNone(generator.generate.call_args.kwargs["descriptions"])
        with (
            patch(
                "experiments.text_to_sql_top3.runner.load_descriptions",
                return_value=({"p/arc": descriptions}, {**identity, "fingerprint": "changed"}),
            ),
            patch("experiments.text_to_sql_top3.runner.ManagedServer") as server,
        ):
            with self.assertRaisesRegex(ValueError, "Cannot resume"):
                run(self.config, resume=output)
            server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
