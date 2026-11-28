"""Model output layouts for percepd v1 (B-D2): one small pure decoder each.

A *layout* is what a model family expects at its input (padding side, channel
order, value range) and how its raw output tensor encodes boxes. Each decoder
is a pure function::

    decode(output, in_w, in_h, allowed) -> RawDetections

``output`` is the model's first output tensor, ``in_w`` x ``in_h`` the model
input size, ``allowed`` the class ids the caller wants (``None`` = all). The
result is every candidate box in model pixels with one confidence and one
class id, before any threshold or NMS. A decoder checks the tensor's shape
and raises :class:`LayoutError` when it is not its layout, so a wrong
``--layout`` fails loudly instead of sending garbage boxes.

The layout of the future trained balloon model is one more function here and
one more entry in :data:`LAYOUTS`.

Class choice: a candidate's class is the best of the *allowed* classes, not
the best of all classes. With a COCO model on a balloon the top class is
often something else (``apple``, ``clock``) while ``sports ball`` or ``kite``
still scores well; the allowed-only rule keeps such a box. This is a bench
choice for the COCO stand-in model (Provisional).

Layouts:

- ``yolox``: YOLOX ONNX export without in-graph decoding (the official
  release files). Output ``(1, N, 5 + C)``: per anchor point ``cx, cy, w, h``
  in grid units (center offset, log size), objectness, class scores. Scores
  are already sigmoid. Strides 8, 16, 32. Input: BGR, 0-255, frame at the
  top-left corner. Verified against ``yolox_tiny.onnx`` (see README.md).
- ``ultralytics``: Ultralytics detect export with NMS outside the graph
  (YOLOv8, YOLO11). Output ``(1, 4 + C, N)``: ``cx, cy, w, h`` in model
  pixels, then class scores (sigmoid, no objectness). Input: RGB, 0-1,
  centered padding. Tested on synthetic tensors only.
- ``ultralytics_e2e``: Ultralytics end-to-end export (YOLOv10, YOLO26; NMS
  free). Output ``(1, N, 6)``: ``x1, y1, x2, y2, conf, class id`` in model
  pixels. Input as ``ultralytics``. Tested on synthetic tensors only; check
  the shape of the real export before use.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from skyweave2.drone.percepd.geometry import Letterbox, letterbox_image

STRIDES = (8, 16, 32)


class LayoutError(ValueError):
    """The output tensor does not have this layout's shape."""


@dataclass(frozen=True, kw_only=True)
class RawDetections:
    """Candidate boxes in model pixels: ``xyxy`` ``(N, 4)``, ``conf`` ``(N,)``,
    ``class_id`` ``(N,)`` int. No threshold, no NMS."""

    xyxy: np.ndarray
    conf: np.ndarray
    class_id: np.ndarray


Decoder = Callable[[np.ndarray, int, int, "Sequence[int] | None"], RawDetections]


def anchor_count(in_w: int, in_h: int) -> int:
    """Anchor points of a three-level head (strides 8, 16, 32) at this input size."""
    return sum((in_h // s) * (in_w // s) for s in STRIDES)


def best_allowed(
    class_scores: np.ndarray, allowed: Sequence[int] | None
) -> tuple[np.ndarray, np.ndarray]:
    """Per row of ``class_scores`` ``(N, C)``: the best score among the
    allowed class ids and that class id."""
    n_classes = class_scores.shape[1]
    if allowed is None:
        ids = np.arange(n_classes)
    else:
        ids = np.array(sorted(set(allowed)), dtype=np.int64)
        if ids.size == 0 or ids[0] < 0 or ids[-1] >= n_classes:
            raise LayoutError(
                f"class ids {list(ids)} do not fit a model with {n_classes} classes"
            )
    sub = class_scores[:, ids]
    best = np.argmax(sub, axis=1)
    return sub[np.arange(sub.shape[0]), best], ids[best]


def _cxcywh_to_xyxy(cx: np.ndarray, cy: np.ndarray, w: np.ndarray, h: np.ndarray) -> np.ndarray:
    return np.stack([cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0], axis=1)


def decode_yolox(
    output: np.ndarray, in_w: int, in_h: int, allowed: Sequence[int] | None
) -> RawDetections:
    """``yolox`` layout: ``(1, N, 5 + C)``, grid-relative boxes."""
    out = np.asarray(output, dtype=np.float64)
    if out.ndim == 3 and out.shape[0] == 1:
        out = out[0]
    n = anchor_count(in_w, in_h)
    if out.ndim != 2 or out.shape[0] != n or out.shape[1] < 6:
        raise LayoutError(
            f"yolox layout needs (1, {n}, 5 + classes) for a {in_w}x{in_h} input, "
            f"got {tuple(np.shape(output))}"
        )
    gx_parts, gy_parts, stride_parts = [], [], []
    for s in STRIDES:
        ws, hs = in_w // s, in_h // s
        xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))  # row-major: y outer, x inner
        gx_parts.append(xv.ravel())
        gy_parts.append(yv.ravel())
        stride_parts.append(np.full(ws * hs, s))
    gx = np.concatenate(gx_parts)
    gy = np.concatenate(gy_parts)
    stride = np.concatenate(stride_parts)
    with np.errstate(over="ignore"):  # a garbage log-size overflows; dropped later as non-finite
        cx = (out[:, 0] + gx) * stride
        cy = (out[:, 1] + gy) * stride
        w = np.exp(out[:, 2]) * stride
        h = np.exp(out[:, 3]) * stride
    cls_conf, class_id = best_allowed(out[:, 5:], allowed)
    return RawDetections(
        xyxy=_cxcywh_to_xyxy(cx, cy, w, h), conf=out[:, 4] * cls_conf, class_id=class_id
    )


