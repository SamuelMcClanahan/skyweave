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
    """Measure accepted components, with moments over the final binary mask.

    OpenCV supplies each component's area and bounding box.  The centroid is
    deliberately recomputed from every foreground pixel inside that bounding
    box, rather than from the component label.  This mirrors the RV1106 IVE
    path, whose compacted region table cannot be mapped reliably back to label
    values in the in-place CCL image.  Distinct components can have overlapping
    axis-aligned boxes; in that case the shared mask pixels enter both moments.

    ``area_px`` remains the connected-component area.  It is not replaced by
    the number of foreground pixels used for the bounding-box moment.
    """
    _require_cv2()
    binary = mask.astype(bool, copy=False)
    count, _, stats, _ = cv2.connectedComponentsWithStats(
        binary.astype(np.uint8), connectivity=4
    )
    out: list[MaskComponent] = []
    for label in range(1, count):  # 0 is background
        area = int(stats[label, cv2.CC_STAT_AREA])
        if not (min_area_px <= area <= max_area_px):
            continue
        bbox_x = int(stats[label, cv2.CC_STAT_LEFT])
        bbox_y = int(stats[label, cv2.CC_STAT_TOP])
        bbox_w = int(stats[label, cv2.CC_STAT_WIDTH])
        bbox_h = int(stats[label, cv2.CC_STAT_HEIGHT])
        local_v, local_u = np.nonzero(
            binary[bbox_y : bbox_y + bbox_h, bbox_x : bbox_x + bbox_w]
        )
        moment_area = int(local_u.size)
        if moment_area == 0:  # pragma: no cover - a cv2 component guarantees one pixel
            raise RuntimeError(f"component {label} has an empty foreground bounding box")
        # Sum integer pixel coordinates before the one floating-point division.
        # Besides being exact for these image sizes, this preserves the old cv2
        # centroid bit pattern whenever no other component enters the bbox.
        sum_u = int(local_u.sum(dtype=np.int64)) + bbox_x * moment_area
        sum_v = int(local_v.sum(dtype=np.int64)) + bbox_y * moment_area
        out.append(
            MaskComponent(
                centroid_u=sum_u / moment_area,
                centroid_v=sum_v / moment_area,
                area_px=area,
                bbox_x=bbox_x,
                bbox_y=bbox_y,
                bbox_w=bbox_w,
                bbox_h=bbox_h,
            )
        )
    return sorted(out, key=lambda c: (c.centroid_v, c.centroid_u))


def count_overlapping_bbox_pairs(components: list[MaskComponent]) -> int:
    """Count unordered accepted-component pairs whose pixel boxes overlap."""
    count = len(components)
    if count < 2:
        return 0
    left = np.fromiter((c.bbox_x for c in components), dtype=np.int64, count=count)
    top = np.fromiter((c.bbox_y for c in components), dtype=np.int64, count=count)
    right = left + np.fromiter((c.bbox_w for c in components), dtype=np.int64, count=count)
    bottom = top + np.fromiter((c.bbox_h for c in components), dtype=np.int64, count=count)
    pairs = 0
    for index in range(count - 1):
        after = slice(index + 1, None)
        pairs += int(
            np.count_nonzero(
                (left[after] < right[index])
                & (right[after] > left[index])
                & (top[after] < bottom[index])
                & (bottom[after] > top[index])
            )
        )
    return int(pairs)
