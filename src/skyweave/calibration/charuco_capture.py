from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import yaml

from skyweave.calibration.charuco import (
    CharucoBoardSpec,
    detect_charuco,
    serialize_detection_payload,
    write_annotated_detection,
)
from skyweave.calibration.charuco_capture_preview import CapturePreview
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource


@dataclass(frozen=True)
class CaptureTarget:
    label: str
    device: str
    id_path: str = ""


def run_capture(args: argparse.Namespace) -> int:
    targets = _capture_targets(args)
    if not targets:
        raise SystemExit("provide --camera-config or --devices")
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
    preview = _start_preview(args, targets)
    try:
        with observations_path.open("w", encoding="utf-8") as writer:
            for camera_id, target in enumerate(targets):
                accepted += _capture_device(args, spec, camera_id, target, output_dir, writer, preview)
    finally:
        if preview is not None:
            preview.stop()

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
    writer: TextIO,
    preview: CapturePreview | None = None,
) -> int:
    label = target.label
    device = target.device
    source = OpenCVCameraSource(camera_id, device, width=args.width, height=args.height, fps=args.fps, fourcc=args.fourcc)
    if preview is not None:
        preview.select_camera(camera_id)
    try:
        source.open()
    except CameraOpenError as exc:
        print(f"charuco_capture_open_failed label={label} device={device} error={exc}")
        if preview is not None:
            preview.mark_failed(camera_id, str(exc))
        return 0

    accepted = 0
    try:
        for _ in range(max(args.warmup_frames, 0)):
            source.read()
        for frame_index in range(args.frames):
            frame_start = time.perf_counter()
            frame = source.read()
            if frame is None:
                print(f"charuco_capture_read_failed label={label} frame={frame_index}")
                if preview is not None:
                    preview.record_read_failure(camera_id)
                _sleep_for_target_fps(frame_start, args.fps)
                continue
            should_sample = frame_index % args.sample_every == 0
            should_preview = preview is not None and preview.should_publish(frame_index)
            if not should_sample and not should_preview:
                _sleep_for_target_fps(frame_start, args.fps)
                continue
            start = time.perf_counter()
            detection, payload = detect_charuco(frame.gray, spec)
            latency_ms = (time.perf_counter() - start) * 1000.0
            if preview is not None and should_preview:
                preview.publish(camera_id, frame.frame_seq, frame.gray, detection, payload, latency_ms)
            if should_sample:
                accepted += _write_observation(args, target, camera_id, frame, detection, payload, output_dir, writer)
                if args.target_accepted and accepted >= args.target_accepted:
                    break
            _sleep_for_target_fps(frame_start, args.fps)
    finally:
        source.close()
        if preview is not None:
            preview.mark_idle(camera_id)
    print(f"charuco_capture_summary label={label} device={device} accepted={accepted}")
    return accepted


def _write_observation(
    args: argparse.Namespace,
    target: CaptureTarget,
    camera_id: int,
    frame,
    detection,
    payload: object | None,
    output_dir: Path,
    writer: TextIO,
) -> int:
    observation = serialize_detection_payload(payload)
    event = {
        "camera_id": camera_id,
        "label": target.label,
        "device": target.device,
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
    if event["accepted"] and args.save_images:
        stem = f"{target.label}_frame{frame.frame_seq:06d}"
        _write_gray(output_dir / "frames" / f"{stem}.pgm", frame.gray)
        write_annotated_detection(frame.gray, payload, output_dir / "annotated" / f"{stem}.png")
    writer.write(json.dumps(event, sort_keys=True) + "\n")
    print(
        "charuco_capture_sample "
        f"label={target.label} frame={frame.frame_seq} corners={detection.corner_count} "
        f"markers={detection.marker_count} accepted={event['accepted']}"
    )
    return 1 if event["accepted"] else 0


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
            "target_accepted": args.target_accepted,
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "fourcc": args.fourcc,
            "min_corners": args.min_corners,
            "preview": args.preview,
        },
        "cameras": [{"label": target.label, "device": target.device, "id_path": target.id_path} for target in targets],
        "notes": [
            "Laptop-screen captures are smoke data only.",
            "Use a printed rigid board for final calibration.",
        ],
    }


def _start_preview(args: argparse.Namespace, targets: list[CaptureTarget]) -> CapturePreview | None:
    if not args.preview:
        return None
    preview = CapturePreview(
        devices=[target.device for target in targets],
        host=args.preview_host,
        port=args.preview_port,
        jpeg_quality=args.preview_jpeg_quality,
        publish_every=args.preview_every,
    )
    preview.start()
    return preview


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


def _sleep_for_target_fps(start_s: float, fps: float) -> None:
    if fps <= 0.0:
        return
    remaining = (1.0 / fps) - (time.perf_counter() - start_s)
    if remaining > 0.0:
        time.sleep(remaining)
