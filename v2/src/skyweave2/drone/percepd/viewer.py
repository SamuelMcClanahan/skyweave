"""Bench viewer for percepd v1 (B-D2): the latest frame with its boxes, as
one JPEG file that is overwritten in place.

Off by default. It exists so the owner can see what the detector sees on a
board without a display. It keeps one picture, the newest; it is not a
recording (project fence: no in-flight frame recording). Do not turn it on
in flight.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from skyweave2.drone.percepd.detect import COCO_NAMES, Detection, class_name

DEFAULT_VIEW_WIDTH = 960


def annotate(
    image: np.ndarray,
    detections: Sequence[Detection],
    *,
    caption: str = "",
    width: int = DEFAULT_VIEW_WIDTH,
    names: Sequence[str] = COCO_NAMES,
) -> np.ndarray:
    """A copy of the frame, scaled down to ``width`` when wider, with each
    detection drawn as a box labeled with its class and confidence."""
    import cv2

    h, w = image.shape[:2]
    k = min(1.0, width / w) if width > 0 else 1.0
    size = (max(1, round(w * k)), max(1, round(h * k)))
    out = cv2.resize(image, size) if k < 1.0 else image.copy()
    green, black = (0, 255, 0), (0, 0, 0)
    font = cv2.FONT_HERSHEY_SIMPLEX
    for det in detections:
        p1 = (round(det.x1 * k), round(det.y1 * k))
        p2 = (round(det.x2 * k), round(det.y2 * k))
        cv2.rectangle(out, p1, p2, green, 2)
        label = f"{class_name(det.class_id, names)} {det.conf:.2f}"
        org = (p1[0], max(12, p1[1] - 4))
        cv2.putText(out, label, org, font, 0.5, black, 2, cv2.LINE_AA)
        cv2.putText(out, label, org, font, 0.5, green, 1, cv2.LINE_AA)
    if caption:
        org = (6, out.shape[0] - 8)
        cv2.putText(out, caption, org, font, 0.5, black, 3, cv2.LINE_AA)
        cv2.putText(out, caption, org, font, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def write_view(path: str | Path, image: np.ndarray, quality: int = 80) -> None:
    """Write ``image`` as a JPEG at ``path``, replacing the old file in one
    step so a reader never sees half a picture. Raises ``OSError`` on failure."""
    import cv2

    path = Path(path)
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise OSError("JPEG encoding failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data.tobytes())
    os.replace(tmp, path)
