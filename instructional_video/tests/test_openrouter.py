"""The OpenRouter path, against a local fake server (no network, no key)."""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ivideo import dialogue, openrouter
from ivideo.project import Project

# Parameter lists as /models reports them (shapes copied from the live listing).
MODELS = [
    {  # like anthropic/claude-opus-5.5: no temperature, has structured outputs
        "id": "test/strict",
        "supported_parameters": [
            "max_tokens",
            "reasoning",
            "response_format",
            "structured_outputs",
        ],
    },
    {  # like x-ai/grok-4.7: temperature and seed allowed
        "id": "test/loose",
        "supported_parameters": [
            "max_tokens",
            "temperature",
            "seed",
            "response_format",
            "structured_outputs",
        ],
    },
    {  # an older model: JSON mode only, no schema
        "id": "test/jsononly",
        "supported_parameters": ["max_tokens", "temperature", "response_format"],
    },
]


class FakeOpenRouter:
    def __init__(self, responses: list[tuple[int, dict]]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.speech_requests: list[dict] = []
        self.audio = b""
        self.audio_type = "audio/mpeg"
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, status, payload, extra=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path.endswith("/models"):
                    self._send(200, {"data": MODELS})
                elif self.path.endswith("/key"):
                    self._send(200, {"data": {"limit": 10, "limit_remaining": 9.5, "usage": 0.5}})
                else:
                    self._send(404, {"error": {"code": 404, "message": "nope"}})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                outer.headers.append(dict(self.headers))
                if self.path.endswith("/audio/speech"):
                    outer.speech_requests.append(body)
                    data = outer.audio
                    self.send_response(200)
                    self.send_header("Content-Type", outer.audio_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                status, payload = outer.responses.pop(0)
                self._send(status, payload, {"Retry-After": "0"} if status == 429 else None)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def completion(content: str, model: str = "test/strict", finish: str = "stop") -> dict:
    return {
        "id": "gen-1",
        "model": model,
        "provider": "Fake",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
            "cost": 0.001,
        },
    }


@pytest.fixture
def fake(monkeypatch):
    servers = []
    openrouter._MODEL_CACHE.clear()
    monkeypatch.setattr("ivideo.openrouter.time.sleep", lambda s: None)

    def make(responses):
        f = FakeOpenRouter(responses)
        servers.append(f)
        monkeypatch.setenv("OPENROUTER_BASE_URL", f.url)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-test-key")
        return f

    yield make
    for s in servers:
        s.close()
    openrouter._MODEL_CACHE.clear()


def test_strict_model_body_and_repair(fake, tmp_path, demo_script):
    bad = copy.deepcopy(demo_script)
    bad["scenes"][0]["lines"][0]["text"] = "**markup** is not spoken"
    f = fake(
        [
            (200, completion("```json\n" + json.dumps(bad) + "\n```")),
            (200, completion(json.dumps(demo_script))),
        ]
    )
    proj = Project(tmp_path / "p")
    data = dialogue.write_dialogue(
        proj, topic="t", model="test/strict", fallbacks=["test/loose"], temperature=0.7, seed=3
    )
    first = f.requests[0]
    assert first["model"] == "test/strict"
    assert "models" not in first  # fallback is client-side
    assert "temperature" not in first and "seed" not in first  # not supported by this model
    assert first["reasoning"] == {"effort": "medium"}
    assert first["max_tokens"] == 32000
    rf = first["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert first["provider"] == {"require_parameters": True}
    assert f.headers[0]["Authorization"] == "Bearer fake-test-key"
    assert f.headers[0]["X-Openrouter-Title"]
    # the repair goes back to the same model, with the validator's complaint
    assert f.requests[1]["model"] == "test/strict"
    assert "markup" in f.requests[1]["messages"][-1]["content"]
    assert [s["id"] for s in data["scenes"]] == ["s01", "s02"]
    assert data["meta"]["openrouter_cost_usd"] == 0.002
    log = json.loads(proj.openrouter_log.read_text())
    assert len(log) == 2 and log[1]["problems"] == [] and log[0]["finish_reason"] == "stop"


def test_client_side_fallback_builds_new_body(fake, tmp_path, demo_script):
    err = {
        "error": {
            "code": 404,
            "message": "No endpoints found that can handle the requested parameters.",
        }
    }
    f = fake([(404, err), (200, completion(json.dumps(demo_script), model="test/loose"))])
    data = dialogue.write_dialogue(
        Project(tmp_path / "p"),
        topic="t",
        model="test/strict",
        fallbacks=["test/loose"],
        temperature=0.5,
    )
    assert [r["model"] for r in f.requests] == ["test/strict", "test/loose"]
    assert "temperature" not in f.requests[0]
    assert f.requests[1]["temperature"] == 0.5
    assert data["meta"]["dialogue_model"] == "test/loose"


def test_json_only_model_gets_schema_in_prompt(fake, tmp_path, demo_script):
    f = fake([(200, completion(json.dumps(demo_script), model="test/jsononly"))])
    dialogue.write_dialogue(Project(tmp_path / "p"), topic="t", model="test/jsononly", fallbacks=[])
    body = f.requests[0]
    assert body["response_format"] == {"type": "json_object"}
    assert "provider" not in body
    assert "JSON Schema" in body["messages"][-1]["content"]


def test_truncation_is_an_error(fake, tmp_path):
    fake([(200, completion('{"title": "cut off', finish="length"))])
    with pytest.raises(SystemExit, match="max_tokens"):
        dialogue.write_dialogue(
            Project(tmp_path / "p"), topic="t", model="test/strict", fallbacks=[]
        )


def test_payment_error_stops_without_fallback(fake, tmp_path):
    f = fake([(402, {"error": {"code": 402, "message": "Insufficient credits"}})])
    with pytest.raises(openrouter.OpenRouterError, match="402"):
        dialogue.write_dialogue(
            Project(tmp_path / "p"), topic="t", model="test/strict", fallbacks=["test/loose"]
        )
    assert len(f.requests) == 1


def test_retries_rate_limit(fake, tmp_path, demo_script):
    f = fake(
        [
            (429, {"error": {"code": 429, "message": "slow down"}}),
            (200, completion(json.dumps(demo_script))),
        ]
    )
    dialogue.write_dialogue(Project(tmp_path / "p"), topic="t", model="test/strict", fallbacks=[])
    assert len(f.requests) == 2 and f.requests[1]["model"] == "test/strict"


def test_gives_up_after_repairs(fake, tmp_path):
    f = fake([(200, completion("not json"))] * 3)
    with pytest.raises(SystemExit, match="still invalid"):
        dialogue.write_dialogue(
            Project(tmp_path / "p"), topic="t", model="test/strict", fallbacks=[], max_repairs=2
        )
    assert len(f.requests) == 3


def test_key_status(fake):
    fake([])
    assert openrouter.key_status()["limit_remaining"] == 9.5


def test_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="OPENROUTER_API_KEY"):
        dialogue.write_dialogue(Project(tmp_path / "p"), topic="t", model="m", fallbacks=[])


def test_sources_are_included(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("Rays cross at the target.")
    msgs = dialogue.build_messages(
        topic="t", audience="a", minutes=2, speakers=1, sources=[("notes.md", src.read_text())]
    )
    assert "Rays cross at the target." in msgs[1]["content"]
    assert "Narrator mode" in msgs[1]["content"]


def _pcm(seconds: float = 1.0, rate: int = 44100) -> bytes:
    import subprocess

    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
         "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip


def test_openrouter_voice_engine(fake, tmp_path, demo_script):
    from ivideo.schema import assign_scene_ids
    from ivideo.tts import synthesize

    f = fake([])
    # the live service sends raw s16le PCM and names the rate in the content type
    f.audio = _pcm(1.0, 44100)
    f.audio_type = "audio/pcm;rate=44100;channels=1"
    proj = Project(tmp_path / "p").ensure()
    script = assign_scene_ids(demo_script)
    timing = synthesize(proj, script)  # default engine: OpenRouter HeyGen
    req = f.speech_requests
    assert len(req) == 6
    assert {r["model"] for r in req} == {"heygen/heygen-voice-1-turbo:free"}
    assert {r["voice"] for r in req} == {"nadine", "bram"}
    assert all(r["response_format"] == "pcm" and "speed" not in r for r in req)
    assert timing["engine"] == "openrouter" and timing["voices"] == {
        "host": "nadine",
        "learner": "bram",
    }
    assert all(0.9 < ln["duration"] < 1.2 for sc in timing["scenes"] for ln in sc["lines"])
    # cached: same text and voice -> no new calls; a new voice -> only that speaker again
    synthesize(proj, script)
    assert len(f.speech_requests) == 6
    synthesize(proj, script, voice_overrides={"host": "gareth"})
    assert len(f.speech_requests) == 9 and f.speech_requests[-1]["voice"] == "gareth"


def test_check_script_cli(tmp_path, demo_script, capsys):
    from ivideo.cli import main

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "brief.json").write_text(json.dumps({"topic": "t", "minutes": 1, "speakers": 2}))
    bad = copy.deepcopy(demo_script)
    bad["scenes"][0]["lines"][0]["text"] = "**bold**"
    (proj / "script.json").write_text(json.dumps(bad))
    main(["check-script", str(proj)])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and any("markup" in p for p in out["problems"])
    (proj / "script.json").write_text(json.dumps(demo_script))
    main(["check-script", str(proj)])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and [s["id"] for s in out["scenes"]] == ["s01", "s02"]
    main(["script-rules", str(proj)])
    rules = json.loads(capsys.readouterr().out)
    assert rules["target_words"] == 155 and "Dialogue mode" in rules["speaker_mode"]


def test_trim_cuts_engine_noise_floor():
    import numpy as np

    from ivideo.tts import SAMPLE_RATE, _trim

    rng = np.random.default_rng(0)
    noise = (rng.uniform(-1, 1, int(0.3 * SAMPLE_RATE)) * 10 ** (-58 / 20)).astype(np.float32)
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    speech = (0.5 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    out = _trim(np.concatenate([noise, speech, noise]))
    # only the 0.03 s pad is left on each side of the 1.0 s of speech
    assert abs(len(out) / SAMPLE_RATE - 1.06) < 0.01
