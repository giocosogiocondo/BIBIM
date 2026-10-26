from __future__ import annotations

from collections import defaultdict
import csv
import math
from pathlib import Path
import random
import statistics

from .common import CONDITIONS, write_json


def average(values):
    values = [value for value in values if value is not None]
    return statistics.mean(values) if values else None


def percentile(values, proportion):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * proportion
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def rank_options(probabilities):
    # Tie-break uses the View name, not whichever condition happened to run first.
    return sorted(probabilities, key=lambda name: (-probabilities[name], name))


def retrieval_metrics(ranking, accepted_sets, k):
    if not accepted_sets:
        return None
    selected = set(ranking[:k])
    values = []
    for accepted in accepted_sets:
        gold = set(accepted)
        if not gold:
            raise ValueError("An empty reference set cannot be scored")
        overlap = len(selected & gold)
        values.append({"recall": overlap / len(gold),
                       "precision": overlap / len(selected) if selected else 0,
                       "all_required_covered": int(gold <= selected),
                       "extra_views": len(selected - gold),
                       "selected_views": len(selected), "required_views": len(gold)})
    return max(values, key=lambda value: (value["recall"], value["precision"]))


def summarize_condition(records, truth, ks):
    summary = {"observations": len(records), "questions": len({r["id"] for r in records}),
               "mean_elapsed_ms": average([r["score"]["elapsed_ms"] for r in records]),
               "median_elapsed_ms": percentile([r["score"]["elapsed_ms"] for r in records], .5),
               "p95_elapsed_ms": percentile([r["score"]["elapsed_ms"] for r in records], .95),
               "mean_request_elapsed_ms": average([r["score"].get("request_elapsed_ms") for r in records]),
               "mean_input_tokens": average([r["score"].get("input_tokens") for r in records])}
    for k in ks:
        rows = [retrieval_metrics(r["ranking"], truth[r["id"]], k) for r in records]
        summary[f"at_{k}"] = {field: average([row[field] for row in rows]) for field in (
            "recall", "precision", "all_required_covered", "extra_views", "selected_views")}
    return summary


def paired_interval(pairs, cases_by_id, truth, k, samples, seed):
    if not pairs:
        return None
    clustered = defaultdict(list)
    for a, b in pairs:
        ga = retrieval_metrics(a["ranking"], truth[a["id"]], k)
        gb = retrieval_metrics(b["ranking"], truth[b["id"]], k)
        clustered[cases_by_id[a["id"]]["project"]].append(
            (gb["recall"] - ga["recall"], gb["all_required_covered"] - ga["all_required_covered"]))
    projects = sorted(clustered)
    point = [item for rows in clustered.values() for item in rows]
    rng = random.Random(seed)
    recall, coverage = [], []
    for _ in range(samples):
        selected = [item for project in rng.choices(projects, k=len(projects)) for item in clustered[project]]
        recall.append(average([item[0] for item in selected]))
        coverage.append(average([item[1] for item in selected]))
    return {"method": "paired cluster bootstrap by project", "samples": samples,
            "projects": len(projects), "seed": seed, "primary_k": k,
            "recall_delta_B_minus_A": {"estimate": average([item[0] for item in point]),
                                       "ci95": [percentile(recall, .025), percentile(recall, .975)]},
            "coverage_delta_B_minus_A": {"estimate": average([item[1] for item in point]),
                                         "ci95": [percentile(coverage, .025), percentile(coverage, .975)]}}


def mcnemar(pairs, truth, k):
    # Repeats of one question are not independent samples for this test.
    a_only = b_only = 0
    for a, b in pairs:
        if a["repeat"] != 0:
            continue
        ga = retrieval_metrics(a["ranking"], truth[a["id"]], k)["all_required_covered"]
        gb = retrieval_metrics(b["ranking"], truth[b["id"]], k)["all_required_covered"]
        a_only += bool(ga and not gb)
        b_only += bool(gb and not ga)
    discordant = a_only + b_only
    p = min(1, 2 * sum(math.comb(discordant, i) for i in range(min(a_only, b_only) + 1))
            / 2 ** discordant) if discordant else 1.0
    return {"method": "exact two-sided McNemar, first repeat only", "A_only": a_only,
            "B_only": b_only, "p_value": p, "k": k}


