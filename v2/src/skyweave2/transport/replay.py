"""Recorded-observation replay in the two frozen pacing modes (D7 scope 4).

This is the ``RecordedYFrameSource`` shape at the observation layer: a
recorded stream in, capture events out, paced either by the synthetic times
the recording carries (DETERMINISTIC) or against the local clock (WALL_CLOCK).
It owns I/O and timing only — it never decides identity, never reorders, and
never edits a measurement.

Splitting an arrival-ordered observation list into datagrams is the one place
that could quietly change the stream, so the rule is explicit: a new capture
event starts when the (camera, session, frame_seq) identity changes, OR when
an ``obs_id`` repeats inside the event under construction. The second clause
is what keeps a transport-level duplicate a DUPLICATE DATAGRAM rather than
collapsing into one datagram carrying the same observation twice — which
would silently convert a duplication fault into a malformed packet and change
what the aligner sees.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum

from skyweave2.contracts import FrameEnvelope, Observation2D


class PacingMode(str, Enum):
    """The two frozen replay modes."""

    # Synthetic capture times are carried verbatim and nothing sleeps: the
    # delivered-event stream is a pure function of the input.
    DETERMINISTIC = "deterministic"
    # Events are scheduled against the local monotonic clock, so real packet
    # age and pacing jitter become measurable.
    WALL_CLOCK = "wall_clock"


@dataclass(frozen=True)
class CaptureEvent:
    """One capture event: exactly one datagram's worth of measurement."""

    envelope: FrameEnvelope
    observations: tuple[Observation2D, ...]
    arrival_index: int  # position in the source stream, for audit

    @property
    def identity(self) -> tuple[int, str, int]:
        return (
            self.envelope.camera_id,
            self.envelope.session_uuid,
            self.envelope.frame_seq,
        )


def capture_events(observations: Sequence[Observation2D]) -> list[CaptureEvent]:
    """Split an ARRIVAL-ordered observation stream into capture events.

    Order is preserved exactly; no observation is added, removed, merged
    across identities, or sorted.
    """
    events: list[CaptureEvent] = []
    current: list[Observation2D] = []
    current_ids: set[int] = set()

    def flush() -> None:
        if current:
            events.append(
                CaptureEvent(
                    envelope=current[0].envelope,
                    observations=tuple(current),
                    arrival_index=len(events),
                )
            )

    for obs in observations:
        if current and (
            obs.envelope != current[0].envelope or obs.obs_id in current_ids
        ):
            flush()
            current, current_ids = [], set()
        current.append(obs)
        current_ids.add(obs.obs_id)
    flush()
    return events


@dataclass
class PacingStats:
    """What the pacer actually did — reported, never assumed."""

    mode: str = PacingMode.DETERMINISTIC.value
    events: int = 0
    observations: int = 0
    # WALL_CLOCK only: scheduled-vs-actual send time, nanoseconds.
    schedule_error_ns: list[int] = field(default_factory=list)
    # Monotonic send timestamps keyed by (camera_id, session_uuid, frame_seq).
    tx_monotonic_ns: dict[tuple[int, str, int], int] = field(default_factory=dict)
    # WALL_CLOCK only: the instant each event was DUE, same key. Packet age is
    # measured against this, not against the send time — otherwise pacing
    # jitter would be silently excluded from the age it helped cause.
    due_monotonic_ns: dict[tuple[int, str, int], int] = field(default_factory=dict)

    @property
    def max_schedule_error_ns(self) -> int:
        return max((abs(e) for e in self.schedule_error_ns), default=0)


class ObservationReplaySource:
    """Recorded observations -> paced capture events.

    ``monotonic_ns`` is injectable so a test can drive WALL_CLOCK mode
    without sleeping; ``sleep`` likewise. The default is the real clock.
    """

    def __init__(
        self,
        observations: Sequence[Observation2D],
        mode: PacingMode = PacingMode.DETERMINISTIC,
        speed: float = 1.0,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if speed <= 0.0:
            raise ValueError("replay speed must be positive")
        self.mode = mode
        self.speed = speed
        self.events = capture_events(observations)
        self._monotonic_ns = monotonic_ns
        self._sleep = sleep
        self.stats = PacingStats(mode=mode.value)

    @property
    def first_capture_ts_ns(self) -> int | None:
        return self.events[0].envelope.capture_ts_ns if self.events else None

    def scheduled_offset_ns(self, event: CaptureEvent) -> int:
        """Nanoseconds after replay start at which this event is due.

        Computed from the event's own capture time relative to the FIRST
        event's, so the recording's timing shape is preserved. Never negative:
        an event whose capture time precedes the first one (a reordered or
        rebooted stream) is due immediately rather than in the past.
        """
        base = self.first_capture_ts_ns
        if base is None:
            return 0
        offset = event.envelope.capture_ts_ns - base
        return max(int(offset / self.speed), 0)

    def replay(
        self, send: Callable[[CaptureEvent], None], start_epoch_ns: int | None = None
    ) -> PacingStats:
        """Pace the recorded events through ``send``.

        DETERMINISTIC never sleeps. WALL_CLOCK sleeps until each event's due
        time and records the scheduling error it actually achieved, so the
        report can state jitter as a measurement rather than a claim.

        ``start_epoch_ns`` is the SHARED time origin every camera paces from,
        and it is not optional in a multi-camera rig. Real nodes are PTP
        disciplined: they capture frame N at the same absolute instant, so
        their capture timestamps mean the same thing. Letting each replay
        process start its own clock at its own spawn time offsets the streams
        by the process skew while their capture times stay identical — the
        aligner then measures the skew as capture-time LATENESS and correctly
        throws the trailing cameras away. Defaults to "now" for the
        single-source case, where there is nothing to synchronise with.
        """
        start_ns = self._monotonic_ns() if start_epoch_ns is None else start_epoch_ns
        if self.mode is PacingMode.WALL_CLOCK:
            now_ns = self._monotonic_ns()
            if start_ns > now_ns:
                self._sleep((start_ns - now_ns) / 1e9)
        for event in self.events:
            if self.mode is PacingMode.WALL_CLOCK:
                due_ns = start_ns + self.scheduled_offset_ns(event)
                now_ns = self._monotonic_ns()
                if due_ns > now_ns:
                    self._sleep((due_ns - now_ns) / 1e9)
                actual_ns = self._monotonic_ns()
                self.stats.schedule_error_ns.append(actual_ns - due_ns)
                self.stats.due_monotonic_ns[event.identity] = due_ns
            else:
                actual_ns = self._monotonic_ns()
            self.stats.tx_monotonic_ns[event.identity] = actual_ns
            send(event)
            self.stats.events += 1
            self.stats.observations += len(event.observations)
        return self.stats

    def __iter__(self) -> Iterator[CaptureEvent]:
        return iter(self.events)
