"""The natural-language answer stage owns answerability and retrieval expansion."""

from __future__ import annotations

import json
import re

from .generation import SQLGenerator

SYSTEM = """Answer the user's question using the supplied SQL attempts and their actual execution results.
Decide whether those results support a complete answer, and write that answer in the question's language.
Check what each SQL query actually measures, including its filters, aggregation, units and joins.
SQL execution success alone does not make a result correct. Empty rows or NULL do not automatically mean that the objects do not exist.
An empty result may support an absence answer only if the SQL correctly checks the requested scope.
Use earlier successful attempts when they contribute information. Do not invent facts, missing values or missing relationships.
If you cannot answer the whole question from these results, set answerable=false and explain what is missing.
That decision will request the next ranked Views, if any remain in this workflow.
An error is not an empty query result; a row limit is not a complete result. Do not claim the whole database lacks information based on a limited retrieval.
Return exactly one JSON object with answerable (boolean), answer (string), and reason (string).
When answerable=true, answer must contain the actual user-facing answer. When false, answer must still contain a concise user-facing explanation of the verified findings and the missing support; reason is for retrieval control and does not replace the answer.

# Answer format

Answer the question using the SQL queries and their execution results.

Every factual claim in your answer MUST include an inline source citation in parentheses. For each value, cite:

- The View and column it was read from.
- The computation method if derived, such as COUNT, SUM, AVG, or a unit conversion.
- The assumption if inferred:
  "Assuming [condition], [conclusion]."

An answer without source citations for every value is incomplete, even if the values are correct.

In tables, cite sources inline or in a dedicated Source column.

Source hierarchy:
direct retrieval from properties > simple computation > complex/geometric computation > stated assumptions.

- Report measurements in metric SI (m, m2, m3).
- Continuous measurements must be within 2%; discrete counts must be exact.
- Enumerate completely, rather than providing only a subset.
- For element inventories, include available name/label, IFC type, storey, material, and key dimensions.
- Introduce each inference with "Assuming" and identify estimated results as estimates.
- If the executed queries establish that information is absent or the relevant fields are empty, report that as a factual finding. State what was checked and the number of objects checked when available.
- Do not phrase a verified absence as a refusal such as "I cannot determine..." or "I am unable to answer...".
- If the requested elements do not exist, say "not available in the model". Distinguish this from an existing element whose measured quantity is zero.
- If the queries do not establish an answer, provide any supported findings and briefly explain what remains unresolved. Do not describe a query failure as confirmed absence.

Be direct and concise."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "answer": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["answerable", "answer", "reason"],
    "additionalProperties": False,
}


def answer_messages(question, attempts):
    evidence = []
    for attempt in attempts:
        generation = attempt.get("generation") or {}
        execution = attempt.get("execution")
        evidence.append(
            {
                "retrieved_top_k": attempt["top_k"],
                "selected_views": attempt["selected_views"],
                "sql": generation.get("sql", generation.get("content")),
                "sql_status": attempt["sql_status"],
                "generation_error": attempt.get("error"),
                "execution": (
                    None
                    if execution is None
                    else {
                        key: execution[key]
                        for key in (
                            "columns",
                            "rows",
                            "error",
                            "truncated",
                            "row_count",
                            "rows_saved",
                        )
                    }
                ),
            }
        )
        if attempt.get("inspection_evidence"):
            evidence[-1]["inspection_queries_and_results"] = attempt["inspection_evidence"]
    # Explicit allowlist: no benchmark answers, reference SQL, or reference results.
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "question": question,
                    "sql_attempts": evidence,
                },
                ensure_ascii=False,
            ),
        },
    ]


def parse_decision(content):
    content = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", content, flags=re.S | re.I)
    if fence:
        content = fence.group(1)
    decision = json.loads(content)
    if not isinstance(decision, dict) or set(decision) != {"answerable", "answer", "reason"}:
        raise ValueError("Answer decision must contain exactly answerable, answer, reason")
    if type(decision["answerable"]) is not bool:
        raise ValueError("answerable must be a JSON boolean")
    if not all(isinstance(decision[key], str) for key in ("answer", "reason")):
        raise ValueError("answer and reason must be strings")
    if decision["answerable"] and not decision["answer"].strip():
        raise ValueError("An answerable decision needs a user-facing answer")
    if not decision["answerable"] and not decision["reason"].strip():
        raise ValueError("A not-answerable decision needs a reason")
    return decision


class AnswerGenerator:
    def __init__(self, client, config):
        self.generator = SQLGenerator(client, config)
        self.config = config

    def generate(self, question, attempts):
        chat = answer_messages(question, attempts)
        if self.config.get('single_pass'):
            chat[0]['content'] = chat[0]['content'].replace(
                'That decision will request the next ranked Views, if any remain in this workflow.',
                'This is the final response. No additional retrieval, inspection or SQL revision is available.')
        result = self.generator.complete(
            chat,
            max_new_tokens=self.config.get("answer_max_new_tokens", self.config["max_new_tokens"]),
            response_schema=RESPONSE_SCHEMA,
        )
        result.update(messages=chat, decision=None, error=None)
        try:
            if result["truncated"] or result["output_limit_reached"]:
                raise ValueError(
                    "Answer model input or output was truncated; no decision accepted"
                )
            result["decision"] = parse_decision(result["content"])
        except ValueError as exc:
            result["error"] = str(exc)
        return result
