from __future__ import annotations

import argparse
import threading
from http.server import ThreadingHTTPServer

from skyweave.calibration.charuco import CharucoBoardSpec
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
    parser.add_argument("--squares-x", type=int, default=10)
    parser.add_argument("--squares-y", type=int, default=7)
    parser.add_argument("--square-mm", type=float, required=True)
    parser.add_argument("--marker-mm", type=float, required=True)
    parser.add_argument("--dictionary", default="DICT_4X4")
    parser.add_argument("--min-lock-corners", type=int, default=12)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    parser.add_argument("--display-scale", type=float, default=0.5, help="Scale frames before detection and streaming.")
    parser.add_argument("--detect-every", type=int, default=2, help="Run ChArUco detection every N displayed frames.")
    parser.add_argument("--reopen-after-failures", type=int, default=5, help="Reopen selected camera after N failed reads.")
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
    return 0 if state.error is None else 1


def _parse_devices(device: str | None, devices: str | None) -> list[str]:
    if devices:
        parsed = [item.strip() for item in devices.split(",") if item.strip()]
        if parsed:
            return parsed
    return [device or "/dev/video0"]


if __name__ == "__main__":
    raise SystemExit(main())
