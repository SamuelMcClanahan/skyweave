"""D series: percepd v1 (DRONE_CONTRACTS_D0.md [P1], [C1], [C2], [C10]; §11.4 B-D2).

percepd reads frames, runs a detector, and sends one [P1] detection packet
per processed frame. These tests need no camera, no model file and no
network beyond loopback UDP.

What is real and what is a stand-in (TESTING_DOCTRINE rule 2): the camera and
the trained model are the physical boundary, so a scripted source and a
scripted backend stand in for them. Everything else is the real code: the
pure geometry and decoders, the frozen wire encoder and decoder
(``packets.py``), the real ``UdpSender`` and ``UdpReceiver`` on loopback, the
real file sources on files the test writes, and the real companion core with
its tracker.

Expected values are boxes the test itself placed (in a synthetic image or a
synthetic output tensor), contract text, or the golden packet fixtures.

One test runs a real ONNX model. It is skipped unless
``SKYWEAVE_PERCEPD_MODEL`` names a model file (and onnxruntime is installed).
"""

from __future__ import annotations

import logging
import os
import select
import signal
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from skyweave2.drone import packets as P
from skyweave2.drone.core import CompanionCore, CoreConfig
from skyweave2.drone.packets import DetectionPacket, PacketKind, TrackState
from skyweave2.drone.percepd import cli
from skyweave2.drone.percepd.detect import (
    DEFAULT_CLASSES,
    BackendError,
    DetectConfig,
    Detection,
    NullBackend,
    parse_classes,
    postprocess,
)
from skyweave2.drone.percepd.geometry import (
    PAD_VALUE,
    letterbox_fit,
    letterbox_image,
    nms,
    to_flight_grid,
)
from skyweave2.drone.percepd.layouts import LAYOUTS, STRIDES, LayoutError, anchor_count
from skyweave2.drone.percepd.onnx_backend import OnnxBackend
from skyweave2.drone.percepd.packet import build_detection_packet
from skyweave2.drone.percepd.pipeline import Percepd, PipelineConfig
from skyweave2.drone.percepd.sources import (
    CameraSource,
    EndOfSource,
    Frame,
    ImageSource,
    SourceError,
    VideoSource,
    board_clock,
)
from skyweave2.drone.udp import UdpReceiver, UdpSender
from tests.drone.fc_attitude import attitude_frame

GOLDEN = Path(__file__).parent / "golden"
GRID_W, GRID_H = P.FLIGHT_GRID_WIDTH, P.FLIGHT_GRID_HEIGHT
SPORTS_BALL, KITE, FRISBEE, PERSON, APPLE = 32, 33, 29, 0, 47


# ---------------------------------------------------------------------------
# Stand-ins for the physical boundary, and helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """An injected board clock ([C1]); each read can step it."""

    def __init__(self, t: int = 5000, step: int = 0) -> None:
        self.t = t
        self.step = step

    def __call__(self) -> int:
        self.t += self.step
        return self.t


class ScriptSource:
    """A camera stand-in. ``script`` items are frames or exceptions to raise
    from ``read``; ``open_errors`` opens fail first."""

    def __init__(self, clock: FakeClock, script: list, open_errors: int = 0) -> None:
        self.clock = clock
        self.script = list(script)
        self.open_errors = open_errors
        self.open_calls = 0
        self.close_calls = 0

    def open(self) -> None:
        self.open_calls += 1
        if self.open_errors > 0:
            self.open_errors -= 1
            raise SourceError("camera not present")

    def read(self) -> Frame:
        if not self.script:
            raise EndOfSource
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return Frame(image=item, t_cap=self.clock())

    def close(self) -> None:
        self.close_calls += 1


