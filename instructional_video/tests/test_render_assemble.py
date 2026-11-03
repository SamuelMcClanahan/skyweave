"""Render real Manim scenes against tone-engine timing, then assemble. About 20-40 s."""

from __future__ import annotations

import json
import shutil

from conftest import FIXTURES, copy_scene

from ivideo.assemble import assemble, build_srt
from ivideo.render import render_scene, render_still


def test_layout_checker_flags_problems(project):
    copy_scene(project, "scene_bad.py", "s01")
    res = render_scene(project, "s01", "draft")
    assert res["ok"], res
    kinds = {i["kind"] for i in res["layout_issues"]}
    assert {"text_overlap", "off_frame", "tiny_text"} <= kinds
    assert res["clean"] is False
    assert len(res["frames"]["frames"]) == 4  # 3 lines + last frame
    assert res["frames"]["start_frame"].endswith("start.png")


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
    assert abs(out["duration"] - out["expected_duration"]) <= 1.5 / 30
    assert abs(out["expected_duration"] - timing["total_duration"]) < 0.3
    kinds = " ".join(out["streams"])
    assert "h264" in kinds and "aac" in kinds and "mov_text" in kinds
    srt = (project.out_dir / "final.srt").read_text()
    assert srt.count("-->") == 6
    assert out["offsets"]["s02"] > 0
    qa = out["qa"]
    assert qa["duration_ok"] and qa["streams_ok"] and qa["subtitles_ok"], qa
    assert qa["loudness_ok"], qa  # tone voice normalized to -16 LUFS
    assert qa["silence_ok"], qa  # planned gaps are all shorter than 1.7 s
    # audio and video segment lengths are whole frames, so cuts cannot drift
    assert all(abs(s["frames"] / 30 - s["segment"]) < 1e-3 for s in out["scenes"])


def test_srt_format():
    timing = {
        "speakers": [],
        "scenes": [
            {"id": "s01", "lines": [{"speaker": "h", "text": "hi", "start": 0.5, "end": 61.25}]}
        ],
    }
    assert build_srt(timing, {"s01": 3600.0}) == "1\n01:00:00,500 --> 01:01:01,250\nhi\n"


def test_shared_library_and_still(project):
    shutil.copy(FIXTURES / "common.py", project.scenes_dir / "common.py")
    still = render_still(project, "common.py", "Gallery")
    assert still["ok"], still
    copy_scene(project, "scene_common.py", "s01")
    res = render_scene(project, "s01", "draft")
    assert res["ok"], res
    assert res["layout_issues"] == [] and res["drift"] == []
