"""Behavioral checks of routing context, bounded repairs and cached discovery."""
import json
import unittest
from unittest.mock import patch

import test_inspection_sql as base
from experiments.text_to_sql_top3.feedback import score_context, previous_attempts
from experiments.text_to_sql_top3.generation import messages
from experiments.text_to_sql_top3.inspection import observations
from experiments.text_to_sql_top3.metadata import cached_query
from experiments.text_to_sql_top3.workflow import retrieve_and_answer


class FeedbackRevisionTests(unittest.TestCase):
    def setUp(self):
        self.f = base.InspectionSQLTests('test_zero_optional_queries')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config = {**self.f.config, 'view_scores_context': True,
                       'sql_feedback_enabled': True, 'inspection_repair_enabled': True,
                       'main_sql_max_rewrites': 1}
        self.scores = dict(zip(self.f.ranking, [.895, .045, .025, .02, .01, .005]))
        self.f.generator.complete.return_value = self.f.plan()

    def run_flow(self):
        return retrieve_and_answer('How many doors?', self.f.ranking, self.config,
            self.f.inventory, self.f.schemas, self.f.generator, self.f.answerer,
            view_scores=self.scores)

    def test_scores_rounded_two_decimals_without_dropping_small_scores_or_raw_order(self):
        text = score_context({'View_Last': .0041, 'View_First': .0049, 'View_Door': .991})
        self.assertIn('View_Door:0.99', text)
        self.assertIn('View_First:0.00, View_Last:0.00', text)
        self.assertNotIn('.0041', text)

    def test_scores_reach_inspection_and_main_without_adding_unselected_columns(self):
        self.run_flow()
        plan_chat = self.f.generator.complete.call_args.args[0]
        self.assertIn('View_Window:0.01', json.dumps(plan_chat))
        kwargs = self.f.generator.generate.call_args.kwargs
        self.assertEqual(kwargs['view_scores'], self.scores)
        chat = messages('Q', ['View_Door'], self.f.schemas, view_scores=self.scores)
        self.assertIn('View_Window:0.01', chat[1]['content'])
        self.assertNotIn('"View_Window" (', chat[1]['content'])

    def test_other_database_scores_rejected(self):
        self.scores['View_Secret'] = .1
        with self.assertRaises(ValueError): self.run_flow()

    def test_multiple_statements_can_be_repaired_within_remaining_budget(self):
        self.f.generator.complete.side_effect = [
            self.f.plan('SELECT Name FROM View_Door; SELECT Name FROM View_Door'),
            self.f.plan('SELECT Name FROM View_Door'), self.f.plan()]
        r = self.run_flow()
        self.assertEqual(r['inspection_metrics']['query_attempts'], 2)
        self.assertEqual([x['status'] for x in r['inspection_records']], ['inspection_error','ok','done'])
        next_chat = self.f.generator.complete.call_args_list[1].args[0]
        self.assertIn('multiple_statements', json.dumps(next_chat))
        self.assertIn('Door A', json.dumps(self.f.generator.generate.call_args.kwargs))

    def test_missing_column_returns_owner_and_can_be_corrected(self):
        self.f.generator.complete.side_effect = [self.f.plan('SELECT MissingName FROM View_Door'),
            self.f.plan('SELECT Name FROM View_Door'), self.f.plan()]
        r = self.run_flow()
        first = r['inspection_records'][0]
        self.assertEqual(first['status'], 'sql_error')
        self.assertEqual(first['execution_feedback']['kind'], 'missing_column')
        self.assertTrue(first['execution_feedback']['candidate_columns'])
        self.assertEqual(r['inspection_records'][1]['execution']['rows'], [['Door A'],['Door B']])

    def test_duplicate_not_executed_but_counts_toward_three_attempts(self):
        self.f.generator.complete.return_value = self.f.plan('SELECT COUNT(*) FROM View_Door')
        with patch('experiments.text_to_sql_top3.inspection.execute', wraps=base_tests_execute) as execute:
            r = self.run_flow()
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(r['inspection_metrics']['query_attempts'], 3)
        self.assertEqual([x['status'] for x in r['inspection_records']], ['ok','duplicate_skipped','duplicate_skipped'])

    def test_metadata_query_returns_only_public_views_and_no_object_data(self):
        result = cached_query("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name",
            self.f.schemas, ['View_Door'], self.f.inventory['physical_views'])
        self.assertIsNone(result['error'])
        self.assertIn(['View_Window'], result['rows'])
        self.assertFalse(any(row[0].startswith('_sv_') for row in result['rows']))
        self.assertEqual(result['result_kind'], 'schema_metadata')
        self.assertEqual(result['actual_reads'], [])

    def test_cached_table_info_uses_selected_schema(self):
        result = cached_query("SELECT name FROM pragma_table_info('View_Door')",
            self.f.schemas, ['View_Door'], self.f.inventory['physical_views'])
        self.assertIsNone(result['error'])
        self.assertEqual([x[0] for x in result['rows']], self.f.schemas['View_Door'])
        denied = cached_query("SELECT name FROM pragma_table_info('View_Window')",
            self.f.schemas, ['View_Door'], self.f.inventory['physical_views'])
        self.assertIsNotNone(denied['error'])

    def test_schema_catalogue_cannot_be_used_to_read_object_tables_or_join_them(self):
        result = cached_query('SELECT d.Name FROM sqlite_master m JOIN View_Door d ON 1=1',
            self.f.schemas, ['View_Door'], self.f.inventory['physical_views'])
        self.assertIsNotNone(result['error'])
        self.assertEqual(result['rows'], [])

    def test_cached_metadata_does_not_turn_view_names_into_query_permission(self):
        self.f.generator.complete.side_effect = [
            self.f.plan("SELECT name FROM sqlite_master WHERE type='view'"),
            self.f.plan('SELECT * FROM View_Window'), self.f.plan()]
        r = self.run_flow()
        evidence = observations(r['inspection_records'])
        self.assertEqual(evidence[0]['execution']['result_kind'], 'schema_metadata')
        self.assertEqual(r['inspection_records'][1]['status'], 'sql_error')

    def test_same_view_main_rewrite_gets_sql_error_and_answer_reason(self):
        bad = {**self.f.fixture.generation(), 'sql': 'SELECT MissingName FROM View_Door'}
        self.f.generator.generate.side_effect = [bad, self.f.fixture.generation()]
        negative = self.f.answer(False)
        negative['decision']['reason'] = 'Fix MissingName; need an actual door count.'
        self.f.answerer.generate.side_effect = [negative, self.f.answer(True)]
        r = self.run_flow()
        self.assertEqual([a['top_k'] for a in r['attempts']], [3,3])
        self.assertEqual([a['attempt_kind'] for a in r['attempts']], ['initial','rewrite'])
        feedback = self.f.generator.generate.call_args.kwargs['feedback']
        self.assertEqual(feedback[0]['sql_status'], 'sql_error')
        self.assertIn('Fix MissingName', feedback[0]['answerability']['reason'])
        self.assertEqual(feedback[0]['execution_feedback']['kind'], 'missing_column')
        self.assertEqual(r['main_sql_rewrites'], 1)
        self.assertEqual(r['status'], 'answered')

    def test_one_main_rewrite_budget_shared_across_expansion_and_no_extra_inspection_budget(self):
        self.f.generator.complete.side_effect = [
            self.f.plan('SELECT Name FROM View_Door'),
            self.f.plan('SELECT COUNT(*) FROM View_Door'),
            self.f.plan('SELECT COUNT(*) FROM View_Space')]
        self.f.answerer.generate.return_value = self.f.answer(False)
        r = self.run_flow()
        self.assertEqual([a['top_k'] for a in r['attempts']], [3,3,6])
        self.assertEqual(r['main_sql_rewrites'], 1)
        self.assertEqual(r['inspection_metrics']['query_attempts'], 3)
        self.assertEqual(self.f.generator.complete.call_count, 3)
        self.assertEqual(len(self.f.generator.generate.call_args.kwargs['feedback']), 2)

    def test_empty_rows_are_normal_evidence_not_execution_failure(self):
        self.f.generator.complete.side_effect = [self.f.plan("SELECT Name FROM View_Door WHERE Name='absent'"), self.f.plan()]
        r = self.run_flow()
        first = r['inspection_records'][0]
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(first['execution']['rows'], [])
        self.assertNotIn('execution_feedback', first)

    def test_feedback_excludes_benchmark_fields_and_marks_large_previews(self):
        attempt = {'top_k':3,'selected_views':['View_Door'],'sql_status':'ok',
                   'generation':{'sql':'SELECT Name FROM View_Door','reference_sql':'SECRET'},
                   'execution':{'columns':['Name'],'rows':[['x'*500] for _ in range(50)],
                                'error':None,'truncated':True,'row_count':None,'rows_saved':50},
                   'answer_generation':{'decision':{'answerable':False,'answer':'Partial','reason':'Need complete list','gold':'SECRET'}},
                   'ground_truth':'SECRET'}
        result = previous_attempts([attempt])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertTrue(result[0]['execution']['rows_previewed'])
        self.assertTrue(result[0]['execution']['rows'][0][0]['truncated'])


from experiments.text_to_sql_top3.sql import execute as base_tests_execute

if __name__ == '__main__': unittest.main()
