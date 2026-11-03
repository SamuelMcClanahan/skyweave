"""Minimal OpenRouter chat-completions client (stdlib only).

Docs: https://openrouter.ai/docs/api-reference/chat-completion
Auth: Authorization: Bearer $OPENROUTER_API_KEY
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


class OpenRouterError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"OpenRouter HTTP {status}: {body[:800]}")
        self.status = status
        self.body = body


@dataclass
class Completion:
    content: str
    model: str
    usage: dict
    raw: dict
    request: dict = field(default_factory=dict)


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "OPENROUTER_API_KEY is not set. Get a key at https://openrouter.ai/keys and "
            "export it (or add it to this environment's secrets)."
        )
    return key


def base_url() -> str:
    return os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _post(path: str, body: dict, key: str, timeout: float) -> dict:
    req = urllib.request.Request(
        base_url() + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Optional attribution headers (shown on openrouter.ai rankings).
            "HTTP-Referer": "https://github.com/SamuelMcClanahan/skyweave",
            "X-Title": APP_TITLE,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise OpenRouterError(e.code, e.read().decode(errors="replace")) from None


def chat(
    messages: list[dict],
    *,
    model: str,
    fallbacks: list[str] | None = None,
    schema: dict | None = None,
    schema_name: str = "output",
    temperature: float = 0.7,
    max_tokens: int = 16000,
    seed: int | None = None,
    retries: int = 4,
    timeout: float = 300.0,
) -> Completion:
    """One chat completion. Retries 429/5xx with exponential backoff.

    With a schema, it asks for strict JSON-Schema structured output and tells the
    router to use only providers that support it (provider.require_parameters).
    """
    key = api_key()
    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if fallbacks:
        body["models"] = [model, *[m for m in fallbacks if m != model]]
    if seed is not None:
        body["seed"] = seed
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }
        body["provider"] = {"require_parameters": True}
    delay = 2.0
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = _post("/chat/completions", body, key, timeout)
        except OpenRouterError as e:
            last = e
            if e.status in (408, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        if "error" in raw and not raw.get("choices"):
            err = raw["error"]
            status = int(err.get("code", 0) or 0) if str(err.get("code", "")).isdigit() else 0
            last = OpenRouterError(status, json.dumps(err))
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise last
        choice = raw["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return Completion(
            content=content,
            model=raw.get("model", model),
            usage=raw.get("usage") or {},
            raw=raw,
            request={k: v for k, v in body.items() if k != "messages"},
        )
    raise RuntimeError(f"OpenRouter request failed: {last}")


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


def list_models(timeout: float = 30.0) -> list[dict]:
    req = urllib.request.Request(base_url() + "/models")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())["data"]
