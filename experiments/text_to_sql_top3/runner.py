from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time

from experiments.view_selection_ablation.backend import HTTP, LlamaDecisionPort, ManagedServer
from experiments.view_selection_ablation.common import (
    check_pin,
    digest_json,
    file_hash,
    read_json,
    write_json,
)
from experiments.view_selection_ablation.evaluation import rank_options
from experiments.view_selection_ablation.prompts import ordered_options
from experiments.view_selection_ablation.runner import hardware_snapshot, select_cases
from .generation import ContextOverflow, SQLGenerator, messages
from .answer import AnswerGenerator
from .schema import HERE, load_prepared_sql, physical_views, retrieval_schedule
from .sql import execute, result_agreement
from .workflow import retrieve_and_answer
from .single_pass import retrieve_once, validate_single_pass
from .profiles import context_mode, context_policy, load_profiles
from .descriptions import load_descriptions


def server_config(config, manifest):
    return {
        **manifest["base_server_config"],
        **{key: config[key] for key in ("context_tokens", "port", "http_timeout_seconds", "seed")},
    }


def plan(config, *, limit=None, ids=None):
    validate_single_pass(config)
    manifest, inventories, cases, schemas = load_prepared_sql(config)
    selected = choose_cases(cases, config, limit=limit, ids=ids)
    profile_started = time.perf_counter()
    profile_config = config
    preview_cases = selected
    if config.get('single_pass') and config.get('statistics_on_demand'):
        # Preview only: actual routing remains one fresh inference during run.
        profile_config = {**config, 'routing_mode': 'saved'}
        preview_cases = [{**c, 'saved_route': {'ranking': preview_ranking(c, inventories[c['inventory_key']], config)}}
                         for c in selected]
    profiles, profile_fingerprints = load_profiles(profile_config, inventories, schemas, preview_cases)
    profile_ms = (time.perf_counter() - profile_started) * 1000
    descriptions, dictionary_identity = load_descriptions(config, inventories, schemas, selected)
    adaptive = config.get("answer_stage", False)
    limits = retrieval_schedule(config) if adaptive else [config["top_k"]]
    maximum_rounds = sum(
        len({min(k, len(inventories[c["inventory_key"]]["groups"])) for k in limits})
        for c in selected
    )
    maximum_rounds += len(selected) * config.get('main_sql_max_rewrites', 0)
    maxima = []
    # Frozen cases may contain an old Top-3 selection. Always derive the current
    # selection from the full frozen ranking and this run's retrieval policy.
    for limit_k in limits:
        details = []
        for case in selected:
            inv = inventories[case["inventory_key"]]
            groups = preview_ranking(case, inv, config)[:limit_k]
            views = physical_views(groups, inv)
            chat = messages(
                case["question"],
                views,
                schemas[case["inventory_key"]],
                profiles.get(case["inventory_key"]),
                descriptions.get(case["inventory_key"]),
                view_scores=case.get('saved_route', {}).get('probabilities') if config.get('view_scores_context') else None,
            )
            details.append(
                {
                    "id": case["id"],
                    "database": case["inventory_key"],
                    "top_k": len(groups),
                    "requested_top_k": limit_k,
                    "selected_groups": groups,
                    "physical_views": views,
                    "columns": sum(len(schemas[case["inventory_key"]][v]) for v in views),
                    "prompt_characters": sum(len(m["content"]) for m in chat),
                }
            )
        maxima.append(max(details, key=lambda d: d["prompt_characters"]))
    example_case = selected[0]
    example_views = physical_views(
        preview_ranking(example_case, inventories[example_case["inventory_key"]], config)[: limits[0]],
        inventories[example_case["inventory_key"]],
    )
    return {
        "prepared_fingerprint": manifest["fingerprint"],
        "questions_available": len(cases),
        "selected_questions": len(selected),
        "routing_mode": config["routing_mode"],
        "routing_condition": config["routing_condition"],
        "model": manifest["model"],
        "sql_generation_requests": len(selected),
        "sql_generation_requests_max": maximum_rounds,
        "answer_generation_requests_min": len(selected) if adaptive else 0,
        "answer_generation_requests_max": maximum_rounds if adaptive else 0,
        "retrieval_limits": limits,
        "initial_top_k": limits[0],
        "answer_model": manifest["model"]["path"] if adaptive else None,
        "answerability_owner": "final natural-language answer model" if adaptive else None,
        "new_routing_requests": len(selected) if config["routing_mode"] == "live" else 0,
        "view_scores_context": config.get('view_scores_context', False),
        "main_sql_max_rewrites": config.get('main_sql_max_rewrites', 0),
        "inspection_repair_enabled": config.get('inspection_repair_enabled', False),
        "single_pass": config.get('single_pass', False),
        "planned_model_calls_per_question": {'routing': 1, 'sql': 1, 'answer': 1} if config.get('single_pass') else None,
        "reference_comparison_candidates": (
            sum(c["historical_reference_eligible"] for c in selected)
            if config["compare_reference_results"]
            else 0
        ),
        "context_tokens": config["context_tokens"],
        "max_new_tokens": config["max_new_tokens"],
        "maximum_saved_route_prompt": maxima[0],
        "maximum_expanded_prompt": maxima[-1] if len(maxima) > 1 else None,
        "maximum_prompt_by_retrieval_limit": maxima,
        "prompt_plan_basis": ("alphabetical schema preview only; live routing runs during inference"
                              if config["routing_mode"] == "live" and not example_case.get("saved_route")
                              else "saved ranking; live routing may select different Views"),
        "token_fit": "Exact tokenization is checked before each generation; overflow is recorded without truncation",
        "context_policy": context_policy(config),
        "column_context": context_mode(config),
        "column_descriptions": config.get("column_descriptions", False),
        "column_dictionary": dictionary_identity,
        "statistics_preparation_ms": profile_ms,
        "profile_views": sum(map(len, profile_fingerprints.values())),
        "profile_fingerprint": digest_json(profile_fingerprints),
        "model_loaded": False,
        "inference_executed": False,
        "example_messages": messages(
            example_case["question"],
            example_views,
            schemas[example_case["inventory_key"]],
            profiles.get(example_case["inventory_key"]),
            descriptions.get(example_case["inventory_key"]),
            view_scores=example_case.get('saved_route', {}).get('probabilities') if config.get('view_scores_context') else None,
        ),
    }


