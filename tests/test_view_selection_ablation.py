"""Experiment checks with synthetic data and mocked HTTP; never load an LLM."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from experiments.view_selection_ablation.backend import LlamaDecisionPort, ManagedServer, label_distribution
from experiments.view_selection_ablation.common import HERE, load_config, read_json
from experiments.view_selection_ablation.data import compile_reference, prepare, review_labels
from experiments.view_selection_ablation.evaluation import make_report, retrieval_metrics
from experiments.view_selection_ablation.prompts import (
    chat_messages, condition_order, ordered_options, systemone_request,
)


class ViewSelectionAblationTest(unittest.TestCase):
    def setUp(self):
        self.profile = read_json(HERE / "decisio_prompt_profile.json")
        self.labels = [{"code": "A", "spaced_token_id": 1, "token_ids": [1, 2]},
                       {"code": "B", "spaced_token_id": 3, "token_ids": [3]}]
        self.options = ["View_Door", "View_Wall"]
        self.descriptions = {"View_Door": "Controlled access.", "View_Wall": "Space boundaries."}

    def test_only_descriptions_change_and_gold_never_enters_prompt(self):
        case = {"id": "7", "question": "How many doors?", "sql": "SECRET SQL",
                "ground_truth": "SECRET ANSWER", "reference_view_sets": [["View_Door"]]}
        a, b = [chat_messages(case["question"], self.options, self.descriptions, condition,
                             self.labels, "Choose a View.", self.profile)
                for condition in ("name_only", "name_description")]
        self.assertEqual(a[0], b[0])
        self.assertNotIn("SECRET", json.dumps([a, b]))
        self.assertNotIn("Controlled access", a[1]["content"])
        self.assertEqual(b[1]["content"].replace(": Controlled access.", "").replace(": Space boundaries.", ""),
                         a[1]["content"])

    def test_decisio_default_description_renderer_cannot_drop_the_view_name(self):
        request = systemone_request("Doors?", self.options, self.descriptions,
                                    "name_description", "Choose.")
        self.assertEqual(request["questions"]["view"]["criteria"]["View_Door"],
                         "View_Door: Controlled access.")
        a = systemone_request("Doors?", self.options, self.descriptions, "name_only", "Choose.")
        self.assertTrue(all(value is None for value in a["questions"]["view"]["criteria"].values()))

    def test_candidate_order_is_label_independent_and_shared_between_conditions(self):
        case = {"id": "8", "reference_view_sets": [["View_Door"]]}
        inventory = {"groups": self.options}
        first = ordered_options(case, inventory, 17, 0)
        case["reference_view_sets"] = [["View_Wall"]]
        self.assertEqual(first, ordered_options(case, inventory, 17, 0))
        orders = {tuple(condition_order(17, str(i), 0)) for i in range(100)}
        self.assertEqual(len(orders), 2)

    def test_all_token_forms_are_summed_before_temperature(self):
        response = {"probs": [{"top_probs": [{"id": 1, "prob": .2},
                                               {"id": 2, "prob": .3},
                                               {"id": 3, "prob": .1}]}]}
        probabilities, mass = label_distribution(response, self.labels, 1)
        self.assertAlmostEqual(probabilities[0], 5 / 6)
        self.assertAlmostEqual(probabilities[1], 1 / 6)
        self.assertAlmostEqual(mass, .6)
        warmer, _ = label_distribution(response, self.labels, 2)
        self.assertGreater(warmer[1], probabilities[1])

    def test_missing_or_ambiguous_token_scores_fail_instead_of_filling_zero(self):
        for values in ([{"id": 1, "prob": 1}], [{"token": "A", "prob": .5}]):
            with self.assertRaises(ValueError):
                label_distribution({"completion_probabilities": [{"top_probs": values}]}, self.labels, 1)

    def test_context_overflow_fails_without_a_completion_request(self):
        class Client:
            def request(self, path, payload=None):
                if path == "/apply-template":
                    return {"prompt": "a prompt"}
                if path == "/tokenize":
                    return {"tokens": list(range(100))}
                raise AssertionError("Completion must not be requested")
        backend = LlamaDecisionPort(Client(), {"instructions": "Choose.", "context_tokens": 64},
                                    self.profile, self.labels)
        with self.assertRaisesRegex(ValueError, "no truncation"):
            backend.score("Doors?", self.options, self.descriptions, "name_only")

    def test_local_scoring_requests_one_token_and_uniform_label_bias(self):
        seen = {}
        class Client:
            def request(self, path, payload=None):
                seen[path] = payload
                if path == "/apply-template":
                    return {"prompt": "<bos>prompt without an open thought channel"}
                if path == "/tokenize":
                    return {"tokens": [1, 2, 3]}
                if path == "/completion":
                    return {"tokens_evaluated": 4, "probs": [{"top_probs": [{"id": 1, "prob": .2},
                                                       {"id": 2, "prob": .3},
                                                       {"id": 3, "prob": .5}]}]}
                raise AssertionError(path)
        config = {"instructions": "Choose.", "context_tokens": 100, "seed": 17,
                  "label_logit_bias": 100.0, "choice_temperature": 4.672}
        backend = LlamaDecisionPort(Client(), config, self.profile, self.labels)
        result = backend.score("Doors?", self.options, self.descriptions, "name_description")
        self.assertEqual(seen["/completion"]["n_predict"], 1)
        self.assertFalse(seen["/completion"]["cache_prompt"])
        self.assertEqual(seen["/completion"]["logit_bias"], [[1, 100.0], [2, 100.0], [3, 100.0]])
        self.assertAlmostEqual(result["probabilities"]["View_Door"], .5)
        self.assertTrue(seen['/tokenize']['add_special'])
        self.assertEqual(result["input_tokens"], 4)
        self.assertEqual(result["tokenizer_input_tokens"], 3)
        self.assertEqual(result["input_tokens_source"], 'server.tokens_evaluated')

    def test_multi_view_recall_and_coverage_are_different(self):
        m = retrieval_metrics(["View_Door", "View_Project"], [["View_Door", "View_Space"]], 1)
        self.assertEqual(m["recall"], .5)
        self.assertEqual(m["all_required_covered"], 0)
        alternate = retrieval_metrics(["View_Door"], [["View_Door", "View_Space"], ["View_Door"]], 1)
        self.assertEqual(alternate["all_required_covered"], 1)

    def test_compile_checks_do_not_execute_sql_or_modify_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite"
            with closing(sqlite3.connect(path)) as con:
                con.executescript("CREATE TABLE _sv_Door(ifc_id INTEGER); INSERT INTO _sv_Door VALUES(1);"
                                  "CREATE VIEW View_Door AS SELECT ifc_id FROM _sv_Door;")
                con.commit()
            before = path.read_bytes()
            self.assertIsNone(compile_reference(path, "SELECT COUNT(*) FROM View_Door", ["View_Door"]))
            self.assertIsNotNone(compile_reference(path, "SELECT nonexistent FROM View_Door", ["View_Door"]))
            self.assertIsNotNone(compile_reference(path, "DELETE FROM _sv_Door", ["View_Door"]))
            self.assertEqual(path.read_bytes(), before)

    def test_unreviewed_labels_do_not_become_semantic_gold(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "label_review.json"
            path.write_text(json.dumps([{"id": "1", "status": "pending", "accepted_view_sets": [["View_Door"]]},
                                        {"id": "2", "status": "excluded", "accepted_view_sets": []}]))
            cases = [{"id": str(i), "run_eligible": True, "inventory_key": "model"} for i in (1, 2)]
            approved, excluded = review_labels({"prepared_directory": folder}, {"model": {"groups": self.options}}, cases)
            self.assertEqual(approved, {})
            self.assertEqual(excluded, {"2"})

    def test_partial_pairs_are_reported_as_incomplete_and_not_scored(self):
        with tempfile.TemporaryDirectory() as folder:
            case = {"id": "1", "project": "p", "historical_reference_eligible": True,
                    "reference_view_sets": [["View_Door"]]}
            record = {"id": "1", "repeat": 0, "condition": "name_only", "status": "ok",
                      "ranking": ["View_Door"], "score": {"elapsed_ms": 1, "input_tokens": 10}}
            config = {"backend": "mock", "primary_k": 3, "top_k": [1, 3, 5],
                      "bootstrap_samples": 50, "seed": 17}
            report = make_report(folder, [case], [record], {}, config, 2)
            self.assertFalse(report["complete"])
            self.assertEqual(report["pending_pairs"], 1)
            self.assertEqual(report["scopes"]["historical_reference_agreement"]["paired_questions"], 0)
            self.assertEqual(report["scopes"]["manual_semantic_gold"]["eligible_questions"], 0)

    def test_complete_pair_produces_deltas_and_preserves_manual_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            case = {"id": "1", "project": "p", "historical_reference_eligible": True,
                    "reference_view_sets": [["View_Door"]]}
            records = [{"id": "1", "repeat": 0, "condition": condition, "status": "ok",
                        "ranking": ranking, "score": {"elapsed_ms": 10 + i, "input_tokens": 10 + i}}
                       for i, (condition, ranking) in enumerate([
                           ("name_only", ["View_Wall", "View_Door"]),
                           ("name_description", ["View_Door", "View_Wall"])])]
            config = {"backend": "mock", "primary_k": 1, "top_k": [1, 3, 5],
                      "bootstrap_samples": 50, "seed": 17}
            report = make_report(folder, [case], records, {"999": [["View_Door"]]}, config, 2)
            result = report["scopes"]["historical_reference_agreement"]
            self.assertTrue(report["complete"])
            self.assertEqual(result["paired_interval"]["recall_delta_B_minus_A"]["estimate"], 1)
            self.assertEqual(result["mcnemar"]["B_only"], 1)
            self.assertEqual(report["scopes"]["manual_semantic_gold"]["eligible_questions"], 0)
            self.assertIn("name_description", (Path(folder) / "REPORT.md").read_text())

    def test_manual_exclusion_removes_a_legacy_reference_from_scoring(self):
        with tempfile.TemporaryDirectory() as folder:
            case = {"id": "1", "project": "p", "historical_reference_eligible": True,
                    "reference_view_sets": [["View_Door"]]}
            config = {"backend": "mock", "primary_k": 3, "top_k": [1, 3, 5],
                      "bootstrap_samples": 50, "seed": 17}
            report = make_report(folder, [case], [], {}, config, 2, manually_excluded={"1"})
            self.assertEqual(report["scopes"]["historical_reference_agreement"]["eligible_questions"], 0)

    def test_dry_run_does_not_call_run_or_start_a_server(self):
        from experiments.view_selection_ablation.cli import main
        with patch("experiments.view_selection_ablation.cli.load_config", return_value={}), \
             patch("experiments.view_selection_ablation.cli.plan", return_value={"example": {}, "inference_executed": False}), \
             patch("experiments.view_selection_ablation.cli.run") as execute:
            self.assertEqual(main(["run", "--dry-run"]), 0)
            execute.assert_not_called()

    def test_server_uses_cuda_dll_path_and_stops_only_its_child(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("experiments.view_selection_ablation.backend.check_pin"), \
             patch("experiments.view_selection_ablation.backend.socket.socket") as socket_factory, \
             patch("experiments.view_selection_ablation.backend.subprocess.Popen") as launch, \
             patch("experiments.view_selection_ablation.backend.HTTP.request", return_value={"status": "ok"}):
            config = load_config()
            socket_factory.return_value.__enter__.return_value.connect_ex.return_value = 1
            launch.return_value.poll.return_value = None
            with ManagedServer(config, folder, {"path": config["llama_server"]}):
                environment = launch.call_args.kwargs["env"]
                self.assertEqual(environment["GGML_BACKEND_PATH"],
                                 str(Path(config["cuda_dll_directory"]) / "ggml-cuda.dll"))
                self.assertEqual(launch.call_count, 1)
            launch.return_value.terminate.assert_called_once()
            launch.return_value.kill.assert_not_called()

    def test_zero_repeats_is_rejected_instead_of_using_the_default(self):
        from experiments.view_selection_ablation.runner import plan
        with patch("experiments.view_selection_ablation.runner.load_prepared", return_value=({}, {}, [], [])), \
             patch("experiments.view_selection_ablation.runner.review_labels", return_value=({}, set())):
            with self.assertRaisesRegex(ValueError, "repeats must be positive"):
                plan({"seed": 17, "repeats": 1}, repeats=0)

    def test_historical_attachments_do_not_exclude_or_expand_a_question(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            question_csv = root / "questions.csv"
            question_csv.write_text("id,question,project,ifc_model,category\n1,Doors?,p,main,1\n", encoding="utf-8")
            fixtures = {
                "reference_json": [{"id": 1, "question": "Doors?", "answerability": "yes",
                                    "referenced_views": ["other.View_Door"], "attached_databases": {"other": {}}}],
                "judgments_json": [{"id": 1, "correct": True}],
                "investigations_json": [], "description_catalog": {},
            }
            config = {"question_csv": str(question_csv), "databases": str(root),
                      "prepared_directory": str(root / "prepared"),
                      "model_path": str(root / "not_a_model.gguf"), "llama_server": str(root / "not_a_server.exe")}
            for key, value in fixtures.items():
                path = root / (key + ".json")
                path.write_text(json.dumps(value), encoding="utf-8")
                config[key] = str(path)
            Path(config["model_path"]).write_bytes(b"synthetic metadata fixture")
            Path(config["llama_server"]).write_bytes(b"synthetic server fixture")
            inventory = {"p/main": {"groups": ["View_Door"], "physical_views": ["View_Door"]}}
            with patch("experiments.view_selection_ablation.data.inventory_databases", return_value=inventory), \
                 patch("experiments.view_selection_ablation.data.read_metadata", return_value={}), \
                 patch("experiments.view_selection_ablation.data.label_pool", return_value=self.labels):
                manifest = prepare(config, log=lambda message: None)
            case = json.loads((root / "prepared" / "cases.jsonl").read_text(encoding="utf-8"))
            self.assertTrue(case["run_eligible"])
            self.assertEqual(case["inventory_key"], "p/main")
            self.assertFalse(case["historical_reference_eligible"])
            self.assertEqual(case["reference_view_sets"], [])
            self.assertEqual(manifest["counts"]["run_eligible"], 1)
            self.assertEqual(manifest["question_scope"], "csv_project_ifc_model_only")


if __name__ == "__main__":
    unittest.main()
