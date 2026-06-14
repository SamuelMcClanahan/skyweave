from __future__ import annotations

import threading
import time
from http.server import ThreadingHTTPServer
from typing import Any

from skyweave.calibration.charuco import draw_annotated_detection
from skyweave.calibration.charuco_live_server import _display_host, _make_handler
from skyweave.calibration.charuco_live_state import (
    LiveState,
    _mark_failed,
    _mark_idle,
    _record_frame,
    _record_read_failure,
    _select_camera,
)


class CapturePreview:
    def __init__(self, devices: list[str], host: str, port: int, jpeg_quality: int, publish_every: int) -> None:
        self.state = LiveState(devices=devices)
        self.host = host
        self.port = port
        self.jpeg_quality = jpeg_quality
        self.publish_every = publish_every
        self._cv2 = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._frame_times: list[float] = []

    @property
    def url(self) -> str:
        return f"http://{_display_host(self.host)}:{self.port}/"

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), _make_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"charuco_capture_preview_url={self.url}", flush=True)

    def stop(self) -> None:
        with self.state.condition:
            self.state.running = False
            self.state.condition.notify_all()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def select_camera(self, camera_index: int) -> None:
        self._frame_times.clear()
        _select_camera(self.state, camera_index)

    def mark_failed(self, camera_index: int, error: str) -> None:
        _mark_failed(self.state, camera_index, error)

    def mark_idle(self, camera_index: int) -> None:
        _mark_idle(self.state, camera_index)

    def record_read_failure(self, camera_index: int) -> None:
        _record_read_failure(self.state, camera_index)

    def should_publish(self, frame_index: int) -> bool:
        return frame_index % self.publish_every == 0

    def publish(
        self,
        camera_index: int,
        frame_seq: int,
        gray: Any,
        detection: Any,
        payload: object | None,
        latency_ms: float,
    ) -> None:
        cv2 = self._import_cv2()
        annotated = draw_annotated_detection(gray, payload)
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.jpeg_quality)]
        ok, encoded = cv2.imencode(".jpg", annotated, encode_params)
        if not ok:
            return
        now = time.perf_counter()
        self._frame_times.append(now)
        if len(self._frame_times) > 30:
            self._frame_times.pop(0)
        _record_frame(
            self.state,
            camera_index=camera_index,
            frame_seq=frame_seq,
            detection_dictionary=detection.dictionary,
            marker_count=detection.marker_count,
            corner_count=detection.corner_count,
            latency_ms=latency_ms,
            sharpness=_sharpness_score(cv2, gray),
            capture_fps=_fps_from_times(self._frame_times),
            frame_jpeg=bytes(encoded),
        )

    def _import_cv2(self):
        if self._cv2 is None:
            import cv2  # type: ignore[import-not-found]

            self._cv2 = cv2
        return self._cv2


def _fps_from_times(times: list[float]) -> float:
    if len(times) < 2:
        return 0.0
    elapsed = times[-1] - times[0]
    return (len(times) - 1) / elapsed if elapsed > 0.0 else 0.0


def _sharpness_score(cv2, gray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())
