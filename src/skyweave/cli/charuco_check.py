from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from skyweave.calibration.charuco import CharucoBoardSpec, detect_charuco, write_annotated_detection
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect a ChArUco calibration target in live camera frames.")
    parser.add_argument("--device", default=None, help="Single camera device, e.g. /dev/video0.")
    parser.add_argument("--devices", default=None, help="Comma-separated camera devices.")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--warmup-frames", type=int, default=10)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--squares-x", type=int, default=10)
    parser.add_argument("--squares-y", type=int, default=7)
    parser.add_argument("--square-mm", type=float, required=True)
    parser.add_argument("--marker-mm", type=float, required=True)
    parser.add_argument("--dictionary", default="DICT_4X4")
    parser.add_argument("--min-corners", type=int, default=12)
    parser.add_argument("--console-every", type=int, default=10)
    parser.add_argument("--snapshot-dir", default=None, help="Optional directory for annotated detection snapshots.")
    parser.add_argument("--jsonl", default=None, help="Optional per-frame JSONL detection log.")
    args = parser.parse_args(argv)

    if args.device and args.devices:
        parser.error("use either --device or --devices, not both")
    if not args.device and not args.devices:
        parser.error("provide --device or --devices")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc must be exactly 4 characters")

    devices = [args.device] if args.device else [item.strip() for item in args.devices.split(",") if item.strip()]
    spec = CharucoBoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_length_m=args.square_mm / 1000.0,
        marker_length_m=args.marker_mm / 1000.0,
        dictionary=args.dictionary,
    )
    snapshot_dir = Path(args.snapshot_dir) if args.snapshot_dir else None
    writer = _open_jsonl(args.jsonl)
    try:
        return _run(args, devices, spec, snapshot_dir, writer)
    finally:
        if writer:
            writer.close()


def _run(
    args: argparse.Namespace,
    devices: list[str],
    spec: CharucoBoardSpec,
    snapshot_dir: Path | None,
    writer,
) -> int:
    sources: list[OpenCVCameraSource] = []
    for camera_id, device in enumerate(devices):
        source = OpenCVCameraSource(
            camera_id=camera_id,
            device=device,
            width=args.width,
            height=args.height,
            fps=args.fps,
            fourcc=args.fourcc,
        )
        try:
            source.open()
            settings = source.effective_settings()
        except CameraOpenError as exc:
            print(f"charuco_camera_open_failed camera_id={camera_id} device={device} error={exc}", file=sys.stderr)
            return 2
        print(
            "charuco_camera_opened "
            f"camera_id={camera_id} device={device} "
            f"effective_size={int(settings['width'])}x{int(settings['height'])} "
            f"effective_fps={settings['fps']:.2f}"
        )
        sources.append(source)

    try:
        _warmup(sources, args.warmup_frames)
        stats = {source.camera_id: DetectionStats() for source in sources}
        for frame_index in range(args.frames):
            for source in sources:
                frame = source.read()
                if frame is None:
                    stats[source.camera_id].read_failures += 1
                    continue
                start = time.perf_counter()
                detection, payload = detect_charuco(frame.gray, spec)
                latency_ms = (time.perf_counter() - start) * 1000.0
                improved = stats[source.camera_id].record(
                    frame.frame_seq,
                    detection.dictionary,
                    detection.corner_count,
                    detection.marker_count,
                    latency_ms,
                )
                event = {
                    "camera_id": source.camera_id,
                    "frame_seq": frame.frame_seq,
                    "capture_ts_ns": frame.capture_ts_ns,
                    "dictionary": detection.dictionary,
                    "detected": detection.detected,
                    "marker_count": detection.marker_count,
                    "corner_count": detection.corner_count,
                    "latency_ms": latency_ms,
                }
                if writer:
                    writer.write(json.dumps(event, sort_keys=True) + "\n")
                if snapshot_dir and (improved or stats[source.camera_id].frames == 1):
                    output = snapshot_dir / f"camera{source.camera_id}_frame{frame.frame_seq:04d}_{detection.dictionary}.png"
                    write_annotated_detection(frame.gray, payload, output)
                if _should_print(frame_index, args.console_every):
                    print(
                        "charuco_frame "
                        f"camera_id={source.camera_id} frame={frame_index} "
                        f"dictionary={detection.dictionary} markers={detection.marker_count} "
                        f"corners={detection.corner_count} latency={latency_ms:.3f}ms"
                    )
    finally:
        for source in sources:
            source.close()

    passed = True
    for camera_id, item in stats.items():
        camera_passed = item.best_corners >= args.min_corners and item.read_failures == 0
        passed = passed and camera_passed
        print(
            "charuco_check "
            f"camera_id={camera_id} frames={item.frames} read_failures={item.read_failures} "
            f"best_dictionary={item.best_dictionary} best_markers={item.best_markers} "
            f"best_corners={item.best_corners} best_frame={item.best_frame_seq} "
            f"detection_rate={item.detection_rate:.3f} latency_p50={_percentile(item.latencies_ms, 50.0):.3f}ms "
            f"latency_p95={_percentile(item.latencies_ms, 95.0):.3f}ms passed={camera_passed}"
        )
    if args.jsonl:
        print(f"log_path={args.jsonl}")
    if snapshot_dir:
        print(f"snapshot_dir={snapshot_dir}")
    return 0 if passed else 1


class DetectionStats:
    def __init__(self) -> None:
        self.frames = 0
        self.read_failures = 0
        self.detected_frames = 0
        self.best_corners = 0
        self.best_markers = 0
        self.best_dictionary = "none"
        self.best_frame_seq = -1
        self.latencies_ms: list[float] = []

    def record(
        self,
        frame_seq: int,
        dictionary: str,
        corner_count: int,
        marker_count: int,
        latency_ms: float,
    ) -> bool:
        self.frames += 1
        self.latencies_ms.append(latency_ms)
        if corner_count > 0:
            self.detected_frames += 1
        if (corner_count, marker_count) > (self.best_corners, self.best_markers):
            self.best_corners = corner_count
            self.best_markers = marker_count
            self.best_dictionary = dictionary
            self.best_frame_seq = frame_seq
            return True
        return False

    @property
    def detection_rate(self) -> float:
        return self.detected_frames / self.frames if self.frames else 0.0


def _warmup(sources: list[OpenCVCameraSource], frames: int) -> None:
    for _ in range(max(frames, 0)):
        for source in sources:
            source.read()


def _open_jsonl(path: str | None):
    if path is None:
        return None
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.open("w", encoding="utf-8")


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile / 100.0
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _should_print(frame_index: int, console_every: int) -> bool:
    return console_every > 0 and frame_index % console_every == 0


if __name__ == "__main__":
    raise SystemExit(main())
