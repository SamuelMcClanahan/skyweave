"""Shared look for every scene, so scenes made by different agents match.

Default theme "3b1b": a 3Blue1Brown-inspired look. Black background, white serif
text (CMU Serif, the Computer Modern face that LaTeX uses, so text matches the
math), and Manim's classic named colors (BLUE #58C4DD, YELLOW #F7D96F, GREEN
#83C167, RED #FC6255, TEAL, GOLD, PURPLE, PINK, MAROON, GREY). Those names come
with `from manim import *`. Set IVIDEO_THEME=slate for the older dark-slate look.

Frame: 14.22 x 8 Manim units (16:9). The safe area keeps 0.5 units of margin.
"""

from __future__ import annotations

import os
import subprocess

from manim import (
    BLUE,
    DOWN,
    GREEN,
    GREY_B,
    LEFT,
    PURPLE,
    RED,
    UL,
    UP,
    WHITE,
    YELLOW,
    Line,
    ManimColor,
    Text,
    VGroup,
    config,
)

THEME = os.environ.get("IVIDEO_THEME", "3b1b").lower()


def _has_font(name: str) -> bool:
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return name in out


if THEME == "slate":
    BG = ManimColor("#0F1720")  # near-black blue
    FG = ManimColor("#E8EEF4")  # main text
    MUTED = ManimColor("#8A99A8")  # secondary text, axes, guides
    ACCENT = ManimColor("#4FB3FF")  # primary highlight (blue)
    ACCENT_2 = ManimColor("#FFB547")  # secondary highlight (amber)
    GOOD = ManimColor("#5BD18B")  # correct / success (green)
    BAD = ManimColor("#FF6B6B")  # wrong / warning (red)
    PURPLE = ManimColor("#B794F6")
    FONT = "DejaVu Sans"
else:  # "3b1b"
    BG = ManimColor("#000000")
    FG = WHITE
    MUTED = GREY_B  # secondary text, axes, guides
    ACCENT = BLUE  # first concept / variable
    ACCENT_2 = YELLOW  # second concept / variable, highlights
    GOOD = GREEN
    BAD = RED
    FONT = "CMU Serif" if _has_font("CMU Serif") else "DejaVu Serif"
PALETTE = [ACCENT, ACCENT_2, GOOD, PURPLE, BAD]
MONO = "DejaVu Sans Mono"

TITLE_SIZE = 40
BODY_SIZE = 32
LABEL_SIZE = 26
MIN_FONT_SIZE = 20  # smaller text is hard to read at 1080p; the layout check flags it

MARGIN = 0.5


def frame_w() -> float:
    return config.frame_width


def frame_h() -> float:
    return config.frame_height


def safe_box() -> tuple[float, float, float, float]:
    """(left, right, bottom, top) of the safe area in scene units."""
    w, h = frame_w() / 2 - MARGIN, frame_h() / 2 - MARGIN
    return (-w, w, -h, h)


def text(s: str, size: int = BODY_SIZE, color=FG, **kw) -> Text:
    return Text(s, font=FONT, font_size=size, color=color, **kw)


def label(s: str, color=FG, **kw) -> Text:
    return Text(s, font=FONT, font_size=LABEL_SIZE, color=color, **kw)


def title(s: str) -> VGroup:
    """Scene title. 3b1b theme: centered at the top, plain serif, no box or underline.
    Slate theme: top-left with an accent underline."""
    if THEME == "slate":
        t = Text(s, font=FONT, font_size=TITLE_SIZE, color=FG, weight="BOLD")
        fit_width(t)
        t.to_corner(UL, buff=MARGIN)
        rule = Line(t.get_corner(DOWN + LEFT), t.get_corner(DOWN + LEFT) + [t.width, 0, 0])
        rule.set_stroke(ACCENT, width=4).shift(DOWN * 0.15)
        return VGroup(t, rule)
    t = Text(s, font=FONT, font_size=TITLE_SIZE, color=FG)
    fit_width(t)
    t.to_edge(UP, buff=MARGIN)
    return VGroup(t)


def content_top() -> float:
    """y of the top of the content area, below a title."""
    return frame_h() / 2 - MARGIN - 1.0


SUBTITLE_BAND = -3.0  # keep key content above this y: players draw subtitles below it
MIN_GAP = 0.3  # minimum space between objects that must not touch


def content_bottom() -> float:
    """y of the bottom of the content area (above the subtitle band)."""
    return SUBTITLE_BAND


def fit_width(mob, max_width: float | None = None):
    """Shrink mob so it fits inside the safe width (never grows it)."""
    left, right, _, _ = safe_box()
    mw = max_width if max_width is not None else right - left
    if mob.width > mw:
        mob.scale_to_fit_width(mw)
    return mob


GRID_COLS = "ABCDEF"
GRID_ROWS = 6


def cell(name: str):
    """Center of a named cell in the 6x6 content grid (Code2Video's anchor idea).

    Columns A-F run left to right, rows 1-6 top to bottom. The grid covers the
    content area: inside the safe box, below the title, above the subtitle band.
    Example: cell("A1") is top-left, cell("F6") is bottom-right, cell("C3") is near
    the center. Use it with move_to(): obj.move_to(cell("B4")).
    """
    import numpy as np

    name = name.strip().upper()
    col, row = GRID_COLS.index(name[0]), int(name[1:]) - 1
    if not (0 <= row < GRID_ROWS):
        raise ValueError(f"row must be 1-{GRID_ROWS}: {name}")
    left, right, _, _ = safe_box()
    top, bottom = content_top(), content_bottom()
    w, h = (right - left) / len(GRID_COLS), (top - bottom) / GRID_ROWS
    return np.array([left + (col + 0.5) * w, top - (row + 0.5) * h, 0.0])


def cell_size() -> tuple[float, float]:
    """(width, height) of one grid cell in scene units."""
    left, right, _, _ = safe_box()
    return (right - left) / len(GRID_COLS), (content_top() - content_bottom()) / GRID_ROWS


__all__ = [
    "THEME",
    "BG",
    "FG",
    "MUTED",
    "ACCENT",
    "ACCENT_2",
    "GOOD",
    "BAD",
    "PURPLE",
    "PALETTE",
    "FONT",
    "MONO",
    "TITLE_SIZE",
    "BODY_SIZE",
    "LABEL_SIZE",
    "MIN_FONT_SIZE",
    "MARGIN",
    "safe_box",
    "text",
    "label",
    "title",
    "content_top",
    "content_bottom",
    "fit_width",
    "cell",
    "cell_size",
    "SUBTITLE_BAND",
    "MIN_GAP",
]
