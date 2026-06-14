from __future__ import annotations

import argparse

from skyweave.calibration.charuco import (
    DEFAULT_CHARUCO_DICTIONARY,
    DEFAULT_CHARUCO_MARKER_MM,
    DEFAULT_CHARUCO_SQUARES_X,
    DEFAULT_CHARUCO_SQUARES_Y,
    DEFAULT_CHARUCO_SQUARE_MM,
)
from skyweave.calibration.charuco_capture import (
    _capture_targets,
    run_capture,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture ChArUco observations for camera calibration.")
    parser.add_argument("--camera-config", default=None, help="YAML file from skyweave-camera-inventory.")
    parser.add_argument("--devices", default=None, help="Comma-separated capture nodes, e.g. /dev/video0,/dev/video2.")
    parser.add_argument("--labels", default=None, help="Comma-separated physical labels matching --devices.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--warmup-frames", type=int, default=10)
    parser.add_argument("--sample-every", type=int, default=5)
    parser.add_argument("--target-accepted", type=int, default=0, help="Stop early after this many accepted samples.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--squares-x", type=int, default=DEFAULT_CHARUCO_SQUARES_X)
    parser.add_argument("--squares-y", type=int, default=DEFAULT_CHARUCO_SQUARES_Y)
    parser.add_argument("--square-mm", type=float, default=DEFAULT_CHARUCO_SQUARE_MM)
    parser.add_argument("--marker-mm", type=float, default=DEFAULT_CHARUCO_MARKER_MM)
    parser.add_argument("--dictionary", default=DEFAULT_CHARUCO_DICTIONARY)
    parser.add_argument("--min-corners", type=int, default=24)
    parser.add_argument("--save-images", action="store_true")
    parser.add_argument("--preview", action="store_true", help="Serve a live annotated preview from the capture process.")
    parser.add_argument("--preview-host", default="0.0.0.0")
    parser.add_argument("--preview-port", type=int, default=8090)
    parser.add_argument("--preview-every", type=int, default=1, help="Publish every N frames to the preview.")
    parser.add_argument("--preview-jpeg-quality", type=int, default=85)
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    return run_capture(args)


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.sample_every <= 0:
        parser.error("--sample-every must be positive")
    if args.target_accepted < 0:
        parser.error("--target-accepted must be non-negative")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc must be exactly 4 characters")
    if args.preview_every <= 0:
        parser.error("--preview-every must be positive")
    if not 1 <= args.preview_jpeg_quality <= 100:
        parser.error("--preview-jpeg-quality must be between 1 and 100")
    if not _capture_targets(args):
        parser.error("provide --camera-config or --devices")


if __name__ == "__main__":
    raise SystemExit(main())
