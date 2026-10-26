"""One live routing result -> one main SQL -> one final answer, without loops."""
from .workflow import sql_attempt


def validate_single_pass(config):
    if not config.get('single_pass'):
        return
    if (config.get('routing_mode') != 'live' or not config.get('answer_stage')
            or config.get('top_k') != 3 or config.get('retrieval_limits') != [3]
            or config.get('inspection_enabled') or config.get('inspection_repair_enabled')
            or config.get('sql_feedback_enabled') or config.get('main_sql_max_rewrites', 0)):
        raise ValueError('Single pass requires live routing, Top3 only, one answer stage, '
                         'and no inspection, feedback loop or SQL rewrite.')


def retrieve_once(question, ranking, config, inventory, schemas, generator, answerer,
                  profiles=None, descriptions=None, view_scores=None):
    validate_single_pass(config)
    if not ranking or answerer is None:
        raise ValueError('Single pass requires ranked Views and one final-answer model')
    groups = ranking[:3]
    attempt = sql_attempt(question, groups, groups, config, inventory, schemas,
                          generator, profiles, descriptions,
                          view_scores=view_scores if config.get('view_scores_context') else None)
    attempt['attempt_kind'] = 'single_pass'
    attempts = [attempt]
    outcome = {'attempts': attempts, 'answerable': None, 'final_answer': None,
               'answer_reason': None, 'status': 'answer_generation_error',
               'stop_reason': 'single_pass_complete', 'main_sql_rewrites': 0}
    try:
        answer = answerer.generate(question, attempts)
        attempt['answer_generation'] = answer
        if answer.get('error') or not answer.get('decision'):
            raise ValueError(answer.get('error') or 'No final answer decision')
        decision = answer['decision']
        outcome.update(answerable=decision['answerable'], final_answer=decision['answer'],
                       answer_reason=decision['reason'],
                       status='answered' if decision['answerable'] else 'not_answerable')
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        outcome.update(answer_reason=str(exc), stop_reason='single_pass_answer_error')
    # An SQL error/empty/NULL result is still sent once to the final answer model.
    # The decision is descriptive only; it never triggers further generation.
    outcome.update({k:attempt[k] for k in ('selected_groups','selected_views','messages',
                                          'generation','execution','error')})
    return outcome
