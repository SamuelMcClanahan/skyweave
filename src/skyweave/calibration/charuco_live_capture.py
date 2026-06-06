from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from skyweave.calibration.charuco import CharucoBoardSpec, detect_charuco, draw_annotated_detection
from skyweave.calibration.charuco_live_state import (
    LiveCameraSettings,
    LiveState,
    _fps_from_times,
    _is_running,
    _mark_failed,
    _mark_idle,
    _record_read_failure,
    _requested_index,
    _select_camera,
)
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource


def _capture_loop(args: argparse.Namespace, spec: CharucoBoardSpec, state: LiveState) -> None:
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError:
        state.update_error("OpenCV is required. Install with: python -m pip install -e '.[camera]'")
        return

    sources: dict[int, OpenCVCameraSource] = {}
    active_specs = [spec for _ in state.devices]
    active_signature: tuple[int, int, float, str, int] | None = None
    settings_revision = -1
    settings, settings_revision = state.settings_snapshot()
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.camera.jpeg_quality)]
    frame_times: list[list[float]] = [[] for _ in state.devices]
    consecutive_read_failures = [0 for _ in state.devices]
    cached_detections = [("none", 0, 0) for _ in state.devices]
    try:
        while _is_running(state):
            loop_start = time.perf_counter()
            next_settings, next_revision = state.settings_snapshot()
            if next_revision != settings_revision:
                settings = next_settings
                settings_revision = next_revision
                encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.camera.jpeg_quality)]
            requested_index = _requested_index(state)
            if requested_index != state.selected_index:
                _select_camera(state, requested_index)

            camera_signature = settings.camera.capture_signature()
            if active_signature != camera_signature:
                for index, source in sources.items():
                    source.close()
                    _mark_idle(state, index)
                sources = {}
                active_signature = camera_signature
                active_specs = [spec for _ in state.devices]
                frame_times = [[] for _ in state.devices]
                consecutive_read_failures = [0 for _ in state.devices]
                cached_detections = [("none", 0, 0) for _ in state.devices]

            _open_missing_sources(settings.camera, state, sources)
            if not sources:
                time.sleep(0.25)
                continue

            for camera_index in range(len(state.devices)):
                source = sources.get(camera_index)
                if source is None:
                    continue
                frame = source.read()
                if frame is None:
                    consecutive_read_failures[camera_index] += 1
                    _record_read_failure(state, camera_index)
                    if consecutive_read_failures[camera_index] >= settings.camera.reopen_after_failures:
                        source.close()
                        sources.pop(camera_index, None)
                        _mark_failed(
                            state,
                            camera_index,
                            f"{consecutive_read_failures[camera_index]} consecutive read failures",
                        )
                        consecutive_read_failures[camera_index] = 0
                    continue
                consecutive_read_failures[camera_index] = 0

                start = time.perf_counter()
                display_gray = _resize_gray(cv2, frame.gray, settings.camera.display_scale)
                sharpness = _sharpness_score(cv2, display_gray)
                should_detect = frame.frame_seq % settings.camera.detect_every == 0
                if should_detect:
                    detection, payload = detect_charuco(display_gray, active_specs[camera_index])
                    annotated = draw_annotated_detection(display_gray, payload)
                    cached_detections[camera_index] = (
                        detection.dictionary,
                        detection.marker_count,
                        detection.corner_count,
                    )
                else:
                    detection_dictionary, marker_count, corner_count = cached_detections[camera_index]
                    annotated = cv2.cvtColor(display_gray, cv2.COLOR_GRAY2BGR)
                    detection = _CachedDetection(detection_dictionary, marker_count, corner_count)
                ok, encoded = cv2.imencode(".jpg", annotated, encode_params)
                latency_ms = (time.perf_counter() - start) * 1000.0
                if not ok:
                    state.update_error("failed to encode annotated frame")
                    return

                if (
                    detection.corner_count >= settings.camera.min_lock_corners
                    and active_specs[camera_index].dictionary != detection.dictionary
                ):
                    active = active_specs[camera_index]
                    active_specs[camera_index] = CharucoBoardSpec(
                        squares_x=active.squares_x,
                        squares_y=active.squares_y,
                        square_length_m=active.square_length_m,
                        marker_length_m=active.marker_length_m,
                        dictionary=detection.dictionary,
                    )

                now = time.perf_counter()
                frame_times[camera_index].append(now)
                if len(frame_times[camera_index]) > 30:
                    frame_times[camera_index].pop(0)
                state.record_camera_frame(
                    camera_index=camera_index,
                    frame_seq=frame.frame_seq,
                    detection_dictionary=detection.dictionary,
                    marker_count=detection.marker_count,
                    corner_count=detection.corner_count,
                    latency_ms=latency_ms,
                    sharpness=sharpness,
                    capture_fps=_fps_from_times(frame_times[camera_index]),
                    frame_jpeg=bytes(encoded),
                )

            elapsed = time.perf_counter() - loop_start
            target = 1.0 / settings.camera.fps if settings.camera.fps > 0.0 else 0.0
            if target > elapsed:
                time.sleep(target - elapsed)
    except Exception as exc:
        state.update_error(str(exc))
    finally:
        for source in sources.values():
            source.close()


def _open_missing_sources(
    settings: LiveCameraSettings,
    state: LiveState,
    sources: dict[int, OpenCVCameraSource],
) -> None:
    for index in range(len(state.devices)):
        if index in sources:
            continue
        state.set_camera_status(index, "opening")
        source = _open_source(settings, state, index)
        if source is None:
            continue
        for _ in range(max(settings.warmup_frames, 0)):
            source.read()
        sources[index] = source

def _open_source(settings: LiveCameraSettings, state: LiveState, index: int) -> OpenCVCameraSource | None:
    device = state.devices[index]
    source = OpenCVCameraSource(
        camera_id=index,
        device=device,
        width=settings.width,
        height=settings.height,
        fps=settings.fps,
        fourcc=settings.fourcc,
    )
    try:
        source.open()
    except CameraOpenError as exc:
        _mark_failed(state, index, str(exc))
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
