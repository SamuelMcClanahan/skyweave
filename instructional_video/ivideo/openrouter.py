"""Minimal OpenRouter chat-completions client (stdlib only).

Docs: https://openrouter.ai/docs/api-reference/chat-completion
Auth: Authorization: Bearer $OPENROUTER_API_KEY

Rules this client follows (from the research, see RESEARCH.md):
- Send only parameters the model lists in supported_parameters. Claude 5.5 and
  GPT-6.1 Sol reject temperature/seed with HTTP 400.
- Fall back client-side, one request body per model (a shared "models" array
  would force one parameter set on every model).
- finish_reason "length" is a hard error: truncated JSON cannot be repaired.
- 401/402/403 stop at once. 408/429/5xx retry with backoff (Retry-After honored).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
APP_TITLE = "ivideo instructional video pipeline"
APP_URL = "https://github.com/SamuelMcClanahan/skyweave"
RETRYABLE = (408, 429, 500, 502, 503, 504)
FATAL = (401, 402, 403)


class OpenRouterError(RuntimeError):
    def __init__(self, status: int, body: str, retry_after: float | None = None):
        super().__init__(f"OpenRouter HTTP {status}: {body[:800]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after


class TruncatedError(RuntimeError):
    """The model stopped at max_tokens. The output is incomplete."""


@dataclass
class Completion:
    content: str
    model: str
    usage: dict
    raw: dict
    finish_reason: str = ""
    request: dict = field(default_factory=dict)


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "OPENROUTER_API_KEY is not set. Get a key at https://openrouter.ai/keys and "
            "export it (or add it to this environment's settings)."
        )
    return key


def base_url() -> str:
    return os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _headers(key: str | None) -> dict:
    h = {
        "Content-Type": "application/json",
        # Optional attribution headers (app name on openrouter.ai rankings).
        "HTTP-Referer": APP_URL,
        "X-Title": APP_TITLE,
        "X-OpenRouter-Title": APP_TITLE,
    }
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _request(method: str, path: str, body: dict | None, key: str | None, timeout: float) -> dict:
    req = urllib.request.Request(
        base_url() + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers=_headers(key),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        try:
            retry_after = float(ra) if ra else None
        except ValueError:
            retry_after = None
        raise OpenRouterError(e.code, e.read().decode(errors="replace"), retry_after) from None


_MODEL_CACHE: dict[str, dict] = {}


def list_models(timeout: float = 30.0) -> list[dict]:
    return _request("GET", "/models", None, None, timeout)["data"]


def model_info(model: str) -> dict | None:
    """The /models entry for a model id, or None if unknown or the listing fails."""
    if not _MODEL_CACHE:
        try:
            for m in list_models():
                _MODEL_CACHE[m["id"]] = m
        except Exception:
            return None
    return _MODEL_CACHE.get(model)


def key_status(timeout: float = 20.0) -> dict:
    """GET /key: credit limit and usage for the current key."""
    return _request("GET", "/key", None, api_key(), timeout).get("data", {})


def build_body(
    model: str,
    messages: list[dict],
    *,
    supported: set[str] | None,
    schema: dict | None,
    schema_name: str,
    max_tokens: int,
    temperature: float | None,
    seed: int | None,
    reasoning_effort: str | None,
) -> tuple[dict, bool]:
    """Request body for one model. Returns (body, uses_json_schema)."""
    sup = supported or set()
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None and "temperature" in sup:
        body["temperature"] = temperature
    if seed is not None and "seed" in sup:
        body["seed"] = seed
    if reasoning_effort and "reasoning" in sup:
        body["reasoning"] = {"effort": reasoning_effort}
    strict = False
    if schema is not None:
        if "structured_outputs" in sup:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
            # Route only to endpoints that honor the schema (some Claude hosts do not).
            body["provider"] = {"require_parameters": True}
            strict = True
        else:
            if "response_format" in sup:
                body["response_format"] = {"type": "json_object"}
            hint = "Return only JSON matching this JSON Schema:\n" + json.dumps(schema)
            body["messages"] = [
                *messages[:-1],
                {**messages[-1], "content": messages[-1]["content"] + "\n\n" + hint},
            ]
    return body, strict


def _post_with_retries(body: dict, key: str, retries: int, timeout: float) -> dict:
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            raw = _request("POST", "/chat/completions", body, key, timeout)
        except OpenRouterError as e:
            if e.status in RETRYABLE and attempt < retries:
                time.sleep(e.retry_after if e.retry_after else delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        if raw.get("error") and not raw.get("choices"):
            err = raw["error"]
            code = err.get("code")
            status = int(code) if str(code).isdigit() else 502
            e = OpenRouterError(status, json.dumps(err))
            if status in RETRYABLE and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise e
        return raw
    raise RuntimeError("unreachable")


def chat(
    messages: list[dict],
    *,
    models: list[str],
    schema: dict | None = None,
    schema_name: str = "output",
    max_tokens: int = 32000,
    temperature: float | None = None,
    seed: int | None = None,
    reasoning_effort: str | None = "medium",
    retries: int = 4,
    timeout: float = 600.0,
    attempts_log: list | None = None,
) -> Completion:
    """One completion. Tries each model in order until one answers.

    attempts_log (optional) receives one dict per failed model.
    """
    key = api_key()
    errors: list[str] = []
    for model in models:
        info = model_info(model)
        supported = set(info.get("supported_parameters") or []) if info else None
        body, _ = build_body(
            model,
            messages,
            supported=supported,
            schema=schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            temperature=temperature,
            seed=seed,
            reasoning_effort=reasoning_effort,
        )
        try:
            raw = _post_with_retries(body, key, retries, timeout)
        except OpenRouterError as e:
            if e.status in FATAL:
                raise
            errors.append(f"{model}: {e}")
            if attempts_log is not None:
                attempts_log.append({"model": model, "error": str(e)})
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            errors.append(f"{model}: {e}")
            if attempts_log is not None:
                attempts_log.append({"model": model, "error": str(e)})
            continue
        choice = raw["choices"][0]
        finish = choice.get("finish_reason") or choice.get("native_finish_reason") or ""
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        req = {k: v for k, v in body.items() if k != "messages"}
        if finish == "length":
            raise TruncatedError(
                f"{model} stopped at max_tokens={max_tokens} (finish_reason=length); "
                "the JSON is incomplete. Raise max_tokens or shorten the video."
            )
        return Completion(
            content=content,
            model=raw.get("model", model),
            usage=raw.get("usage") or {},
            raw={k: raw.get(k) for k in ("id", "model", "provider", "created")},
            finish_reason=finish,
            request=req,
        )
    raise RuntimeError("every OpenRouter model failed:\n" + "\n".join(errors))


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_content(content: str):
    """Parse model output as JSON. Accepts a ```json fenced block or leading prose."""
    text = content.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
