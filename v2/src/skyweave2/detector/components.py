"""Connected components + measurements on foreground masks (cv2, required).

The refusal rule (F-D0-8): cv2 is the authoritative CCL implementation for
D4. If it is missing, raise — never fall back silently to a pure-python
path whose component semantics could differ from what the scorecards were
built on.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - exercised via _require_cv2 in tests
    cv2 = None


def _require_cv2():
    if cv2 is None:
        raise RuntimeError(
            "OpenCV (cv2) is required for the D4 detector path and is not "
            "installed. Refusing to run with a fallback (finding F-D0-8); "
            "install opencv-python-headless via `uv sync`."
        )
    return cv2


@dataclass(frozen=True)
class MaskComponent:
    """One connected component at PROC resolution."""

    centroid_u: float
    centroid_v: float
    area_px: int
    bbox_x: int
    bbox_y: int
    bbox_w: int
    bbox_h: int


def open_mask(mask: np.ndarray, radius_px: int) -> np.ndarray:
    """Morphological opening with an elliptical kernel; radius 0 = no-op."""
    _require_cv2()
    if radius_px <= 0:
        return mask
    size = 2 * radius_px + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    return cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, kernel).astype(bool)


def find_components(
    mask: np.ndarray, min_area_px: int, max_area_px: int
) -> list[MaskComponent]:
    """cv2 connectedComponentsWithStats, area-gated, deterministic order."""
    _require_cv2()
    count, _, stats, centroids = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=4
    )
    out: list[MaskComponent] = []
    for label in range(1, count):  # 0 is background
        area = int(stats[label, cv2.CC_STAT_AREA])
        if not (min_area_px <= area <= max_area_px):
            continue
        out.append(
            MaskComponent(
                centroid_u=float(centroids[label, 0]),
                centroid_v=float(centroids[label, 1]),
                area_px=area,
                bbox_x=int(stats[label, cv2.CC_STAT_LEFT]),
                bbox_y=int(stats[label, cv2.CC_STAT_TOP]),
                bbox_w=int(stats[label, cv2.CC_STAT_WIDTH]),
                bbox_h=int(stats[label, cv2.CC_STAT_HEIGHT]),
            )
        )
    return sorted(out, key=lambda c: (c.centroid_v, c.centroid_u))
