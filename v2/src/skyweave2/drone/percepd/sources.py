"""Frame sources for percepd v1 (B-D2; DRONE_CONTRACTS_D0.md [C1]).

A source hands out BGR frames, each stamped with ``t_cap``: board-monotonic
milliseconds read from the injected clock right at the grab ([C1]).

- :class:`CameraSource`: a V4L2/UVC camera through OpenCV. The flight mode is
  YUYV 1920 x 1200 (brief §2).
- :class:`VideoSource` and :class:`ImageSource`: a video file, or an image
  file or folder, for bench tests and CI. They are paced at ``fps`` so they
  behave like a camera.

What the stamp is, and what it ignores. For the camera, ``t_cap`` is taken
when ``VideoCapture.grab()`` returns, which is when the kernel hands the
finished frame to this process. It is later than the true capture time by:
the exposure (the stamp is after the end of exposure, not at mid-exposure),
the sensor readout, the USB transfer, the time the frame waits in the driver
queue, and the scheduling delay of the grab thread. None of these is measured
(E1-F1 stays open). The grab thread reads every frame as it arrives, so the
queue wait is small while that thread keeps up. As a bench aid the camera
source also reads the driver's own buffer timestamp (OpenCV
``CAP_PROP_POS_MSEC``; for ``uvcvideo`` it is ``CLOCK_MONOTONIC`` at the
start of the frame's USB transfer, not verified on the Cubie) and keeps
``stamp - driver timestamp`` in :attr:`CameraSource.driver_lag_ms`; percepd
logs it and never sends it.

For file sources ``t_cap`` is the read time: the stamps are real board time,
not the time the pictures were taken.

A source failure is :class:`SourceError`; the caller closes the source, backs
off and opens it again. The end of a file source is :class:`EndOfSource`.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from skyweave2.drone.types import Clock

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp")


class SourceError(RuntimeError):
    """The source cannot open or cannot deliver a frame now. Retry later."""


class EndOfSource(Exception):  # noqa: N818 - a normal end, not an error
    """A file source has no more frames."""


@dataclass(frozen=True, kw_only=True)
class Frame:
    """One grabbed frame: BGR ``H x W x 3`` uint8, and its capture stamp."""

    image: np.ndarray
    t_cap: int  # board ms ([C1])


class FrameSource(Protocol):
    def open(self) -> None:
        """Open or reopen. Raises :class:`SourceError` when not possible now."""

    def read(self) -> Frame:
        """Block until the next frame. Raises :class:`SourceError` on a failed
        grab and :class:`EndOfSource` at the end of a file source."""

    def close(self) -> None:
        """Release the device or file. Safe to call twice."""


def board_clock() -> Clock:
    """[C1] board ms on ``CLOCK_MONOTONIC``, the domain of the companion's
    clock (``companiond.live_clock``). percepd's one wall-clock read."""
    return lambda: time.monotonic_ns() // 1_000_000


class _Pacer:
    """Holds a file source to ``fps`` frames per second. ``fps <= 0`` = no wait."""

    def __init__(self, fps: float, clock: Clock, sleep: Callable[[float], Any]) -> None:
        self._period_ms = 1000.0 / fps if fps > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._due: float | None = None

    def wait(self) -> None:
        if self._period_ms <= 0.0:
            return
        now = self._clock()
        if self._due is not None and now < self._due:
            self._sleep((self._due - now) / 1000.0)
            self._due += self._period_ms
        else:
            self._due = now + self._period_ms  # first frame, or late: no burst


