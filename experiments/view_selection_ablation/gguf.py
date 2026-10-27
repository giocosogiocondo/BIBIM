"""Read model metadata and label tokens without loading tensors or running inference."""

from __future__ import annotations

import itertools
from pathlib import Path
import string
import struct

SCALARS = {
    0: "B",
    1: "b",
    2: "H",
    3: "h",
    4: "I",
    5: "i",
    6: "f",
    7: "?",
    10: "Q",
    11: "q",
    12: "d",
}
KEEP = {
    "general.name",
    "general.architecture",
    "general.file_type",
    "general.quantization_version",
    "tokenizer.ggml.model",
    "tokenizer.ggml.tokens",
    "tokenizer.ggml.bos_token_id",
    "tokenizer.ggml.eos_token_id",
    "tokenizer.ggml.add_bos_token",
    "tokenizer.chat_template",
}


def read_metadata(path, *, required_architecture: str | None = "gemma4"):
    with Path(path).open("rb") as stream:

        def integer():
            raw = stream.read(8)
            if len(raw) != 8:
                raise ValueError("Truncated GGUF metadata")
            return struct.unpack("<Q", raw)[0]

        def text(capture=True):
            size = integer()
            if size > 128 * 1024 * 1024:
                raise ValueError("Unreasonable GGUF metadata string")
            if not capture:
                stream.seek(size, 1)
                return None
            data = stream.read(size)
            if len(data) != size:
                raise ValueError("Truncated GGUF string")
            return data.decode("utf-8", errors="strict")

        def value(kind, capture=True):
            if kind in SCALARS:
                fmt = "<" + SCALARS[kind]
                data = stream.read(struct.calcsize(fmt))
                return struct.unpack(fmt, data)[0] if capture else None
            if kind == 8:
                return text(capture)
            if kind == 9:
                subtype = struct.unpack("<I", stream.read(4))[0]
                count = integer()
                if count > 10_000_000:
                    raise ValueError("Unreasonable GGUF metadata array")
                if not capture and subtype in SCALARS:
                    stream.seek(count * struct.calcsize("<" + SCALARS[subtype]), 1)
                    return None
                result = [] if capture else None
                for _ in range(count):
                    item = value(subtype, capture)
                    if capture:
                        result.append(item)
                return result
            raise ValueError(f"Unsupported GGUF metadata type {kind}")

        if stream.read(4) != b"GGUF":
            raise ValueError("The model is not a GGUF file")
        version = struct.unpack("<I", stream.read(4))[0]
        if version not in (2, 3):
            raise ValueError(f"Unsupported GGUF version {version}")
        tensors, count = integer(), integer()
        if count > 100_000:
            raise ValueError("Unreasonable GGUF metadata count")
        result = {"gguf_version": version, "tensor_count": tensors}
        for _ in range(count):
            key = text()
            kind = struct.unpack("<I", stream.read(4))[0]
            item = value(kind, key in KEEP)
            if key in KEEP:
                result[key] = item
        result["metadata_end_offset"] = stream.tell()
    if (
        required_architecture not in (None, "any", "*")
        and result.get("general.architecture") != required_architecture
    ):
        label = "Gemma 4" if required_architecture == "gemma4" else required_architecture
        raise ValueError(f"This experiment profile requires a {label} GGUF")
    return result


def label_pool(metadata, count=255):
    tokens = metadata["tokenizer.ggml.tokens"]
    vocab = {token: index for index, token in enumerate(tokens)}
    codes = itertools.chain(
        string.ascii_uppercase,
        (a + b for a in string.ascii_uppercase for b in string.ascii_uppercase),
    )
    result = []
    used = set()
    for code in codes:
        spaced = next(
            (
                vocab[prefix + code]
                for prefix in ("\u2581", "\u0120", " ")
                if prefix + code in vocab
            ),
            None,
        )
        if spaced is None:
            continue
        forms = [spaced]
        if code in vocab:
            forms.append(vocab[code])
        if len(code) == 1 and f"<0x{ord(code):02X}>" in vocab:
            forms.append(vocab[f"<0x{ord(code):02X}>"])
        forms = list(dict.fromkeys(forms))
        if used.intersection(forms):
            raise ValueError("A label token belongs to more than one option")
        used.update(forms)
        result.append({"code": code, "spaced_token_id": spaced, "token_ids": forms})
        if len(result) == count:
            return result
    if len(result) < count:
        raise ValueError(f"Only {len(result)} single-token label codes exist; {count} required")
    return result
