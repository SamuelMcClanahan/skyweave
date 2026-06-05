from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from skyweave.calibration.charuco import (
    DEFAULT_CHARUCO_DICTIONARY,
    DEFAULT_CHARUCO_MARKER_MM,
    DEFAULT_CHARUCO_SQUARES_X,
    DEFAULT_CHARUCO_SQUARES_Y,
    DEFAULT_CHARUCO_SQUARE_MM,
    CharucoBoardSpec,
)
from skyweave.calibration.charuco_live_capture import _capture_loop
from skyweave.calibration.charuco_live_server import _display_host, _html_page, _make_handler
from skyweave.calibration.charuco_live_state import LiveState, _fps_from_times


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a live ChArUco detection web viewer.")
    parser.add_argument("--device", default=None)
    parser.add_argument("--devices", default=None, help="Comma-separated camera devices.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--warmup-frames", type=int, default=10)
    parser.add_argument("--squares-x", type=int, default=DEFAULT_CHARUCO_SQUARES_X)
    parser.add_argument("--squares-y", type=int, default=DEFAULT_CHARUCO_SQUARES_Y)
    parser.add_argument("--square-mm", type=float, default=DEFAULT_CHARUCO_SQUARE_MM)
    parser.add_argument("--marker-mm", type=float, default=DEFAULT_CHARUCO_MARKER_MM)
    parser.add_argument("--dictionary", default=DEFAULT_CHARUCO_DICTIONARY)
    parser.add_argument("--min-lock-corners", type=int, default=12)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    parser.add_argument("--display-scale", type=float, default=0.5, help="Scale frames before detection and streaming.")
    parser.add_argument("--detect-every", type=int, default=2, help="Run ChArUco detection every N displayed frames.")
    parser.add_argument("--reopen-after-failures", type=int, default=5, help="Reopen selected camera after N failed reads.")
    parser.add_argument("--jsonl", default=None, help="Optional path for live status snapshots.")
    parser.add_argument("--log-every-s", type=float, default=1.0, help="Seconds between live JSONL status snapshots.")
    args = parser.parse_args(argv)
    if args.device and args.devices:
        parser.error("use either --device or --devices, not both")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc must be exactly 4 characters")
    if not 1 <= args.jpeg_quality <= 100:
        parser.error("--jpeg-quality must be between 1 and 100")
    if args.display_scale <= 0.0:
        parser.error("--display-scale must be positive")
    if args.detect_every <= 0:
        parser.error("--detect-every must be positive")
    if args.reopen_after_failures <= 0:
        parser.error("--reopen-after-failures must be positive")
    if args.log_every_s <= 0.0:
        parser.error("--log-every-s must be positive")

    devices = _parse_devices(args.device, args.devices)
    state = LiveState(devices=devices)
    spec = CharucoBoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_length_m=args.square_mm / 1000.0,
        marker_length_m=args.marker_mm / 1000.0,
        dictionary=args.dictionary,
    )
    thread = threading.Thread(target=_capture_loop, args=(args, spec, state), daemon=True)
    thread.start()
    log_thread = _start_status_logger(state, args.jsonl, args.log_every_s)

    handler = _make_handler(state)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"charuco_live_url=http://{_display_host(args.host)}:{args.port}/")
    print("press Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with state.condition:
            state.running = False
            state.condition.notify_all()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        if log_thread:
            log_thread.join(timeout=2.0)
    return 0 if state.error is None else 1


def _parse_devices(device: str | None, devices: str | None) -> list[str]:
    if devices:
        parsed = [item.strip() for item in devices.split(",") if item.strip()]
        if parsed:
            return parsed
    return [device or "/dev/video0"]


def _start_status_logger(state: LiveState, path: str | None, interval_s: float) -> threading.Thread | None:
    if path is None:
        return None
    thread = threading.Thread(
        target=_status_log_loop,
        args=(state, Path(path), interval_s),
        daemon=True,
    )
    thread.start()
    return thread


def _status_log_loop(state: LiveState, path: Path, interval_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as writer:
        while True:
            snapshot = state.snapshot()
            snapshot["log_ts_ns"] = time.time_ns()
            writer.write(json.dumps(snapshot, sort_keys=True) + "\n")
            writer.flush()
            if not snapshot.get("running", False):
                break
            time.sleep(interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