def decode_ultralytics(
    output: np.ndarray, in_w: int, in_h: int, allowed: Sequence[int] | None
) -> RawDetections:
    """``ultralytics`` layout: ``(1, 4 + C, N)``, boxes in model pixels."""
    out = np.asarray(output, dtype=np.float64)
    if out.ndim == 3 and out.shape[0] == 1:
        out = out[0]
    n = anchor_count(in_w, in_h)
    if out.ndim != 2 or out.shape[1] != n or out.shape[0] < 5:
        raise LayoutError(
            f"ultralytics layout needs (1, 4 + classes, {n}) for a {in_w}x{in_h} input, "
            f"got {tuple(np.shape(output))}"
        )
    out = out.T  # (N, 4 + C)
    conf, class_id = best_allowed(out[:, 4:], allowed)
    return RawDetections(
        xyxy=_cxcywh_to_xyxy(out[:, 0], out[:, 1], out[:, 2], out[:, 3]),
        conf=conf,
        class_id=class_id,
    )


def decode_ultralytics_e2e(
    output: np.ndarray, in_w: int, in_h: int, allowed: Sequence[int] | None
) -> RawDetections:
    """``ultralytics_e2e`` layout: ``(1, N, 6)`` rows of ``x1, y1, x2, y2,
    conf, class id``; NMS already done in the graph. Unused rows are zero."""
    out = np.asarray(output, dtype=np.float64)
    if out.ndim == 3 and out.shape[0] == 1:
        out = out[0]
    if out.ndim != 2 or out.shape[1] != 6:
        raise LayoutError(f"ultralytics_e2e layout needs (1, N, 6), got {tuple(np.shape(output))}")
    class_id = np.rint(out[:, 5]).astype(np.int64)
    conf = out[:, 4].copy()
    if allowed is not None:
        conf[~np.isin(class_id, np.array(sorted(set(allowed)), dtype=np.int64))] = 0.0
    return RawDetections(xyxy=out[:, :4].copy(), conf=conf, class_id=class_id)


@dataclass(frozen=True, kw_only=True)
class ModelLayout:
    """One model family: input preparation plus its output decoder."""

    name: str
    decode: Decoder
    needs_nms: bool
    center_pad: bool  # padding on both sides (True) or frame at the top-left (False)
    rgb: bool  # model wants RGB (True) or BGR (False); frames arrive BGR
    scale: float  # multiplier on the 0-255 pixel values


LAYOUTS: dict[str, ModelLayout] = {
    "yolox": ModelLayout(
        name="yolox", decode=decode_yolox, needs_nms=True, center_pad=False, rgb=False, scale=1.0
    ),
    "ultralytics": ModelLayout(
        name="ultralytics",
        decode=decode_ultralytics,
        needs_nms=True,
        center_pad=True,
        rgb=True,
        scale=1.0 / 255.0,
    ),
    "ultralytics_e2e": ModelLayout(
        name="ultralytics_e2e",
        decode=decode_ultralytics_e2e,
        needs_nms=False,
        center_pad=True,
        rgb=True,
        scale=1.0 / 255.0,
    ),
}


def make_blob(frame: np.ndarray, lb: Letterbox, layout: ModelLayout) -> np.ndarray:
    """A BGR frame -> the model input tensor ``(1, 3, in_h, in_w)`` float32."""
    img = letterbox_image(frame, lb)
    if layout.rgb:
        img = img[:, :, ::-1]
    blob = np.ascontiguousarray(img.transpose(2, 0, 1), dtype=np.float32)
    if layout.scale != 1.0:
        blob *= np.float32(layout.scale)
    return blob[None]
