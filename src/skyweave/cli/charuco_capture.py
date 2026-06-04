from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from skyweave.calibration.charuco import (
    CharucoBoardSpec,
    detect_charuco,
    serialize_detection_payload,
    write_annotated_detection,
)
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource


@dataclass(frozen=True)
class CaptureTarget:
    label: str
    device: str
    id_path: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture ChArUco observations for camera calibration.")
    parser.add_argument("--camera-config", default=None, help="YAML file from skyweave-camera-inventory.")
    parser.add_argument("--devices", default=None, help="Comma-separated capture nodes, e.g. /dev/video0,/dev/video2.")
    parser.add_argument("--labels", default=None, help="Comma-separated physical labels matching --devices.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--warmup-frames", type=int, default=10)
    parser.add_argument("--sample-every", type=int, default=5)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--squares-x", type=int, default=10)
    parser.add_argument("--squares-y", type=int, default=7)
    parser.add_argument("--square-mm", type=float, required=True)
    parser.add_argument("--marker-mm", type=float, required=True)
    parser.add_argument("--dictionary", default="DICT_4X4_50")
    parser.add_argument("--min-corners", type=int, default=24)
    parser.add_argument("--save-images", action="store_true")
    args = parser.parse_args(argv)
    if args.sample_every <= 0:
        parser.error("--sample-every must be positive")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc must be exactly 4 characters")

    targets = _capture_targets(args)
    if not targets:
        parser.error("provide --camera-config or --devices")
    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir()
    spec = CharucoBoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_length_m=args.square_mm / 1000.0,
        marker_length_m=args.marker_mm / 1000.0,
        dictionary=args.dictionary,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    observations_path = output_dir / "observations.jsonl"
    manifest = _manifest(args, targets, output_dir)
    (output_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    accepted = 0
    with observations_path.open("w", encoding="utf-8") as writer:
        for camera_id, target in enumerate(targets):
            accepted += _capture_device(args, spec, camera_id, target, output_dir, writer)

    print(f"capture_dir={output_dir}")
    print(f"observations={observations_path}")
    print(f"accepted_observations={accepted}")
    return 0 if accepted > 0 else 1


def _capture_device(
    args: argparse.Namespace,
    spec: CharucoBoardSpec,
    camera_id: int,
    target: CaptureTarget,
    output_dir: Path,
    writer,
) -> int:
    label = target.label
    device = target.device
    source = OpenCVCameraSource(camera_id, device, width=args.width, height=args.height, fps=args.fps, fourcc=args.fourcc)
    try:
        source.open()
    except CameraOpenError as exc:
        print(f"charuco_capture_open_failed label={label} device={device} error={exc}")
        return 0

    accepted = 0
    try:
        for _ in range(max(args.warmup_frames, 0)):
            source.read()
        for frame_index in range(args.frames):
            frame = source.read()
            if frame is None:
                print(f"charuco_capture_read_failed label={label} frame={frame_index}")
                continue
            if frame_index % args.sample_every != 0:
                continue
            detection, payload = detect_charuco(frame.gray, spec)
            observation = serialize_detection_payload(payload)
            event = {
                "camera_id": camera_id,
                "label": label,
                "device": device,
                "id_path": target.id_path,
                "frame_seq": frame.frame_seq,
                "capture_ts_ns": frame.capture_ts_ns,
                "image_width": frame.image_width,
                "image_height": frame.image_height,
                "dictionary": detection.dictionary,
                "marker_count": detection.marker_count,
                "corner_count": detection.corner_count,
                "accepted": detection.corner_count >= args.min_corners,
                "corner_ids": observation.corner_ids,
                "corners_px": observation.corners_px,
                "marker_ids": observation.marker_ids,
                "marker_corners_px": observation.marker_corners_px,
            }
            if event["accepted"]:
                accepted += 1
                if args.save_images:
                    stem = f"{label}_frame{frame.frame_seq:06d}"
                    _write_gray(output_dir / "frames" / f"{stem}.pgm", frame.gray)
                    write_annotated_detection(frame.gray, payload, output_dir / "annotated" / f"{stem}.png")
            writer.write(json.dumps(event, sort_keys=True) + "\n")
            print(
                "charuco_capture_sample "
                f"label={label} frame={frame.frame_seq} corners={detection.corner_count} "
                f"markers={detection.marker_count} accepted={event['accepted']}"
            )
    finally:
        source.close()
    print(f"charuco_capture_summary label={label} device={device} accepted={accepted}")
    return accepted


def _manifest(args: argparse.Namespace, targets: list[CaptureTarget], output_dir: Path) -> dict[str, object]:
    return {
        "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "output_dir": str(output_dir),
        "board": {
            "squares_x": args.squares_x,
            "squares_y": args.squares_y,
            "square_mm": args.square_mm,
            "marker_mm": args.marker_mm,
            "dictionary": args.dictionary,
        },
        "capture": {
            "frames": args.frames,
            "warmup_frames": args.warmup_frames,
            "sample_every": args.sample_every,
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "fourcc": args.fourcc,
            "min_corners": args.min_corners,
        },
        "cameras": [
            {"label": target.label, "device": target.device, "id_path": target.id_path}
            for target in targets
        ],
        "notes": [
            "Laptop-screen captures are smoke data only.",
            "Use a printed rigid board for final calibration.",
        ],
    }


def _write_gray(path: Path, gray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"P5\n{gray.shape[1]} {gray.shape[0]}\n255\n".encode("ascii") + gray.tobytes())


def _capture_targets(args: argparse.Namespace) -> list[CaptureTarget]:
    if args.devices:
        devices = [item.strip() for item in args.devices.split(",") if item.strip()]
        labels = _parse_labels(args.labels, len(devices))
        return [CaptureTarget(label=label, device=device) for label, device in zip(labels, devices)]

    if args.camera_config:
        targets = _load_camera_config(Path(args.camera_config))
        if args.labels:
            labels = _parse_labels(args.labels, len(targets))
            return [
                CaptureTarget(label=label, device=target.device, id_path=target.id_path)
                for label, target in zip(labels, targets)
            ]
        return targets

    return []


def _load_camera_config(path: Path) -> list[CaptureTarget]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cameras = data.get("cameras", [])
    if not isinstance(cameras, list):
        raise SystemExit(f"{path} must contain a cameras list")
    targets: list[CaptureTarget] = []
    for index, camera in enumerate(cameras):
        if not isinstance(camera, dict):
            continue
        device = str(camera.get("device", "")).strip()
        if not device:
            continue
        targets.append(
            CaptureTarget(
                label=str(camera.get("label") or f"camera_{index}"),
                device=device,
                id_path=str(camera.get("id_path") or ""),
            )
        )
    return targets


def _parse_labels(labels: str | None, expected: int) -> list[str]:
    if labels is None:
        return [f"camera_{index}" for index in range(expected)]
    parsed = [item.strip() for item in labels.split(",") if item.strip()]
    if len(parsed) != expected:
        raise SystemExit(f"--labels count ({len(parsed)}) must match --devices count ({expected})")
    return parsed


def _default_output_dir() -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    return Path("data/calibration") / f"charuco_capture_{stamp}"


if __name__ == "__main__":
    raise SystemExit(main())
