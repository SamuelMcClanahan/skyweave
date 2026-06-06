from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field

from skyweave.camera.motion import MOTION_BACKEND_CHOICES, MotionPacketConfig
from skyweave.config import KalmanConfig


RESOLUTION_PRESETS = (
    (1280, 800),
    (800, 600),
    (640, 480),
    (420, 420),
)


@dataclass
class CameraStatus:
    device: str
    status: str = "idle"
    error: str | None = None
    frame_seq: int = -1
    dictionary: str = "none"
    marker_count: int = 0
    corner_count: int = 0
    capture_fps: float = 0.0
    latency_ms: float = 0.0
    sharpness: float = 0.0
    last_frame_time: float = 0.0
    frames: int = 0
    read_failures: int = 0
    detected_frames: int = 0
    best_dictionary: str = "none"
    best_marker_count: int = 0
    best_corner_count: int = 0
    best_frame_seq: int = -1
    best_sharpness: float = 0.0

    @property
    def detection_rate(self) -> float:
        return self.detected_frames / self.frames if self.frames else 0.0


@dataclass
class LiveCameraSettings:
    width: int = 1280
    height: int = 800
    fps: float = 30.0
    fourcc: str = "MJPG"
    warmup_frames: int = 10
    jpeg_quality: int = 85
    display_scale: float = 0.5
    detect_every: int = 2
    min_lock_corners: int = 12
    reopen_after_failures: int = 5

    def capture_signature(self) -> tuple[int, int, float, str, int]:
        return (self.width, self.height, self.fps, self.fourcc, self.warmup_frames)

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "fourcc": self.fourcc,
            "warmup_frames": self.warmup_frames,
            "jpeg_quality": self.jpeg_quality,
            "display_scale": self.display_scale,
            "detect_every": self.detect_every,
            "min_lock_corners": self.min_lock_corners,
            "reopen_after_failures": self.reopen_after_failures,
            "resolution_presets": [{"width": w, "height": h} for w, h in RESOLUTION_PRESETS],
        }


@dataclass
class LiveMotionSettings:
    threshold: int = 32
    min_area_px: int = 4
    max_components: int = 8
    max_patch_side_px: int = 64
    max_motion_pixels: int = 225
    backend: str = "auto"

    def to_motion_config(self) -> MotionPacketConfig:
        return MotionPacketConfig(
            threshold=self.threshold,
            min_area_px=self.min_area_px,
            max_components=self.max_components,
            max_patch_side_px=self.max_patch_side_px,
            max_motion_pixels=self.max_motion_pixels,
            backend=self.backend,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "min_area_px": self.min_area_px,
            "max_components": self.max_components,
            "max_patch_side_px": self.max_patch_side_px,
            "max_motion_pixels": self.max_motion_pixels,
            "backend": self.backend,
            "backend_choices": list(MOTION_BACKEND_CHOICES),
        }


@dataclass
class LiveKalmanSettings:
    sigma_accel_mps2: float = 6.0
    initial_position_var: float = 1.0
    initial_velocity_var: float = 4.0
    measurement_var_scale: float = 1.0
    coast_seconds: float = 2.0

    def to_kalman_config(self) -> KalmanConfig:
        return KalmanConfig(
            sigma_accel_mps2=self.sigma_accel_mps2,
            initial_position_var=self.initial_position_var,
            initial_velocity_var=self.initial_velocity_var,
            measurement_var_scale=self.measurement_var_scale,
            coast_seconds=self.coast_seconds,
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "sigma_accel_mps2": self.sigma_accel_mps2,
            "initial_position_var": self.initial_position_var,
            "initial_velocity_var": self.initial_velocity_var,
            "measurement_var_scale": self.measurement_var_scale,
            "coast_seconds": self.coast_seconds,
        }


@dataclass
class LiveTrackTelemetry:
    track_id: int | None = None
    status: str = "not_attached"
    update_count: int = 0
    miss_count: int = 0
    position_m: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    velocity_mps: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    speed_mps: float = 0.0
    covariance_diag: list[float] = field(default_factory=list)
    measurement_age_ms: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "status": self.status,
            "update_count": self.update_count,
            "miss_count": self.miss_count,
            "position_m": self.position_m,
            "velocity_mps": self.velocity_mps,
            "speed_mps": self.speed_mps,
            "covariance_diag": self.covariance_diag,
            "measurement_age_ms": self.measurement_age_ms,
        }


