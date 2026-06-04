from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class CameraStatus:
    device: str
    status: str = "idle"
    error: str | None = None
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
class LiveState:
    devices: list[str] = field(default_factory=lambda: ["/dev/video0"])
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(init=False)
    frame_jpeg: bytes | None = None
    frame_version: int = 0
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

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)
        self.cameras = [CameraStatus(device=device) for device in self.devices]

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            selected = self.cameras[self.selected_index]
            stale_age_ms = (time.perf_counter() - self.last_frame_time) * 1000.0 if self.last_frame_time > 0.0 else 0.0
            return {
                "running": self.running,
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
                "cameras": [
                    {
                        "index": index,
                        "device": item.device,
                        "selected": index == self.selected_index,
                        "requested": index == self.requested_index,
                        "status": item.status,
                        "error": item.error,
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

def _select_camera(state: LiveState, index: int) -> None:
    with state.condition:
        state.selected_index = index
        state.camera_id = index
        state.frame_seq = -1
        state.dictionary = "none"
        state.marker_count = 0
        state.corner_count = 0
        state.capture_fps = 0.0
        state.latency_ms = 0.0
        state.sharpness = 0.0
        state.frame_jpeg = None
        state.frame_version += 1
        state.cameras[index].status = "opening"
        state.cameras[index].error = None
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
    with state.condition:
        camera = state.cameras[camera_index]
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
        state.camera_id = camera_index
        state.frame_seq = frame_seq
        state.dictionary = detection_dictionary
        state.marker_count = marker_count
        state.corner_count = corner_count
        state.capture_fps = capture_fps
        state.latency_ms = latency_ms
        state.sharpness = sharpness
        state.last_frame_time = time.perf_counter()
        state.frame_jpeg = frame_jpeg
        state.frame_version += 1
        state.condition.notify_all()

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