class ScriptBackend:
    """A detector stand-in: the n-th call returns (or raises) the n-th item;
    the last item repeats."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = 0

    def detect(self, frame: np.ndarray) -> list[Detection]:
        item = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return list(item)


def _frame(w: int = GRID_W, h: int = GRID_H) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _det(x1: float, y1: float, x2: float, y2: float, conf: float = 0.9, k: int = 32) -> Detection:
    return Detection(x1=x1, y1=y1, x2=x2, y2=y2, conf=conf, class_id=k)


def _link() -> tuple[UdpReceiver, UdpSender]:
    """The companion's real detection receiver ([C10]) and percepd's real sender."""
    rx = UdpReceiver(PacketKind.DETECTION, port=0)
    return rx, UdpSender(*rx.address)


def _received(rx: UdpReceiver, timeout_s: float = 1.0) -> list[DetectionPacket]:
    """Every datagram waiting on ``rx``, through the frozen decoder ([C7])."""
    select.select([rx.fileno()], [], [], timeout_s)
    out = [r.packet for r in rx.poll()]
    assert rx.rejected == 0
    return out


def _percepd(source, backend, sender, clock, **config) -> tuple[Percepd, list[float]]:
    waits: list[float] = []
    cfg = PipelineConfig(stats_interval_ms=0, **config)
    return Percepd(source, backend, sender, clock, cfg, wait=waits.append), waits


def _yolox_tensor(in_w: int, in_h: int, entries, n_classes: int = 80) -> np.ndarray:
    """A YOLOX raw output with the given boxes placed in it. Each entry is
    ``(stride, model-pixel xyxy, objectness, {class id: score})``; the box is
    written at the grid cell of its center, in the grid-relative encoding of
    the YOLOX head (center offset in cells, log size in strides)."""
    out = np.zeros((1, anchor_count(in_w, in_h), 5 + n_classes), dtype=np.float32)
    for stride, (x1, y1, x2, y2), obj, scores in entries:
        offset = sum((in_w // s) * (in_h // s) for s in STRIDES[: STRIDES.index(stride)])
        cx, cy, w, h = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1
        gx, gy = int(cx // stride), int(cy // stride)
        row = out[0, offset + gy * (in_w // stride) + gx]
        row[:4] = (cx / stride - gx, cy / stride - gy, np.log(w / stride), np.log(h / stride))
        row[4] = obj
        for class_id, score in scores.items():
            row[5 + class_id] = score
    return out


def _xyxy(dets: list[Detection]) -> np.ndarray:
    return np.array([[d.x1, d.y1, d.x2, d.y2] for d in dets])


# ---------------------------------------------------------------------------
# Letterbox and its inverse (B-D2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("in_size", "center", "expected"),
    [(416, False, (416, 260, 0, 0)), (640, True, (640, 400, 0, 120))],
)
def test_d_letterbox_fits_the_flight_frame(in_size, center, expected) -> None:
    """B-D2: a 1920 x 1200 frame keeps its 8:5 shape inside a square input."""
    lb = letterbox_fit(GRID_W, GRID_H, in_size, in_size, center=center)
    assert (lb.new_w, lb.new_h, lb.pad_x, lb.pad_y) == expected


@pytest.mark.parametrize("center", [False, True])
def test_d_letterbox_inverse_finds_a_rectangle_drawn_in_the_frame(center) -> None:
    """B-D2: a rectangle drawn in the frame, found again in the letterboxed
    image and mapped back with ``to_frame``, is the rectangle that was drawn.
    The padding is the pad value."""
    frame = _frame()
    drawn = (600, 300, 900, 500)  # frame edge coordinates x1, y1, x2, y2
    frame[drawn[1] : drawn[3], drawn[0] : drawn[2]] = 255
    lb = letterbox_fit(GRID_W, GRID_H, 640, 640, center=center)
    img = letterbox_image(frame, lb)
    assert img.shape == (640, 640, 3)
    pad_rows = np.r_[0 : lb.pad_y, lb.pad_y + lb.new_h : 640]
    assert pad_rows.size > 0 and np.all(img[pad_rows] == PAD_VALUE)
    ys, xs = np.nonzero(img[:, :, 0] > 250)  # 255 only where the rectangle fully covers
    found = lb.to_frame(np.array([[xs.min(), ys.min(), xs.max() + 1, ys.max() + 1]]))[0]
    assert np.allclose(found, drawn, atol=1.0 / lb.sx)  # within one model pixel


def test_d_letterbox_round_trip_is_exact() -> None:
    """B-D2: ``to_frame`` is the inverse of ``to_model`` for both padding sides."""
    rng = np.random.default_rng(7)
    boxes = rng.uniform(0, 1200, size=(20, 4))
    for center in (False, True):
        lb = letterbox_fit(GRID_W, GRID_H, 416, 416, center=center)
        assert np.allclose(lb.to_frame(lb.to_model(boxes)), boxes, atol=1e-9)


# ---------------------------------------------------------------------------
# Decoders, one per output layout, on synthetic tensors (B-D2)
# ---------------------------------------------------------------------------


def test_d_yolox_layout_decodes_boxes_placed_at_each_stride() -> None:
    """B-D2: YOLOX raw output -> the boxes that were placed, in frame pixels."""
    lb = letterbox_fit(GRID_W, GRID_H, 416, 416, center=False)
    placed = np.array([[100, 80, 160, 150], [800, 500, 1100, 760], [1300, 200, 1900, 1100]], float)
    model = lb.to_model(placed)
    tensor = _yolox_tensor(
        416,
        416,
        [
            (8, model[0], 0.9, {SPORTS_BALL: 0.8}),
            (16, model[1], 1.0, {KITE: 0.5}),
            (32, model[2], 0.8, {PERSON: 0.5}),
        ],
    )
    dets = postprocess(tensor, LAYOUTS["yolox"], lb, DetectConfig(conf_min=0.25))
    assert [d.class_id for d in dets] == [SPORTS_BALL, KITE, PERSON]  # best confidence first
    assert [d.conf for d in dets] == pytest.approx([0.72, 0.5, 0.4], abs=1e-6)  # obj * class
    assert np.allclose(_xyxy(dets), placed, atol=0.01)


def test_d_ultralytics_layout_decodes_placed_boxes() -> None:
    """B-D2: Ultralytics detect export ``(1, 4 + C, N)``, centered padding."""
    lb = letterbox_fit(GRID_W, GRID_H, 640, 640, center=True)
    placed = np.array([[200, 100, 400, 380], [1500, 900, 1800, 1150]], float)
    model = lb.to_model(placed)
    tensor = np.zeros((1, 84, anchor_count(640, 640)), dtype=np.float32)
    wanted = zip(model, (KITE, FRISBEE), (0.7, 0.6), strict=True)
    for col, (box, class_id, score) in enumerate(wanted):
        tensor[0, :4, 100 + col] = (
            (box[0] + box[2]) / 2,
            (box[1] + box[3]) / 2,
            box[2] - box[0],
            box[3] - box[1],
        )
        tensor[0, 4 + class_id, 100 + col] = score
    dets = postprocess(tensor, LAYOUTS["ultralytics"], lb, DetectConfig())
    assert [(d.class_id, round(d.conf, 6)) for d in dets] == [(KITE, 0.7), (FRISBEE, 0.6)]
    assert np.allclose(_xyxy(dets), placed, atol=0.01)


def test_d_ultralytics_e2e_layout_decodes_placed_boxes() -> None:
    """B-D2: Ultralytics end-to-end export ``(1, N, 6)``; zero rows are empty."""
    lb = letterbox_fit(GRID_W, GRID_H, 640, 640, center=True)
    placed = np.array([[200, 100, 400, 380], [1500, 900, 1800, 1150]], float)
    tensor = np.zeros((1, 300, 6), dtype=np.float32)
    tensor[0, 0] = (*lb.to_model(placed)[0], 0.8, SPORTS_BALL)
    tensor[0, 1] = (*lb.to_model(placed)[1], 0.6, PERSON)
    layout = LAYOUTS["ultralytics_e2e"]
    dets = postprocess(tensor, layout, lb, DetectConfig())
    assert [(d.class_id, round(d.conf, 6)) for d in dets] == [(SPORTS_BALL, 0.8), (PERSON, 0.6)]
    assert np.allclose(_xyxy(dets), placed, atol=0.01)
    only_ball = postprocess(tensor, layout, lb, DetectConfig(class_ids=(SPORTS_BALL,)))
    assert [d.class_id for d in only_ball] == [SPORTS_BALL]


@pytest.mark.parametrize(
    ("layout", "shape"),
    [("yolox", (1, 84, 8400)), ("ultralytics", (1, 8400, 85)), ("ultralytics_e2e", (1, 84, 8400))],
)
def test_d_a_tensor_of_another_layout_is_refused(layout, shape) -> None:
    """B-D2: a wrong ``--layout`` fails loudly, it never sends garbage boxes."""
    lb = letterbox_fit(GRID_W, GRID_H, 640, 640, center=True)
    with pytest.raises(LayoutError):
        postprocess(np.zeros(shape, dtype=np.float32), LAYOUTS[layout], lb, DetectConfig())


# ---------------------------------------------------------------------------
# NMS, thresholds, class filter (B-D2)
# ---------------------------------------------------------------------------


def test_d_nms_keeps_the_best_of_overlapping_boxes() -> None:
    """B-D2: greedy NMS, best score first; a far box is untouched."""
    boxes = np.array([[5, 5, 105, 105], [0, 0, 100, 100], [300, 300, 400, 400]], float)
    assert nms(boxes, np.array([0.8, 0.9, 0.7]), 0.45) == [1, 2]


def test_d_nms_threshold_discriminates() -> None:
    """B-D2, doctrine rule 4: two boxes with IoU 1/3 merge below it, not above."""
    boxes = np.array([[0, 0, 100, 100], [50, 0, 150, 100]], float)  # overlap 50 of union 150
    scores = np.array([0.9, 0.8])
    assert nms(boxes, scores, 0.30) == [0]
    assert nms(boxes, scores, 0.40) == [0, 1]


def test_d_nms_runs_only_where_the_layout_needs_it() -> None:
    """B-D2: two candidates on one object are one box for ``yolox``; the
    end-to-end layout already did its NMS in the graph and is left alone."""
    lb = letterbox_fit(GRID_W, GRID_H, 416, 416, center=False)
    a, b = (100.0, 130.0, 180.0, 210.0), (104.0, 132.0, 184.0, 212.0)
    yolox = _yolox_tensor(
        416, 416, [(8, a, 1.0, {SPORTS_BALL: 0.9}), (16, b, 1.0, {SPORTS_BALL: 0.6})]
    )
    dets = postprocess(yolox, LAYOUTS["yolox"], lb, DetectConfig())
    assert [round(d.conf, 6) for d in dets] == [0.9]
    e2e = np.zeros((1, 300, 6), dtype=np.float32)
    e2e[0, 0] = (*a, 0.9, SPORTS_BALL)
    e2e[0, 1] = (*b, 0.6, SPORTS_BALL)
    lbc = letterbox_fit(GRID_W, GRID_H, 640, 640, center=True)
    assert len(postprocess(e2e, LAYOUTS["ultralytics_e2e"], lbc, DetectConfig())) == 2


def test_d_class_filter_keeps_the_best_allowed_class() -> None:
    """B-D2: only allowed classes become boxes. A box whose top class is not
    allowed is kept under its best allowed class when that one passes."""
    lb = letterbox_fit(GRID_W, GRID_H, 416, 416, center=False)
    tensor = _yolox_tensor(
        416,
        416,
        [
            (8, (20, 20, 60, 60), 1.0, {SPORTS_BALL: 0.9}),
            (8, (200, 20, 240, 60), 1.0, {PERSON: 0.8}),
            (8, (300, 150, 340, 190), 1.0, {APPLE: 0.7, KITE: 0.6}),
        ],
    )
    layout = LAYOUTS["yolox"]
    allowed = parse_classes(DEFAULT_CLASSES)
    filtered = postprocess(tensor, layout, lb, DetectConfig(class_ids=allowed))
    assert [(d.class_id, round(d.conf, 6)) for d in filtered] == [(SPORTS_BALL, 0.9), (KITE, 0.6)]
    everything = postprocess(tensor, layout, lb, DetectConfig(class_ids=None))
    assert [d.class_id for d in everything] == [SPORTS_BALL, PERSON, APPLE]
    with pytest.raises(LayoutError):  # a class id the model does not have
        postprocess(tensor, layout, lb, DetectConfig(class_ids=(80,)))


def test_d_confidence_threshold_discriminates() -> None:
    """B-D2, doctrine rule 4: a 0.30 box passes a 0.29 threshold, not 0.31."""
    lb = letterbox_fit(GRID_W, GRID_H, 416, 416, center=False)
    tensor = _yolox_tensor(416, 416, [(8, (20, 20, 60, 60), 0.5, {SPORTS_BALL: 0.6})])
    layout = LAYOUTS["yolox"]
    assert len(postprocess(tensor, layout, lb, DetectConfig(conf_min=0.29))) == 1
    assert postprocess(tensor, layout, lb, DetectConfig(conf_min=0.31)) == []


def test_d_class_list_parsing() -> None:
    """B-D2: class names or ids; the documented default; ``all`` = no filter."""
    assert parse_classes(DEFAULT_CLASSES) == (FRISBEE, SPORTS_BALL, KITE)
    assert parse_classes("kite, 0,16") == (0, 16, KITE)
    assert parse_classes("all") is None
    for bad in ("balloon", "", "kite,-1"):
        with pytest.raises(ValueError):
            parse_classes(bad)


# ---------------------------------------------------------------------------
# Flight grid [C2] and packet building [P1]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("box", "size", "expected"),
    [
        # The whole image is the whole grid: [-0.5, 1919.5] x [-0.5, 1199.5].
        ((0, 0, 1920, 1200), (1920, 1200), (-0.5, -0.5, 1920.0, 1200.0)),
        ((100, 200, 300, 500), (1920, 1200), (99.5, 199.5, 200.0, 300.0)),
        # A half-size source: every length doubles.
        ((50, 100, 150, 250), (960, 600), (99.5, 199.5, 200.0, 300.0)),
        # Another aspect ratio: each axis has its own scale (3 and 2.5).
        ((100, 100, 200, 200), (640, 480), (299.5, 249.5, 300.0, 250.0)),
        # Clipped to the image.
        ((-50, -20, 100, 100), (1920, 1200), (-0.5, -0.5, 100.0, 100.0)),
        ((1900, 1150, 2000, 1300), (1920, 1200), (1899.5, 1149.5, 20.0, 50.0)),
    ],
)
def test_d_frame_pixels_map_to_the_flight_grid(box, size, expected) -> None:
    """[C2]: boxes land on the 1920 x 1200 grid whose (0, 0) is the center of
    the top-left pixel; a source of another size is scaled to it."""
    assert to_flight_grid(*box, *size) == pytest.approx(expected)


def test_d_packet_matches_the_golden_fixture() -> None:
    """[P1], [C5]: detections at the golden boxes' frame positions encode,
    through the frozen encoder, to the golden detection packet byte for byte."""
    dets = [_det(12.5, 1100.5, 19.0, 1106.5, conf=0.33), _det(940.75, 581.0, 980.75, 619.5, 0.91)]
    pkt = build_detection_packet(dets, t_cap=123456, frame_seq=7, frame_w=GRID_W, frame_h=GRID_H)
    assert P.encode(pkt) == (GOLDEN / "detection.json").read_bytes().strip()
    empty = build_detection_packet([], t_cap=123473, frame_seq=8, frame_w=GRID_W, frame_h=GRID_H)
    assert P.encode(empty) == (GOLDEN / "detection_empty.json").read_bytes().strip()


def test_d_packet_boxes_are_valid_clipped_and_capped() -> None:
    """[P1]: every box passes the frozen decoder (``w, h > 0``, conf in
    [0, 1]) and lies inside the image; unusable boxes are left out; at most
    ``max_boxes`` remain, the most confident ones."""
    dets = [
        _det(-300, -300, 5000, 5000, conf=1.2),  # larger than the image, conf above 1
        _det(float("nan"), 0, 10, 10),  # not a number
        _det(2500, 100, 2600, 200),  # outside the image
        _det(1919.8, 100, 2100, 200),  # a 0.2 px sliver after clipping
        _det(10, 10, 10, 50),  # no width
    ]
    dets += [_det(100 + 10 * i, 100, 150 + 10 * i, 180, conf=0.30 + 0.01 * i) for i in range(30)]
    pkt = build_detection_packet(
        dets, t_cap=1, frame_seq=2, frame_w=GRID_W, frame_h=GRID_H, max_boxes=20
    )
    decoded = P.decode(PacketKind.DETECTION, P.encode(pkt))
    assert decoded == pkt and len(decoded.boxes) == 20
    assert type(decoded.t_cap) is int and type(decoded.frame_seq) is int
    confs = [b.conf for b in decoded.boxes]
    assert confs == sorted(confs, reverse=True)
    assert confs[0] == 1.0 and confs[-1] == pytest.approx(0.41)  # 1.0, then the best 19 of 30
    for b in decoded.boxes:
        assert all(type(val) is float for val in (b.x, b.y, b.w, b.h, b.conf))
        assert b.w > 0 and b.h > 0 and 0.0 <= b.conf <= 1.0
        assert -0.5 <= b.x and b.x + b.w <= GRID_W - 0.5 + 1e-9
        assert -0.5 <= b.y and b.y + b.h <= GRID_H - 0.5 + 1e-9


# ---------------------------------------------------------------------------
# The loop: keep-latest, stamps, empty frames ([P1], [C1])
# ---------------------------------------------------------------------------


def test_d_keep_latest_drops_show_as_frame_seq_gaps() -> None:
    """[P1], [C1]: ``frame_seq`` counts grabbed frames. When the detector is
    slower than the camera only the newest frame is processed, the gap shows
    in ``frame_seq``, and ``t_cap`` is the clock at that frame's grab, not at
    its send."""
    clock = FakeClock(t=5000, step=12)  # every clock read is 12 ms later
    rx, tx = _link()
    source = ScriptSource(clock, [_frame(64, 40) for _ in range(6)])
    p, _ = _percepd(source, NullBackend(), tx, clock)
    grab_stamps: list[int] = []
    for grabs in (3, 1, 2):
        for _ in range(grabs):
            assert p.grab_step()
            grab_stamps.append(clock.t)
        clock.t += 500  # a slow detector: the stamp must not move
        assert p.process_step() is not None
    got = _received(rx)
    assert [pkt.frame_seq for pkt in got] == [2, 3, 5]
    assert [pkt.t_cap for pkt in got] == [grab_stamps[2], grab_stamps[3], grab_stamps[5]]
    assert (p.grabbed, p.sent, p.dropped) == (6, 3, 3)
    assert p.process_step() is None  # nothing queued behind the newest frame
    rx.close()
    tx.close()


def test_d_t_cap_never_decreases_and_frame_seq_increases() -> None:
    """[C1], [P1]: over a run, ``t_cap`` is monotonic non-decreasing (also when
    two grabs fall in the same millisecond) and ``frame_seq`` strictly rises."""
    clock = FakeClock(t=100)
    rx, tx = _link()
    source = ScriptSource(clock, [_frame(64, 40) for _ in range(5)])
    p, _ = _percepd(source, NullBackend(), tx, clock)
    for advance in (0, 0, 7, 13, 0):  # ms between grabs
        clock.t += advance
        assert p.grab_step()
        p.process_step()
    got = _received(rx)
    assert [pkt.t_cap for pkt in got] == [100, 100, 107, 120, 120]
    assert [pkt.frame_seq for pkt in got] == [0, 1, 2, 3, 4]
    rx.close()
    tx.close()


def test_d_a_frame_with_no_boxes_still_sends_a_packet() -> None:
    """[P1]: one packet per processed frame, including frames with no boxes."""
    clock = FakeClock()
    rx, tx = _link()
    source = ScriptSource(clock, [_frame(), _frame()])
    backend = ScriptBackend([], [_det(100, 200, 300, 500, conf=0.5)])
    p, _ = _percepd(source, backend, tx, clock)
    while p.grab_step():
        p.process_step()
    empty, one = _received(rx)
    assert empty.boxes == () and empty.frame_seq == 0
    assert one.boxes == (P.Box(x=99.5, y=199.5, w=200.0, h=300.0, conf=0.5),)
    rx.close()
    tx.close()


# ---------------------------------------------------------------------------
# Robustness: source, detector and send failures (B-D2)
# ---------------------------------------------------------------------------


def test_d_source_failures_are_survived_with_backoff() -> None:
    """B-D2: a camera that is not there, a failed grab and an unexpected
    error from the capture library are counted and retried; the source is
    reopened; ``frame_seq`` goes on."""
    clock = FakeClock(step=10)
    rx, tx = _link()
    script = [_frame(64, 40), SourceError("grab failed"), _frame(64, 40), RuntimeError("cv2")]
    source = ScriptSource(clock, [*script, _frame(64, 40)], open_errors=2)
    p, waits = _percepd(source, NullBackend(), tx, clock)
    while p.grab_step():
        p.process_step()
    assert [pkt.frame_seq for pkt in _received(rx)] == [0, 1, 2]
    assert (p.grab_failures, p.opens, source.open_calls) == (4, 3, 5)
    assert source.close_calls == 4  # closed after every failure
    # Two failed opens back off 0.5 s then 1 s; a good frame resets the wait.
    assert waits == [0.5, 1.0, 0.5, 0.5]
    rx.close()
    tx.close()


def test_d_backoff_doubles_up_to_its_cap() -> None:
    """B-D2: a camera that stays unplugged is retried at a bounded rate."""
    clock = FakeClock()
    rx, tx = _link()
    p, waits = _percepd(ScriptSource(clock, [], open_errors=6), NullBackend(), tx, clock)
    for _ in range(6):
        assert p.grab_step()
    assert waits == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]
    rx.close()
    tx.close()


def test_d_a_missing_camera_device_is_a_source_error() -> None:
    """B-D2: the real camera source reports a device that is not present as
    the retryable error, and the loop survives it."""
    clock = FakeClock()
    camera = CameraSource("/dev/video-skyweave-none", clock=clock)
    with pytest.raises(SourceError):
        camera.open()
    rx, tx = _link()
    p, waits = _percepd(camera, NullBackend(), tx, clock)
    assert p.grab_step() and p.grab_failures == 1 and waits == [0.5]
    rx.close()
    tx.close()


def test_d_send_failure_does_not_stop_the_loop() -> None:
    """B-D2: a socket that cannot send is counted; frames keep being processed."""
    clock = FakeClock(step=10)
    rx, tx = _link()
    tx.close()  # sendto on a closed socket fails with OSError
    source = ScriptSource(clock, [_frame(64, 40) for _ in range(3)])
    p, _ = _percepd(source, NullBackend(), tx, clock)
    while p.grab_step():
        assert p.process_step() is not None
    assert (p.processed, p.sent, p.send_errors) == (3, 0, 3)
    assert _received(rx, timeout_s=0.0) == []
    rx.close()


def test_d_detector_error_skips_the_frame_only() -> None:
    """B-D2, [P1]: a frame the detector failed on sends nothing (an empty
    packet would claim "no target"); it shows as a ``frame_seq`` gap."""
    clock = FakeClock(step=10)
    rx, tx = _link()
    backend = ScriptBackend([], RuntimeError("inference failed"), [])
    p, _ = _percepd(ScriptSource(clock, [_frame(64, 40) for _ in range(3)]), backend, tx, clock)
    while p.grab_step():
        p.process_step()
    assert [pkt.frame_seq for pkt in _received(rx)] == [0, 2]
    assert (p.detect_errors, p.sent) == (1, 2)
    rx.close()
    tx.close()


# ---------------------------------------------------------------------------
# File sources, the threaded run, SIGTERM (B-D2)
# ---------------------------------------------------------------------------


def _write_images(folder: Path, n: int) -> None:
    for i in range(n):
        assert cv2.imwrite(str(folder / f"img_{i:02d}.png"), np.full((40, 64, 3), 10 * i, np.uint8))


def test_d_image_source_reads_a_folder_in_order(tmp_path) -> None:
    """B-D2, [C1]: images in name order, stamped by the injected clock, paced
    by the injected sleep; a file that does not decode is skipped; the end is
    ``EndOfSource``; ``loop`` starts over."""
    _write_images(tmp_path, 3)
    (tmp_path / "img_01b.jpg").write_bytes(b"not a picture")
    clock = FakeClock(t=1000)
    sleeps: list[float] = []
    src = ImageSource(tmp_path, fps=10, clock=clock, sleep=sleeps.append)
    src.open()
    frames = [src.read() for _ in range(3)]
    assert [int(f.image[0, 0, 0]) for f in frames] == [0, 10, 20]
    assert all(f.t_cap == 1000 and f.image.shape == (40, 64, 3) for f in frames)
    assert sleeps == pytest.approx([0.1, 0.2, 0.3])  # the clock stood still: 10 fps pacing
    with pytest.raises(EndOfSource):
        src.read()
    loop = ImageSource(tmp_path, fps=0, loop=True, clock=clock)
    loop.open()
    assert [int(loop.read().image[0, 0, 0]) for _ in range(5)] == [0, 10, 20, 0, 10]
    with pytest.raises(SourceError):
        ImageSource(tmp_path / "none", clock=clock).open()


def test_d_video_source_reads_a_file(tmp_path) -> None:
    """B-D2: a video file gives its frames, then ``EndOfSource``."""
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (64, 48))
    assert writer.isOpened()
    for i in range(4):
        writer.write(np.full((48, 64, 3), 50 * i, np.uint8))
    writer.release()
    src = VideoSource(path, fps=0, clock=FakeClock(t=77))
    src.open()
    frames = [src.read() for _ in range(4)]
    assert all(f.image.shape == (48, 64, 3) and f.t_cap == 77 for f in frames)
    assert [round(float(f.image.mean()) / 50) for f in frames] == [0, 1, 2, 3]
    with pytest.raises(EndOfSource):
        src.read()
    src.close()


def test_d_every_frame_run_processes_each_image(tmp_path) -> None:
    """B-D2: ``every_frame`` (file sources) drops nothing: one packet per image."""
    _write_images(tmp_path, 4)
    clock = board_clock()
    rx, tx = _link()
    source = ImageSource(tmp_path, fps=0, clock=clock)
    p = Percepd(
        source, NullBackend(), tx, clock, PipelineConfig(every_frame=True, stats_interval_ms=0)
    )
    p.run(threading.Event())
    assert [pkt.frame_seq for pkt in _received(rx)] == [0, 1, 2, 3]
    assert p.dropped == 0
    rx.close()
    tx.close()


def test_d_threaded_run_ends_with_the_file_source(tmp_path) -> None:
    """B-D2, [P1]: the keep-latest run (grab thread plus detector thread)
    ends cleanly when a file source ends. Every grabbed frame is either sent
    or counted as dropped, and the newest frame is never the one dropped."""
    _write_images(tmp_path, 5)
    clock = board_clock()
    rx, tx = _link()
    p = Percepd(
        ImageSource(tmp_path, fps=500, clock=clock),
        NullBackend(),
        tx,
        clock,
        PipelineConfig(stats_interval_ms=0),
    )
    p.run(threading.Event())
    got = _received(rx)
    seqs = [pkt.frame_seq for pkt in got]
    assert seqs == sorted(set(seqs)) and seqs[-1] == 4
    assert p.grabbed == 5 and p.sent + p.dropped == 5 and len(got) == p.sent
    t_caps = [pkt.t_cap for pkt in got]
    assert t_caps == sorted(t_caps)
    rx.close()
    tx.close()


def test_d_sigterm_ends_the_process_cleanly(tmp_path) -> None:
    """B-D2: ``python -m skyweave2.drone.percepd`` exits 0 on SIGTERM. The
    signal is sent once the first packet has arrived, so the handler is in
    place; the command line is the documented bench one (``--backend null``)."""
    _write_images(tmp_path, 2)
    rx = UdpReceiver(PacketKind.DETECTION, port=0)
    before = signal.getsignal(signal.SIGTERM)

    def kill_after_first_packet() -> None:
        select.select([rx.fileno()], [], [], 10.0)  # a packet: the loop and its handler are up
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=kill_after_first_packet, daemon=True)
    killer.start()
    argv = ["--images", str(tmp_path), "--loop", "--fps", "200", "--backend", "null"]
    rc = cli.main([*argv, "--dest", f"127.0.0.1:{rx.address[1]}", "--stats-s", "0"])
    killer.join(timeout=5.0)
    assert rc == 0
    assert signal.getsignal(signal.SIGTERM) is before  # the test process's handler is back
    assert len(_received(rx, timeout_s=0.0)) >= 1
    rx.close()


def test_d_command_line_refusals(capsys) -> None:
    """B-D2: bad command lines stop before anything opens."""
    assert cli.parse_dest("127.0.0.1:14601") == ("127.0.0.1", 14601)
    assert cli.build_parser().parse_args(["--camera", "/dev/video0"]).dest == (
        "127.0.0.1",
        14601,  # contract §9 detection port, the default
    )
    with pytest.raises(SystemExit):
        cli.main(["--camera", "/dev/video0", "--every-frame", "--backend", "null"])
    with pytest.raises(SystemExit):
        cli.main(["--images", "x", "--backend", "null", "--dest", "nowhere"])
    assert cli.main(["--images", "x"]) == 2  # the onnx backend needs --model
    assert cli.main(["--images", "x", "--backend", "null", "--classes", "balloon"]) == 2
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Bench viewer and stats (B-D2)
# ---------------------------------------------------------------------------


def test_d_viewer_writes_the_latest_frame_only_when_asked(tmp_path) -> None:
    """B-D2: with a view path, every N-th processed frame replaces one JPEG;
    without one, nothing is written (no frame recording)."""
    clock = FakeClock(step=10)
    rx, tx = _link()
    view = tmp_path / "view" / "latest.jpg"
    view.parent.mkdir()
    backend = ScriptBackend([_det(400, 300, 800, 700)])
    frames = [_frame() for _ in range(3)]
    p, _ = _percepd(ScriptSource(clock, frames), backend, tx, clock, view_path=view, view_every=2)
    written = []
    picture = None
    while p.grab_step():
        p.process_step()
        written.append(view.exists())
        if view.exists():
            picture = cv2.imread(str(view))
            view.unlink()
    assert written == [True, False, True]
    assert list(view.parent.iterdir()) == []  # no temporary file left either
    assert picture is not None and picture.shape == (600, 960, 3)  # the default 960 px width
    assert picture[:, :, 1].max() > 200  # the green box is drawn

    off, _ = _percepd(ScriptSource(clock, [_frame()]), backend, tx, clock)
    assert off.config.view_path is None  # the default
    off.grab_step()
    assert off.process_step() is not None
    assert list(tmp_path.rglob("*.jpg")) == []
    rx.close()
    tx.close()


def test_d_stats_line_reports_rates_and_counters(caplog) -> None:
    """B-D2: one periodic log line with fps in, fps out, inference ms, drops."""
    clock = FakeClock(t=0)
    ticks = iter(range(0, 1000, 1))
    rx, tx = _link()
    source = ScriptSource(clock, [_frame(64, 40) for _ in range(6)])
    cfg = PipelineConfig(stats_interval_ms=2000)
    # The timer stand-in advances 0.02 s per read: each inference "takes" 20 ms.
    p = Percepd(source, NullBackend(), tx, clock, cfg, timer=lambda: next(ticks) * 0.02)
    with caplog.at_level(logging.INFO, logger="skyweave2.drone.percepd.pipeline"):
        for _ in range(3):
            p.grab_step()
            p.grab_step()
            p.process_step()
        assert caplog.records == []  # the interval has not passed
        clock.t = 2000
        p.process_step()
    (record,) = caplog.records
    line = record.getMessage()
    assert "\n" not in line
    for part in ("fps_in=3.0", "fps_out=1.5", "infer_ms=20.0", "drops=3", "grab_fail=0"):
        assert part in line, line
    rx.close()
    tx.close()


# ---------------------------------------------------------------------------
# The chain: percepd -> UDP -> companion core with its tracker ([C10], E2-D5)
# ---------------------------------------------------------------------------


def test_d_companion_core_tracks_percepd_packets() -> None:
    """[P1], [C10], E2-D5: packets from percepd, received on the companion's
    detection receiver, are accepted by the real companion core; its tracker
    confirms a track at the box center on the flight grid ([C2]). The FC's
    ATTITUDE (level, not turning) reaches the core with each frame: the
    tracker needs the attitude at ``t_cap`` (V-D10)."""
    clock = FakeClock(t=10_000, step=13)
    rx, tx = _link()
    core = CompanionCore(CoreConfig().with_tracker(clock.t))
    det = _det(900.5, 500.5, 1000.5, 580.5, conf=0.8)  # frame pixels; center (950.5, 540.5)
    n = core.config.tracker.m_confirm + 1
    source = ScriptSource(clock, [_frame() for _ in range(n)])
    p, _ = _percepd(source, ScriptBackend([det]), tx, clock)
    states = []
    while p.grab_step():
        p.process_step()
        (pkt,) = _received(rx)
        core.on_mavlink_rx(attitude_frame(pkt.t_cap), pkt.t_cap)
        out = core.on_detection(pkt, clock())
        (track,) = out.tracks
        states.append(track.state)
    assert (core.tracker.stale_dropped, core.tracker.degraded_dropped) == (0, 0)
    assert states[-1] is TrackState.CONFIRMED and states[0] is TrackState.TENTATIVE
    assert (track.u, track.v_px) == pytest.approx((950.0, 540.0), abs=0.5)
    assert (track.w, track.h) == pytest.approx((100.0, 80.0), abs=0.5)
    rx.close()
    tx.close()


# ---------------------------------------------------------------------------
# The optional extra, and the optional real model
# ---------------------------------------------------------------------------


def test_d_onnxruntime_is_an_optional_extra(monkeypatch, tmp_path) -> None:
    """B-D2: this module imported every percepd module without onnxruntime in
    the fast tier; asking for the ONNX backend without it is one clear error."""
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # makes the import fail
    with pytest.raises(BackendError, match="onnxruntime"):
        OnnxBackend(tmp_path / "model.onnx", LAYOUTS["yolox"], DetectConfig())


def test_d_real_onnx_model_end_to_end() -> None:
    """B-D2 (optional): a real model file through the real ONNX backend.

    Set ``SKYWEAVE_PERCEPD_MODEL`` to a model file. Optional:
    ``SKYWEAVE_PERCEPD_LAYOUT`` (default ``yolox``), ``SKYWEAVE_PERCEPD_IMAGE``
    (an image with at least one object of ``SKYWEAVE_PERCEPD_CLASSES``,
    default ``all``).
    """
    model = os.environ.get("SKYWEAVE_PERCEPD_MODEL")
    if not model:
        pytest.skip("SKYWEAVE_PERCEPD_MODEL is not set (see percepd/README.md)")
    pytest.importorskip("onnxruntime")
    layout = LAYOUTS[os.environ.get("SKYWEAVE_PERCEPD_LAYOUT", "yolox")]
    classes = parse_classes(os.environ.get("SKYWEAVE_PERCEPD_CLASSES", "all"))
    backend = OnnxBackend(model, layout, DetectConfig(class_ids=classes))
    image_path = os.environ.get("SKYWEAVE_PERCEPD_IMAGE")
    image = cv2.imread(image_path) if image_path else np.full((GRID_H, GRID_W, 3), 114, np.uint8)
    assert image is not None, f"image {image_path} does not decode"
    dets = backend.detect(image)
    h, w = image.shape[:2]
    for d in dets:
        assert 0 <= d.x1 < d.x2 <= w and 0 <= d.y1 < d.y2 <= h and 0.25 <= d.conf <= 1.0
        assert classes is None or d.class_id in classes
    if image_path:
        assert dets, "the model found nothing in the test image"
    pkt = build_detection_packet(dets, t_cap=1, frame_seq=0, frame_w=w, frame_h=h)
    assert P.decode(PacketKind.DETECTION, P.encode(pkt)) == pkt
    assert len(pkt.boxes) == min(len(dets), 20)
