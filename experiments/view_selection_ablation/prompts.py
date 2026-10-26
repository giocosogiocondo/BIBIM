from __future__ import annotations

import hashlib
import random

from .common import CONDITIONS


def ordered_options(case, inventory, seed, repeat):
    options = list(inventory["groups"])
    raw = f"{seed}:{case['id']}:{repeat}:options".encode()
    random.Random(int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")).shuffle(options)
    return options


def condition_order(seed, case_id, repeat):
    # Stable AB/BA counterbalancing, independent of reference labels and outcomes.
    reverse = hashlib.sha256(f"{seed}:{case_id}:{repeat}:conditions".encode()).digest()[0] & 1
    return list(reversed(CONDITIONS)) if reverse else list(CONDITIONS)


def option_texts(options, descriptions, condition):
    if condition not in CONDITIONS:
        raise ValueError("Unknown ablation condition")
    if condition == "name_only":
        return list(options)
    return [name + ": " + descriptions[name] for name in options]


def user_content(question, options, descriptions, condition, labels, instructions, profile):
    texts = option_texts(options, descriptions, condition)
    if len(texts) > len(labels):
        raise ValueError("Not enough single-token option labels")
    listing = "\n".join(f"{label['code']}. {text}" for label, text in zip(labels, texts))
    return f"{question}\n\n{instructions}\n\nOptions:\n{listing}\n\n{profile['answer_line']}"


def chat_messages(question, options, descriptions, condition, labels, instructions, profile):
    return [
        {"role": "system", "content": profile["system_prompt_text"]},
        {"role": "user", "content": user_content(
            question, options, descriptions, condition, labels, instructions, profile)},
    ]


def systemone_request(question, options, descriptions, condition, instructions):
    # Decisio's default describe_options can hide a key when a description exists.
    # Include the SAME View name in B's text so B adds a description rather than replacing A's name.
    criteria = {name: None if condition == "name_only" else name + ": " + descriptions[name]
                for name in options}
    return {"state": question, "questions": {
        "view": {"type": "choice", "instructions": instructions, "criteria": criteria}}}
