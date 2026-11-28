"""Detections, the backend interface, and the pure post-processing step
(B-D2).

A backend turns one frame into a list of :class:`Detection` in frame pixels.
Everything after the model's raw tensor is :func:`postprocess`, a pure
function: decode (per layout), confidence threshold, class filter, NMS when
the layout needs it, and mapping back from the letterboxed model input to the
frame. The ONNX backend calls it; an NPU backend will call the same function.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from skyweave2.drone.percepd.geometry import Letterbox, nms
from skyweave2.drone.percepd.layouts import ModelLayout

COCO_NAMES: tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)  # fmt: skip
"""The 80 COCO class names in model class-id order."""

DEFAULT_CLASSES = "sports ball,kite,frisbee"
"""Default class filter for the COCO stand-in model: the COCO classes a party
balloon or a small flying toy is most often reported as (Provisional)."""

MAX_NMS_CANDIDATES = 300
"""Only the best candidates above the threshold go into NMS (bounds its time)."""


class BackendError(RuntimeError):
    """A detector backend cannot start (missing package, bad model file)."""


@dataclass(frozen=True, kw_only=True)
class Detection:
    """One box in frame pixels (edge coordinates, ``x1 < x2``, ``y1 < y2``)."""

    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    class_id: int


@dataclass(frozen=True, kw_only=True)
class DetectConfig:
    """Thresholds and the class filter."""

    conf_min: float = 0.25  # a box needs conf >= this
    iou_max: float = 0.45  # NMS: a box overlapping a better box by more than this is dropped
    class_ids: tuple[int, ...] | None = None  # allowed class ids; None = every class

    def __post_init__(self) -> None:
        if not 0.0 <= self.conf_min <= 1.0:
            raise ValueError("conf_min must be in [0, 1]")
        if not 0.0 <= self.iou_max <= 1.0:
            raise ValueError("iou_max must be in [0, 1]")
        if self.class_ids is not None and len(self.class_ids) == 0:
            raise ValueError("class_ids must name at least one class, or be None for all")


class Backend(Protocol):
    """A detector. ``detect`` takes one BGR frame (H x W x 3, uint8) and returns
    the boxes that passed the backend's thresholds and class filter."""

    def detect(self, frame: np.ndarray) -> list[Detection]: ...


class NullBackend:
    """Reports no boxes. For bringing up the camera, the stamps and the UDP
    link without a model or onnxruntime (``--backend null``)."""

    def detect(self, frame: np.ndarray) -> list[Detection]:
        return []


def parse_classes(spec: str, names: Sequence[str] = COCO_NAMES) -> tuple[int, ...] | None:
    """``"sports ball,kite,29"`` -> class ids; ``"all"`` -> ``None`` (no filter).
    Each item is a class name from ``names`` or a class id >= 0."""
    items = [s.strip() for s in spec.split(",") if s.strip()]
    if not items:
        raise ValueError("the class list is empty; name classes or say 'all'")
    if items == ["all"]:
        return None
    ids: list[int] = []
    for item in items:
        if item.isdigit():
            ids.append(int(item))
        elif item in names:
            ids.append(names.index(item))
        else:
            raise ValueError(f"unknown class {item!r}; use a COCO class name, a class id, or 'all'")
    return tuple(sorted(set(ids)))


def class_name(class_id: int, names: Sequence[str] = COCO_NAMES) -> str:
    return names[class_id] if 0 <= class_id < len(names) else str(class_id)


def postprocess(
    output: np.ndarray, layout: ModelLayout, lb: Letterbox, config: DetectConfig
) -> list[Detection]:
    """One raw output tensor -> the frame's detections, best first.

    Boxes are clipped to the frame; a box with no area left is dropped, as is
    any box with a non-finite number.
    """
    raw = layout.decode(output, lb.in_w, lb.in_h, config.class_ids)
    conf = raw.conf
    finite = np.isfinite(conf) & np.all(np.isfinite(raw.xyxy), axis=1)
    idx = np.flatnonzero(finite & (conf >= config.conf_min))
    if idx.size == 0:
        return []
    idx = idx[np.argsort(-conf[idx], kind="stable")][:MAX_NMS_CANDIDATES]
    if layout.needs_nms:
        idx = idx[nms(raw.xyxy[idx], conf[idx], config.iou_max)]
    boxes = lb.to_frame(raw.xyxy[idx])
    boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0.0, float(lb.frame_w))
    boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0.0, float(lb.frame_h))
    out: list[Detection] = []
    for (x1, y1, x2, y2), c, k in zip(boxes, conf[idx], raw.class_id[idx], strict=True):
        if x2 > x1 and y2 > y1:
            out.append(
                Detection(
                    x1=float(x1),
                    y1=float(y1),
                    x2=float(x2),
                    y2=float(y2),
                    conf=float(min(max(c, 0.0), 1.0)),
                    class_id=int(k),
                )
            )
    return out
