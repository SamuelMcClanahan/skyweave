"""Command line of percepd v1 (B-D2). See README.md in this folder.

    python -m skyweave2.drone.percepd --camera /dev/video0 --model yolox_tiny.onnx
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from pathlib import Path

from skyweave2.drone.packets import PacketKind
from skyweave2.drone.percepd.detect import (
    DEFAULT_CLASSES,
    Backend,
    BackendError,
    DetectConfig,
    NullBackend,
    parse_classes,
)
from skyweave2.drone.percepd.layouts import LAYOUTS
from skyweave2.drone.percepd.packet import DEFAULT_MAX_BOXES
from skyweave2.drone.percepd.pipeline import Percepd, PipelineConfig
from skyweave2.drone.percepd.sources import (
    CameraSource,
    FrameSource,
    ImageSource,
    VideoSource,
    board_clock,
)
from skyweave2.drone.percepd.viewer import DEFAULT_VIEW_WIDTH
from skyweave2.drone.types import Clock
from skyweave2.drone.udp import DEFAULT_PORTS, LOOPBACK, UdpSender

log = logging.getLogger("skyweave2.drone.percepd")

DEFAULT_DEST = f"{LOOPBACK}:{DEFAULT_PORTS[PacketKind.DETECTION]}"
CAMERA_FPS = 80.0  # the flight mode, brief §2
FILE_FPS = 10.0


def parse_dest(text: str) -> tuple[str, int]:
    """``host:port`` -> ``(host, port)``."""
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise argparse.ArgumentTypeError(f"expected host:port, got {text!r}")
    return (host, int(port))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m skyweave2.drone.percepd",
        description="SkyWeave percepd v1: camera -> detector -> [P1] detection packets (UDP).",
    )
    src = p.add_argument_group("frame source (choose one)")
    one = src.add_mutually_exclusive_group(required=True)
    one.add_argument("--camera", metavar="DEV", help="V4L2 device, for example /dev/video0")
    one.add_argument("--video", metavar="FILE", type=Path, help="video file (bench)")
    one.add_argument("--images", metavar="PATH", type=Path, help="image file or folder (bench)")
    src.add_argument("--width", type=int, default=1920, help="camera width (default 1920)")
    src.add_argument("--height", type=int, default=1200, help="camera height (default 1200)")
    src.add_argument("--pixfmt", default="YUYV", help="camera pixel format (default YUYV)")
    src.add_argument(
        "--fps",
        type=float,
        default=None,
        help=f"camera rate (default {CAMERA_FPS:g}); file sources are paced at this rate "
        f"(default {FILE_FPS:g}, 0 = no pacing)",
    )
    src.add_argument("--loop", action="store_true", help="file sources: start over at the end")
    src.add_argument(
        "--every-frame",
        action="store_true",
        help="file sources: process every frame in one thread, drop nothing",
    )

    det = p.add_argument_group("detector")
    det.add_argument("--backend", choices=("onnx", "null"), default="onnx")
    det.add_argument("--model", type=Path, help="ONNX model file (backend onnx)")
    det.add_argument("--layout", choices=sorted(LAYOUTS), default="yolox")
    det.add_argument("--threads", type=int, default=0, help="CPU threads (0 = automatic)")
    det.add_argument("--conf", type=float, default=0.25, help="confidence threshold")
    det.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold")
    det.add_argument(
        "--classes",
        default=DEFAULT_CLASSES,
        help=f"allowed COCO class names or class ids, comma separated, or 'all' "
        f"(default {DEFAULT_CLASSES!r})",
    )
    det.add_argument("--max-boxes", type=int, default=DEFAULT_MAX_BOXES, help="boxes per packet")

    out = p.add_argument_group("output")
    out.add_argument(
        "--dest",
        type=parse_dest,
        default=parse_dest(DEFAULT_DEST),
        metavar="HOST:PORT",
        help=f"the companion's detection port (default {DEFAULT_DEST})",
    )
    out.add_argument("--stats-s", type=float, default=5.0, help="stats log period (0 = off)")
    out.add_argument(
        "--view",
        type=Path,
        metavar="JPEG",
        help="bench only: write the latest annotated frame to this file (off by default)",
    )
    out.add_argument("--view-every", type=int, default=10, help="viewer: every N processed frames")
    out.add_argument("--view-width", type=int, default=DEFAULT_VIEW_WIDTH)
    out.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    return p


def make_source(args: argparse.Namespace, clock: Clock) -> FrameSource:
    if args.camera is not None:
        return CameraSource(
            args.camera,
            width=args.width,
            height=args.height,
            fps=CAMERA_FPS if args.fps is None else args.fps,
            pixfmt=args.pixfmt,
            clock=clock,
        )
    fps = FILE_FPS if args.fps is None else args.fps
    if args.video is not None:
        return VideoSource(args.video, fps=fps, loop=args.loop, clock=clock)
    return ImageSource(args.images, fps=fps, loop=args.loop, clock=clock)


def make_backend(args: argparse.Namespace) -> Backend:
    config = DetectConfig(
        conf_min=args.conf, iou_max=args.iou, class_ids=parse_classes(args.classes)
    )
    if args.backend == "null":
        return NullBackend()
    if args.model is None:
        raise BackendError("--backend onnx needs --model FILE (see percepd/README.md)")
    from skyweave2.drone.percepd.onnx_backend import OnnxBackend  # lazy: optional extra

    return OnnxBackend(args.model, LAYOUTS[args.layout], config, threads=args.threads)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.every_frame and args.camera is not None:
        parser.error("--every-frame is for file sources; a camera always runs keep-latest")
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    clock = board_clock()
    try:
        config = PipelineConfig(
            max_boxes=args.max_boxes,
            every_frame=args.every_frame,
            stats_interval_ms=round(args.stats_s * 1000),
            view_path=args.view,
            view_every=args.view_every,
            view_width=args.view_width,
        )
        source = make_source(args, clock)
        backend = make_backend(args)
    except (BackendError, ValueError) as exc:
        log.error("percepd not started: %s", exc)
        return 2
    sender = UdpSender(*args.dest)
    stop = threading.Event()
    previous = {
        sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    log.info(
        "percepd v1: backend %s, classes %s, dest %s:%d%s",
        args.backend,
        args.classes,
        *args.dest,
        f", viewer {args.view}" if args.view else "",
    )
    try:
        Percepd(source, backend, sender, clock, config).run(stop)
    finally:
        sender.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
