"""Synthetic SQLite and mocked model tests; never load an LLM or run benchmark inference."""

from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from experiments.text_to_sql_top3.generation import (
    ContextOverflow,
    SQLGenerator,
    extract_sql,
    messages,
)
from experiments.text_to_sql_top3.runner import plan, process_case, run
from experiments.text_to_sql_top3.schema import (
    column_inventory,
    load_prepared_sql,
    physical_views,
    prepare,
    saved_routes,
)
from experiments.text_to_sql_top3.sql import execute, result_agreement
from experiments.view_selection_ablation.common import file_pin, write_json


class SQLBaselineTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.db = self.root / "model.sqlite"
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript("""
                CREATE TABLE _sv_Door(ifc_id INTEGER, Name TEXT, "Pressure Drop" REAL, etc TEXT);
                INSERT INTO _sv_Door VALUES (1,'Door A',2.5,'{"SECRET_KEY":7}'),(2,'Door B',3,NULL);
                CREATE VIEW View_Door AS SELECT * FROM _sv_Door;
                CREATE TABLE _sv_DoorCustomPset(ifc_id INTEGER, Custom_Width REAL);
                INSERT INTO _sv_DoorCustomPset VALUES(1,900),(2,800);
                CREATE VIEW View_DoorCustomPset AS SELECT * FROM _sv_DoorCustomPset;
                CREATE TABLE _sv_Space(ifc_id INTEGER, Name TEXT);
                INSERT INTO _sv_Space VALUES(3,'Room A');
                CREATE VIEW View_Space AS SELECT * FROM _sv_Space;
                CREATE TABLE _sv_Wall(ifc_id INTEGER);
                INSERT INTO _sv_Wall VALUES(4);
                CREATE VIEW View_Wall AS SELECT * FROM _sv_Wall;
                CREATE TABLE IfcDoor(step_id INTEGER);
                INSERT INTO IfcDoor VALUES(1);
            """)
            con.commit()
        self.pin = file_pin(self.db, full_hash=True)
        self.build = self.root / "model.build.json"
        write_json(self.build, {"schema": "IFC4"})
        self.inventory = {
            "database": self.pin,
            "build": file_pin(self.build, full_hash=True),
            "groups": ["View_Door", "View_Space", "View_Wall"],
            "physical_views": ["View_Door", "View_DoorCustomPset", "View_Space", "View_Wall"],
            "descriptions": {
                "View_Door": "SECRET_DESCRIPTION",
                "View_Space": "Space",
                "View_Wall": "Wall",
            },
        }
        self.schemas = column_inventory(self.inventory)
        self.config = {
            "routing_run": str(self.root / "routing"),
            "prepared_directory": str(self.root / "prepared"),
            "results_directory": str(self.root / "results"),
            "routing_condition": "name_only",
            "routing_repeat": 0,
            "routing_mode": "saved",
            "top_k": 3,
            "context_tokens": 512,
            "max_new_tokens": 32,
            "temperature": 0.0,
            "seed": 17,
            "port": 8782,
            "http_timeout_seconds": 2,
            "sql_timeout_seconds": 1,
            "max_result_rows": 100,
            "compare_reference_results": True,
        }
        self.case = {
            "id": "1",
            "question": "How many doors?",
            "project": "p",
            "ifc_model": "arc",
            "inventory_key": "p/arc",
            "run_eligible": True,
            "historical_reference_eligible": True,
            "reference_view_sets": [["View_Door"]],
            "saved_route": {
                "ranking": ["View_Door", "View_Space", "View_Wall"],
                "elapsed_ms": 123,
            },
            "selected_groups": ["View_Door", "View_Space", "View_Wall"],
            "selected_views": self.inventory["physical_views"],
            "ground_truth_for_review_only": "SECRET_ANSWER",
        }

    def query(self, sql, views=None, **kwargs):
        return execute(self.pin, views or ["View_Door", "View_DoorCustomPset"], sql, **kwargs)

    def generation(self, sql="SELECT COUNT(*) FROM View_Door;", **kwargs):
        return {
            "sql": sql,
            "content": sql,
            "truncated": False,
            "output_limit_reached": False,
            "input_tokens": 10,
            "generation_ms": 1.5,
            **kwargs,
        }

    def test_prompt_contains_only_selected_column_names_and_fixed_rules(self):
        text = json.dumps(messages(self.case["question"], ["View_Door"], self.schemas))
        for secret in (
            "SECRET_KEY",
            "Door A",
            "SECRET_DESCRIPTION",
            "SECRET_ANSWER",
            "Custom_Width",
        ):
            self.assertNotIn(secret, text)
        self.assertIn("Pressure Drop", text)
        self.assertNotIn("View_Wall", text)
        self.assertNotIn('"REAL"', text)

    def test_companion_expansion_and_database_membership(self):
        self.assertEqual(
            physical_views(["View_Door"], self.inventory), ["View_Door", "View_DoorCustomPset"]
        )
        with self.assertRaises(ValueError):
            physical_views(["View_Beam"], self.inventory)

    def test_only_metadata_is_read_during_column_inventory(self):
        self.assertEqual(self.schemas["View_Door"], ["ifc_id", "Name", "Pressure Drop", "etc"])
        self.assertNotIn("SECRET_KEY", json.dumps(self.schemas))

    def test_selected_view_count_join_and_quoted_domain_column(self):
        result = self.query(
            "SELECT d.Name,c.Custom_Width,d.\"Pressure Drop\",'DROP' "
            "FROM View_Door d JOIN View_DoorCustomPset c USING(ifc_id) ORDER BY d.ifc_id"
        )
        self.assertIsNone(result["error"])
        self.assertEqual(result["row_count"], 2)
        self.assertIsNone(self.query("SELECT count(*) FROM View_Door")["error"])
        self.assertEqual(file_pin(self.db, full_hash=True), self.pin)

    def test_cte_and_json_each_can_read_selected_view(self):
        result = self.query(
            "WITH c AS (SELECT j.value FROM View_Door d,json_each(d.etc) j) "
            "SELECT count(*) FROM c"
        )
        self.assertIsNone(result["error"])
        self.assertEqual(result["rows"], [[1]])

    def test_nonselected_view_and_raw_table_bypasses_are_denied(self):
        for sql in (
            "SELECT * FROM View_Wall",
            "SELECT * FROM IfcDoor",
            'SELECT count(*) FROM "_sv_Door"',
            "SELECT * FROM _sv_Door",
            "WITH View_Door AS (SELECT * FROM IfcDoor) SELECT * FROM View_Door",
            "WITH View_Door AS (SELECT * FROM View_Wall) SELECT * FROM View_Door",
            "SELECT * FROM sqlite_master",
            "SELECT * FROM pragma_table_info('View_Wall')",
        ):
            with self.subTest(sql=sql):
                self.assertIsNotNone(self.query(sql)["error"])

    def test_cross_database_and_mutating_statements_are_denied(self):
        for sql in (
            "SELECT * FROM other.View_Door",
            "SELECT * FROM temp.View_Door",
            "ATTACH DATABASE 'other.sqlite' AS other",
            "DELETE FROM _sv_Door",
            "WITH c AS (SELECT 1) DELETE FROM _sv_Door",
            "SELECT * FROM View_Door; SELECT * FROM View_Wall;",
            "SELECT load_extension('bad')",
        ):
            with self.subTest(sql=sql):
                self.assertIsNotNone(self.query(sql)["error"])
        self.assertEqual(file_pin(self.db, full_hash=True), self.pin)

    def test_double_quoted_unknown_column_is_not_a_string_literal(self):
        result = self.query('SELECT "invented_column" FROM View_Door')
        self.assertIsNotNone(result["error"])

    def test_row_cap_is_explicit_and_not_a_complete_reference(self):
        truncated = self.query("SELECT * FROM View_Door", max_rows=1)
        full = self.query("SELECT * FROM View_Door")
        self.assertTrue(truncated["truncated"])
        self.assertIsNone(truncated["row_count"])
        self.assertFalse(result_agreement(truncated, full))
        self.assertIsNone(result_agreement(full, truncated))

    def test_progress_timeout_interrupts_expensive_query(self):
        result = self.query(
            "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) "
            "SELECT sum(n) FROM c",
            timeout=0.001,
        )
        self.assertIsNotNone(result["error"])

    def test_result_agreement_preserves_duplicates_but_not_row_order_or_aliases(self):
        a = {"columns": ["a"], "rows": [[1], [2], [1]], "error": None, "truncated": False}
        b = {**a, "columns": ["renamed"], "rows": [[2], [1], [1]]}
        self.assertTrue(result_agreement(a, b))
        self.assertFalse(result_agreement(a, {**b, "rows": [[1], [2]]}))
        self.assertFalse(result_agreement(None, b))

    def test_generation_is_sql_not_a_biased_single_token_decision(self):
        client = Mock()
        client.request.side_effect = [
            {"prompt": "APPLIED"},
            {"tokens": list(range(10))},
            {
                "content": "SELECT count(*) FROM View_Door;",
                "tokens_evaluated": 10,
                "tokens_predicted": 8,
                "stop_type": "eos",
            },
        ]
        result = SQLGenerator(client, self.config).generate(
            "Count doors", ["View_Door"], self.schemas
        )
        request = client.request.call_args.args[1]
        self.assertEqual(request["n_predict"], 32)
        self.assertNotIn("logit_bias", request)
        self.assertNotIn("n_probs", request)
        self.assertFalse(result["output_limit_reached"])

    def test_context_overflow_never_calls_completion(self):
        client = Mock()
        client.request.side_effect = [{"prompt": "APPLIED"}, {"tokens": list(range(500))}]
        with self.assertRaises(ContextOverflow):
            SQLGenerator(client, self.config).generate("Count doors", ["View_Door"], self.schemas)
        self.assertEqual(client.request.call_count, 2)

    def test_optional_sql_fence_and_non_sql_output_is_not_an_abstention(self):
        self.assertEqual(extract_sql("```sql\nSELECT 1;\n```"), "SELECT 1;")
        self.assertEqual(extract_sql("INSUFFICIENT_CONTEXT"), "INSUFFICIENT_CONTEXT")
        self.assertIsNotNone(self.query(extract_sql("INSUFFICIENT_CONTEXT"))["error"])
        self.assertIsNotNone(self.query(extract_sql("Here is SQL: SELECT 1;"))["error"])

    def test_replay_routes_do_not_call_router_or_put_answers_in_prompt(self):
        generator, router = Mock(), Mock()
        generator.generate.return_value = self.generation()
        reference = {
            "views": ["View_Door"],
            "sql": "SELECT COUNT(*) FROM View_Door;",
            "basis": "test",
        }
        record = process_case(
            self.case, self.config, self.inventory, self.schemas, generator, router, reference
        )
        router.score.assert_not_called()
        self.assertEqual(record["status"], "ok")
        self.assertTrue(record["reference_comparison"]["result_agreement"])
        self.assertIsNone(record["reference_comparison"]["semantic_correctness"])
        self.assertNotIn("SECRET_ANSWER", json.dumps(record["messages"]))
        self.assertNotIn("SELECT COUNT", json.dumps(record["messages"]))

    def test_live_router_top3_limits_generation_and_execution(self):
        generator, router = Mock(), Mock()
        inventory = {**self.inventory, "groups": self.inventory["groups"] + ["View_Beam"]}
        router.score.return_value = {
            "probabilities": {
                "View_Door": 0.4,
                "View_Space": 0.3,
                "View_Wall": 0.2,
                "View_Beam": 0.1,
            },
            "elapsed_ms": 12,
        }
        generator.generate.return_value = self.generation()
        record = process_case(
            self.case,
            {**self.config, "routing_mode": "live"},
            inventory,
            self.schemas,
            generator,
            router,
            None,
        )
        self.assertEqual(record["selected_groups"], ["View_Door", "View_Space", "View_Wall"])
        self.assertNotIn("View_Beam", json.dumps(record["messages"]))

    def test_limited_output_is_not_executed(self):
        for generation, status in [
            (self.generation(output_limit_reached=True), "generation_limit"),
            (self.generation(truncated=True), "input_truncated"),
        ]:
            generator = Mock()
            generator.generate.return_value = generation
            with patch("experiments.text_to_sql_top3.runner.execute") as query:
                record = process_case(
                    self.case, self.config, self.inventory, self.schemas, generator, None, None
                )
                self.assertEqual(record["status"], status)
                query.assert_not_called()

    def prepare_fixture(self):
        routing = Path(self.config["routing_run"])
        routing.mkdir()
        model, binary = self.root / "model.gguf", self.root / "server.exe"
        model.write_bytes(b"synthetic model pin; never loaded")
        binary.write_bytes(b"synthetic server pin; never launched")
        question_csv = self.root / "questions.csv"
        with question_csv.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=["id", "project", "ifc_model", "question", "ground_truth"]
            )
            writer.writeheader()
            writer.writerow(
                {
                    "id": "1",
                    "project": "p",
                    "ifc_model": "arc",
                    "question": self.case["question"],
                    "ground_truth": "SECRET_ANSWER",
                }
            )
        refs = self.root / "references.json"
        write_json(
            refs,
            [
                {
                    "id": "1",
                    "sql": "SELECT count(*) FROM View_Door",
                    "referenced_views": ["View_Door"],
                }
            ],
        )
        source = {
            "prepared_fingerprint": "source-fingerprint",
            "question_ids": ["1"],
            "config": {
                "backend": "llama_cpp_decisio_port",
                "question_csv": str(question_csv),
                "reference_json": str(refs),
                "host": "127.0.0.1",
                "model_path": str(model),
            },
            "model": file_pin(model, full_hash=True),
            "server": file_pin(binary, full_hash=True),
        }
        write_json(routing / "run_manifest.json", source)
        record = {
            "id": "1",
            "condition": "name_only",
            "repeat": 0,
            "status": "ok",
            "project": "p",
            "options": self.inventory["groups"],
            "ranking": self.inventory["groups"],
            "score": {
                "probabilities": {"View_Door": 0.7, "View_Space": 0.2, "View_Wall": 0.1},
                "elapsed_ms": 1,
            },
        }
        (routing / "records.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        labels = [{"code": "A", "token_ids": [1]}]
        with patch(
            "experiments.text_to_sql_top3.schema.load_prepared",
            return_value=(
                {"fingerprint": "source-fingerprint"},
                {"p/arc": self.inventory},
                [self.case],
                labels,
            ),
        ):
            prepare(self.config, log=lambda _: None)
        return source

    def test_saved_route_wrong_database_or_missing_records_are_rejected(self):
        self.prepare_fixture()
        path = Path(self.config["routing_run"]) / "records.jsonl"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["options"].append("View_Outside")
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            saved_routes(path, [self.case], {"p/arc": self.inventory}, "name_only", 0)

    def test_prepare_and_plan_do_not_load_models_and_keep_all_questions(self):
        self.prepare_fixture()
        manifest, _, cases, _ = load_prepared_sql(self.config)
        self.assertEqual(len(cases), 1)
        self.assertFalse(manifest["model_loaded"])
        with patch("experiments.text_to_sql_top3.runner.ManagedServer") as server:
            result = plan(self.config)
            server.assert_not_called()
        self.assertEqual(result["sql_generation_requests"], 1)
        self.assertEqual(result["new_routing_requests"], 0)
        self.assertNotIn("SECRET_ANSWER", json.dumps(result["example_messages"]))

    def test_mocked_end_to_end_resume_and_code_configuration_pins(self):
        self.prepare_fixture()
        generator = Mock()
        generator.generate.return_value = self.generation()
        server = Mock()
        server.__enter__ = Mock(return_value=SimpleNamespace(command=["MOCKED; NEVER LAUNCHED"]))
        server.__exit__ = Mock(return_value=False)
        with (
            patch("experiments.text_to_sql_top3.runner.ManagedServer", return_value=server),
            patch("experiments.text_to_sql_top3.runner.HTTP") as http,
            patch("experiments.text_to_sql_top3.runner.SQLGenerator", return_value=generator),
            patch("experiments.text_to_sql_top3.runner.hardware_snapshot", return_value={}),
        ):
            http.return_value.request.return_value = {"synthetic": True}
            result = run(self.config)
        self.assertTrue(result["complete"])
        self.assertEqual(result["status_counts"], {"ok": 1})
        self.assertEqual(result["reference_result_matches"], 1)
        self.assertIsNone(result["semantic_accuracy"])
        output = next(Path(self.config["results_directory"]).iterdir())
        with patch("experiments.text_to_sql_top3.runner.ManagedServer") as launch:
            resumed = run(self.config, resume=output)
            launch.assert_not_called()
        self.assertTrue(resumed["complete"])
        with self.assertRaises(ValueError):
            run({**self.config, "temperature": 0.5}, resume=output)

    def test_changed_schema_snapshot_blocks_run(self):
        self.prepare_fixture()
        path = Path(self.config["prepared_directory"]) / "schemas.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_prepared_sql(self.config)

    def test_cli_dry_run_never_calls_generation_runner(self):
        from experiments.text_to_sql_top3.cli import main

        with (
            patch("experiments.text_to_sql_top3.cli.load_config", return_value=self.config),
            patch(
                "experiments.text_to_sql_top3.cli.plan",
                return_value={"example_messages": [], "inference_executed": False},
            ),
            patch("experiments.text_to_sql_top3.cli.run") as launch,
        ):
            self.assertEqual(main(["run", "--dry-run"]), 0)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
