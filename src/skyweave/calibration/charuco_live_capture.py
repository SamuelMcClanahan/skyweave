from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from skyweave.calibration.charuco import CharucoBoardSpec, detect_charuco, draw_annotated_detection
from skyweave.calibration.charuco_live_state import (
    LiveState,
    _fps_from_times,
    _is_running,
    _mark_failed,
    _mark_idle,
    _record_frame,
    _record_read_failure,
    _requested_index,
    _select_camera,
    _wait_for_selection_change,
)
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource


def _capture_loop(args: argparse.Namespace, spec: CharucoBoardSpec, state: LiveState) -> None:
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError:
        state.update_error("OpenCV is required. Install with: python -m pip install -e '.[camera]'")
        return

    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(args.jpeg_quality)]
    source: OpenCVCameraSource | None = None
    active_spec = spec
    active_index: int | None = None
    frame_times: list[float] = []
    consecutive_read_failures = 0
    cached_detection = ("none", 0, 0)
    try:
        while _is_running(state):
            requested_index = _requested_index(state)
            if active_index != requested_index:
                if source is not None:
                    source.close()
                    _mark_idle(state, active_index)
                source = None
                active_index = requested_index
                active_spec = spec
                frame_times.clear()
                consecutive_read_failures = 0
                cached_detection = ("none", 0, 0)
                _select_camera(state, active_index)
                source = _open_source(args, state, active_index)
                if source is None:
                    active_index = None
                    time.sleep(1.0)
                    continue
                for _ in range(max(args.warmup_frames, 0)):
                    source.read()

            if source is None:
                _wait_for_selection_change(state, active_index, timeout_s=0.25)
                continue

            frame_start = time.perf_counter()
            frame = source.read()
            if frame is None:
                consecutive_read_failures += 1
                _record_read_failure(state, active_index)
                if consecutive_read_failures >= args.reopen_after_failures:
                    source.close()
                    source = None
                    _mark_failed(state, active_index, f"{consecutive_read_failures} consecutive read failures")
                    active_index = None
                    time.sleep(0.25)
                else:
                    time.sleep(0.02)
                continue
            consecutive_read_failures = 0

            start = time.perf_counter()
            display_gray = _resize_gray(cv2, frame.gray, args.display_scale)
            sharpness = _sharpness_score(cv2, display_gray)
            should_detect = frame.frame_seq % args.detect_every == 0
            if should_detect:
                detection, payload = detect_charuco(display_gray, active_spec)
                annotated = draw_annotated_detection(display_gray, payload)
                cached_detection = (detection.dictionary, detection.marker_count, detection.corner_count)
            else:
                detection_dictionary, marker_count, corner_count = cached_detection
                annotated = cv2.cvtColor(display_gray, cv2.COLOR_GRAY2BGR)
                detection = _CachedDetection(detection_dictionary, marker_count, corner_count)
            ok, encoded = cv2.imencode(".jpg", annotated, encode_params)
            latency_ms = (time.perf_counter() - start) * 1000.0
            if not ok:
                state.update_error("failed to encode annotated frame")
                return

            if detection.corner_count >= args.min_lock_corners and active_spec.dictionary != detection.dictionary:
                active_spec = CharucoBoardSpec(
                    squares_x=active_spec.squares_x,
                    squares_y=active_spec.squares_y,
                    square_length_m=active_spec.square_length_m,
                    marker_length_m=active_spec.marker_length_m,
                    dictionary=detection.dictionary,
                )

            now = time.perf_counter()
            frame_times.append(now)
            if len(frame_times) > 30:
                frame_times.pop(0)
            _record_frame(
                state,
                camera_index=active_index,
                frame_seq=frame.frame_seq,
                detection_dictionary=detection.dictionary,
                marker_count=detection.marker_count,
                corner_count=detection.corner_count,
                latency_ms=latency_ms,
                sharpness=sharpness,
                capture_fps=_fps_from_times(frame_times),
                frame_jpeg=bytes(encoded),
            )

            elapsed = time.perf_counter() - frame_start
            target = 1.0 / args.fps if args.fps > 0.0 else 0.0
            if target > elapsed:
                time.sleep(target - elapsed)
    except Exception as exc:
        state.update_error(str(exc))
    finally:
        if source is not None:
            source.close()

def _open_source(args: argparse.Namespace, state: LiveState, index: int) -> OpenCVCameraSource | None:
    device = state.devices[index]
    source = OpenCVCameraSource(
        camera_id=index,
        device=device,
        width=args.width,
        height=args.height,
        fps=args.fps,
        fourcc=args.fourcc,
    )
    try:
        source.open()
    except CameraOpenError as exc:
        with state.condition:
            state.cameras[index].status = "failed"
            state.cameras[index].error = str(exc)
            state.frame_jpeg = None
            state.frame_version += 1
            state.condition.notify_all()
        return None
    with state.condition:
        state.cameras[index].status = "running"
        state.cameras[index].error = None
        state.condition.notify_all()
    return source

@dataclass(frozen=True)
class _CachedDetection:
    dictionary: str
    marker_count: int
    corner_count: int

def _resize_gray(cv2, gray, scale: float):
    if abs(scale - 1.0) < 1.0e-6:
        return gray
    return cv2.resize(gray, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

def _sharpness_score(cv2, gray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())