def preview_ranking(case, inventory, config):
    if "saved_route" in case:
        return case["saved_route"]["ranking"]
    if config["routing_mode"] != "live":
        raise ValueError("No saved route; live routing is required")
    return sorted(inventory["groups"])


def choose_cases(cases, config, *, limit=None, ids=None):
    if ids:
        requested = {str(i) for i in ids}
        if requested - {case["id"] for case in cases}:
            raise ValueError("Unknown question ID")
        cases = [case for case in cases if case["id"] in requested]
    if config.get("input_mode") == "custom_live":
        selected = [c for c in cases if c["run_eligible"]]
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            selected = sorted(selected, key=lambda c: digest_json([config["seed"], c["id"], "sample"]))[:limit]
        selected = sorted(selected, key=lambda c: c["id"])
    else:
        selected = select_cases(cases, limit, config["seed"])
    if not selected:
        raise ValueError("No questions selected")
    return selected


def process_case(
    case,
    config,
    inventory,
    schemas,
    generator,
    router,
    reference,
    answerer=None,
    profiles=None,
    descriptions=None,
):
    started = time.perf_counter()
    record = {
        "id": case["id"],
        "project": case["project"],
        "ifc_model": case["ifc_model"],
        "database": inventory["database"]["path"],
        "question": case["question"],
        "routing_mode": config["routing_mode"],
        "routing_condition": config["routing_condition"],
        "column_context": context_mode(config),
        "column_descriptions": config.get("column_descriptions", False),
        "initial_top_k": config["top_k"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": None,
        "error": None,
        "generation": None,
        "execution": None,
        "reference_comparison": None,
    }
    try:
        validate_single_pass(config)
        if context_mode(config) == "statistics" and profiles is None and not config.get('statistics_on_demand'):
            raise ValueError("Column statistics are required; refusing a names-only fallback")
        if config.get("column_descriptions", False) and descriptions is None:
            raise ValueError(
                "Column descriptions are required; refusing a statistics-only fallback"
            )
        if config["routing_mode"] == "live":
            options = ordered_options(case, inventory, config["seed"], config["routing_repeat"])
            score = router.score(
                case["question"], options, inventory["descriptions"], config["routing_condition"]
            )
            ranking = rank_options(score["probabilities"])
            record["routing"] = {
                "ranking": ranking,
                "elapsed_ms": score["elapsed_ms"],
                "score": score,
            }
        else:
            ranking = case["saved_route"]["ranking"]
            record["routing"] = {**case["saved_route"], "new_inference_executed": False}
        if config.get('single_pass') and config.get('statistics_on_demand') and profiles is None:
            profile_started = time.perf_counter()
            key = case['inventory_key']
            selected_profiles, profile_fingerprints = load_profiles(
                {**config, 'routing_mode': 'saved'}, {key: inventory}, {key: schemas},
                [{**case, 'saved_route': {'ranking': ranking}}])
            profiles = selected_profiles.get(key)
            record['column_statistics_elapsed_ms'] = (time.perf_counter()-profile_started)*1000
            record['column_profile_fingerprints'] = profile_fingerprints.get(key, {})
        if config.get("answer_stage", False):
            record.update(
                (retrieve_once if config.get('single_pass') else retrieve_and_answer)(
                    case["question"],
                    ranking,
                    config,
                    inventory,
                    schemas,
                    generator,
                    answerer,
                    profiles,
                    descriptions,
                    view_scores=(record['routing'].get('probabilities') or record['routing'].get('score', {}).get('probabilities')),
                )
            )
        else:
            record["selected_groups"] = ranking[: config["top_k"]]
            record["selected_views"] = physical_views(record["selected_groups"], inventory)
            record["messages"] = messages(
                case["question"], record["selected_views"], schemas, profiles, descriptions
            )
            context = {}
            if profiles is not None:
                context["profiles"] = profiles
            if descriptions is not None:
                context["descriptions"] = descriptions
            generation = generator.generate(
                case["question"], record["selected_views"], schemas, **context
            )
            record["generation"] = generation
            if generation["truncated"]:
                record["status"] = "input_truncated"
            elif generation["output_limit_reached"]:
                record["status"] = "generation_limit"
            else:
                execution = execute(
                    inventory["database"],
                    record["selected_views"],
                    generation["sql"],
                    timeout=config["sql_timeout_seconds"],
                    max_rows=config["max_result_rows"],
                )
                record["execution"] = execution
                record["status"] = (
                    "sql_error"
                    if execution["error"]
                    else "result_truncated" if execution["truncated"] else "ok"
                )
    except ContextOverflow as exc:
        record.update(status="context_overflow", error=str(exc))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        record.update(status="generation_error", error=str(exc))
    record["pipeline_elapsed_ms"] = (time.perf_counter() - started) * 1000
    generations = []
    for attempt in record.get("attempts", []):
        generations.extend(g for g in (attempt.get("generation"), attempt.get("answer_generation")) if g)
    generations.extend(r["generation"] for r in record.get("inspection_records", []) if r.get("generation"))
    record["model_usage_sql_answer_inspection"] = {
        "input_tokens": sum(g.get("input_tokens") or 0 for g in generations),
        "output_tokens": sum(g.get("output_tokens") or 0 for g in generations),
        "generation_ms": sum(g.get("generation_ms") or 0 for g in generations),
        "responses_with_usage": len(generations),
        "note": "SQL, answer, and inspection responses; saved routing excluded. Failed calls without a response may have no usage.",
    }
    if config.get('single_pass'):
        route = record.get('routing', {}).get('score')
        completed = {'routing': int(route is not None),
                     'sql': sum(a.get('generation') is not None for a in record.get('attempts', [])),
                     'answer': sum(a.get('answer_generation') is not None for a in record.get('attempts', []))}
        record['single_pass'] = True
        record['completed_model_responses'] = completed
        record['planned_model_calls'] = {'routing': 1, 'sql': 1, 'answer': 1}
        route = route or {}
        usage = dict(record['model_usage_sql_answer_inspection'])
        usage.update(input_tokens=usage['input_tokens']+(route.get('input_tokens') or 0),
                     output_tokens=usage['output_tokens']+((route.get('raw_response') or {}).get('tokens_predicted', 1) if route else 0),
                     generation_ms=usage['generation_ms']+(route.get('request_elapsed_ms') or 0),
                     responses_with_usage=usage['responses_with_usage']+completed['routing'],
                     note='Measured live routing, SQL and final answer responses. Failed calls without responses may have unavailable usage.')
        record['model_usage_routing_sql_answer'] = usage
        sql_gen = record.get('generation') or {}
        final_gen = (record.get('attempts') or [{}])[-1].get('answer_generation') or {}
        record['stage_metrics'] = {
            'routing_elapsed_ms':route.get('elapsed_ms'),
            'column_statistics_elapsed_ms':record.get('column_statistics_elapsed_ms', 0),
            'sql_generation_elapsed_ms':sql_gen.get('elapsed_ms', sql_gen.get('generation_ms')),
            'sql_execution_elapsed_ms':(record.get('execution') or {}).get('elapsed_ms'),
            'answer_generation_elapsed_ms':final_gen.get('elapsed_ms', final_gen.get('generation_ms')),
        }
    # Reference execution is a separate evaluation stage, after the model response.
    if reference and config["compare_reference_results"]:
        replay = execute(
            inventory["database"],
            reference["views"],
            reference["sql"],
            timeout=config["sql_timeout_seconds"],
            max_rows=config["max_result_rows"],
        )
        record["reference_comparison"] = {
            "basis": reference["basis"],
            "reference_sql": reference["sql"],
            "reference_views": reference["views"],
            "reference_execution": replay,
            "result_agreement": result_agreement(record["execution"], replay),
            "definition": "Strict unordered row multiset; duplicates and column order preserved; column aliases ignored; no numeric rounding",
            "semantic_correctness": None,
        }
    return record


def report(config, output, selected, records, *, fatal_error=None):
    limits = retrieval_schedule(config) if config.get("answer_stage") else [config["top_k"]]
    counts = Counter(record["status"] for record in records)
    comparisons = [r["reference_comparison"] for r in records if r["reference_comparison"]]
    valid = [c for c in comparisons if c["result_agreement"] is not None]
    matched = sum(c["result_agreement"] is True for c in valid)
    attempts = [a for r in records for a in r.get("attempts", [r])]
    sql_statuses = Counter(a.get("sql_status", a.get("status")) for a in attempts)
    answer_times = [
        a["answer_generation"]["generation_ms"]
        for a in attempts
        if a.get("answer_generation") and a["answer_generation"].get("generation_ms") is not None
    ]

    def mean(key):
        values = [
            r["generation"][key]
            for r in attempts
            if r["generation"] and r["generation"].get(key) is not None
        ]
        return statistics.mean(values) if values else None

    summary = {
        "format": "BIBIM-ranked-views-sql-results/2",
        "column_context": context_mode(config),
        "column_descriptions": config.get("column_descriptions", False),
        "context_policy": context_policy(config),
        "initial_top_k": config["top_k"],
        "retrieval_limits": limits,
        "planned_questions": len(selected),
        "recorded_questions": len(records),
        "pending_questions": len(selected) - len(records),
        "complete": len(selected) == len(records) and fatal_error is None,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status_counts": dict(counts),
        "fatal_error": fatal_error,
        "sql_execution_successes": sum(
            r["execution"] is not None and not r["execution"]["error"] for r in records
        ),
        "complete_result_successes": sum(
            r["execution"] is not None
            and not r["execution"]["error"]
            and not r["execution"]["truncated"]
            for r in records
        ),
        "sql_attempts": len(attempts),
        "sql_attempt_status_counts": dict(sql_statuses),
        "answer_generation_requests": sum(
            a.get("answer_generation") is not None for a in attempts
        ),
        "model_declared_answerable": counts["answered"],
        "model_declared_not_answerable": counts["not_answerable"],
        "expanded_questions": sum(len({a['top_k'] for a in r.get('attempts', [])}) > 1 for r in records),
        "answered_after_expansion": sum(
            r["status"] == "answered" and len({a['top_k'] for a in r.get('attempts', [])}) > 1 for r in records
        ),
        "mean_generation_ms": mean("generation_ms"),
        "mean_answer_generation_ms": statistics.mean(answer_times) if answer_times else None,
        "mean_input_tokens": mean("input_tokens"),
        "reference_comparisons_attempted": len(comparisons),
        "reference_comparable_questions": len(valid),
        "reference_result_matches": matched,
        "reference_result_agreement_rate": matched / len(valid) if valid else None,
        "semantic_accuracy": None,
        "routing_mode": config["routing_mode"],
        "view_scores_context": config.get('view_scores_context', False),
        "inspection_repair_enabled": config.get('inspection_repair_enabled', False),
        "main_sql_rewrites": sum(r.get('main_sql_rewrites', 0) for r in records),
        "single_pass": config.get('single_pass', False),
        "notes": [
            "All questions run, regardless of historical SQL eligibility.",
            "Execution success and exact reference-result agreement are not semantic SQL accuracy.",
            "Reference SQL is replayed on the current designated DB, never supplied to the model.",
            context_policy(config),
            "The answer model receives the question, SQL and actual result rows.",
            "Final answer-model answerability is a routing decision, not verified correctness.",
            "Saved routing latency is historical; it is not included in the new generation runtime.",
        ],
    }
    if config.get("inspection_enabled", False):
        summary["inspection"] = {
            "max_queries_per_question": config.get("inspection_max_queries", 3),
            **{key: sum(r.get("inspection_metrics", {}).get(key, 0) for r in records)
               for key in ("planner_calls", "query_attempts", "input_tokens", "output_tokens", "elapsed_ms", "sql_elapsed_ms")},
            "questions_with_inspection_queries": sum(r.get("inspection_metrics", {}).get("query_attempts", 0) > 0 for r in records),
        }
        summary["total_model_usage_sql_answer_inspection"] = {
            key: sum(r.get("model_usage_sql_answer_inspection", {}).get(key, 0) for r in records)
            for key in ("input_tokens", "output_tokens", "generation_ms", "responses_with_usage")
        }
    if config.get('single_pass'):
        summary['completed_model_responses'] = {
            k:sum(r.get('completed_model_responses', {}).get(k, 0) for r in records)
            for k in ('routing','sql','answer')}
        summary['total_model_usage_routing_sql_answer'] = {
            k:sum(r.get('model_usage_routing_sql_answer', {}).get(k, 0) for r in records)
            for k in ('input_tokens','output_tokens','generation_ms','responses_with_usage')}
        summary['mean_pipeline_elapsed_ms'] = statistics.mean([r['pipeline_elapsed_ms'] for r in records]) if records else None
        summary['mean_live_routing_ms'] = statistics.mean([r['routing']['elapsed_ms'] for r in records if r.get('routing')]) if any(r.get('routing') for r in records) else None
        summary['column_statistics_elapsed_ms'] = sum(r.get('column_statistics_elapsed_ms', 0) for r in records)
        summary['single_pass_count_violations'] = [r['id'] for r in records
            if any(n>1 for n in r.get('completed_model_responses', {}).values())
            or len(r.get('attempts', []))>1 or r.get('inspection_records')]
        summary['notes'][-1] = 'Live routing latency is included in per-question pipeline time; no saved routing score is reused.'
        summary['notes'][-2] = 'Final answer-model answerability is descriptive only; it never triggers another retrieval or generation.'
    write_json(output / "summary.json", summary)
    by_id = {c["id"]: c for c in selected}
    with (output / "review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = [
            "id",
            "project",
            "ifc_model",
            "question",
            "ground_truth",
            "selected_groups",
            "selected_views",
            "status",
            "sql",
            "error",
            "result_preview",
            "result_truncated",
            "reference_result_agreement",
            "manual_review_status",
            "manual_correct",
            "manual_notes",
            "final_answer",
            "model_answerable",
            "answer_reason",
            "retrieval_rounds",
            "final_top_k",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for r in records:
            generation, execution = r["generation"] or {}, r["execution"] or {}
            comparison = r["reference_comparison"] or {}
            writer.writerow(
                {
                    **{
                        key: r[key] for key in ("id", "project", "ifc_model", "question", "status")
                    },
                    "ground_truth": by_id[r["id"]]["ground_truth_for_review_only"],
                    "selected_groups": json.dumps(r.get("selected_groups", [])),
                    "selected_views": json.dumps(r.get("selected_views", [])),
                    "sql": generation.get("sql"),
                    "error": r["error"] or execution.get("error"),
                    "result_preview": json.dumps(
                        {
                            "columns": execution.get("columns", []),
                            "rows": execution.get("rows", [])[:20],
                        },
                        ensure_ascii=False,
                    ),
                    "result_truncated": execution.get("truncated"),
                    "reference_result_agreement": comparison.get("result_agreement"),
                    "manual_review_status": "pending",
                    "manual_correct": "",
                    "manual_notes": "",
                    "final_answer": r.get("final_answer"),
                    "model_answerable": r.get("answerable"),
                    "answer_reason": r.get("answer_reason"),
                    "retrieval_rounds": len({a.get('top_k', config['top_k']) for a in r.get('attempts', [r])}),
                    "final_top_k": len(r.get("selected_groups", [])),
                }
            )
    lines = [
        "# Ranked Views → column context → SQL → natural-language answer",
        "",
            f"Routing: `{config['routing_mode']}` / `{config['routing_condition']}`. "
        f"Questions: {len(records)}/{len(selected)}. Complete: {summary['complete']}.",
        "",
        "SQL context: " + context_policy(config),
        "",
        "## Outcomes",
        "",
        "| Status | Questions |",
        "|---|---:|",
    ]
    lines.extend(f"| {status} | {count} |" for status, count in sorted(counts.items()))
    lines.extend(
        [
            "",
            f"SQL execution successes: {summary['sql_execution_successes']}. "
            f"Complete final SQL results: {summary['complete_result_successes']}.",
            "",
            f"Current-DB reference-result matches: {matched}/{len(valid)} comparable questions. "
            f"Reference replays unavailable/incomplete: {len(comparisons) - len(valid)}.",
            "",
            "A match compares unordered result rows, including duplicates, with column order preserved. "
            "Different valid SQL projections/rounding can fail this strict comparison. "
            "This is reference-result agreement, not independently verified semantic accuracy.",
            "",
            f"Mean generation request ms: {summary['mean_generation_ms']}. "
            f"Mean input tokens: {summary['mean_input_tokens']}.",
            "",
            "## Files and limits",
            "",
            "- `records.jsonl`: every prompt, selected Views, SQL, response, execution, timings, and reference replay.",
            "- `review.csv`: all recorded questions for independent semantic review; every label starts pending.",
            "- `summary.json`: machine-readable completion, errors, timing, and reference agreement.",
            "- `run_manifest.json`: frozen model, data, schema, configuration and code identities.",
            "- Physical `etc` is visible as a column, but its internal keys are deliberately hidden in this baseline.",
            ("- Top3 only; final answer-model judgment never triggers another retrieval."
             if config.get('single_pass') else
             f"- Retrieval schedule: {limits}; each expansion preserves earlier Views and occurs only after a negative final answer-model judgment."),
            "- Every generated query is limited to the selected physical Views in the CSV-designated database.",
            "- Context overflow and output exhaustion are recorded without silent truncation. Main SQL rewrite limit per question: " + str(config.get('main_sql_max_rewrites', 0)) + ".",
            "- A result exceeding the configured row cap is marked incomplete and is not a successful agreement.",
            ("- Live routing runs the local Gemma Decisio prompt/readout adapter once; this does not measure an official Decisio checkpoint."
             if config.get('single_pass') else
             "- Saved routing reuses an earlier Gemma/Decisio-prompt-adapter run; it does not rerun Decisio or measure official Decisio FP8 latency."),
            "- To resume, use the same configuration, limit/IDs and unchanged inputs/code.",
        ]
    )
    if config.get("answer_stage", False):
        lines.extend(
            [
                "",
                "## Final answer stage",
                "",
                f"Model declared answerable: {counts['answered']}; not answerable: {counts['not_answerable']}.",
                f"Expanded questions: {summary['expanded_questions']}; answered after expansion: {summary['answered_after_expansion']}.",
                f"SQL attempts: {len(attempts)}; SQL attempt outcomes: {dict(sql_statuses)}.",
                f"Same-View main SQL rewrites: {summary['main_sql_rewrites']}. These do not count as retrieval expansion.",
                f"Mean answer generation request ms: {summary['mean_answer_generation_ms']}.",
                "The SQL model has no abstention rule. The answer model sees all previous SQL/result attempts, including errors and result limits.",
                "Model answerability is not benchmark correctness. Reference agreement, if enabled, compares only the last SQL result and does not grade the final natural-language answer.",
            ]
        )
    if config.get('single_pass'):
        lines += ['', '## Fixed single-pass pipeline', '',
                  'One live routing inference, one SQL generation/execution, one final answer. No inspection, expansion or SQL rewrite.',
                  'Completed model responses: '+json.dumps(summary['completed_model_responses'])+'.',
                  'Routing implementation: local Gemma Decisio prompt/readout adapter; not a separately installed official Decisio checkpoint.',
                  'Pipeline time includes live routing and selected-View statistics cache loading/building.']
    if fatal_error:
        lines.extend(["", "Fatal error: " + fatal_error])
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def run(config, *, limit=None, ids=None, resume=None):
    validate_single_pass(config)
    manifest, inventories, cases, schemas = load_prepared_sql(config)
    selected = choose_cases(cases, config, limit=limit, ids=ids)
    profile_started = time.perf_counter()
    if config.get('single_pass') and config.get('statistics_on_demand'):
        profiles, profile_fingerprints = {}, {}
    else:
        profiles, profile_fingerprints = load_profiles(config, inventories, schemas, selected)
    profile_ms = (time.perf_counter() - profile_started) * 1000
    descriptions, dictionary_identity = load_descriptions(config, inventories, schemas, selected)
    support = Path(config["prepared_directory"])
    references = read_json(support / "reference_queries.json")
    code = {
        str(p.relative_to(HERE.parent)): file_hash(p)
        for folder in (HERE, HERE.parent / "view_selection_ablation")
        for p in folder.glob("*.py")
    }
    signature = digest_json(
        {
            "prepared": manifest["fingerprint"],
            "config": config,
            "ids": [c["id"] for c in selected],
            "code": code,
            "column_profiles": profile_fingerprints,
            "column_dictionary": dictionary_identity,
        }
    )
    if resume:
        output = Path(resume).resolve()
        if read_json(output / "run_manifest.json")["signature"] != signature:
            raise ValueError(
                "Cannot resume: configuration, question selection, code, or inputs differ"
            )
    else:
        output = Path(config["results_directory"]) / datetime.now(timezone.utc).strftime(
            "%Y%m%d_%H%M%S_%f_UTC"
        )
        output.mkdir(parents=True)
        write_json(
            output / "run_manifest.json",
            {
                "signature": signature,
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "config": config,
                "prepared_fingerprint": manifest["fingerprint"],
                "context_policy": context_policy(config),
                "column_profiles": profile_fingerprints,
                "column_dictionary": dictionary_identity,
                "statistics_preparation_ms": profile_ms,
                "statistics_timing": ('Selected-View cache load/build after live routing, included in per-question latency'
                                      if config.get('statistics_on_demand') else
                                      'Offline cache preparation before model startup; excluded from per-question pipeline latency'),
                "model": manifest["model"],
                "server": manifest["server"],
                "source_hashes": code,
                "question_ids": [c["id"] for c in selected],
                "hardware": hardware_snapshot(),
                "thinking": False,
                "first_pass_only": config.get('single_pass', False) or not config.get("answer_stage", False),
                "single_pass": config.get('single_pass', False),
                "planned_model_calls_per_question": ({'routing':1,'sql':1,'answer':1}
                                                     if config.get('single_pass') else None),
                "answerability_owner": (
                    "final answer model" if config.get("answer_stage") else None
                ),
                "routing_implementation": "llama_cpp_decisio_port (Gemma with adapted Decisio prompt/readout)",
            },
        )
    records_path = output / "records.jsonl"
    records = []
    if records_path.exists():
        lines = records_path.read_bytes().splitlines(keepends=True)
        keep = 0
        for i, line in enumerate(lines):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if i != len(lines) - 1:
                    raise ValueError("Corrupt records before the last line; refusing to resume")
                break
            records.append(record)
            keep += len(line)
        if keep != records_path.stat().st_size:
            with records_path.open("r+b") as stream:
                stream.truncate(keep)  # Only an interrupted, incomplete final JSON line.
        if records and keep and not records_path.read_bytes().endswith(b"\n"):
            with records_path.open("ab") as stream:
                stream.write(b"\n")
    seen = {r["id"] for r in records}
    if len(seen) != len(records) or seen - {c["id"] for c in selected}:
        raise ValueError("Duplicate or unexpected saved SQL result IDs")
    pending = [c for c in selected if c["id"] not in seen]
    print(
        f"Top-{config['top_k']} SQL ({context_mode(config)}{' + descriptions' if config.get('column_descriptions') else ''}): {len(selected)} questions; pending {len(pending)}. Results: {output}",
        flush=True,
    )
    if not pending:
        return report(config, output, selected, records)
    backend_config = server_config(config, manifest)
    fatal = None
    try:
        check_pin(manifest["model"], full_hash=True)
        with ManagedServer(backend_config, output, manifest["server"]) as server:
            write_json(output / "server_command.json", server.command)
            client = HTTP(
                f"http://{backend_config['host']}:{backend_config['port']}",
                config["http_timeout_seconds"],
            )
            write_json(
                output / "preflight.json",
                {
                    "props": client.request("/props"),
                    "thinking": False,
                    "context_policy": context_policy(config),
                },
            )
            router = LlamaDecisionPort(
                client,
                backend_config,
                read_json(support / "routing_profile.json"),
                read_json(support / "labels.json"),
            )
            if config["routing_mode"] == "live":
                write_json(output / "routing_preflight.json", router.preflight())
            generator = SQLGenerator(client, config)
            answerer = (
                AnswerGenerator(client, config) if config.get("answer_stage", False) else None
            )
            failures = 0
            with records_path.open("a", encoding="utf-8", newline="\n") as stream:
                for case in pending:
                    record = process_case(
                        case,
                        config,
                        inventories[case["inventory_key"]],
                        schemas[case["inventory_key"]],
                        generator,
                        router,
                        references.get(case["id"]),
                        answerer=answerer,
                        profiles=profiles.get(case["inventory_key"]),
                        descriptions=descriptions.get(case["inventory_key"]),
                    )
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    records.append(record)
                    print(
                        f"Question {len(records)}/{len(selected)}, id={case['id']}: {record['status']}",
                        flush=True,
                    )
                    failures = (
                        failures + 1
                        if record["status"] in ("generation_error", "answer_generation_error")
                        else 0
                    )
                    if failures >= 5:
                        raise RuntimeError(
                            "Five consecutive generation/routing failures; partial results retained"
                        )
    except KeyboardInterrupt:
        fatal = "Interrupted by user; saved completed questions can be resumed"
    except Exception as exc:
        fatal = type(exc).__name__ + ": " + str(exc)
    finally:
        result = report(config, output, selected, records, fatal_error=fatal)
    print("Report: " + str(output / "REPORT.md"), flush=True)
    return result
