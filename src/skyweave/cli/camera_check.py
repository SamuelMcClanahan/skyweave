from __future__ import annotations

import argparse
import sys

from skyweave.camera.check_common import (
    MotionCameraState,
    _open_jsonl,
    _percentile,
    _write_pgm_snapshot,
)
from skyweave.camera.check_parallel import _process_motion_source
from skyweave.camera.check_runtime import _run_live, _run_synthetic
from skyweave.camera.motion import MotionPacketConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a headless camera packet-generation smoke check.")
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--fps", type=float, default=100.0)
    parser.add_argument("--square-size", type=int, default=18)
    parser.add_argument("--threshold", type=int, default=32)
    parser.add_argument("--max-patch-side", type=int, default=64)
    parser.add_argument("--max-motion-pixels", type=int, default=225)
    parser.add_argument("--console-every", type=int, default=1)
    parser.add_argument("--jsonl", default=None, help="Optional JSONL output path.")
    parser.add_argument("--device", default=None, help="Single live camera device, e.g. /dev/video0 or 0.")
    parser.add_argument("--devices", default=None, help="Comma-separated live camera devices.")
    parser.add_argument("--fourcc", default=None, help="Best-effort requested fourcc, e.g. MJPG or YUYV.")
    parser.add_argument("--snapshot-dir", default=None, help="Optional live-mode directory for first-frame PGM snapshots.")
    parser.add_argument("--warmup-frames", type=int, default=5, help="Live frames to drop before packet stats.")
    parser.add_argument("--profile-stages", action="store_true", help="Print live capture/decode/build stage timing.")
    parser.add_argument("--parallel-cameras", action="store_true", help="Process live cameras concurrently.")
    parser.add_argument(
        "--motion-backend",
        choices=("python", "opencv", "opencv_contours"),
        default="python",
        help="Motion packet backend for frame diff and connected components.",
    )
    parser.add_argument(
        "--live-pipeline",
        choices=("motion-packet", "jpeg-frame"),
        default="motion-packet",
        help="Live mode pipeline: motion packets or whole-frame JPEG benchmark.",
    )
    parser.add_argument("--jpeg-quality", type=int, default=80, help="JPEG quality for --live-pipeline jpeg-frame.")
    args = parser.parse_args(argv)
    if args.device and args.devices:
        parser.error("use either --device or --devices, not both")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc must be exactly 4 characters")
    if not 1 <= args.jpeg_quality <= 100:
        parser.error("--jpeg-quality must be between 1 and 100")

    config = MotionPacketConfig(
        threshold=args.threshold,
        max_patch_side_px=args.max_patch_side,
        max_motion_pixels=args.max_motion_pixels,
        backend=args.motion_backend,
    )
    writer = _open_jsonl(args.jsonl)
    try:
        if args.device or args.devices:
            devices = [args.device] if args.device else [item.strip() for item in args.devices.split(",") if item.strip()]
            return _run_live(args, config, devices, writer)
        return _run_synthetic(args, config, writer)
    finally:
        if writer:
            writer.close()


if __name__ == "__main__":
    sys.exit(main())
