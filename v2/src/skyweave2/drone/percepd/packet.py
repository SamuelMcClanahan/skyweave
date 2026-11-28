"""Detections -> one [P1] detection packet (DRONE_CONTRACTS_D0.md [P1], [C2]).

Pure: the caller supplies ``t_cap`` and ``frame_seq``. The packet type and
its encoder are the frozen ones in ``skyweave2.drone.packets``; nothing here
writes JSON.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from skyweave2.drone.packets import Box, DetectionPacket
from skyweave2.drone.percepd.detect import Detection
from skyweave2.drone.percepd.geometry import to_flight_grid

MIN_BOX_PX = 1.0
"""A box narrower or lower than this on the flight grid (after clipping to the
image) is not sent: [P1] needs ``w, h > 0`` and a sliver at the image edge is
not a target (percepd v1 choice, Provisional)."""

DEFAULT_MAX_BOXES = 20


def build_detection_packet(
    detections: Iterable[Detection],
    *,
    t_cap: int,
    frame_seq: int,
    frame_w: int,
    frame_h: int,
    max_boxes: int = DEFAULT_MAX_BOXES,
) -> DetectionPacket:
    """The [P1] packet of one processed frame. Zero boxes is a valid packet.

    Each detection goes to the [C2] flight grid (top-left origin, clipped to
    the image). Boxes are ordered best confidence first and cut at
    ``max_boxes``. Box edges are rounded to 0.01 px and confidence to 1e-4,
    which keeps the datagram short and loses nothing a detector knows.
    """
    boxes: list[Box] = []
    for det in detections:
        vals = (det.x1, det.y1, det.x2, det.y2, det.conf)
        if not all(math.isfinite(v) for v in vals):
            continue
        x, y, w, h = to_flight_grid(det.x1, det.y1, det.x2, det.y2, frame_w, frame_h)
        if w < MIN_BOX_PX or h < MIN_BOX_PX:
            continue
        # Round the edges, not the size, so a rounded box never leaves the image.
        left, top = round(x, 2), round(y, 2)
        boxes.append(
            Box(
                x=left,
                y=top,
                w=round(round(x + w, 2) - left, 2),
                h=round(round(y + h, 2) - top, 2),
                conf=round(min(max(det.conf, 0.0), 1.0), 4),
            )
        )
    boxes.sort(key=lambda b: -b.conf)  # stable: equal confidences keep the input order
    return DetectionPacket(
        t_cap=int(t_cap), frame_seq=int(frame_seq), boxes=tuple(boxes[: max(0, max_boxes)])
    )
