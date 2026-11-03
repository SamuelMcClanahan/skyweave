"""SyncedScene: a Manim Scene that is timed to the narration audio.

Usage in a scene file (scenes/s01.py):

    from ivideo.synced import *          # manim + style + SyncedScene

    class Main(SyncedScene):
        def construct(self):
            self.at_line(0)                       # wait until line 0 starts
            t = title(self.scene_title)
            self.play(Write(t), run_time=self.fit(0, 0.5))
            self.at_line(1)
            ...
            self.finish()                         # hold until the scene audio ends

`ivideo render` sets IVIDEO_TIMING, IVIDEO_SCENE and IVIDEO_REPORT. After every
play() and at finish() the scene checks the layout (text off-frame, text
overlapping text, tiny text) and writes all problems plus timing drift to the
report JSON.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from manim import *  # noqa: F403  (scene files get the full manim namespace)
from manim import Mobject, Scene, VMobject, config
from manim.mobject.text.tex_mobject import SingleStringMathTex
from manim.mobject.text.text_mobject import MarkupText, Paragraph, Text

from .style import *  # noqa: F403
from .style import BG, MIN_FONT_SIZE, SUBTITLE_BAND

# BackgroundRectangle / add_background_rectangle default to config.background_color.
config.background_color = BG

TEXT_TYPES = (Text, MarkupText, Paragraph, SingleStringMathTex)
EDGE_TOL = 0.02  # scene units allowed past the frame edge
OVERLAP_MIN = 0.08  # fraction of the smaller box that must overlap to count


@dataclass(frozen=True)
class Beat:
    """One spoken line with its absolute start/end time in the scene."""

    index: int
    speaker: str
    text: str
    visual: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2


def _load_timing(sid: str) -> dict:
    path = os.environ.get("IVIDEO_TIMING")
    if not path:
        raise RuntimeError(
            "IVIDEO_TIMING is not set. Render with `ivideo render`, not plain manim."
        )
    data = json.loads(Path(path).read_text())
    for sc in data["scenes"]:
        if sc["id"] == sid:
            return sc
    raise RuntimeError(f"scene {sid!r} not in {path}")


def _visible(m: Mobject) -> bool:
    if isinstance(m, VMobject):
        try:
            fo = float(np.max(m.get_fill_opacities())) if len(m.get_fill_opacities()) else 0.0
            so = float(np.max(m.get_stroke_opacities())) if len(m.get_stroke_opacities()) else 0.0
        except Exception:
            return True
        if fo < 0.05 and so < 0.05:
            # A group: visible if any member is visible.
            return any(_visible(s) for s in m.submobjects) if m.submobjects else False
    return True


def _box(m: Mobject) -> tuple[float, float, float, float] | None:
    if m.has_no_points() and not m.submobjects:
        return None
    try:
        return (m.get_left()[0], m.get_right()[0], m.get_bottom()[1], m.get_top()[1])
    except Exception:
        return None


def _name(m: Mobject) -> str:
    if isinstance(m, (Text, MarkupText)):
        s = m.text if hasattr(m, "text") else ""
    elif isinstance(m, SingleStringMathTex):
        s = getattr(m, "tex_string", "")
    elif isinstance(m, Paragraph):
        s = " ".join(getattr(x, "text", "") for x in m.submobjects)
    else:
        s = ""
    s = " ".join(str(s).split())
    return f"{type(m).__name__}({s[:40]!r})" if s else type(m).__name__


def _collect_texts(mobs: list[Mobject]) -> list[Mobject]:
    out: list[Mobject] = []

    def walk(m: Mobject) -> None:
        if isinstance(m, TEXT_TYPES):
            if _visible(m):
                out.append(m)
            return
        for s in m.submobjects:
            walk(s)

    for m in mobs:
        walk(m)
    return out


def layout_issues(mobs: list[Mobject], allowed: list[set[int]] | None = None) -> list[dict]:
    """Find things that leave the frame, text on top of text, tiny text, and text in
    the subtitle band. `allowed`: groups of mobject ids that may overlap on purpose."""
    hw, hh = config.frame_width / 2, config.frame_height / 2
    issues: list[dict] = []
    for m in mobs:  # anything (not only text) leaving the frame
        if not _visible(m):
            continue
        b = _box(m)
        if b is None:
            continue
        x0, x1, y0, y1 = b
        if x0 < -hw - EDGE_TOL or x1 > hw + EDGE_TOL or y0 < -hh - EDGE_TOL or y1 > hh + EDGE_TOL:
            issues.append({"kind": "off_frame", "a": _name(m), "box": [round(v, 2) for v in b]})
    texts = _collect_texts(mobs)
    boxes = [(_box(m), m) for m in texts]
    boxes = [(b, m) for b, m in boxes if b is not None]
    for m in texts:
        try:
            fs = float(m.font_size)
        except Exception:
            fs = None
        if fs is not None and fs < MIN_FONT_SIZE * 0.98:
            issues.append({"kind": "tiny_text", "a": _name(m), "font_size": round(float(fs), 1)})
        b = _box(m)
        if b is not None and b[2] < SUBTITLE_BAND - 0.05:
            issues.append({"kind": "subtitle_band", "a": _name(m), "bottom": round(b[2], 2)})
    for i in range(len(boxes)):
        (l1, r1, b1, t1), m1 = boxes[i]
        for j in range(i + 1, len(boxes)):
            (l2, r2, b2, t2), m2 = boxes[j]
            if allowed and any(id(m1) in g and id(m2) in g for g in allowed):
                continue
            iw = min(r1, r2) - max(l1, l2)
            ih = min(t1, t2) - max(b1, b2)
            if iw <= 0 or ih <= 0:
                continue
            a1 = max((r1 - l1) * (t1 - b1), 1e-6)
            a2 = max((r2 - l2) * (t2 - b2), 1e-6)
            frac = iw * ih / min(a1, a2)
            if frac >= OVERLAP_MIN:
                issues.append(
                    {
                        "kind": "text_overlap",
                        "a": _name(m1),
                        "b": _name(m2),
                        "overlap": round(frac, 2),
                    }
                )
    return issues


class SyncedScene(Scene):
    """Scene timed to timing.json. Subclass it as `class Main(SyncedScene)`."""

    def setup(self) -> None:
        super().setup()
        self.camera.background_color = BG
        self.scene_id = os.environ.get("IVIDEO_SCENE", "")
        self._timing = _load_timing(self.scene_id)
        self.lines = [
            Beat(ln["index"], ln["speaker"], ln["text"], ln["visual"], ln["start"], ln["end"])
            for ln in self._timing["lines"]
        ]
        self.scene_duration = float(self._timing["duration"])
        self.scene_title = self._timing["title"]
        self._drift: list[dict] = []
        self._issues: dict[tuple, dict] = {}
        self._allowed: list[set[int]] = []
        self._finished = False

    # timing helpers
    def line(self, i: int) -> Beat:
        return self.lines[i]

    @property
    def now(self) -> float:
        return float(self.time)

    def wait_until(self, t: float, why: str = "") -> None:
        """Wait until absolute scene time t. Records drift if the scene is already late."""
        dt = t - self.time
        frame = 1.0 / config.frame_rate
        if dt >= frame:
            self.wait(dt)
        elif dt < -0.15:
            self._drift.append(
                {
                    "at": round(self.time, 2),
                    "target": round(t, 2),
                    "late_by": round(-dt, 2),
                    "why": why,
                }
            )

    def at_line(self, i: int, offset: float = 0.0) -> None:
        """Wait until line i starts (plus offset seconds)."""
        self.wait_until(self.lines[i].start + offset, f"line {i}")

    def remaining(self, i: int) -> float:
        """Seconds left until line i ends."""
        return max(0.0, self.lines[i].end - self.time)

    def fit(self, i: int, fraction: float = 0.5, lo: float = 0.3, hi: float = 2.5) -> float:
        """A run_time that uses `fraction` of the time left in line i, clamped to [lo, hi]."""
        return float(min(hi, max(lo, self.remaining(i) * fraction)))

    def finish(self) -> None:
        """Hold the last frame until the scene audio ends, then write the report."""
        self.wait_until(self.scene_duration, "scene end")
        self._finished = True
        self._check("finish")
        self._write_report()

    # layout checks
    def allow_overlap(self, *mobs: Mobject) -> None:
        """Mark these mobjects (and everything inside them) as allowed to overlap each
        other on purpose, for example a label inside its box. Other overlaps still count."""
        self._allowed.append({id(x) for m in mobs for x in m.get_family()})

    def _check(self, where: str) -> None:
        for issue in layout_issues(list(self.mobjects), self._allowed):
            key = (issue["kind"], issue.get("a"), issue.get("b"))
            if key not in self._issues:
                issue["t"] = round(self.time, 2)
                issue["where"] = where
                self._issues[key] = issue

    def play(self, *args, **kwargs):
        super().play(*args, **kwargs)
        self._check(f"play at t={self.time:.2f}")

    def tear_down(self) -> None:
        if not self._finished:
            self._drift.append(
                {
                    "at": round(self.time, 2),
                    "target": self.scene_duration,
                    "late_by": 0,
                    "why": "finish() not called",
                }
            )
            self._write_report()
        super().tear_down()

    def _write_report(self) -> None:
        path = os.environ.get("IVIDEO_REPORT")
        if not path:
            return
        rep = {
            "scene": self.scene_id,
            "target_duration": self.scene_duration,
            "rendered_duration": round(self.time, 3),
            "duration_error": round(self.time - self.scene_duration, 3),
            "drift": self._drift,
            "layout_issues": list(self._issues.values()),
            "clean": not self._drift
            and not self._issues
            and abs(self.time - self.scene_duration) < 0.25,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(rep, indent=2))
