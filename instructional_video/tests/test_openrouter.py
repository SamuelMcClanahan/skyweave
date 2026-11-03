"""The OpenRouter path, against a local fake server (no network, no key)."""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ivideo import dialogue
from ivideo.project import Project


class FakeOpenRouter:
    def __init__(self, responses: list[tuple[int, dict]]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                outer.headers.append(dict(self.headers))
                status, payload = outer.responses.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def completion(content: str, model: str = "test/model") -> dict:
    return {
        "id": "gen-1",
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
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

    def make(responses):
        f = FakeOpenRouter(responses)
        servers.append(f)
        monkeypatch.setenv("OPENROUTER_BASE_URL", f.url)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-test-key")
        return f

    yield make
    for s in servers:
        s.close()


def test_structured_request_and_repair(fake, tmp_path, demo_script):
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
        proj,
        topic="triangulation",
        model="test/model",
        fallbacks=["test/fb"],
        minutes=1,
        speakers=2,
    )

    first = f.requests[0]
    assert first["model"] == "test/model"
    assert first["models"] == ["test/model", "test/fb"]
    rf = first["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert first["provider"] == {"require_parameters": True}
    assert f.headers[0]["Authorization"] == "Bearer fake-test-key"
    # second request carries the validator's complaint back to the model
    assert "markup" in f.requests[1]["messages"][-1]["content"]
    assert [s["id"] for s in data["scenes"]] == ["s01", "s02"]
    assert data["meta"]["dialogue_model"] == "test/model"
    log = json.loads(proj.openrouter_log.read_text())
    assert len(log) == 2 and log[1]["problems"] == [] and log[0]["usage"]["cost"] == 0.001


def test_falls_back_to_prompted_json(fake, tmp_path, demo_script):
    err = {
        "error": {
            "code": 404,
            "message": "No endpoints found that can handle the requested parameters.",
        }
    }
    f = fake([(404, err), (200, completion(json.dumps(demo_script)))])
    dialogue.write_dialogue(Project(tmp_path / "p"), topic="t", model="test/model", fallbacks=[])
    assert "response_format" not in f.requests[1]
    assert "JSON Schema" in f.requests[1]["messages"][-1]["content"]


def test_retries_rate_limit(fake, tmp_path, demo_script, monkeypatch):
    monkeypatch.setattr("ivideo.openrouter.time.sleep", lambda s: None)
    f = fake(
        [
            (429, {"error": {"code": 429, "message": "slow down"}}),
            (200, completion(json.dumps(demo_script))),
        ]
    )
    dialogue.write_dialogue(Project(tmp_path / "p"), topic="t", model="m", fallbacks=[])
    assert len(f.requests) == 2


def test_gives_up_after_repairs(fake, tmp_path):
    f = fake([(200, completion("not json"))] * 3)
    with pytest.raises(SystemExit, match="still invalid"):
        dialogue.write_dialogue(
            Project(tmp_path / "p"), topic="t", model="m", fallbacks=[], max_repairs=2
        )
    assert len(f.requests) == 3


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
