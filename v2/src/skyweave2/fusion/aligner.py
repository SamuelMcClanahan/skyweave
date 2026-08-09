"""Event-time batching: observations grouped by capture time, never arrival.

Batches are one frame period wide (config). Each observation is keyed by its
envelope's ``capture_ts_ns`` and validated through the frozen
``SessionRegistry`` (a node reboot is a new session, accepted cleanly; a
same-session sequence regression is flagged and quarantined).

Late observations — capture time older than the lateness window behind the
newest capture time seen — are recorded with a reason and NEVER enter a
current solve (working plan §6; test F1). Stale/duplicate/anomalous
envelopes are likewise recorded, not solved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skyweave2.contracts import Observation2D
from skyweave2.contracts.time import SessionEvent, SessionRegistry
from skyweave2.fusion.config import FusionConfig


def obs_key(obs: Observation2D) -> str:
    """The audit identity of one observation (D0 obs_ids format)."""
    e = obs.envelope
    return f"{e.camera_id}:{e.session_uuid}:{e.frame_seq}:{obs.obs_id}"


@dataclass(frozen=True)
class ExcludedObservation:
    obs: Observation2D
    reason: str  # "late" | "stale_or_duplicate" | "sequence_anomaly"


@dataclass
class Batch:
    """All observations of one event-time window, across cameras."""

    batch_index: int  # capture_ts_ns // batch_period_ns
    observations: list[Observation2D] = field(default_factory=list)

    @property
    def ts_ns(self) -> int:
        """Representative event time: lower median of member capture times."""
        times = sorted(o.envelope.capture_ts_ns for o in self.observations)
        return times[(len(times) - 1) // 2]

    def sorted_observations(self) -> list[Observation2D]:
        return sorted(
            self.observations,
            key=lambda o: (o.envelope.camera_id, o.envelope.frame_seq, o.obs_id),
        )


class EventAligner:
    """Streaming aligner: feed observations in ARRIVAL order, collect batches
    in EVENT-TIME order once their emit horizon has passed."""

    def __init__(self, config: FusionConfig):
        self._config = config.aligner
        self._registry = SessionRegistry()
        self._pending: dict[int, Batch] = {}
        self._emitted: set[int] = set()
        self._newest_capture_ns: int | None = None
        self._seen: dict[tuple[int, str], set[tuple[int, int]]] = {}
        self.excluded: list[ExcludedObservation] = []

    def _batch_index(self, ts_ns: int) -> int:
        return ts_ns // self._config.batch_period_ns

    def offer(self, obs: Observation2D) -> None:
        """Accept one observation from the wire (arrival order irrelevant).

        The frozen SessionRegistry is the session authority (a reboot is a
        new session, accepted cleanly). Its sequence-regression flag is
        refined by the §6 reorder buffer: a frame arriving out of order but
        never seen before is REORDER (accepted into its event-time batch);
        only a genuinely repeated (frame_seq, obs_id) is quarantined.
        """
        event = self._registry.classify(obs.envelope)
        seen_key = (obs.envelope.camera_id, obs.envelope.session_uuid)
        obs_ident = (obs.envelope.frame_seq, obs.obs_id)
        seen = self._seen.setdefault(seen_key, set())
        if event in (SessionEvent.STALE_OR_DUPLICATE, SessionEvent.SEQUENCE_ANOMALY):
            if obs_ident in seen:
                self.excluded.append(ExcludedObservation(obs, event.value))
                return
            # Unseen lower sequence: transport reorder, not corruption.
        elif obs_ident in seen:
            self.excluded.append(
                ExcludedObservation(obs, SessionEvent.STALE_OR_DUPLICATE.value)
            )
            return
        seen.add(obs_ident)
        ts = obs.envelope.capture_ts_ns
        if self._newest_capture_ns is None or ts > self._newest_capture_ns:
            self._newest_capture_ns = ts
        if ts < self._newest_capture_ns - self._config.lateness_ns:
            self.excluded.append(ExcludedObservation(obs, "late"))
            return
        index = self._batch_index(ts)
        if index in self._emitted:
            # Its batch already solved: late by definition, never re-opened.
            self.excluded.append(ExcludedObservation(obs, "late"))
            return
        self._pending.setdefault(index, Batch(batch_index=index)).observations.append(obs)

    def ready_batches(self) -> list[Batch]:
        """Batches whose window is past the emit horizon, in event-time order."""
        if self._newest_capture_ns is None:
            return []
        out: list[Batch] = []
        horizon = self._newest_capture_ns - self._config.emit_horizon_ns
        for index in sorted(self._pending):
            batch_end = (index + 1) * self._config.batch_period_ns
            if batch_end <= horizon:
                out.append(self._pending.pop(index))
                self._emitted.add(index)
        return out

    def flush(self) -> list[Batch]:
        """End of stream: emit everything still pending, in order."""
        out = [self._pending.pop(i) for i in sorted(self._pending)]
        for batch in out:
            self._emitted.add(batch.batch_index)
        return out