@dataclass
class LiveTuningSettings:
    camera: LiveCameraSettings = field(default_factory=LiveCameraSettings)
    motion: LiveMotionSettings = field(default_factory=LiveMotionSettings)
    kalman: LiveKalmanSettings = field(default_factory=LiveKalmanSettings)

    def to_dict(self) -> dict[str, object]:
        return {
            "camera": self.camera.to_dict(),
            "motion": self.motion.to_dict(),
            "kalman": self.kalman.to_dict(),
        }


@dataclass
class LiveState:
    devices: list[str] = field(default_factory=lambda: ["/dev/video0"])
    tuning: LiveTuningSettings = field(default_factory=LiveTuningSettings)
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(init=False)
    frame_jpeg: bytes | None = None
    frame_version: int = 0
    frame_jpegs: list[bytes | None] = field(init=False)
    frame_versions: list[int] = field(init=False)
    running: bool = True
    selected_index: int = 0
    requested_index: int = 0
    camera_id: int = 0
    frame_seq: int = -1
    dictionary: str = "none"
    marker_count: int = 0
    corner_count: int = 0
    capture_fps: float = 0.0
    latency_ms: float = 0.0
    sharpness: float = 0.0
    last_frame_time: float = 0.0
    error: str | None = None
    cameras: list[CameraStatus] = field(init=False)
    settings_revision: int = 0
    track: LiveTrackTelemetry = field(default_factory=LiveTrackTelemetry)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)
        self.cameras = [CameraStatus(device=device) for device in self.devices]
        self.frame_jpegs = [None for _ in self.devices]
        self.frame_versions = [0 for _ in self.devices]

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            selected = self.cameras[self.selected_index]
            stale_age_ms = (time.perf_counter() - selected.last_frame_time) * 1000.0 if selected.last_frame_time > 0.0 else 0.0
            return {
                "running": self.running,
                "settings_revision": self.settings_revision,
                "selected_index": self.selected_index,
                "requested_index": self.requested_index,
                "camera_id": self.camera_id,
                "device": selected.device,
                "status": selected.status,
                "frame_seq": self.frame_seq,
                "dictionary": self.dictionary,
                "marker_count": self.marker_count,
                "corner_count": self.corner_count,
                "best_dictionary": selected.best_dictionary,
                "best_marker_count": selected.best_marker_count,
                "best_corner_count": selected.best_corner_count,
                "best_frame_seq": selected.best_frame_seq,
                "detection_rate": selected.detection_rate,
                "frames": selected.frames,
                "read_failures": selected.read_failures,
                "capture_fps": self.capture_fps,
                "latency_ms": self.latency_ms,
                "sharpness": self.sharpness,
                "best_sharpness": selected.best_sharpness,
                "stale_age_ms": stale_age_ms,
                "error": selected.error or self.error,
                "settings": self.tuning.to_dict(),
                "track": self.track.to_dict(),
                "cameras": [
                    {
                        "index": index,
                        "device": item.device,
                        "selected": index == self.selected_index,
                        "requested": index == self.requested_index,
                        "status": item.status,
                        "error": item.error,
                        "frame_seq": item.frame_seq,
                        "dictionary": item.dictionary,
                        "marker_count": item.marker_count,
                        "corner_count": item.corner_count,
                        "capture_fps": item.capture_fps,
                        "latency_ms": item.latency_ms,
                        "sharpness": item.sharpness,
                        "stale_age_ms": (
                            (time.perf_counter() - item.last_frame_time) * 1000.0
                            if item.last_frame_time > 0.0
                            else 0.0
                        ),
                        "frames": item.frames,
                        "read_failures": item.read_failures,
                        "best_dictionary": item.best_dictionary,
                        "best_corner_count": item.best_corner_count,
                        "best_marker_count": item.best_marker_count,
                        "best_frame_seq": item.best_frame_seq,
                        "best_sharpness": item.best_sharpness,
                        "detection_rate": item.detection_rate,
                    }
                    for index, item in enumerate(self.cameras)
                ],
            }

    def request_camera(self, index: int) -> bool:
        with self.condition:
            if index < 0 or index >= len(self.cameras):
                return False
            self.requested_index = index
            self.condition.notify_all()
            return True

    def update_error(self, error: str) -> None:
        with self.condition:
            self.error = error
            self.running = False
            self.condition.notify_all()

    def settings_snapshot(self) -> tuple[LiveTuningSettings, int]:
        with self.lock:
            return copy.deepcopy(self.tuning), self.settings_revision

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        with self.condition:
            if "camera" in payload:
                _update_camera_settings(self.tuning.camera, _dict_payload(payload["camera"], "camera"))
            if "motion" in payload:
                _update_motion_settings(self.tuning.motion, _dict_payload(payload["motion"], "motion"))
            if "kalman" in payload:
                _update_kalman_settings(self.tuning.kalman, _dict_payload(payload["kalman"], "kalman"))
            self.settings_revision += 1
            self.condition.notify_all()
            return {
                "settings_revision": self.settings_revision,
                "settings": self.tuning.to_dict(),
            }

    def update_track(self, track: LiveTrackTelemetry) -> None:
        with self.condition:
            self.track = track
            self.condition.notify_all()

    def set_camera_status(self, index: int, status: str, error: str | None = None) -> None:
        with self.condition:
            if 0 <= index < len(self.cameras):
                self.cameras[index].status = status
                self.cameras[index].error = error
                self.condition.notify_all()

    def record_camera_frame(
        self,
        camera_index: int,
        frame_seq: int,
        detection_dictionary: str,
        marker_count: int,
        corner_count: int,
        latency_ms: float,
        sharpness: float,
        capture_fps: float,
        frame_jpeg: bytes | None = None,
    ) -> None:
        with self.condition:
            camera = self.cameras[camera_index]
            camera.status = "running"
            camera.error = None
            camera.frames += 1
            if corner_count > 0:
                camera.detected_frames += 1
            if (corner_count, marker_count) > (camera.best_corner_count, camera.best_marker_count):
                camera.best_corner_count = corner_count
                camera.best_marker_count = marker_count
                camera.best_dictionary = detection_dictionary
                camera.best_frame_seq = frame_seq
            if sharpness > camera.best_sharpness:
                camera.best_sharpness = sharpness
            camera.frame_seq = frame_seq
            camera.dictionary = detection_dictionary
            camera.marker_count = marker_count
            camera.corner_count = corner_count
            camera.capture_fps = capture_fps
            camera.latency_ms = latency_ms
            camera.sharpness = sharpness
            camera.last_frame_time = time.perf_counter()

            if frame_jpeg is not None:
                self.frame_jpegs[camera_index] = frame_jpeg
                self.frame_versions[camera_index] += 1

            if camera_index == self.selected_index:
                self.camera_id = camera_index
                self.frame_seq = frame_seq
                self.dictionary = detection_dictionary
                self.marker_count = marker_count
                self.corner_count = corner_count
                self.capture_fps = capture_fps
                self.latency_ms = latency_ms
                self.sharpness = sharpness
                self.last_frame_time = camera.last_frame_time
                if frame_jpeg is not None:
                    self.frame_jpeg = frame_jpeg
                    self.frame_version += 1
            self.condition.notify_all()


