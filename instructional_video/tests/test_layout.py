"""Layout rules that need no render: grid cells, overlap tags, subtitle band, static guard."""

from __future__ import annotations

import numpy as np
from manim import Rectangle, Text

from ivideo.render import static_check
from ivideo.style import SUBTITLE_BAND, cell, cell_size, content_top, safe_box
from ivideo.synced import layout_issues


def test_grid_cells_cover_the_content_area():
    left, right, _, _ = safe_box()
    w, h = cell_size()
    assert np.allclose(cell("A1"), [left + w / 2, content_top() - h / 2, 0])
    assert np.allclose(cell("F6"), [right - w / 2, SUBTITLE_BAND + h / 2, 0])
    assert cell("c3")[0] < 0 < cell("D3")[0]  # columns C and D straddle the center


def test_overlap_tag_and_subtitle_band():
    box = Rectangle(width=3, height=1)
    a = Text("label one", font_size=28)
    b = Text("label two", font_size=28).move_to(a.get_center())
    low = Text("too low", font_size=28).move_to([0, -3.6, 0])
    kinds = [i["kind"] for i in layout_issues([box, a, b, low])]
    assert kinds.count("text_overlap") == 1 and "subtitle_band" in kinds
    allowed = [{id(x) for m in (a, b) for x in m.get_family()}]
    kinds = [i["kind"] for i in layout_issues([box, a, b], allowed)]
    assert "text_overlap" not in kinds


def test_static_check_catches_manimgl_and_randomness():
    assert static_check("from manimlib import *\n")
    assert any("ShowCreation" in m for m in static_check("x = ShowCreation(c)\n"))
    assert any("seed" in m for m in static_check("import random\nx = random.uniform(0, 1)\n"))
    assert static_check("import random\nrandom.seed(3)\nx = random.uniform(0, 1)\n") == []
    assert any("SyntaxError" in m for m in static_check("def f(:\n"))
    assert static_check("from ivideo.synced import *\nclass Main(SyncedScene):\n    pass\n") == []
