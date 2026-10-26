"""Single-pass stage counts and live-routing integration, without model/API calls."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_adaptive_sql_answer as base
from experiments.text_to_sql_top3.answer import AnswerGenerator, SYSTEM as ANSWER_SYSTEM
from experiments.text_to_sql_top3.runner import process_case, run
from experiments.text_to_sql_top3.single_pass import validate_single_pass


class SinglePass347Tests(unittest.TestCase):
    def setUp(self):
        self.f=base.AdaptiveSQLAnswerTest('test_answerable_top1_stops_without_retrieving_second_group')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config={**self.f.config,'single_pass':True,'routing_mode':'live',
                     'top_k':3,'retrieval_limits':[3],'inspection_enabled':False,
                     'inspection_repair_enabled':False,'sql_feedback_enabled':False,
                     'view_scores_context':False,'main_sql_max_rewrites':0,
                     'statistics_on_demand':False}
        self.router=Mock()
        self.router.preflight.return_value={'synthetic':True}
        self.probs={v:0.0 for v in self.f.ranking}
        self.probs.update(View_Window=.6,View_Space=.25,View_Door=.15)
        self.router.score.return_value={'probabilities':self.probs,'elapsed_ms':4,
            'request_elapsed_ms':3,'input_tokens':80,'raw_response':{'tokens_predicted':1}}
        self.f.generator.generate.side_effect=lambda *a,**kw:self.f.fixture.generation(output_tokens=7)
        self.f.answerer.generate.return_value=self.answer(True)

    def answer(self,ok):
        value=self.f.verdict(ok,reason='Evidence checked')
        value['decision']['answer']='Two doors.' if ok else 'The requested field is not available in the retrieved results.'
        return {**value,'input_tokens':11,'output_tokens':13}

    def perform(self,config=None):
        return process_case(self.f.fixture.case,config or self.config,self.f.inventory,
                            self.f.schemas,self.f.generator,self.router,
                            {'sql':'SECRET_REFERENCE'},answerer=self.f.answerer)

    def test_exactly_one_routing_sql_and_answer(self):
        r=self.perform()
        self.router.score.assert_called_once()
        self.f.generator.generate.assert_called_once()
        self.f.answerer.generate.assert_called_once()
        self.f.generator.complete.assert_not_called()
        self.assertEqual(r['completed_model_responses'],{'routing':1,'sql':1,'answer':1})
        self.assertEqual(len(r['attempts']),1)
        self.assertNotIn('inspection_records',r)
        self.assertEqual(r['main_sql_rewrites'],0)

    def test_top3_comes_from_fresh_router_not_saved_route(self):
        r=self.perform()
        self.assertEqual(r['selected_groups'],['View_Window','View_Space','View_Door'])
        self.assertNotEqual(r['selected_groups'],self.f.fixture.case['saved_route']['ranking'][:3])
        self.assertNotIn('View_Wall',self.f.generator.generate.call_args.args[1])

    def test_false_answerability_still_stops_and_keeps_user_facing_answer(self):
        self.f.answerer.generate.return_value=self.answer(False)
        r=self.perform()
        self.assertEqual(r['status'],'not_answerable')
        self.assertIn('not available',r['final_answer'])
        self.assertEqual(r['stop_reason'],'single_pass_complete')
        self.assertEqual(len(r['attempts']),1)
        self.f.generator.generate.assert_called_once()

    def test_sql_error_goes_to_final_answer_once_without_repair(self):
        self.f.generator.generate.side_effect=lambda *a,**kw:self.f.fixture.generation('SELECT MissingColumn FROM View_Door')
        self.f.answerer.generate.return_value=self.answer(False)
        r=self.perform()
        self.assertEqual(r['attempts'][0]['sql_status'],'sql_error')
        self.f.answerer.generate.assert_called_once()
        self.assertIsNotNone(self.f.answerer.generate.call_args.args[1][0]['execution']['error'])
        self.assertEqual(r['completed_model_responses'],{'routing':1,'sql':1,'answer':1})

    def test_empty_result_does_not_trigger_more_sql(self):
        self.f.generator.generate.side_effect=lambda *a,**kw:self.f.fixture.generation("SELECT Name FROM View_Door WHERE Name='absent'")
        self.f.answerer.generate.return_value=self.answer(False)
        r=self.perform()
        self.assertEqual(r['execution']['rows'],[])
        self.assertEqual(r['attempts'][0]['sql_status'],'ok')
        self.f.generator.generate.assert_called_once()

    def test_final_answer_failure_is_recorded_without_retry(self):
        self.f.answerer.generate.return_value={'decision':None,'error':'Invalid JSON'}
        r=self.perform()
        self.assertEqual(r['status'],'answer_generation_error')
        self.f.answerer.generate.assert_called_once()
        self.assertEqual(len(r['attempts']),1)

    def test_live_routing_error_does_not_fall_back_to_saved_ranking(self):
        self.router.score.side_effect=RuntimeError('Routing unavailable')
        r=self.perform()
        self.assertEqual(r['status'],'generation_error')
        self.f.generator.generate.assert_not_called()
        self.f.answerer.generate.assert_not_called()

    def test_usage_includes_measured_live_routing(self):
        r=self.perform()
        usage=r['model_usage_routing_sql_answer']
        self.assertEqual(usage['input_tokens'],101)
        self.assertEqual(usage['output_tokens'],21)
        self.assertEqual(usage['responses_with_usage'],3)
        self.assertEqual(usage['generation_ms'],6.5)
        self.assertGreater(r['pipeline_elapsed_ms'],0)

    def test_profiles_loaded_only_for_live_top3_and_companions(self):
        cfg={**self.config,'statistics_on_demand':True,'column_context':'statistics',
             'statistics_directory':str(self.f.root/'statistics')}
        r=self.perform(cfg)
        self.assertEqual(r['status'],'answered')
        self.assertEqual(set(r['column_profile_fingerprints']),
                         {'View_Window','View_Space','View_Door','View_DoorCustomPset'})
        profiles=self.f.generator.generate.call_args.kwargs['profiles']
        self.assertNotIn('View_Wall',profiles)
        self.assertEqual(profiles['View_Door']['row_count'],2)
        self.assertGreaterEqual(r['column_statistics_elapsed_ms'],0)

    def test_config_rejects_any_repeat_path_or_saved_routing(self):
        for change in [{'inspection_enabled':True},{'retrieval_limits':[3,6]},
                       {'main_sql_max_rewrites':1},{'routing_mode':'saved'},
                       {'sql_feedback_enabled':True},{'top_k':1}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_single_pass({**self.config,**change})

    def test_answer_prompt_has_finality_without_changing_scoring_rubric(self):
        answerer=AnswerGenerator(Mock(),self.config)
        answerer.generator.complete=Mock(return_value={
            'content':'{"answerable":false,"answer":"No value was found.","reason":"Field empty"}',
            'truncated':False,'output_limit_reached':False})
        answerer.generate('Q',[])
        text=answerer.generator.complete.call_args.args[0][0]['content']
        self.assertIn('No additional retrieval, inspection or SQL revision is available.',text)
        self.assertNotIn('will request the next ranked Views',text)
        self.assertEqual(text.split('# Answer format')[1],ANSWER_SYSTEM.split('# Answer format')[1])

    def test_questions_and_reference_answers_do_not_leak_to_model_evidence(self):
        r=self.perform()
        self.assertNotIn('SECRET_REFERENCE',json.dumps(self.f.generator.generate.call_args.args))
        self.assertNotIn('SECRET_ANSWER',json.dumps(r['messages']))
        self.assertIsNone(r['reference_comparison'])

    def test_mocked_complete_run_summary_has_three_responses_no_expansion(self):
        self.f.fixture.prepare_fixture()
        # This fixture's DB has only the three original groups.
        groups=self.f.fixture.inventory['groups']
        self.router.score.return_value={**self.router.score.return_value,
                                       'probabilities':dict(zip(groups,[.8,.15,.05]))}
        server=Mock()
        server.__enter__=Mock(return_value=SimpleNamespace(command=['MOCK']))
        server.__exit__=Mock(return_value=False)
        with (patch('experiments.text_to_sql_top3.runner.ManagedServer',return_value=server),
              patch('experiments.text_to_sql_top3.runner.HTTP') as http,
              patch('experiments.text_to_sql_top3.runner.LlamaDecisionPort',return_value=self.router),
              patch('experiments.text_to_sql_top3.runner.SQLGenerator',return_value=self.f.generator),
              patch('experiments.text_to_sql_top3.runner.AnswerGenerator',return_value=self.f.answerer),
              patch('experiments.text_to_sql_top3.runner.hardware_snapshot',return_value={})):
            http.return_value.request.return_value={'synthetic':True}
            summary=run(self.config)
        self.assertTrue(summary['complete'],summary.get('fatal_error'))
        self.assertEqual(summary['completed_model_responses'],{'routing':1,'sql':1,'answer':1})
        self.assertEqual(summary['single_pass_count_violations'],[])
        self.assertEqual(summary['expanded_questions'],0)
        self.assertEqual(summary['main_sql_rewrites'],0)
        output=next(Path(self.config['results_directory']).iterdir())
        manifest=json.loads((output/'run_manifest.json').read_text())
        self.assertTrue(manifest['first_pass_only'])


if __name__=='__main__':unittest.main()
