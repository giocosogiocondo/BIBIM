from __future__ import annotations

import json
import re
import time

from .schema import quote
from .profiles import render_statistics
from .feedback import score_context

SYSTEM = """Generate one SQLite SELECT query to answer the user's question using only the supplied Views and column context.
Return only the SQL, without explanation or Markdown. Do not invent Views, columns, data values, or JSON keys.
Always generate a SQL query that retrieves relevant available information, including a partial result when the schema is limited.
The downstream answer model will decide whether the query and results answer the question.
Use double quotes for SQL identifiers and single quotes for string values. Qualify ambiguous columns with their View alias.
A base View and its same-named CustomPset companion describe the same objects and can join on ifc_id.
Do not assume ifc_id matches between different entity classes. No other databases or raw tables are available.
Do not assume a particular etc JSON key exists. Produce one query only; no schema exploration or follow-up queries."""

STATISTICS_RULES = """
Each View includes its total row count once. Column lines are: "column" | observed SQLite storage type | [[value,count],...] | NULL count.
The values are the up to three most frequent non-NULL scalar values with their exact counts. Binary payload values are omitted.
INTEGER 0/1 and TEXT 'false'/'true' are different encodings: use the observed values and types when constructing filters.
Top values are a partial summary, not the complete set of allowed or existing values. A missing value in this summary is not evidence of absence.
UNKNOWN means no non-NULL value was observed. Mixed storage types are separated by |. No units are inferred from values; use only explicit known units.
Identifier and etc payload values are omitted. Long values are marked preview/truncated; a preview is not an exact literal for an equality filter.
No column descriptions, JSON-key inventories, reference SQL, or benchmark answers are supplied."""


def messages(question, views, schemas, profiles=None, descriptions=None, inspections=None,
             view_scores=None, feedback=None):
    if profiles is None:
        listing = "\n\n".join(
            quote(view) + " (\n  " + ",\n  ".join(quote(c) for c in schemas[view]) + "\n)"
            for view in views
        )
        rules = "\nNo descriptions, data types, example values, statistics, or JSON-key inventories are supplied."
        heading = "Available Views and column names:"
    else:
        listing = "\n\n".join(
            render_statistics(view, schemas[view], profiles[view]) for view in views
        )
        rules = STATISTICS_RULES
        heading = 'Available Views and column statistics ("column" | type | top [[value,count],...] | NULL count):'
    if descriptions is not None:
        if profiles is None:
            rules = "\nColumn descriptions are supplied for matched fields. No data types, values, statistics or JSON-key inventories are supplied."
        else:
            rules = rules.replace(
                "No column descriptions, JSON-key inventories, reference SQL, or benchmark answers are supplied.",
                "Matched IFC/BIBIM column descriptions are supplied. No JSON-key inventories, reference SQL or benchmark answers are supplied.",
            )
        rules += "\nDescriptions explain field semantics, not data presence or allowed observed values. Unmatched, ambiguous and custom fields may have no definition; use their actual names and available statistics."
        definitions = []
        for view in views:
            fields = descriptions.get(view, {})
            lines = [
                quote(column) + " | " + fields[column]
                for column in schemas[view]
                if column in fields
            ]
            if lines:
                definitions.append(quote(view) + ":\n" + "\n".join(lines))
        listing += "\n\nColumn descriptions (English):\n" + "\n\n".join(definitions)
    chat = [
        {"role": "system", "content": SYSTEM + rules},
        {
            "role": "user",
            "content": "Question:\n" + question + "\n\n" + heading + "\n" + listing,
        },
    ]
    if view_scores is not None:
        chat[1]['content'] += '\n\n' + score_context(view_scores)
    if feedback:
        chat[0]['content'] += ('\nUse the earlier SQL attempts, execution errors/results and answerability findings '
                               'to revise the query. Address the stated failure or missing support; '
                               'retain verified missing values and disclose assumptions rather than treating them as zero.')
        chat.append({'role': 'user', 'content': json.dumps(
            {'earlier_main_sql_attempts': feedback}, ensure_ascii=False)})
    if inspections:
        chat.append({"role": "user", "content": json.dumps(
            {"observed_inspection_queries_and_results": inspections}, ensure_ascii=False)})
    return chat


def extract_sql(content):
    content = content.strip()
    fence = re.fullmatch(r"```(?:sql|sqlite)?\s*\n(.*?)\n```", content, flags=re.S | re.I)
    return fence.group(1).strip() if fence else content


class ContextOverflow(ValueError):
    pass


class SQLGenerator:
    def __init__(self, client, config):
        self.client, self.config = client, config

    def generate(self, question, views, schemas, profiles=None, descriptions=None, inspections=None,
                 view_scores=None, feedback=None):
        if self.config.get("column_descriptions", False) and descriptions is None:
            raise ValueError(
                "Column descriptions are required; refusing a statistics-only fallback"
            )
        chat = messages(question, views, schemas, profiles, descriptions, inspections,
                        view_scores, feedback)
        result = self.complete(chat)
        return {**result, "sql": extract_sql(result["content"])}

    def complete(self, chat, *, max_new_tokens=None, response_schema=None):
        started = time.perf_counter()
        output_budget = self.config["max_new_tokens"] if max_new_tokens is None else max_new_tokens
        prompt = self.client.request(
            "/apply-template",
            {
                "messages": chat,
                "add_generation_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False},
                "enable_thinking": False,
            },
        ).get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Missing applied chat template")
        if prompt.count("<|channel>") != prompt.count("<channel|>"):
            raise ValueError("Thinking channel was left open")
        tokens = self.client.request(
            "/tokenize",
            {
                "content": prompt,
                "add_special": True,
                "parse_special": True,
            },
        )["tokens"]
        if len(tokens) + output_budget + 16 > self.config["context_tokens"]:
            raise ContextOverflow(
                f"Input {len(tokens)} + output budget {output_budget} exceeds context "
                f"{self.config['context_tokens']}; no columns were dropped"
            )
        request = {
            "prompt": prompt,
            "n_predict": output_budget,
            "stream": False,
            "temperature": self.config["temperature"],
            "top_k": 0,
            "top_p": 1.0,
            "min_p": 0.0,
            "repeat_penalty": 1.0,
            "seed": self.config["seed"],
            "cache_prompt": False,
            "id_slot": 0,
        }
        if response_schema is not None:
            request["json_schema"] = response_schema
        inference_started = time.perf_counter()
        response = self.client.request("/completion", request)
        inference_ms = (time.perf_counter() - inference_started) * 1000
        content = response.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Empty model response")
        exhausted = bool(
            response.get("stopped_limit")
            or response.get("stop_type") == "limit"
            or response.get("tokens_predicted", 0) >= output_budget
        )
        return {
            "content": content,
            "input_tokens": response.get("tokens_evaluated", len(tokens)),
            "tokenizer_input_tokens": len(tokens),
            "output_tokens": response.get("tokens_predicted"),
            "generation_ms": inference_ms,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "truncated": bool(
                response.get("truncated")
                or response.get("stop_type") in ("context", "limit_context")
            ),
            "output_limit_reached": exhausted,
            "request": request,
            "raw_response": response,
        }
