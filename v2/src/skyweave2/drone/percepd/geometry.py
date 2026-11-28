"""Pure box geometry for percepd v1 (DRONE_CONTRACTS_D0.md [C2], [P1]; B-D2).

No camera, no model, no clock, no socket: arrays and numbers in, arrays and
numbers out, so every function here is unit-tested on synthetic values.

Three coordinate systems appear:

- **Model pixels**: the detector's input tensor, ``in_w`` x ``in_h``.
- **Frame pixels**: the source image, ``frame_w`` x ``frame_h``. Detector
  convention ("edge" coordinates): ``x = 0.0`` is the left edge of the left
  pixel column and ``x = frame_w`` is the right edge of the right column.
- **Flight grid** ([C2]): 1920 x 1200 continuous pixels where ``(0.0, 0.0)``
  is the *center* of the top-left pixel. The image spans ``[-0.5, 1919.5]``
  along ``u`` and ``[-0.5, 1199.5]`` along ``v``.

:func:`to_flight_grid` is the one place where frame pixels become flight-grid
pixels: scale each axis to 1920 x 1200, then subtract the half pixel between
the two conventions.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave2.drone.packets import FLIGHT_GRID_HEIGHT, FLIGHT_GRID_WIDTH

PAD_VALUE = 114
"""Gray level of the letterbox padding (the value YOLOX and Ultralytics train with)."""


@dataclass(frozen=True, kw_only=True)
class Letterbox:
    """How a frame sits inside the model input: resized by ``sx``, ``sy``
    (model px per frame px, equal up to rounding) to ``new_w`` x ``new_h``,
    with its top-left corner at ``(pad_x, pad_y)`` model pixels."""

    frame_w: int
    frame_h: int
    in_w: int
    in_h: int
    new_w: int
    new_h: int
    pad_x: int
    pad_y: int

    @property
    def sx(self) -> float:
        return self.new_w / self.frame_w

    @property
    def sy(self) -> float:
        return self.new_h / self.frame_h

    def to_model(self, xyxy: np.ndarray) -> np.ndarray:
        """Frame-pixel boxes ``(N, 4)`` as ``x1, y1, x2, y2`` -> model pixels."""
        out = np.asarray(xyxy, dtype=np.float64).reshape(-1, 4).copy()
        out[:, [0, 2]] = out[:, [0, 2]] * self.sx + self.pad_x
        out[:, [1, 3]] = out[:, [1, 3]] * self.sy + self.pad_y
        return out

    def to_frame(self, xyxy: np.ndarray) -> np.ndarray:
        """Model-pixel boxes ``(N, 4)`` as ``x1, y1, x2, y2`` -> frame pixels.
        The exact inverse of :meth:`to_model`; no clipping."""
        out = np.asarray(xyxy, dtype=np.float64).reshape(-1, 4).copy()
        out[:, [0, 2]] = (out[:, [0, 2]] - self.pad_x) / self.sx
        out[:, [1, 3]] = (out[:, [1, 3]] - self.pad_y) / self.sy
        return out


def letterbox_fit(frame_w: int, frame_h: int, in_w: int, in_h: int, *, center: bool) -> Letterbox:
    """Fit a ``frame_w`` x ``frame_h`` frame into ``in_w`` x ``in_h`` keeping
    its aspect ratio. ``center`` puts the padding on both sides (Ultralytics);
    otherwise the frame sits at the top-left corner (YOLOX)."""
    if min(frame_w, frame_h, in_w, in_h) <= 0:
        raise ValueError("letterbox sizes must be > 0")
    scale = min(in_w / frame_w, in_h / frame_h)
    new_w = min(in_w, max(1, int(round(frame_w * scale))))
    new_h = min(in_h, max(1, int(round(frame_h * scale))))
    pad_x = (in_w - new_w) // 2 if center else 0
    pad_y = (in_h - new_h) // 2 if center else 0
    return Letterbox(
        frame_w=frame_w,
        frame_h=frame_h,
        in_w=in_w,
        in_h=in_h,
        new_w=new_w,
        new_h=new_h,
        pad_x=pad_x,
        pad_y=pad_y,
    )


def letterbox_image(frame: np.ndarray, lb: Letterbox) -> np.ndarray:
    """The frame (H x W x 3, uint8) resized and padded to the model input
    size, same channel order, padding :data:`PAD_VALUE`."""
    import cv2  # a base dependency; imported here so the pure math imports without it

    if frame.shape[0] != lb.frame_h or frame.shape[1] != lb.frame_w:
        raise ValueError("frame size differs from the letterbox's frame size")
    out = np.full((lb.in_h, lb.in_w, 3), PAD_VALUE, dtype=np.uint8)
    resized = cv2.resize(frame, (lb.new_w, lb.new_h), interpolation=cv2.INTER_LINEAR)
    out[lb.pad_y : lb.pad_y + lb.new_h, lb.pad_x : lb.pad_x + lb.new_w] = resized
    return out


def iou_one_to_many(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    """IoU of one ``x1, y1, x2, y2`` box with each row of ``boxes``."""
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])
    inter = np.clip(x2 - x1, 0.0, None) * np.clip(y2 - y1, 0.0, None)
    area = (box[2] - box[0]) * (box[3] - box[1])
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    union = area + areas - inter
    return np.where(union > 0.0, inter / np.where(union > 0.0, union, 1.0), 0.0)


def nms(xyxy: np.ndarray, scores: np.ndarray, iou_max: float) -> list[int]:
    """Greedy non-maximum suppression. Returns the kept row indices, highest
    score first. A box is dropped when its IoU with a kept box is above
    ``iou_max``. Class-agnostic: percepd looks for one kind of target, and two
    class names on one object must not become two boxes."""
    order = [int(i) for i in np.argsort(-np.asarray(scores, dtype=np.float64), kind="stable")]
    boxes = np.asarray(xyxy, dtype=np.float64).reshape(-1, 4)
    keep: list[int] = []
    while order:
        i = order.pop(0)
        keep.append(i)
        if not order:
            break
        rest = np.array(order)
        ious = iou_one_to_many(boxes[i], boxes[rest])
        order = [int(j) for j in rest[ious <= iou_max]]
    return keep


def to_flight_grid(
    x1: float, y1: float, x2: float, y2: float, frame_w: int, frame_h: int
) -> tuple[float, float, float, float]:
    """One frame-pixel box (edge coordinates) -> ``x, y, w, h`` on the [C2]
    flight grid, clipped to the image.

    Each axis is scaled on its own, so the whole source image maps to the
    whole 1920 x 1200 grid. For the flight camera mode the scale is 1. A
    source with another aspect ratio is stretched (bench sources only).
    ``w`` or ``h`` can come back <= 0 for a box outside the image; the caller
    drops those ([P1] needs ``w, h > 0``).
    """
    sx = FLIGHT_GRID_WIDTH / frame_w
    sy = FLIGHT_GRID_HEIGHT / frame_h
    u1 = min(max(x1 * sx, 0.0), float(FLIGHT_GRID_WIDTH)) - 0.5
    u2 = min(max(x2 * sx, 0.0), float(FLIGHT_GRID_WIDTH)) - 0.5
    v1 = min(max(y1 * sy, 0.0), float(FLIGHT_GRID_HEIGHT)) - 0.5
    v2 = min(max(y2 * sy, 0.0), float(FLIGHT_GRID_HEIGHT)) - 0.5
    return (u1, v1, u2 - u1, v2 - v1)
