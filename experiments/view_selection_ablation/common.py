from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONDITIONS = ("name_only", "name_description")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def digest_json(value):
    return digest_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False).encode("utf-8"))


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resolve(value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load_config(path=None):
    config = read_json(path or HERE / "config.json")
    for name in ("model_path", "llama_server", "cuda_dll_directory", "question_csv",
                 "reference_json", "judgments_json", "investigations_json", "databases",
                 "description_catalog", "prepared_directory", "results_directory"):
        config[name] = str(resolve(config[name]))
    if config["backend"] not in ("llama_cpp_decisio_port", "decisio_api"):
        raise ValueError("Unknown backend")
    if config["primary_k"] not in config["top_k"] or any(k < 1 for k in config["top_k"]):
        raise ValueError("primary_k must be one of the positive top_k values")
    if config["repeats"] < 1 or config["context_tokens"] < 512:
        raise ValueError("Invalid repeat count or context length")
    return config


def file_pin(path, *, full_hash=False):
    path = Path(path)
    stat = path.stat()
    pin = {"path": str(path.resolve()), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if full_hash:
        pin["sha256"] = file_hash(path)
    return pin


def check_pin(pin, *, full_hash=False):
    stat = Path(pin["path"]).stat()
    if (stat.st_size, stat.st_mtime_ns) != (pin["bytes"], pin["mtime_ns"]):
        raise ValueError("Pinned input changed: " + pin["path"])
    if full_hash and file_hash(pin["path"]) != pin["sha256"]:
        raise ValueError("Pinned input hash changed: " + pin["path"])
