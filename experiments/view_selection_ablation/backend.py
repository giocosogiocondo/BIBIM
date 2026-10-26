from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .common import check_pin
from .prompts import chat_messages, systemone_request


class HTTP:
    def __init__(self, url, timeout=300):
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("This local experiment only sends requests to a loopback HTTP server")
        self.url, self.timeout = url.rstrip("/"), timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        request = urllib.request.Request(self.url + path, data=data,
                                         headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(f"Local server returned HTTP {exc.code}: {detail}") from exc


def label_distribution(response, labels, temperature):
    """Read all candidate token forms, sum per label, then apply a fixed temperature.

    The native request gives the SAME +100 logit bias to every allowed token form.
    Conditional ratios within that set are unchanged by a shared additive bias.
    We require every candidate ID in the response, never fill missing IDs with zero.
    """
    steps = response.get("completion_probabilities") or response.get("probs")
    if not isinstance(steps, list) or len(steps) != 1:
        raise ValueError("Expected exactly one next-token probability readout")
    top = steps[0].get("top_probs") or steps[0].get("top_logprobs") or steps[0].get("probs")
    if not isinstance(top, list):
        raise ValueError("Native server did not return per-token probabilities")
    values = {}
    for item in top:
        token_id = item.get("id", item.get("token_id"))
        if not isinstance(token_id, int):
            raise ValueError("Server must return explicit token IDs; a string-only readout is ambiguous")
        if "prob" in item:
            prob = float(item["prob"])
        elif "p" in item:
            prob = float(item["p"])
        elif "logprob" in item:
            prob = math.exp(float(item["logprob"]))
        else:
            raise ValueError("Missing token probability")
        if not math.isfinite(prob) or prob < 0:
            raise ValueError("Invalid token probability")
        if token_id in values:
            raise ValueError("Duplicate token ID in probability response")
        values[token_id] = prob
    ids = [token for label in labels for token in label["token_ids"]]
    missing = sorted(set(ids) - values.keys())
    if missing:
        raise ValueError(f"Incomplete candidate distribution ({len(missing)} missing IDs); no score was produced")
    summed = [sum(values[token] for token in label["token_ids"]) for label in labels]
    if temperature <= 0 or not all(value > 0 and math.isfinite(value) for value in summed):
        raise ValueError("Invalid label mass or temperature")
    logps = [math.log(value) / temperature for value in summed]
    maximum = max(logps)
    weights = [math.exp(value - maximum) for value in logps]
    total = sum(weights)
    return [value / total for value in weights], sum(summed)


class LlamaDecisionPort:
    identity = "Gemma 4 31B Q5_K_M / Decisio v0.15.0 prompt adaptation / llama.cpp"

    def __init__(self, client, config, profile, labels):
        self.client, self.config, self.profile, self.labels = client, config, profile, labels

    def preflight(self):
        for label in self.labels:
            tokens = self.client.request("/tokenize", {
                "content": " " + label["code"], "add_special": False, "parse_special": False})["tokens"]
            tokens = [t["id"] if isinstance(t, dict) else t for t in tokens]
            if tokens != [label["spaced_token_id"]]:
                raise ValueError("Tokenizer differs from the prepared GGUF for label " + label["code"])
        return {"identity": self.identity, "official_decisio_server": False,
                "calibration_validated": False, "labels_tokenization_checked": len(self.labels),
                "props": self.client.request("/props")}

    def score(self, question, options, descriptions, condition):
        started = time.perf_counter()
        labels = self.labels[:len(options)]
        messages = chat_messages(question, options, descriptions, condition, labels,
                                 self.config["instructions"], self.profile)
        template_payload = {"messages": messages, "add_generation_prompt": True,
                            "chat_template_kwargs": {"enable_thinking": False}, "enable_thinking": False}
        prompt = self.client.request("/apply-template", template_payload).get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Server did not return an applied chat template")
        for opening, closing in (("<think>", "</think>"), ("<|channel>", "<channel|>")):
            if prompt.count(opening) != prompt.count(closing):
                raise ValueError("Chat template left a thinking channel open; refusing to score")
        # /completion adds the model's BOS token to string prompts.
        tokens = self.client.request("/tokenize", {"content": prompt, "add_special": True,
                                                   "parse_special": True})["tokens"]
        input_tokens = len(tokens)
        if input_tokens + 2 > self.config["context_tokens"]:
            raise ValueError(f"Prompt needs {input_tokens} tokens; context is {self.config['context_tokens']}; no truncation allowed")
        ids = [token for label in labels for token in label["token_ids"]]
        payload = {
            "prompt": prompt, "n_predict": 1, "stream": False,
            "temperature": 1.0, "top_k": 0, "top_p": 1.0, "min_p": 0.0,
            "typical_p": 1.0, "repeat_penalty": 1.0, "presence_penalty": 0.0,
            "frequency_penalty": 0.0, "seed": self.config["seed"],
            "n_probs": len(ids), "post_sampling_probs": True,
            "logit_bias": [[token, self.config["label_logit_bias"]] for token in ids],
            "cache_prompt": False, "id_slot": 0, "return_tokens": True,
        }
        request_started = time.perf_counter()
        response = self.client.request("/completion", payload)
        inference_ms = (time.perf_counter() - request_started) * 1000
        if response.get("truncated") or response.get("stop_type") in ("limit_context", "context"):
            raise ValueError("Server truncated the prompt or exhausted context")
        probabilities, mass = label_distribution(response, labels, self.config["choice_temperature"])
        server_input_tokens = response.get("tokens_evaluated")
        if server_input_tokens is not None and (type(server_input_tokens) is not int or server_input_tokens < 1):
            raise ValueError("Server returned an invalid evaluated input-token count")
        return {"probabilities": dict(zip(options, probabilities)),
                "input_tokens": server_input_tokens if server_input_tokens is not None else input_tokens,
                "tokenizer_input_tokens": input_tokens,
                "input_tokens_source": "server.tokens_evaluated" if server_input_tokens is not None else "tokenizer_with_special_tokens",
                "request_elapsed_ms": inference_ms,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "candidate_mass_before_final_normalization": mass,
                "server_timings": response.get("timings", {}),
                "request": payload, "raw_response": response,
                "calibration_validated": False}


class DecisioAPI:
    identity = "External local Decisio System One API"

    def __init__(self, client, config):
        self.client, self.config = client, config

    def preflight(self):
        health = self.client.request("/health")
        return {"identity": self.identity, "official_decisio_server": True, "health": health,
                "note": "Record the external server's checkpoint, quantization, and cache configuration separately."}

    def score(self, question, options, descriptions, condition):
        payload = systemone_request(question, options, descriptions, condition, self.config["instructions"])
        started = time.perf_counter()
        response = self.client.request("/v1/systemone", payload)
        elapsed = (time.perf_counter() - started) * 1000
        probabilities = response["answers"]["view"]["probabilities"]
        if set(probabilities) != set(options):
            raise ValueError("Decisio did not return exactly the provided View options")
        values = [float(probabilities[name]) for name in options]
        if not all(math.isfinite(p) and p >= 0 for p in values) or not math.isclose(sum(values), 1, abs_tol=1e-5):
            raise ValueError("Invalid Decisio probability distribution")
        return {"probabilities": dict(zip(options, values)), "elapsed_ms": elapsed,
                "request_elapsed_ms": elapsed, "input_tokens": response.get("usage", {}).get("input_tokens"),
                "request": payload, "raw_response": response}


class ManagedServer:
    """Start only when the user runs the experiment; stop only the child we started."""

    def __init__(self, config, output, pinned_server):
        self.config, self.output, self.pin = config, Path(output), pinned_server
        self.process = None
        self.stream = None

    def __enter__(self):
        check_pin(self.pin, full_hash=True)
        if str(Path(self.config["llama_server"]).resolve()) != self.pin["path"]:
            raise ValueError("Configured server binary differs from the prepared binary")
        with socket.socket() as sock:
            if sock.connect_ex((self.config["host"], self.config["port"])) == 0:
                raise RuntimeError(f"Port {self.config['port']} is already in use; choose another port in config.json")
        binary = Path(self.config["llama_server"])
        command = [str(binary), "--model", self.config["model_path"],
                   "--host", self.config["host"], "--port", str(self.config["port"]),
                   "--ctx-size", str(self.config["context_tokens"]), "--parallel", "1",
                   "--gpu-layers", str(self.config["gpu_layers"]), "--flash-attn", "on",
                   "--cache-type-k", self.config["cache_type_k"], "--cache-type-v", self.config["cache_type_v"],
                   "--batch-size", str(self.config["batch_size"]), "--ubatch-size", str(self.config["ubatch_size"]),
                   "--reasoning", "off", "--chat-template-kwargs", '{"enable_thinking":false}',
                   "--cache-ram", "0", "--no-context-shift", "--fit", "off",
                   "--alias", "bibim-view-ablation-gemma4-31b-q5"]
        self.command = command
        environment = os.environ.copy()
        cuda = self.config["cuda_dll_directory"]
        environment["PATH"] = cuda + os.pathsep + str(binary.parent) + os.pathsep + environment.get("PATH", "")
        # This installed launcher expects a backend DLL path, not its directory.
        environment["GGML_BACKEND_PATH"] = str(Path(cuda) / "ggml-cuda.dll")
        self.stream = (self.output / "server.log").open("wb")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(command, cwd=binary.parent, env=environment,
                                        stdout=self.stream, stderr=subprocess.STDOUT, creationflags=flags)
        client = HTTP(f"http://{self.config['host']}:{self.config['port']}", 3)
        deadline = time.monotonic() + self.config["startup_timeout_seconds"]
        next_notice = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Local server exited before loading. See server.log.")
                try:
                    health = client.request("/health")
                    if health.get("status") == "ok":
                        return self
                except (OSError, RuntimeError, ValueError):
                    pass
                if time.monotonic() >= next_notice:
                    print("Waiting for the local model to load; details: " + str(self.output / "server.log"), flush=True)
                    next_notice = time.monotonic() + 20
                time.sleep(1)
            raise TimeoutError("Local server did not become ready before the startup timeout")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.stream:
            self.stream.close()
