"""Bounded pre-SQL inspection of selected Views, sharing one question-level budget."""
from __future__ import annotations

import json
import time

from .generation import messages
from .feedback import compact_execution, execution_feedback, SQL_RULES
from .metadata import cached_query
from .sql import execute, validate_statement

# Reviewed and approved by the user on 2026-10-22; final-answer rubric is unchanged.
SYSTEM = """You may inspect the selected Views before writing the main answer SQL. Use the question, supplied column statistics, and earlier inspection results to decide whether another small query would help. You may use zero to three inspection queries in total for this question. Do not repeat checks already answered by the supplied statistics or earlier results. Useful checks include actual names/categories, value encodings, missing values, and relationships needed by the question. Return one JSON object with action (inspect or done), purpose, and sql. For inspect, provide one SQLite SELECT query using the selected Views. Prefer aggregates or short lists of relevant values over SELECT *. For done, leave sql empty. After inspection ends, a separate step will generate the main SQL using your observations."""

RESPONSE_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "action": {"const": "inspect"},
                "purpose": {"type": "string"},
                "sql": {"type": "string", "minLength": 1},
            },
            "required": ["action", "purpose", "sql"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "action": {"const": "done"},
                "purpose": {"type": "string"},
                "sql": {"const": ""},
            },
            "required": ["action", "purpose", "sql"],
            "additionalProperties": False,
        },
    ],
}


def observations(records):
    return [{"top_k": r["top_k"], "purpose": r.get("purpose"), "sql": r.get("sql"),
             "status": r["status"], "error": r.get("error"),
             "execution": compact_execution(r.get("execution")),
             "execution_feedback": r.get("execution_feedback")}
            for r in records if r.get("sql")]


class InspectionSession:
    def __init__(self, generator, config, inventory):
        self.generator, self.config, self.inventory = generator, config, inventory
        self.remaining = config.get("inspection_max_queries", 3)
        self.records, self.seen = [], set()

    def run_stage(self, question, groups, views, schemas, profiles, descriptions,
                  view_scores=None, feedback=None):
        while self.remaining > 0:
            chat = messages(question, views, schemas, profiles, descriptions,
                            view_scores=view_scores, feedback=feedback)
            # Keep supplied context-format guidance, replacing only the main-SQL task.
            from .generation import SYSTEM as SQL_SYSTEM
            chat[0]["content"] = SYSTEM + chat[0]["content"][len(SQL_SYSTEM):]
            repair = self.config.get("inspection_repair_enabled", False)
            if repair:
                chat[0]["content"] += "\n" + SQL_RULES + (
                    "\nFailed or duplicate SQL attempts also consume the three-query budget. "
                    "Use execution_feedback to fix a failed query when another check is useful. "
                    "Schema lookups use cached public metadata; their results are schema information, not object data.")
            chat.append({"role": "user", "content": json.dumps({
                "inspection_queries_remaining": self.remaining,
                "earlier_inspections": observations(self.records),
            }, ensure_ascii=False)})
            item = {"top_k": len(groups), "selected_views": list(views), "messages": chat,
                    "generation": None, "sql": None, "purpose": None,
                    "execution": None, "status": "planning", "error": None}
            self.records.append(item)
            started = time.perf_counter()
            charged = False
            try:
                generated = self.generator.complete(chat,
                    max_new_tokens=self.config.get("inspection_max_new_tokens", 512),
                    response_schema=RESPONSE_SCHEMA)
                item["generation"] = generated
                if generated["truncated"] or generated["output_limit_reached"]:
                    raise ValueError("Inspection plan was truncated")
                plan = json.loads(generated["content"])
                if not isinstance(plan, dict) or set(plan) != {"action", "purpose", "sql"}:
                    raise ValueError("Invalid inspection plan fields")
                if not all(isinstance(plan[k], str) for k in plan):
                    raise ValueError("Inspection plan fields must be strings")
                item.update(purpose=plan["purpose"], sql=plan["sql"])
                if plan["action"] == "done":
                    # Action controls execution. Preserve any stray SQL for audit,
                    # but never execute it or pass it on as an observation.
                    if plan["sql"].strip():
                        item["ignored_sql"] = plan["sql"]
                    item["sql"] = ""
                    item["status"] = "done"
                    break
                if plan["action"] != "inspect":
                    raise ValueError("Unknown inspection action")
                if repair:
                    self.remaining -= 1
                    charged = True
                key = plan["sql"].strip().rstrip(";").strip()
                if key in self.seen:
                    item["status"] = "duplicate_skipped"
                    if repair:
                        item["execution_feedback"] = {"kind": "duplicate", "hint": "This exact SQL has already been attempted. Use its recorded result/error, choose a different useful check, or finish."}
                        continue
                    break
                # Invalid SQL attempts consume budget too; never an unbounded repair loop.
                if not charged:
                    self.remaining -= 1
                    charged = True
                self.seen.add(key)
                validate_statement(plan["sql"])
                cached = cached_query(plan["sql"], schemas, views, self.inventory["physical_views"],
                    self.config.get("inspection_max_rows", 50)) if repair else None
                item["execution"] = cached if cached is not None else execute(self.inventory["database"], views, plan["sql"],
                    timeout=self.config.get("inspection_timeout_seconds", 10),
                    max_rows=self.config.get("inspection_max_rows", 50))
                ex = item["execution"]
                item["status"] = "sql_error" if ex["error"] else "result_truncated" if ex["truncated"] else "ok"
                if repair and ex["error"]:
                    item["execution_feedback"] = execution_feedback(ex["error"], plan["sql"], views, schemas, self.inventory["physical_views"])
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                item.update(status="inspection_error", error=str(exc))
                if repair and charged:
                    item["execution_feedback"] = execution_feedback(str(exc), item.get("sql"), views, schemas, self.inventory["physical_views"])
                    continue
                break  # Planner/transport failures cannot start an unbounded retry loop.
            finally:
                item["elapsed_ms"] = (time.perf_counter() - started) * 1000

    def metrics(self):
        gens = [r["generation"] for r in self.records if r.get("generation")]
        return {"planner_calls": len(self.records),
                "query_attempts": self.config.get("inspection_max_queries", 3) - self.remaining,
                "query_limit": self.config.get("inspection_max_queries", 3),
                "input_tokens": sum(g.get("input_tokens") or 0 for g in gens),
                "output_tokens": sum(g.get("output_tokens") or 0 for g in gens),
                "elapsed_ms": sum(r.get("elapsed_ms", 0) for r in self.records),
                "sql_elapsed_ms": sum((r.get("execution") or {}).get("elapsed_ms", 0) for r in self.records)}
