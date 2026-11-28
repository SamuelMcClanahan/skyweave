"""The percepd v1 loop: grab -> detect -> one [P1] packet per processed frame
(DRONE_CONTRACTS_D0.md [P1], [C1], [C10]; B-D2).

Two steps, each callable on its own (the tests drive them without threads):

- :meth:`Percepd.grab_step` reads one frame from the source and puts it in a
  one-frame slot. ``frame_seq`` counts grabbed frames, so a frame that is
  never processed shows as a gap ([P1]).
- :meth:`Percepd.process_step` takes the frame in the slot, runs the
  detector, builds the packet and sends it. A frame with no boxes still
  sends a packet ([P1]).

Keep-latest: the slot holds one frame. A new frame replaces an unprocessed
one (counted as a drop). Frames are never queued, so a slow detector makes
gaps in ``frame_seq``, not latency.

:meth:`Percepd.run` runs the grab step in its own thread and the process step
in the caller's thread, until the stop event is set. With ``every_frame`` it
runs both steps in turn in one thread and drops nothing; that mode is for
file sources (a camera read that way delivers stale frames with fresh
stamps).

Nothing here ends the process: a source that does not open, a failed grab, a
detector error and a send error are each logged, counted and survived. The
source is closed and opened again after a failure, with a wait that doubles
from ``backoff_min_s`` to ``backoff_max_s``.

Time ([C1]): the source stamps ``t_cap`` with the injected board clock at the
grab. The inference duration in the stats line comes from a separate timer;
it is process timing for the log and never enters a packet.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from skyweave2.drone.packets import DetectionPacket
from skyweave2.drone.percepd.detect import COCO_NAMES, Backend
from skyweave2.drone.percepd.packet import DEFAULT_MAX_BOXES, build_detection_packet
from skyweave2.drone.percepd.sources import EndOfSource, FrameSource
from skyweave2.drone.percepd.viewer import DEFAULT_VIEW_WIDTH, annotate, write_view
from skyweave2.drone.types import Clock

log = logging.getLogger(__name__)

_LOG_EVERY_MS = 1000  # a per-frame failure is logged at most this often


class Sender(Protocol):
    """Where packets go: ``skyweave2.drone.udp.UdpSender`` in the live process."""

    def send(self, packet: DetectionPacket) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class PipelineConfig:
    max_boxes: int = DEFAULT_MAX_BOXES
    every_frame: bool = False  # one thread, no drops (file sources)
    stats_interval_ms: int = 5000  # 0 = no stats log
    backoff_min_s: float = 0.5
    backoff_max_s: float = 5.0
    view_path: Path | None = None  # bench viewer; None = off
    view_every: int = 10
    view_width: int = DEFAULT_VIEW_WIDTH

    def __post_init__(self) -> None:
        if self.max_boxes < 0:
            raise ValueError("max_boxes must be >= 0")
        if self.view_every < 1:
            raise ValueError("view_every must be >= 1")
        if not 0.0 <= self.backoff_min_s <= self.backoff_max_s:
            raise ValueError("backoff needs 0 <= backoff_min_s <= backoff_max_s")


@dataclass(frozen=True, kw_only=True)
class _Grabbed:
    image: np.ndarray
    t_cap: int
    frame_seq: int


class LatestSlot:
    """A one-item mailbox. ``put`` replaces an item nobody took (a drop)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: _Grabbed | None = None
        self.dropped = 0

    def put(self, item: _Grabbed) -> None:
        with self._cond:
            if self._item is not None:
                self.dropped += 1
            self._item = item
            self._cond.notify()

    def take(self, timeout_s: float = 0.0) -> _Grabbed | None:
        """The waiting item, or ``None`` after ``timeout_s`` without one."""
        with self._cond:
            if self._item is None and timeout_s > 0:
                self._cond.wait(timeout_s)
            item, self._item = self._item, None
            return item

    def empty(self) -> bool:
        with self._cond:
            return self._item is None

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()


