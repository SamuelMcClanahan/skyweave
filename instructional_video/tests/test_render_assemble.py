"""Render real Manim scenes against silence-engine timing, then assemble. About 20-40 s."""

from __future__ import annotations

import json

from conftest import copy_scene

from ivideo.assemble import assemble, build_srt
from ivideo.render import render_scene


def test_layout_checker_flags_problems(project):
    copy_scene(project, "scene_bad.py", "s01")
    res = render_scene(project, "s01", "draft")
    assert res["ok"], res
    kinds = {i["kind"] for i in res["layout_issues"]}
    assert {"text_overlap", "off_frame", "tiny_text"} <= kinds
    assert res["clean"] is False
    assert len(res["frames"]["frames"]) == 4  # 3 lines + last frame


def test_clean_scene_hits_audio_duration(project):
    copy_scene(project, "scene_good.py", "s01")
    res = render_scene(project, "s01", "draft")
    assert res["ok"], res
    assert res["layout_issues"] == []
    assert res["drift"] == []
    assert abs(res["duration_error"]) < 0.15
    assert abs(res["video_duration"] - res["target_duration"]) < 0.15
    assert res["clean"] is True


def test_broken_scene_reports_traceback(project):
    project.scene_file("s01").write_text(
        "from ivideo.synced import *\nclass Main(SyncedScene):\n    def construct(self):\n        raise ValueError('boom')\n"
    )
    res = render_scene(project, "s01", "draft", frames=False)
    assert res["ok"] is False
    assert "boom" in res["log_tail"]


def test_assemble(project):
    for sid in ("s01", "s02"):
        copy_scene(project, "scene_good.py", sid)
        assert render_scene(project, sid, "draft", frames=False)["ok"]
    out = assemble(project, quality="draft")
    timing = json.loads(project.timing.read_text())
    assert abs(out["duration"] - timing["total_duration"]) < 0.3
    kinds = " ".join(out["streams"])
    assert "h264" in kinds and "aac" in kinds and "mov_text" in kinds
    srt = (project.out_dir / "final.srt").read_text()
    assert srt.count("-->") == 6
    assert out["offsets"]["s02"] > 0


def test_srt_format():
    timing = {
        "speakers": [],
        "scenes": [
            {"id": "s01", "lines": [{"speaker": "h", "text": "hi", "start": 0.5, "end": 61.25}]}
        ],
    }
    assert build_srt(timing, {"s01": 3600.0}) == "1\n01:00:00,500 --> 01:01:01,250\nhi\n"
