"""Cumulative retrieval with bounded inspection and optional main SQL rewrite."""

from __future__ import annotations

from .generation import ContextOverflow, messages
from .schema import physical_views, retrieval_schedule
from .sql import execute
from .inspection import InspectionSession, observations
from .feedback import previous_attempts, execution_feedback


def sql_attempt(
    question,
    groups,
    newly_added,
    config,
    inventory,
    schemas,
    generator,
    profiles=None,
    descriptions=None,
    inspections=None,
    view_scores=None,
    feedback=None,
):
    views = physical_views(groups, inventory)
    attempt = {
        "top_k": len(groups),
        "selected_groups": groups,
        "newly_added_groups": newly_added,
        "selected_views": views,
        "messages": messages(question, views, schemas, profiles, descriptions, inspections,
                             view_scores, feedback),
        "generation": None,
        "execution": None,
        "sql_status": None,
        "error": None,
        "answer_generation": None,
    }
    try:
        context = {}
        if profiles is not None:
            context["profiles"] = profiles
        if descriptions is not None:
            context["descriptions"] = descriptions
        if inspections:
            context["inspections"] = inspections
        if view_scores is not None:
            context['view_scores'] = view_scores
        if feedback:
            context['feedback'] = feedback
        generation = generator.generate(question, views, schemas, **context)
        attempt["generation"] = generation
        if generation["truncated"]:
            attempt["sql_status"] = "input_truncated"
        elif generation["output_limit_reached"]:
            attempt["sql_status"] = "generation_limit"
        else:
            execution = execute(
                inventory["database"],
                views,
                generation["sql"],
                timeout=config["sql_timeout_seconds"],
                max_rows=config["max_result_rows"],
            )
            attempt["execution"] = execution
            attempt["sql_status"] = (
                "sql_error"
                if execution["error"]
                else "result_truncated" if execution["truncated"] else "ok"
            )
    except ContextOverflow as exc:
        attempt.update(sql_status="context_overflow", error=str(exc))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        attempt.update(sql_status="generation_error", error=str(exc))
    if config.get('sql_feedback_enabled'):
        error = attempt.get('error') or (attempt.get('execution') or {}).get('error')
        attempt['execution_feedback'] = execution_feedback(
            error, (attempt.get('generation') or {}).get('sql'), views, schemas,
            inventory['physical_views'])
    return attempt


def retrieve_and_answer(
    question,
    ranking,
    config,
    inventory,
    schemas,
    generator,
    answerer,
    profiles=None,
    descriptions=None,
    view_scores=None,
):
    if answerer is None:
        raise ValueError("The adaptive workflow requires the final answer model")
    if not ranking:
        raise ValueError("No ranked View groups are available")
    attempts = []
    outcome = {
        "attempts": attempts,
        "answerable": None,
        "final_answer": None,
        "answer_reason": None,
        "status": "not_answerable",
        "stop_reason": None,
    }
    previous_count = 0
    if config.get('view_scores_context'):
        if not isinstance(view_scores, dict) or set(view_scores) != set(inventory['groups']):
            raise ValueError('View scores must match the current database View groups')
        view_scores = {v: view_scores[v] for v in ranking}
    else:
        view_scores = None
    rewrites_remaining = config.get('main_sql_max_rewrites', 0)
    outcome['main_sql_rewrites'] = 0
    inspector = (
        InspectionSession(generator, config, inventory)
        if config.get("inspection_enabled", False) else None
    )
    if inspector:
        outcome["inspection_records"] = inspector.records
    for limit in retrieval_schedule(config):
        groups = ranking[:limit]
        if len(groups) == previous_count:
            outcome["stop_reason"] = "no_more_ranked_views"
            break
        is_rewrite = False
        while True:
            inspected = None
            feedback = previous_attempts(attempts) if config.get('sql_feedback_enabled') else None
            if inspector:
                inspector.run_stage(question, groups, physical_views(groups, inventory),
                                    schemas, profiles, descriptions, view_scores, feedback)
                inspected = observations(inspector.records)
            attempt = sql_attempt(
                question, groups, [] if is_rewrite else groups[previous_count:],
                config, inventory, schemas, generator, profiles, descriptions,
                inspections=inspected, view_scores=view_scores, feedback=feedback,
            )
            attempt['attempt_kind'] = 'rewrite' if is_rewrite else 'initial' if not attempts else 'expanded'
            if inspector:
                attempt['inspection_evidence'] = inspected
            attempts.append(attempt)
            previous_count = len(groups)
            try:
                judgment = answerer.generate(question, attempts)
                attempt['answer_generation'] = judgment
                if judgment.get('error') or not judgment.get('decision'):
                    raise ValueError(judgment.get('error') or 'Answer model returned no decision')
                decision = judgment['decision']
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                outcome.update(status='answer_generation_error', stop_reason='invalid_answer_decision',
                               answer_reason=str(exc))
                break
            outcome.update(answerable=decision['answerable'], final_answer=decision['answer'],
                           answer_reason=decision['reason'])
            if decision['answerable']:
                outcome.update(status='answered', stop_reason='answer_model_can_answer')
                break
            if rewrites_remaining and attempt['sql_status'] in ('ok', 'sql_error', 'result_truncated'):
                rewrites_remaining -= 1
                outcome['main_sql_rewrites'] += 1
                is_rewrite = True
                continue
            break
        if outcome['status'] in ('answered', 'answer_generation_error'):
            break
    if outcome["stop_reason"] is None:
        outcome["stop_reason"] = (
            "no_more_ranked_views" if previous_count == len(ranking) else "retrieval_limit_reached"
        )
    last = attempts[-1]
    if inspector:
        outcome["inspection_metrics"] = inspector.metrics()
    # Legacy top-level fields describe the last SQL attempt; all attempts are retained above.
    outcome.update(
        {
            key: last[key]
            for key in (
                "selected_groups",
                "selected_views",
                "messages",
                "generation",
                "execution",
                "error",
            )
        }
    )
    return outcome
