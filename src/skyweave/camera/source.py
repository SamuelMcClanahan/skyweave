from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from skyweave.camera.opencv_runtime import configure_opencv_runtime
from skyweave.timestamps import monotonic_ns


@dataclass(frozen=True)
class CameraFrame:
    camera_id: int
    frame_seq: int
    capture_ts_ns: int
    gray: np.ndarray

    @property
    def image_height(self) -> int:
        return int(self.gray.shape[0])

    @property
    def image_width(self) -> int:
        return int(self.gray.shape[1])


class CameraOpenError(RuntimeError):
    pass


class ArrayCameraSource:
    def __init__(
        self,
        camera_id: int,
        frames: Iterable[np.ndarray],
        timestamps_ns: Iterable[int] | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.frames = [frame_to_gray(frame) for frame in frames]
        self.timestamps_ns = list(timestamps_ns) if timestamps_ns is not None else None
        self.frame_seq = 0

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def read(self) -> CameraFrame | None:
        if self.frame_seq >= len(self.frames):
            return None
        ts_ns = (
            self.timestamps_ns[self.frame_seq]
            if self.timestamps_ns is not None and self.frame_seq < len(self.timestamps_ns)
            else monotonic_ns()
        )
        frame = CameraFrame(
            camera_id=self.camera_id,
            frame_seq=self.frame_seq,
            capture_ts_ns=int(ts_ns),
            gray=self.frames[self.frame_seq],
        )
        self.frame_seq += 1
        return frame


class OpenCVCameraSource:
    def __init__(
        self,
        camera_id: int,
        device: str | int,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        fourcc: str | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc = fourcc
        self.frame_seq = 0
        self._cap = None
        self._cv2 = None

    def open(self) -> None:
        if self._cap is not None:
            return
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CameraOpenError("OpenCV is required for live camera capture. Install with .[camera].") from exc
        configure_opencv_runtime(cv2)

        device_arg = self._video_capture_device(self.device)
        use_v4l2 = isinstance(device_arg, int) or str(device_arg).startswith("/dev/")
        api_preference = cv2.CAP_V4L2 if use_v4l2 else cv2.CAP_ANY
        cap = cv2.VideoCapture(device_arg, api_preference)
        if self.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc.upper()))
        if self.width is not None:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
        if self.height is not None:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
        if self.fps is not None:
            cap.set(cv2.CAP_PROP_FPS, float(self.fps))

        if not cap.isOpened():
            cap.release()
            raise CameraOpenError(f"failed to open camera device {self.device!r}")

        self._cap = cap
        self._cv2 = cv2

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def read(self) -> CameraFrame | None:
        capture_ts_ns = self.grab()
        if capture_ts_ns is None:
            return None
        return self.retrieve(capture_ts_ns)

    def grab(self) -> int | None:
        self.open()
        assert self._cap is not None
        ok = self._cap.grab()
        capture_ts_ns = monotonic_ns()
        return capture_ts_ns if ok else None

    def retrieve(self, capture_ts_ns: int | None = None) -> CameraFrame | None:
        frame, _, _ = self.retrieve_timed(capture_ts_ns)
        return frame

    def retrieve_timed(self, capture_ts_ns: int | None = None) -> tuple[CameraFrame | None, float, float]:
        self.open()
        assert self._cap is not None
        start = time.perf_counter()
        ok, frame = self._cap.retrieve()
        retrieve_ms = (time.perf_counter() - start) * 1000.0
        if not ok or frame is None:
            return None, retrieve_ms, 0.0

        if capture_ts_ns is None:
            capture_ts_ns = monotonic_ns()
        start = time.perf_counter()
        gray = frame_to_gray(frame, self._cv2)
        gray_ms = (time.perf_counter() - start) * 1000.0
        result = CameraFrame(
            camera_id=self.camera_id,
            frame_seq=self.frame_seq,
            capture_ts_ns=capture_ts_ns,
            gray=gray,
        )
        self.frame_seq += 1
        return result, retrieve_ms, gray_ms

    def effective_settings(self) -> dict[str, float]:
        self.open()
        assert self._cap is not None
        cv2 = self._cv2
        return {
            "width": float(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": float(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": float(self._cap.get(cv2.CAP_PROP_FPS)),
            "fourcc": float(self._cap.get(cv2.CAP_PROP_FOURCC)),
        }

    @staticmethod
    def _video_capture_device(device: str | int) -> str | int:
        if isinstance(device, int):
            return device
        value = str(device)
        if value.isdigit():
            return int(value)
        if value.startswith("/"):
            return str(Path(value))
        return value


def frame_to_gray(frame: np.ndarray, cv2_module=None) -> np.ndarray:
    arr = np.asarray(frame)
    if arr.ndim == 2:
        return np.ascontiguousarray(arr.astype(np.uint8, copy=False))
    if arr.ndim != 3:
        raise ValueError(f"unsupported frame shape {arr.shape}")

    channels = arr.shape[2]
    if channels == 1:
        return np.ascontiguousarray(arr[:, :, 0].astype(np.uint8, copy=False))
    if channels == 2:
        return np.ascontiguousarray(arr[:, :, 0].astype(np.uint8, copy=False))
    if channels == 3:
        if cv2_module is not None:
            return np.ascontiguousarray(cv2_module.cvtColor(arr, cv2_module.COLOR_BGR2GRAY))
        b = arr[:, :, 0].astype(np.float32)
        g = arr[:, :, 1].astype(np.float32)
        r = arr[:, :, 2].astype(np.float32)
        return np.ascontiguousarray(np.clip(0.114 * b + 0.587 * g + 0.299 * r, 0, 255).astype(np.uint8))
    if channels == 4:
        if cv2_module is not None:
            return np.ascontiguousarray(cv2_module.cvtColor(arr, cv2_module.COLOR_BGRA2GRAY))
        b = arr[:, :, 0].astype(np.float32)
        g = arr[:, :, 1].astype(np.float32)
        r = arr[:, :, 2].astype(np.float32)
        return np.ascontiguousarray(np.clip(0.114 * b + 0.587 * g + 0.299 * r, 0, 255).astype(np.uint8))

    raise ValueError(f"unsupported frame channel count {channels}")
