"""Synthetic DB + mocked completions: no benchmark inference or paid calls."""
import ast
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_top3_text_to_sql as base_tests
from experiments.text_to_sql_top3.inspection import InspectionSession, observations, compact_execution
from experiments.text_to_sql_top3.schema import column_inventory, load_config
from experiments.text_to_sql_top3.workflow import retrieve_and_answer
from experiments.text_to_sql_top3.answer import answer_messages, SYSTEM as ANSWER_SYSTEM
from experiments.view_selection_ablation.common import file_pin


class InspectionSQLTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base_tests.SQLBaselineTest('test_companion_expansion_and_database_membership')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with sqlite3.connect(self.fixture.db) as c:
            for name in ['Beam', 'Column', 'Window']:
                c.executescript(f'CREATE TABLE _sv_{name}(ifc_id INTEGER); CREATE VIEW View_{name} AS SELECT * FROM _sv_{name};')
        self.ranking = ['View_Door', 'View_Space', 'View_Wall', 'View_Beam', 'View_Column', 'View_Window']
        self.inventory = {**self.fixture.inventory, 'database': file_pin(self.fixture.db, full_hash=True),
                          'groups': self.ranking,
                          'physical_views': self.fixture.inventory['physical_views'] + self.ranking[3:]}
        self.schemas = column_inventory(self.inventory)
        self.config = {**self.fixture.config, 'answer_stage': True, 'top_k': 3,
                       'retrieval_limits': [3, 6], 'inspection_enabled': True,
                       'inspection_max_queries': 3, 'inspection_max_new_tokens': 32}
        self.generator = Mock()
        self.generator.generate.side_effect = lambda *a, **kw: self.fixture.generation()
        self.answerer = Mock()
        self.answerer.generate.return_value = self.answer(True)

    def answer(self, ok):
        return {'decision': {'answerable': ok, 'answer': 'Two doors.' if ok else 'Need more data.',
                             'reason': 'Retrieved count.' if ok else 'Need another View.'}, 'error': None}

    def plan(self, sql='', purpose='Check actual values'):
        return {**self.fixture.generation(), 'content': json.dumps(
            {'action': 'inspect' if sql else 'done', 'purpose': purpose, 'sql': sql}),
                'output_tokens': 7}

    def run_workflow(self):
        return retrieve_and_answer('How many doors?', self.ranking, self.config,
                                   self.inventory, self.schemas, self.generator, self.answerer)

    def test_zero_optional_queries(self):
        self.generator.complete.return_value = self.plan()
        result = self.run_workflow()
        self.assertEqual(result['inspection_metrics']['query_attempts'], 0)
        self.assertEqual(self.generator.generate.call_count, 1)
        self.assertEqual(result['attempts'][0]['top_k'], 3)
        self.assertEqual(result['status'], 'answered')

    def test_done_with_stray_sql_is_termination_not_executed_evidence(self):
        plan = {'action': 'done', 'purpose': 'Enough information',
                'sql': 'SELECT Name FROM View_Door'}
        self.generator.complete.return_value = {
            **self.plan(), 'content': json.dumps(plan)}
        result = self.run_workflow()
        record = result['inspection_records'][0]
        self.assertEqual(record['status'], 'done')
        self.assertIsNone(record['execution'])
        self.assertEqual(record['ignored_sql'], plan['sql'])
        self.assertEqual(record['generation']['content'], json.dumps(plan))
        self.assertEqual(record['sql'], '')
        self.assertEqual(result['inspection_metrics']['query_attempts'], 0)
        self.assertEqual(result['attempts'][0]['inspection_evidence'], [])
        self.assertNotIn('inspections', self.generator.generate.call_args.kwargs)
        self.assertEqual(result['status'], 'answered')

    def test_observations_feed_next_inspection_main_sql_and_answer(self):
        self.generator.complete.side_effect = [self.plan('SELECT DISTINCT Name FROM View_Door'), self.plan()]
        result = self.run_workflow()
        second_chat = self.generator.complete.call_args_list[1].args[0]
        self.assertIn('Door A', json.dumps(second_chat))
        kwargs = self.generator.generate.call_args.kwargs
        self.assertEqual(kwargs['inspections'][0]['execution']['rows'], [['Door A'], ['Door B']])
        final_chat = answer_messages('How many doors?', result['attempts'])
        self.assertIn('inspection_queries_and_results', final_chat[1]['content'])
        self.assertEqual(result['inspection_metrics']['query_attempts'], 1)
        self.assertEqual(result['inspection_metrics']['input_tokens'], 20)
        self.assertEqual(result['inspection_metrics']['output_tokens'], 14)

    def test_global_three_query_cap_does_not_reset_after_expansion(self):
        self.generator.complete.side_effect = [
            self.plan('SELECT Name FROM View_Door'), self.plan('SELECT COUNT(*) FROM View_Door'),
            self.plan('SELECT COUNT(*) FROM View_Space')]
        self.answerer.generate.side_effect = [self.answer(False), self.answer(True)]
        result = self.run_workflow()
        self.assertEqual(self.generator.complete.call_count, 3)
        self.assertEqual([a['top_k'] for a in result['attempts']], [3, 6])
        self.assertEqual(result['inspection_metrics']['query_attempts'], 3)
        self.assertEqual(len(self.generator.generate.call_args_list[1].kwargs['inspections']), 3)

    def test_remaining_budget_can_be_used_after_expansion(self):
        self.generator.complete.side_effect = [self.plan(), self.plan('SELECT COUNT(*) FROM View_Window'), self.plan()]
        self.answerer.generate.side_effect = [self.answer(False), self.answer(True)]
        result = self.run_workflow()
        self.assertEqual(result['inspection_records'][1]['top_k'], 6)
        self.assertEqual(result['inspection_records'][1]['execution']['rows'], [[0]])
        self.assertEqual(result['inspection_metrics']['query_attempts'], 1)

    def test_unselected_view_denied_and_error_becomes_evidence(self):
        self.generator.complete.side_effect = [self.plan('SELECT * FROM View_Window'), self.plan()]
        result = self.run_workflow()
        first = result['inspection_records'][0]
        self.assertEqual(first['status'], 'sql_error')
        self.assertIsNotNone(first['execution']['error'])
        self.assertEqual(self.generator.generate.call_args.kwargs['inspections'][0]['status'], 'sql_error')
        self.assertEqual(result['inspection_metrics']['query_attempts'], 1)

    def test_write_denied_database_preserved_and_main_still_runs(self):
        self.generator.complete.return_value = self.plan('DELETE FROM View_Door')
        result = self.run_workflow()
        self.assertEqual(result['inspection_records'][0]['status'], 'inspection_error')
        self.assertEqual(result['inspection_metrics']['query_attempts'], 1)
        self.assertEqual(result['execution']['rows'], [[2]])

    def test_duplicate_query_does_not_repeat_execution(self):
        self.generator.complete.return_value = self.plan('SELECT COUNT(*) FROM View_Door')
        result = self.run_workflow()
        self.assertEqual(result['inspection_metrics']['query_attempts'], 1)
        self.assertEqual(result['inspection_records'][-1]['status'], 'duplicate_skipped')

    def test_invalid_planner_response_falls_through(self):
        self.generator.complete.return_value = {**self.plan(), 'content': 'not JSON'}
        result = self.run_workflow()
        self.assertEqual(result['inspection_records'][0]['status'], 'inspection_error')
        self.assertEqual(result['status'], 'answered')

    def test_preview_markers_and_reference_allowlist(self):
        execution = {'columns': ['c']*40, 'rows': [['x'*200]*40 for _ in range(50)],
                     'error': None, 'truncated': True, 'row_count': None, 'rows_saved': 50}
        evidence = compact_execution(execution)
        self.assertTrue(evidence['columns_previewed'])
        self.assertTrue(evidence['rows_previewed'])
        self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=False)), 12000)
        records = [{'top_k': 3, 'purpose': 'Check', 'sql': 'SELECT 1', 'status': 'ok',
                    'execution': execution, 'ground_truth': 'SECRET_GOLD', 'reference_sql': 'SECRET_SQL'}]
        self.assertNotIn('SECRET', json.dumps(observations(records)))

    def test_maximum_query_config_and_final_answer_prompt_preserved(self):
        config_path = self.fixture.root/'config.json'
        for cap in [-1, 4, True]:
            config_path.write_text(json.dumps({**self.config, 'inspection_max_queries': cap}))
            with self.assertRaises(ValueError): load_config(config_path)
        import hashlib
        self.assertEqual(hashlib.sha256(ANSWER_SYSTEM.encode()).hexdigest(),
                         "e7b630c9a6c92059c92d4f5de7e31b6e450aa29d8dbb49fd3282edb56bcf1749")

if __name__ == '__main__': unittest.main()