def _select_camera(state: LiveState, index: int) -> None:
    with state.condition:
        camera = state.cameras[index]
        state.selected_index = index
        state.camera_id = index
        state.frame_seq = camera.frame_seq
        state.dictionary = camera.dictionary
        state.marker_count = camera.marker_count
        state.corner_count = camera.corner_count
        state.capture_fps = camera.capture_fps
        state.latency_ms = camera.latency_ms
        state.sharpness = camera.sharpness
        state.last_frame_time = camera.last_frame_time
        state.frame_jpeg = state.frame_jpegs[index]
        state.frame_version += 1
        state.condition.notify_all()

def _record_read_failure(state: LiveState, index: int) -> None:
    with state.condition:
        state.cameras[index].read_failures += 1
        state.condition.notify_all()

def _mark_failed(state: LiveState, index: int | None, error: str) -> None:
    if index is None:
        return
    with state.condition:
        state.cameras[index].status = "failed"
        state.cameras[index].error = error
        state.frame_jpegs[index] = None
        state.frame_versions[index] += 1
        if index == state.selected_index:
            state.frame_jpeg = None
            state.frame_version += 1
        state.condition.notify_all()

def _mark_idle(state: LiveState, index: int | None) -> None:
    if index is None:
        return
    with state.condition:
        if state.cameras[index].status == "running":
            state.cameras[index].status = "idle"
        state.condition.notify_all()


def _record_frame(
    state: LiveState,
    camera_index: int,
    frame_seq: int,
    detection_dictionary: str,
    marker_count: int,
    corner_count: int,
    latency_ms: float,
    sharpness: float,
    capture_fps: float,
    frame_jpeg: bytes,
) -> None:
    state.record_camera_frame(
        camera_index=camera_index,
        frame_seq=frame_seq,
        detection_dictionary=detection_dictionary,
        marker_count=marker_count,
        corner_count=corner_count,
        latency_ms=latency_ms,
        sharpness=sharpness,
        capture_fps=capture_fps,
        frame_jpeg=frame_jpeg,
    )