def make_report(output, cases, records, approved, config, planned, *, fatal=None,
                manually_excluded=None):
    output = Path(output)
    by_id = {case["id"]: case for case in cases}
    # A retried failed record is superseded by its latest attempt, never double-counted.
    latest = {(r["id"], r["repeat"], r["condition"]): r for r in records}
    paired = []
    for qid, repeat in sorted({(r["id"], r["repeat"]) for r in latest.values()}):
        a, b = (latest.get((qid, repeat, condition)) for condition in CONDITIONS)
        if a and b and a.get("status") == b.get("status") == "ok":
            paired.append((a, b))
    scopes = {
        "historical_reference_agreement": {
            case["id"]: case["reference_view_sets"] for case in cases
            if case["historical_reference_eligible"] and case["id"] not in approved
            and case["id"] not in (manually_excluded or set())},
        "manual_semantic_gold": {qid: sets for qid, sets in approved.items() if qid in by_id},
    }
    report = {"format": "BIBIM-view-name-ablation-results/1", "conditions": list(CONDITIONS),
              "planned_records": planned, "successful_records": sum(r.get("status") == "ok" for r in latest.values()),
              "error_records": sum(r.get("status") == "error" for r in latest.values()),
              "completed_pairs": len(paired), "pending_pairs": planned // 2 - len(paired),
              "complete": len(paired) * 2 == planned, "fatal_error": fatal,
              "backend": config["backend"], "primary_k": config["primary_k"], "scopes": {},
              "notes": [
                  "Only paired successful requests enter A/B comparisons; missing and failed pairs remain explicit.",
                  "Historical SQL reference agreement is not minimal-View semantic accuracy.",
                  "No manual semantic accuracy is reported unless approved labels exist.",
                  "Local GGUF uses an adapted Decisio prompt/readout; it is not the official FP8 benchmark configuration.",
                  "This experiment selects Views only; it does not measure SQL-generation or final-answer accuracy."]}
    pair_csv = []
    for scope, truth in scopes.items():
        pairs = [(a, b) for a, b in paired if a["id"] in truth]
        conditions = {condition: [pair[index] for pair in pairs] for index, condition in enumerate(CONDITIONS)}
        result = {"eligible_questions": len(truth), "paired_observations": len(pairs),
                  "paired_questions": len({a["id"] for a, _ in pairs}),
                  "conditions": {name: summarize_condition(rows, truth, config["top_k"])
                                 for name, rows in conditions.items()},
                  "paired_interval": paired_interval(pairs, by_id, truth, config["primary_k"],
                                                     config["bootstrap_samples"], config["seed"]),
                  "mcnemar": mcnemar(pairs, truth, config["primary_k"]) if pairs else None}
        # This top-1 number has an unambiguous meaning only for single-required-View labels.
        singles = {qid: sets for qid, sets in truth.items() if all(len(s) == 1 for s in sets)}
        result["single_view_top1"] = {
            condition: average([retrieval_metrics(r["ranking"], singles[r["id"]], 1)["all_required_covered"]
                                for r in rows if r["id"] in singles])
            for condition, rows in conditions.items()}
        report["scopes"][scope] = result
        for a, b in pairs:
            row = {"scope": scope, "id": a["id"], "repeat": a["repeat"],
                   "project": by_id[a["id"]]["project"],
                   "reference_sets": str(truth[a["id"]]),
                   "A_elapsed_ms": a["score"]["elapsed_ms"], "B_elapsed_ms": b["score"]["elapsed_ms"],
                   "A_input_tokens": a["score"].get("input_tokens"), "B_input_tokens": b["score"].get("input_tokens")}
            for k in config["top_k"]:
                for prefix, record in (("A", a), ("B", b)):
                    m = retrieval_metrics(record["ranking"], truth[record["id"]], k)
                    row[f"{prefix}_recall_at_{k}"] = m["recall"]
                    row[f"{prefix}_covered_at_{k}"] = m["all_required_covered"]
                    row[f"{prefix}_extra_at_{k}"] = m["extra_views"]
            pair_csv.append(row)
    write_json(output / "summary.json", report)
    with (output / "paired_comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        if pair_csv:
            writer = csv.DictWriter(stream, fieldnames=list(pair_csv[0]))
            writer.writeheader()
            writer.writerows(pair_csv)

    def pct(value):
        return "not scored" if value is None else f"{value * 100:.2f}%"

    def number(value):
        return "not measured" if value is None else f"{value:.2f}"

    lines = ["# View name vs description ablation", "",
             f"Backend: `{config['backend']}`. Complete: **{report['complete']}**. "
             f"Successful pairs: {len(paired)}/{planned // 2}. Errors: {report['error_records']}.", "",
             "A: View names only. B: the SAME names plus fixed official-definition-based English descriptions.", "",
             "A main View and CustomPset companion are one candidate. SQL and reference answers are never shown to the model.", ""]
    for scope, result in report["scopes"].items():
        lines.extend(["## " + scope.replace("_", " ").title(), "",
                      f"Eligible questions: {result['eligible_questions']}; paired questions: {result['paired_questions']}.", ""])
        if not result["paired_observations"]:
            lines.extend(["No scored pairs. No accuracy claim is made for this scope.", ""])
            continue
        lines.extend(["| Condition | Single-View Top-1 | Recall@1 | Recall@3 | Recall@5 | All required covered@3 | Extra Views@3 | Mean ms | p95 ms | Mean input tokens |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for condition, summary in result["conditions"].items():
            at = lambda k: summary.get(f"at_{k}", {})
            lines.append(
                f"| {condition} | {pct(result['single_view_top1'][condition])} | "
                f"{pct(at(1).get('recall'))} | {pct(at(3).get('recall'))} | {pct(at(5).get('recall'))} | "
                f"{pct(at(3).get('all_required_covered'))} | {number(at(3).get('extra_views'))} | "
                f"{number(summary['mean_elapsed_ms'])} | {number(summary['p95_elapsed_ms'])} | "
                f"{number(summary['mean_input_tokens'])} |")
        interval = result["paired_interval"]
        delta = interval["recall_delta_B_minus_A"]
        lines.extend(["", f"Primary Recall@{config['primary_k']} B minus A: "
                      f"{delta['estimate'] * 100:+.2f} percentage points; project-cluster bootstrap "
                      f"95% interval [{delta['ci95'][0] * 100:+.2f}, {delta['ci95'][1] * 100:+.2f}].", ""])
    lines.extend(["## Interpretation", "", *["- " + note for note in report["notes"]], "",
                  "See `records.jsonl` for every request, response, ranking, elapsed time, and failure; "
                  "`run_manifest.json` for the model/backend/configuration pins; and "
                  "`paired_comparison.csv` for question-level changes.", ""])
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return report