class CameraSource:
    """A V4L2/UVC camera through OpenCV."""

    def __init__(
        self,
        device: str,
        *,
        width: int = 1920,
        height: int = 1200,
        fps: float = 80.0,
        pixfmt: str = "YUYV",
        clock: Clock,
    ) -> None:
        if len(pixfmt) != 4:
            raise ValueError("pixfmt is a four-character code, for example YUYV")
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.pixfmt = pixfmt
        self._clock = clock
        self._cap: Any = None
        self.driver_lag_ms: int | None = None

    def open(self) -> None:
        import cv2

        self.close()
        if not self.device.isdigit() and not os.path.exists(self.device):
            raise SourceError(f"camera {self.device} is not present")
        api = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
        cap = cv2.VideoCapture(int(self.device) if self.device.isdigit() else self.device, api)
        if not cap.isOpened():
            cap.release()
            raise SourceError(f"camera {self.device} did not open")
        # Pixel format first: the sizes and rates a UVC camera offers depend on it.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.pixfmt))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        got_fps = float(cap.get(cv2.CAP_PROP_FPS))
        code = int(cap.get(cv2.CAP_PROP_FOURCC))
        got_fmt = "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))
        asked = (self.width, self.height, self.pixfmt)
        level = logging.INFO if (got_w, got_h, got_fmt) == asked else logging.WARNING
        log.log(
            level,
            "camera %s: asked %s %dx%d@%g, got %s %dx%d@%g",
            self.device,
            self.pixfmt,
            self.width,
            self.height,
            self.fps,
            got_fmt,
            got_w,
            got_h,
            got_fps,
        )
        self._cap = cap

    def read(self) -> Frame:
        import cv2

        cap = self._cap
        if cap is None:
            raise SourceError("camera is not open")
        if not cap.grab():
            raise SourceError(f"camera {self.device}: grab failed")
        t_cap = self._clock()  # [C1]: right at the grab, before the color conversion
        ok, image = cap.retrieve()
        if not ok or image is None:
            raise SourceError(f"camera {self.device}: frame not readable")
        lag = t_cap - cap.get(cv2.CAP_PROP_POS_MSEC)
        self.driver_lag_ms = int(lag) if 0 <= lag < 1000 else None
        return Frame(image=image, t_cap=t_cap)

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class VideoSource:
    """A video file, paced at ``fps``. ``loop`` starts over at the end."""

    def __init__(
        self,
        path: str | Path,
        *,
        fps: float = 10.0,
        loop: bool = False,
        clock: Clock,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.path = Path(path)
        self.loop = loop
        self._clock = clock
        self._pacer = _Pacer(fps, clock, sleep)
        self._cap: Any = None
        self._read_this_pass = 0

    def open(self) -> None:
        import cv2

        self.close()
        if not self.path.is_file():
            raise SourceError(f"video file not found: {self.path}")
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            cap.release()
            raise SourceError(f"video file did not open: {self.path}")
        self._cap = cap
        self._read_this_pass = 0

    def read(self) -> Frame:
        if self._cap is None:
            raise SourceError("video file is not open")
        self._pacer.wait()
        if not self._cap.grab():
            if not (self.loop and self._read_this_pass > 0):
                raise EndOfSource
            self.open()
            if not self._cap.grab():
                raise SourceError(f"video file did not restart: {self.path}")
        t_cap = self._clock()
        ok, image = self._cap.retrieve()
        if not ok or image is None:
            raise SourceError(f"video frame not readable: {self.path}")
        self._read_this_pass += 1
        return Frame(image=image, t_cap=t_cap)

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class ImageSource:
    """One image file, or the images of a folder in name order, paced at
    ``fps``. ``loop`` starts over at the end. A file that does not decode is
    logged and skipped."""

    def __init__(
        self,
        path: str | Path,
        *,
        fps: float = 10.0,
        loop: bool = False,
        clock: Clock,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.path = Path(path)
        self.loop = loop
        self._clock = clock
        self._pacer = _Pacer(fps, clock, sleep)
        self._files: list[Path] | None = None
        self._next = 0
        self._read_this_pass = 0

    def open(self) -> None:
        if self.path.is_dir():
            files = sorted(
                p for p in self.path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file()
            )
        elif self.path.is_file():
            files = [self.path]
        else:
            raise SourceError(f"image path not found: {self.path}")
        if not files:
            raise SourceError(f"no images ({', '.join(IMAGE_SUFFIXES)}) in {self.path}")
        self._files = files
        self._next = 0
        self._read_this_pass = 0

    def read(self) -> Frame:
        import cv2

        if self._files is None:
            raise SourceError("image source is not open")
        while True:
            if self._next >= len(self._files):
                if not self.loop:
                    raise EndOfSource
                if self._read_this_pass == 0:
                    raise SourceError(f"no readable image in {self.path}")
                self._next = 0
                self._read_this_pass = 0
            path = self._files[self._next]
            self._next += 1
            self._pacer.wait()
            t_cap = self._clock()
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                log.warning("image %s does not decode; skipped", path)
                continue
            self._read_this_pass += 1
            return Frame(image=image, t_cap=t_cap)

    def close(self) -> None:
        self._files = None