def _requested_index(state: LiveState) -> int:
    with state.lock:
        return state.requested_index

def _wait_for_selection_change(state: LiveState, active_index: int | None, timeout_s: float) -> None:
    with state.condition:
        state.condition.wait_for(lambda: state.requested_index != active_index or not state.running, timeout=timeout_s)

def _is_running(state: LiveState) -> bool:
    with state.lock:
        return state.running

def _fps_from_times(times: list[float]) -> float:
    if len(times) < 2:
        return 0.0
    elapsed = times[-1] - times[0]
    return (len(times) - 1) / elapsed if elapsed > 0.0 else 0.0


def _dict_payload(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} settings must be an object")
    return value


def _update_camera_settings(settings: LiveCameraSettings, payload: dict[str, object]) -> None:
    if "width" in payload:
        settings.width = _positive_int(payload["width"], "camera.width")
    if "height" in payload:
        settings.height = _positive_int(payload["height"], "camera.height")
    if "fps" in payload:
        settings.fps = _positive_float(payload["fps"], "camera.fps")
    if "fourcc" in payload:
        settings.fourcc = _fourcc(payload["fourcc"])
    if "warmup_frames" in payload:
        settings.warmup_frames = _nonnegative_int(payload["warmup_frames"], "camera.warmup_frames")
    if "jpeg_quality" in payload:
        settings.jpeg_quality = _bounded_int(payload["jpeg_quality"], "camera.jpeg_quality", 1, 100)
    if "display_scale" in payload:
        settings.display_scale = _positive_float(payload["display_scale"], "camera.display_scale")
    if "detect_every" in payload:
        settings.detect_every = _positive_int(payload["detect_every"], "camera.detect_every")
    if "min_lock_corners" in payload:
        settings.min_lock_corners = _nonnegative_int(payload["min_lock_corners"], "camera.min_lock_corners")
    if "reopen_after_failures" in payload:
        settings.reopen_after_failures = _positive_int(payload["reopen_after_failures"], "camera.reopen_after_failures")


def _update_motion_settings(settings: LiveMotionSettings, payload: dict[str, object]) -> None:
    if "threshold" in payload:
        settings.threshold = _bounded_int(payload["threshold"], "motion.threshold", 0, 255)
    if "min_area_px" in payload:
        settings.min_area_px = _positive_int(payload["min_area_px"], "motion.min_area_px")
    if "max_components" in payload:
        settings.max_components = _positive_int(payload["max_components"], "motion.max_components")
    if "max_patch_side_px" in payload:
        settings.max_patch_side_px = _positive_int(payload["max_patch_side_px"], "motion.max_patch_side_px")
    if "max_motion_pixels" in payload:
        settings.max_motion_pixels = _positive_int(payload["max_motion_pixels"], "motion.max_motion_pixels")
    if "backend" in payload:
        backend = str(payload["backend"])
        if backend not in MOTION_BACKEND_CHOICES:
            raise ValueError(f"motion.backend must be one of {', '.join(MOTION_BACKEND_CHOICES)}")
        settings.backend = backend


def _update_kalman_settings(settings: LiveKalmanSettings, payload: dict[str, object]) -> None:
    if "sigma_accel_mps2" in payload:
        settings.sigma_accel_mps2 = _nonnegative_float(payload["sigma_accel_mps2"], "kalman.sigma_accel_mps2")
    if "initial_position_var" in payload:
        settings.initial_position_var = _positive_float(payload["initial_position_var"], "kalman.initial_position_var")
    if "initial_velocity_var" in payload:
        settings.initial_velocity_var = _positive_float(payload["initial_velocity_var"], "kalman.initial_velocity_var")
    if "measurement_var_scale" in payload:
        settings.measurement_var_scale = _positive_float(payload["measurement_var_scale"], "kalman.measurement_var_scale")
    if "coast_seconds" in payload:
        settings.coast_seconds = _nonnegative_float(payload["coast_seconds"], "kalman.coast_seconds")


def _fourcc(value: object) -> str:
    text = str(value).strip().upper()
    if len(text) != 4:
        raise ValueError("camera.fourcc must be exactly 4 characters")
    return text


def _positive_int(value: object, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_int(value: object, name: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _bounded_int(value: object, name: str, min_value: int, max_value: int) -> int:
    parsed = int(value)
    if not min_value <= parsed <= max_value:
        raise ValueError(f"{name} must be between {min_value} and {max_value}")
    return parsed


def _positive_float(value: object, name: str) -> float:
    parsed = float(value)
    if parsed <= 0.0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_float(value: object, name: str) -> float:
    parsed = float(value)
    if parsed < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return parsed