class Percepd:
    """Source, backend and sender wired into the two steps above."""

    def __init__(
        self,
        source: FrameSource,
        backend: Backend,
        sender: Sender,
        clock: Clock,
        config: PipelineConfig | None = None,
        *,
        names: Sequence[str] = COCO_NAMES,
        wait: Callable[[float], Any] = time.sleep,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.source = source
        self.backend = backend
        self.sender = sender
        self.clock = clock
        self.config = config if config is not None else PipelineConfig()
        self.names = names
        self.slot = LatestSlot()
        self._wait = wait
        self._timer = timer
        self._source_open = False
        self._backoff_s = self.config.backoff_min_s
        self._ended = threading.Event()
        self._last_log: dict[str, int] = {}
        # Counters, all since start.
        self.grabbed = 0  # frames read from the source; the next frame_seq
        self.processed = 0  # frames the detector finished
        self.sent = 0  # packets handed to the socket
        self.boxes_sent = 0
        self.grab_failures = 0  # failed opens and failed reads
        self.opens = 0  # successful opens (the first one included)
        self.detect_errors = 0
        self.send_errors = 0
        self.view_errors = 0
        self._infer_ms: list[float] = []  # this stats interval
        self._stats_t = clock()
        self._stats_prev = (0, 0, 0, 0)  # grabbed, sent, boxes_sent, dropped

    @property
    def dropped(self) -> int:
        """Frames replaced in the slot before the detector took them."""
        return self.slot.dropped

    # -- grab side ---------------------------------------------------------------

    def grab_step(self) -> bool:
        """Read one frame into the slot. ``False`` at the end of a file source."""
        if not self._source_open:
            try:
                self.source.open()
            except Exception as exc:  # noqa: BLE001 - any open failure is retried
                self._grab_failed("open", exc)
                return True
            self._source_open = True
            self.opens += 1
        try:
            frame = self.source.read()
        except EndOfSource:
            return False
        except Exception as exc:  # noqa: BLE001 - any read failure is retried
            self._grab_failed("read", exc)
            return True
        self._backoff_s = self.config.backoff_min_s
        item = _Grabbed(image=frame.image, t_cap=frame.t_cap, frame_seq=self.grabbed)
        self.grabbed += 1
        self.slot.put(item)
        return True

    def _grab_failed(self, what: str, exc: Exception) -> None:
        self.grab_failures += 1
        log.warning(
            "source %s failed (%s: %s); retry in %.1f s",
            what,
            type(exc).__name__,
            exc,
            self._backoff_s,
        )
        self._close_source()
        self._wait(self._backoff_s)
        self._backoff_s = min(self.config.backoff_max_s, max(self._backoff_s, 0.001) * 2.0)

    def _close_source(self) -> None:
        self._source_open = False
        try:
            self.source.close()
        except Exception:  # noqa: BLE001 - a close failure must not end the loop
            log.exception("source close failed")

    def _grab_loop(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set() and self.grab_step():
                pass
        finally:
            self._close_source()
            self._ended.set()
            self.slot.wake()

    # -- process side ------------------------------------------------------------

    def process_step(self, timeout_s: float = 0.0) -> DetectionPacket | None:
        """Process the frame in the slot, waiting up to ``timeout_s`` for one.
        Returns the packet built for it (sent or not), or ``None`` when there
        was no frame or the detector failed."""
        item = self.slot.take(timeout_s)
        packet = None
        if item is not None:
            try:
                packet = self._process(item)
            except Exception:  # noqa: BLE001 - the loop outlives any one frame
                self.detect_errors += 1
                if self._log_due("frame"):
                    log.exception("frame %d failed; loop continues", item.frame_seq)
        self._maybe_stats()
        return packet

    def _process(self, item: _Grabbed) -> DetectionPacket:
        t0 = self._timer()
        detections = self.backend.detect(item.image)
        self._infer_ms.append((self._timer() - t0) * 1000.0)
        self.processed += 1
        frame_h, frame_w = item.image.shape[:2]
        packet = build_detection_packet(
            detections,
            t_cap=item.t_cap,
            frame_seq=item.frame_seq,
            frame_w=frame_w,
            frame_h=frame_h,
            max_boxes=self.config.max_boxes,
        )
        if self.sender.send(packet):
            self.sent += 1
            self.boxes_sent += len(packet.boxes)
        else:
            self.send_errors += 1
            if self._log_due("send"):
                log.warning("detection packet not sent (%d send errors)", self.send_errors)
        cfg = self.config
        if cfg.view_path is not None and (self.processed - 1) % cfg.view_every == 0:
            try:
                caption = f"seq {item.frame_seq}  t_cap {item.t_cap}  boxes {len(packet.boxes)}"
                picture = annotate(
                    item.image, detections, caption=caption, width=cfg.view_width, names=self.names
                )
                write_view(cfg.view_path, picture)
            except Exception as exc:  # noqa: BLE001 - the viewer is a bench aid, never fatal
                self.view_errors += 1
                if self._log_due("view"):
                    log.warning("viewer picture not written: %s", exc)
        return packet

    def _log_due(self, key: str) -> bool:
        now = self.clock()
        last = self._last_log.get(key)
        if last is not None and now - last < _LOG_EVERY_MS:
            return False
        self._last_log[key] = now
        return True

    # -- stats -------------------------------------------------------------------

    def stats_line(self) -> str:
        """One line for the interval since the last call; resets the interval."""
        now = self.clock()
        dt_s = max(1e-3, (now - self._stats_t) / 1000.0)
        grabbed, sent, boxes, dropped = self.grabbed, self.sent, self.boxes_sent, self.dropped
        p_grabbed, p_sent, p_boxes, p_dropped = self._stats_prev
        infer, self._infer_ms = self._infer_ms, []
        self._stats_t = now
        self._stats_prev = (grabbed, sent, boxes, dropped)
        mean = sum(infer) / len(infer) if infer else 0.0
        line = (
            f"fps_in={(grabbed - p_grabbed) / dt_s:.1f} fps_out={(sent - p_sent) / dt_s:.1f} "
            f"infer_ms={mean:.1f} infer_max_ms={max(infer, default=0.0):.1f} "
            f"boxes={boxes - p_boxes} drops={dropped - p_dropped} | total: "
            f"grabbed={grabbed} sent={sent} drops={dropped} grab_fail={self.grab_failures} "
            f"opens={self.opens} detect_err={self.detect_errors} send_err={self.send_errors}"
        )
        if self.config.view_path is not None:
            line += f" view_err={self.view_errors}"
        lag = getattr(self.source, "driver_lag_ms", None)
        return line if lag is None else f"{line} driver_lag_ms={lag}"

    def _maybe_stats(self) -> None:
        every = self.config.stats_interval_ms
        if every > 0 and self.clock() - self._stats_t >= every:
            log.info("percepd %s", self.stats_line())

    # -- run -----------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Loop until ``stop`` is set, or until a file source ends."""
        self._wait = stop.wait
        self._stats_t = self.clock()
        try:
            if self.config.every_frame:
                try:
                    while not stop.is_set() and self.grab_step():
                        self.process_step()
                finally:
                    self._close_source()
            else:
                # Only the grab thread touches the source, so nothing closes a
                # camera under a blocked grab. Daemon: a grab stuck in the
                # driver cannot keep the process alive after SIGTERM.
                grabber = threading.Thread(
                    target=self._grab_loop, args=(stop,), name="percepd-grab", daemon=True
                )
                grabber.start()
                while not stop.is_set():
                    self.process_step(0.25)
                    if self._ended.is_set() and self.slot.empty():
                        break
                grabber.join(timeout=2.0)
        finally:
            log.info("percepd stopped; %s", self.stats_line())
