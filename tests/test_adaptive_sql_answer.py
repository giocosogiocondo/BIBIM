"""Answer-controlled Top-1/Top-3/Top-6 tests with synthetic SQLite and mocked models."""

from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_top3_text_to_sql as base_tests
from experiments.text_to_sql_top3.answer import AnswerGenerator, answer_messages, parse_decision
from experiments.text_to_sql_top3.generation import SYSTEM
from experiments.text_to_sql_top3.runner import process_case, run
from experiments.text_to_sql_top3.schema import column_inventory, load_config, retrieval_schedule
from experiments.text_to_sql_top3.workflow import retrieve_and_answer


class AdaptiveSQLAnswerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = base_tests.SQLBaselineTest(
            methodName="test_companion_expansion_and_database_membership"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.config = {
            **self.fixture.config,
            "answer_stage": True,
            "top_k": 1,
            "retrieval_limits": [1, 3, 6],
            "answer_max_new_tokens": 32,
            "compare_reference_results": False,
        }
        self.ranking = [
            "View_Door",
            "View_Space",
            "View_Wall",
            "View_Beam",
            "View_Column",
            "View_Railing",
            "View_Window",
        ]
        with closing(sqlite3.connect(self.fixture.db)) as con:
            for cls in ("Beam", "Column", "Railing", "Window"):
                con.executescript(
                    f"CREATE TABLE _sv_{cls}(ifc_id INTEGER); INSERT INTO _sv_{cls} VALUES(9); "
                    f"CREATE VIEW View_{cls} AS SELECT * FROM _sv_{cls};"
                )
            con.commit()
        from experiments.view_selection_ablation.common import file_pin

        self.inventory = {
            **self.fixture.inventory,
            "database": file_pin(self.fixture.db, full_hash=True),
            "groups": self.ranking,
            "physical_views": self.fixture.inventory["physical_views"] + self.ranking[3:],
        }
        self.fixture.inventory = {**self.fixture.inventory, "database": self.inventory["database"]}
        self.schemas = column_inventory(self.inventory)
        self.generator = Mock()
        self.generator.generate.side_effect = lambda *args: self.fixture.generation()
        self.answerer = Mock()

    def verdict(self, answerable, answer="There are 2 doors.", reason=""):
        return {
            "decision": {
                "answerable": answerable,
                "answer": answer if answerable else "",
                "reason": reason if answerable else reason or "Need additional information.",
            },
            "error": None,
            "generation_ms": 2,
        }

    def perform(self):
        return retrieve_and_answer(
            "How many doors?",
            self.ranking,
            self.config,
            self.inventory,
            self.schemas,
            self.generator,
            self.answerer,
        )

    def test_sql_prompt_has_no_refusal_token_or_answerability_decision(self):
        self.assertNotIn("INSUFFICIENT_CONTEXT", SYSTEM)
        self.assertIn("Always generate a SQL query", SYSTEM)

    def test_answerable_top1_stops_without_retrieving_second_group(self):
        self.answerer.generate.return_value = self.verdict(True)
        result = self.perform()
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["selected_groups"], self.ranking[:1])
        self.assertEqual(result["selected_views"], ["View_Door", "View_DoorCustomPset"])
        self.generator.generate.assert_called_once()
        self.answerer.generate.assert_called_once()
        self.assertNotIn("View_Space", json.dumps(result["messages"]))

    def test_answer_model_failure_expands_cumulatively_from_top1_to_top3(self):
        self.answerer.generate.side_effect = [self.verdict(False), self.verdict(True)]
        result = self.perform()
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["selected_groups"], self.ranking[:3])
        self.assertEqual(result["attempts"][1]["newly_added_groups"], self.ranking[1:3])
        self.assertEqual([a["top_k"] for a in result["attempts"]], [1, 3])
        self.assertEqual(self.generator.generate.call_count, 2)
        self.assertIn("View_DoorCustomPset", self.generator.generate.call_args.args[1])
        self.assertNotIn("View_Window", self.generator.generate.call_args.args[1])
        self.assertEqual(result["final_answer"], "There are 2 doors.")

    def test_two_negative_decisions_expand_to_top6_preserving_the_previous_groups(self):
        self.answerer.generate.side_effect = [
            self.verdict(False),
            self.verdict(False),
            self.verdict(True),
        ]
        result = self.perform()
        self.assertEqual([a["top_k"] for a in result["attempts"]], [1, 3, 6])
        self.assertEqual(result["attempts"][2]["newly_added_groups"], self.ranking[3:6])
        self.assertEqual(result["selected_groups"], self.ranking[:6])
        self.assertEqual(self.generator.generate.call_count, 3)
        self.assertNotIn("View_Window", json.dumps(result["messages"]))

    def test_second_judge_receives_previous_sql_and_result_rows(self):
        seen = []

        def judge(question, attempts):
            seen.append(answer_messages(question, attempts))
            return self.verdict(len(seen) == 2)

        self.answerer.generate.side_effect = judge
        self.perform()
        evidence = json.loads(seen[1][1]["content"])["sql_attempts"]
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0]["execution"]["rows"], [[2]])
        self.assertEqual(evidence[1]["retrieved_top_k"], 3)

    def test_third_judge_retains_all_previous_sql_and_result_rows(self):
        seen = []

        def judge(question, attempts):
            seen.append(answer_messages(question, attempts))
            return self.verdict(len(seen) == 3)

        self.answerer.generate.side_effect = judge
        self.perform()
        evidence = json.loads(seen[-1][1]["content"])["sql_attempts"]
        self.assertEqual([a["retrieved_top_k"] for a in evidence], [1, 3, 6])
        self.assertEqual([a["execution"]["rows"] for a in evidence], [[[2]], [[2]], [[2]]])

    def test_three_negative_decisions_stop_at_configured_top6(self):
        self.answerer.generate.return_value = self.verdict(False)
        result = self.perform()
        self.assertEqual(result["status"], "not_answerable")
        self.assertEqual(result["stop_reason"], "retrieval_limit_reached")
        self.assertEqual(self.generator.generate.call_count, 3)
        self.assertFalse(result["answerable"])

    def test_ranking_exhaustion_does_not_repeat_same_views(self):
        self.answerer.generate.return_value = self.verdict(False)
        result = retrieve_and_answer(
            "Question",
            self.ranking[:3],
            self.config,
            self.inventory,
            self.schemas,
            self.generator,
            self.answerer,
        )
        self.assertEqual([a["top_k"] for a in result["attempts"]], [1, 3])
        self.assertEqual(result["stop_reason"], "no_more_ranked_views")

    def test_single_candidate_is_not_retrieved_repeatedly(self):
        self.answerer.generate.return_value = self.verdict(False)
        result = retrieve_and_answer(
            "Question",
            self.ranking[:1],
            self.config,
            self.inventory,
            self.schemas,
            self.generator,
            self.answerer,
        )
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["stop_reason"], "no_more_ranked_views")

    def test_legacy_top3_top6_policy_remains_available(self):
        self.answerer.generate.side_effect = [self.verdict(False), self.verdict(True)]
        config = {**self.config, "top_k": 3, "retrieval_limits": [3, 6]}
        result = retrieve_and_answer(
            "Question",
            self.ranking,
            config,
            self.inventory,
            self.schemas,
            self.generator,
            self.answerer,
        )
        self.assertEqual([a["top_k"] for a in result["attempts"]], [3, 6])

    def test_config_accepts_new_policy_and_rejects_inconsistent_or_repeated_limits(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertEqual(retrieval_schedule(load_config(path)), [1, 3, 6])
        self.assertEqual(retrieval_schedule({"top_k": 1}), [1, 3, 6])
        self.assertEqual(retrieval_schedule({"top_k": 3}), [3, 6])
        for limits in ([3, 6], [1, 1, 3], [1, 6, 3], [], [1, True, 6], [1, 0, 6]):
            with self.subTest(limits=limits):
                path.write_text(
                    json.dumps({**self.config, "retrieval_limits": limits}), encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_statistics_follow_all_three_cumulative_retrieval_stages(self):
        from experiments.text_to_sql_top3.profiles import load_profiles

        config = {
            **self.config,
            "column_context": "statistics",
            "statistics_directory": str(self.root / "profiles"),
        }
        case = {**self.fixture.case, "saved_route": {"ranking": self.ranking}}
        profiles, _ = load_profiles(
            config, {"p/arc": self.inventory}, {"p/arc": self.schemas}, [case], log=lambda _: None
        )
        self.generator.generate.side_effect = lambda *args, **kwargs: self.fixture.generation()
        self.answerer.generate.side_effect = [
            self.verdict(False),
            self.verdict(False),
            self.verdict(True),
        ]
        result = retrieve_and_answer(
            "Question",
            self.ranking,
            config,
            self.inventory,
            self.schemas,
            self.generator,
            self.answerer,
            profiles["p/arc"],
        )
        for attempt, count in zip(result["attempts"], (1, 3, 6)):
            self.assertEqual(attempt["selected_groups"], self.ranking[:count])
            text = json.dumps(attempt["messages"])
            self.assertIn("Door A", text)
            self.assertNotIn("View_Window", text)
            for group in self.ranking[count:]:
                self.assertNotIn('"' + group + '"', attempt["messages"][1]["content"])
        self.assertIn("View_Railing", result["attempts"][-1]["messages"][1]["content"])

    def test_empty_result_does_not_trigger_expansion_when_answer_model_can_answer(self):
        self.generator.generate.side_effect = lambda *args: self.fixture.generation(
            "SELECT ifc_id FROM View_Door WHERE 1=0"
        )
        self.answerer.generate.return_value = self.verdict(True, answer="No matching rows.")
        result = self.perform()
        self.assertEqual(result["execution"]["rows"], [])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["final_answer"], "No matching rows.")

    def test_sql_error_is_passed_to_answer_model_and_can_lead_to_expansion(self):
        self.generator.generate.side_effect = [
            self.fixture.generation("SELECT missing FROM View_Door"),
            self.fixture.generation(),
        ]
        self.answerer.generate.side_effect = [self.verdict(False), self.verdict(True)]
        result = self.perform()
        self.assertEqual(result["attempts"][0]["sql_status"], "sql_error")
        first = answer_messages("Question", [result["attempts"][0]])
        self.assertIn("no such column", first[1]["content"])
        self.assertEqual(result["status"], "answered")

    def test_unexpected_non_sql_marker_is_an_error_not_a_sql_stage_refusal(self):
        self.generator.generate.side_effect = [
            self.fixture.generation("INSUFFICIENT_CONTEXT"),
            self.fixture.generation(),
        ]
        self.answerer.generate.side_effect = [self.verdict(False), self.verdict(True)]
        result = self.perform()
        self.assertEqual(result["attempts"][0]["sql_status"], "sql_error")
        self.assertEqual(result["status"], "answered")

    def test_invalid_answer_json_is_a_technical_failure_not_an_expansion_decision(self):
        self.answerer.generate.return_value = {"decision": None, "error": "Invalid JSON"}
        result = self.perform()
        self.assertEqual(result["status"], "answer_generation_error")
        self.assertEqual(len(result["attempts"]), 1)

    def test_answer_prompt_excludes_ground_truth_and_reference_data(self):
        self.answerer.generate.return_value = self.verdict(True)
        result = self.perform()
        result["attempts"][0].update(
            ground_truth="SECRET_GOLD",
            reference_sql="SECRET_REFERENCE",
            reference_execution={"rows": [["SECRET_REFERENCE_ROW"]]},
        )
        text = json.dumps(answer_messages("Count doors", result["attempts"]))
        self.assertNotIn("SECRET_", text)
        self.assertIn("SELECT COUNT", text)
        self.assertIn("rows", text)

    def test_answer_decision_rejects_string_boolean_empty_answer_and_unknown_keys(self):
        for value in [
            dict(answerable="false", answer="", reason="Missing"),
            dict(answerable=True, answer="", reason=""),
            dict(answerable=False, answer="", reason=""),
            dict(answerable=True, answer="2", reason="", confidence=1),
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_decision(json.dumps(value))
        self.assertFalse(
            parse_decision('{"answerable":false,"answer":"","reason":"Missing"}')["answerable"]
        )

    def test_answer_generator_requests_typed_json_from_same_local_model(self):
        client = Mock()
        client.request.side_effect = [
            {"prompt": "APPLIED"},
            {"tokens": list(range(10))},
            {
                "content": '{"answerable":true,"answer":"2 doors","reason":""}',
                "tokens_predicted": 10,
                "tokens_evaluated": 10,
            },
        ]
        result = AnswerGenerator(client, self.config).generate("Question", [])
        request = client.request.call_args.args[1]
        self.assertIn("json_schema", request)
        self.assertEqual(request["json_schema"]["properties"]["answerable"]["type"], "boolean")
        self.assertTrue(result["decision"]["answerable"])

    def test_process_case_uses_final_judge_and_never_reruns_saved_routing(self):
        self.answerer.generate.side_effect = [self.verdict(False), self.verdict(True)]
        case = {**self.fixture.case, "saved_route": {"ranking": self.ranking, "elapsed_ms": 1}}
        router = Mock()
        record = process_case(
            case,
            self.config,
            self.inventory,
            self.schemas,
            self.generator,
            router,
            None,
            answerer=self.answerer,
        )
        router.score.assert_not_called()
        self.assertEqual(record["status"], "answered")
        self.assertEqual(len(record["attempts"]), 2)

    def test_mocked_full_run_writes_final_answer_and_model_decision_separately(self):
        self.fixture.prepare_fixture()
        answerer = Mock()
        answerer.generate.return_value = self.verdict(True)
        server = Mock()
        server.__enter__ = Mock(return_value=SimpleNamespace(command=["MOCKED"]))
        server.__exit__ = Mock(return_value=False)
        with (
            patch("experiments.text_to_sql_top3.runner.ManagedServer", return_value=server),
            patch("experiments.text_to_sql_top3.runner.HTTP") as http,
            patch("experiments.text_to_sql_top3.runner.SQLGenerator", return_value=self.generator),
            patch("experiments.text_to_sql_top3.runner.AnswerGenerator", return_value=answerer),
            patch("experiments.text_to_sql_top3.runner.hardware_snapshot", return_value={}),
        ):
            http.return_value.request.return_value = {"synthetic": True}
            summary = run(self.config)
        self.assertEqual(summary["model_declared_answerable"], 1)
        self.assertIsNone(summary["semantic_accuracy"])
        output = next(Path(self.config["results_directory"]).iterdir())
        with (output / "review.csv").open(encoding="utf-8-sig", newline="") as f:
            review = list(csv.DictReader(f))
        self.assertEqual(review[0]["final_answer"], "There are 2 doors.")
        self.assertEqual(review[0]["manual_review_status"], "pending")


if __name__ == "__main__":
    unittest.main()
