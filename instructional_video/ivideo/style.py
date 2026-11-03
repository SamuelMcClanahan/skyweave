"""Shared look for every scene, so scenes made by different agents match.

Frame: 14.22 x 8 Manim units (16:9). The safe area keeps 0.5 units of margin.
"""

from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    UL,
    Line,
    ManimColor,
    Text,
    VGroup,
    config,
)

BG = ManimColor("#0F1720")  # near-black blue
FG = ManimColor("#E8EEF4")  # main text
MUTED = ManimColor("#8A99A8")  # secondary text, axes, guides
ACCENT = ManimColor("#4FB3FF")  # primary highlight (blue)
ACCENT_2 = ManimColor("#FFB547")  # secondary highlight (amber)
GOOD = ManimColor("#5BD18B")  # correct / success (green)
BAD = ManimColor("#FF6B6B")  # wrong / warning (red)
PURPLE = ManimColor("#B794F6")
PALETTE = [ACCENT, ACCENT_2, GOOD, PURPLE, BAD]

FONT = "DejaVu Sans"
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
    """Scene title pinned to the top-left of the safe area, with an underline."""
    t = Text(s, font=FONT, font_size=TITLE_SIZE, color=FG, weight="BOLD")
    fit_width(t)
    t.to_corner(UL, buff=MARGIN)
    rule = Line(t.get_corner(DOWN + LEFT), t.get_corner(DOWN + LEFT) + [min(t.width, 4.0), 0, 0])
    rule.set_stroke(ACCENT, width=4).shift(DOWN * 0.15)
    return VGroup(t, rule)


def content_top() -> float:
    """y of the top of the content area, below a title."""
    return frame_h() / 2 - MARGIN - 1.0


def fit_width(mob, max_width: float | None = None):
    """Shrink mob so it fits inside the safe width (never grows it)."""
    left, right, _, _ = safe_box()
    mw = max_width if max_width is not None else right - left
    if mob.width > mw:
        mob.scale_to_fit_width(mw)
    return mob


__all__ = [
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
    "fit_width",
]
