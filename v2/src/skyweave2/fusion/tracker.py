"""EKF track lifecycle: six-state CV, refined-XYZ update, NIS gating.

Frozen decisions honored:
- Confirmation by 3-camera support in one batch (route A) OR consistent
  2-camera support across N CONSECUTIVE EVENT-TIME batches (route B); a
  silent batch (no observations anywhere) breaks the streak exactly like a
  missed one — timers advance in event time, not in processed-batch count.
- Lifecycle: tentative, confirmed, coast, reacquired, deleted, plus
  SUPPRESSED — a clutter-judged track keeps claiming its gated
  observations (blocking re-birth) but publishes nothing. The suppression
  mechanism ships now; the clutter judgment that calls it waits for real
  sky data.
- Every update — accepted or gated — appends a frozen-contract
  ``TrackUpdateRecord`` with the exact consumed obs_ids.
- Implausible innovations (NIS above the gate) never pull the state: the
  NIS test runs on PREDICTED COPIES; track state commits only on
  acceptance (a gated update that mutated the prediction would compound
  into a rejection death-spiral — found by test F6).

Determinism: tracks iterate in track_id order, ids are a monotonic counter
in group order, all arithmetic is replay-exact (test F5).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave2.contracts import (
    ClockDomain,
    LocalizationResult,
    Track,
    TrackState,
    TrackUpdateRecord,
)
from skyweave2.fusion.association import CandidateGroup, TrackSeed
from skyweave2.fusion.config import FusionConfig


@dataclass
class _TrackData:
    track_id: int
    x: np.ndarray  # (6,)
    p: np.ndarray  # (6, 6)
    status: TrackState
    created_ts_ns: int
    last_update_ts_ns: int
    last_batch_index: int  # newest event-time batch this track has lived through
    last_support_index: int  # newest batch with an ACCEPTED update
    update_count: int = 0
    miss_count: int = 0
    consecutive_two_cam: int = 0
    coast_batches: int = 0
    suppressed: bool = False
    suppressed_batches: int = 0
    # Systematic channel carried through from the measurement that last
    # updated this track (D0: reported beside the state, NEVER folded into
    # the filter's covariance).
    systematic_bound: tuple[float, float, float] = (0.0, 0.0, 0.0)
    systematic_sources: tuple = ()


def _transition(dt_s: float) -> np.ndarray:
    f = np.eye(6)
    f[0:3, 3:6] = np.eye(3) * dt_s
    return f


def _process_noise(dt_s: float, accel_psd: float) -> np.ndarray:
    q = np.zeros((6, 6))
    dt2, dt3 = dt_s * dt_s, dt_s * dt_s * dt_s
    q[0:3, 0:3] = np.eye(3) * (dt3 / 3.0)
    q[0:3, 3:6] = np.eye(3) * (dt2 / 2.0)
    q[3:6, 0:3] = np.eye(3) * (dt2 / 2.0)
    q[3:6, 3:6] = np.eye(3) * dt_s
    return q * accel_psd


class Tracker:
    def __init__(self, config: FusionConfig, session_uuid: str):
        self._config = config.tracker
        self._session = session_uuid
        self._tracks: dict[int, _TrackData] = {}
        self._next_id = 0
        self.records: list[TrackUpdateRecord] = []

    # ---- inspection -----------------------------------------------------
    def live_tracks(self) -> list[_TrackData]:
        return [
            t for t in sorted(self._tracks.values(), key=lambda t: t.track_id)
            if t.status != TrackState.DELETED
        ]

    def statuses(self) -> dict[int, TrackState]:
        return {t.track_id: t.status for t in sorted(self._tracks.values(),
                                                     key=lambda t: t.track_id)}

    def suppress(self, track_id: int) -> None:
        """External clutter judgment: mechanism only in D5."""
        track = self._tracks[track_id]
        track.suppressed = True
        track.suppressed_batches = 0

    # ---- pipeline interface --------------------------------------------
    def seeds(self, ts_ns: int) -> list[TrackSeed]:
        """Predicted positions for association gating (suppressed included:
        that is what makes suppression consume observations)."""
        out = []
        for track in self.live_tracks():
            dt = max((ts_ns - track.last_update_ts_ns) * 1e-9, 0.0)
            predicted = track.x[0:3] + track.x[3:6] * dt
            out.append(
                TrackSeed(
                    track_id=track.track_id,
                    position=predicted,
                    suppressed=track.suppressed,
                )
            )
        return out

    def _measurement(self, result: LocalizationResult) -> tuple[np.ndarray, np.ndarray]:
        z = np.asarray(result.position, dtype=np.float64)
        r = result.covariance_matrix() + np.eye(3) * self._config.measurement_var_floor_m2
        return z, r

    def _record(self, track_id: int, ts_ns: int, result: LocalizationResult,
                accepted: bool, nis: float | None) -> None:
        self.records.append(
            TrackUpdateRecord(
                track_id=track_id,
                ts_ns=ts_ns,
                clock_domain=ClockDomain(result.clock_domain),
                obs_ids=list(result.obs_ids),
                accepted=accepted,
                nis=nis,
            )
        )

    def _birth(self, group: CandidateGroup, result: LocalizationResult,
               ts_ns: int, batch_index: int) -> int:
        z, r = self._measurement(result)
        x = np.zeros(6)
        x[0:3] = z
        p = np.zeros((6, 6))
        p[0:3, 0:3] = r
        p[3:6, 3:6] = np.eye(3) * self._config.initial_velocity_var
        track = _TrackData(
            track_id=self._next_id,
            x=x,
            p=p,
            status=TrackState.TENTATIVE,
            created_ts_ns=ts_ns,
            last_update_ts_ns=ts_ns,
            last_batch_index=batch_index,
            last_support_index=batch_index,
            update_count=1,
        )
        self._next_id += 1
        track.systematic_bound = tuple(float(v) for v in result.systematic_bound)
        track.systematic_sources = tuple(result.systematic_sources)
        self._tracks[track.track_id] = track
        self._confirmation_step(track, len(set(group.camera_ids)), batch_index,
                                streak_continues=True)
        self._record(track.track_id, ts_ns, result, True, None)
        return track.track_id

    def _confirmation_step(self, track: _TrackData, n_cameras: int,
                           batch_index: int, streak_continues: bool) -> None:
        """Frozen confirmation routes; the 2-camera streak requires strictly
        consecutive event-time batches."""
        if track.suppressed:
            track.last_support_index = batch_index
            return
        if n_cameras >= self._config.confirm_min_cameras:
            track.consecutive_two_cam = 0
            if track.status == TrackState.TENTATIVE:
                track.status = TrackState.CONFIRMED
            elif track.status == TrackState.COAST:
                track.status = TrackState.REACQUIRED
        else:
            track.consecutive_two_cam = (
                track.consecutive_two_cam + 1 if streak_continues else 1
            )
            if track.consecutive_two_cam >= self._config.confirm_two_cam_batches:
                if track.status == TrackState.TENTATIVE:
                    track.status = TrackState.CONFIRMED
                elif track.status == TrackState.COAST:
                    track.status = TrackState.REACQUIRED
        track.last_support_index = batch_index

    def _update(self, track: _TrackData, group: CandidateGroup,
                result: LocalizationResult, ts_ns: int, batch_index: int) -> bool:
        # Predict on COPIES: a gated update must leave the stored state
        # exactly as it was (F6).
        dt = (ts_ns - track.last_update_ts_ns) * 1e-9
        if dt > 0.0:
            f = _transition(dt)
            x_pred = f @ track.x
            p_pred = f @ track.p @ f.T + _process_noise(dt, self._config.accel_psd)
        else:
            x_pred = track.x.copy()
            p_pred = track.p.copy()
        z, r = self._measurement(result)
        innovation = z - x_pred[0:3]
        s = p_pred[0:3, 0:3] + r
        s_inv = np.linalg.inv(s)
        nis = float(innovation @ s_inv @ innovation)
        accepted = nis <= self._config.nis_gate
        self._record(track.track_id, ts_ns, result, accepted, nis)
        if not accepted:
            return False
        h = np.zeros((3, 6))
        h[0:3, 0:3] = np.eye(3)
        k = p_pred @ h.T @ s_inv
        track.x = x_pred + k @ innovation
        track.p = (np.eye(6) - k @ h) @ p_pred
        track.p = 0.5 * (track.p + track.p.T)
        track.last_update_ts_ns = ts_ns
        track.systematic_bound = tuple(float(v) for v in result.systematic_bound)
        track.systematic_sources = tuple(result.systematic_sources)
        track.update_count += 1
        track.miss_count = 0
        track.coast_batches = 0
        if track.status == TrackState.REACQUIRED:
            track.status = TrackState.CONFIRMED
        streak_continues = track.last_support_index == batch_index - 1
        self._confirmation_step(track, len(set(group.camera_ids)), batch_index,
                                streak_continues)
        return True

    def _advance_misses(self, track: _TrackData, misses: int) -> None:
        """Apply N event-time misses (silent or unsupported batches)."""
        for _ in range(misses):
            if track.status == TrackState.DELETED:
                return
            track.miss_count += 1
            track.consecutive_two_cam = 0
            if track.status == TrackState.TENTATIVE:
                if track.miss_count >= self._config.coast_after_misses:
                    track.status = TrackState.DELETED
            elif track.status in (TrackState.CONFIRMED, TrackState.REACQUIRED):
                if track.miss_count >= self._config.coast_after_misses:
                    track.status = TrackState.COAST
                    track.coast_batches = 0
            elif track.status == TrackState.COAST:
                track.coast_batches += 1
                if track.coast_batches >= self._config.delete_after_coast:
                    track.status = TrackState.DELETED

    def update_batch(
        self,
        ts_ns: int,
        results: list[tuple[CandidateGroup, LocalizationResult]],
        batch_index: int,
    ) -> list[Track]:
        """Consume one engine batch; returns the published tracks.

        ``batch_index`` is the aligner's event-time index — required, so the
        event-time gap semantics never silently fall back to a constant.
        """

        # Event-time gap handling BEFORE updates: silent batches count as
        # misses, so a track can coast (and then legitimately REACQUIRE on
        # this batch's update).
        for track in self.live_tracks():
            gap = batch_index - track.last_batch_index - 1
            if gap <= 0:
                continue
            if track.suppressed:
                track.suppressed_batches += gap
                if track.suppressed_batches > self._config.suppress_lifetime_batches:
                    track.status = TrackState.DELETED
            else:
                self._advance_misses(track, gap)

        updated: set[int] = set()
        for group, result in results:
            if group.seed_track_id is not None and group.seed_track_id in self._tracks:
                track = self._tracks[group.seed_track_id]
                if track.status == TrackState.DELETED:
                    # The seed's track died in the gap loop above. Its
                    # solve-validated observations must not vanish (review
                    # finding): they birth a fresh track with a full audit
                    # trail instead of being silently swallowed.
                    if not group.suppressed_seed:
                        updated.add(self._birth(group, result, ts_ns, batch_index))
                    continue
                if self._update(track, group, result, ts_ns, batch_index):
                    updated.add(track.track_id)
            elif group.source in ("direct", "voxel"):
                updated.add(self._birth(group, result, ts_ns, batch_index))

        for track in self.live_tracks():
            if track.suppressed:
                track.suppressed_batches += 1
                if track.suppressed_batches > self._config.suppress_lifetime_batches:
                    track.status = TrackState.DELETED
            elif track.track_id not in updated:
                self._advance_misses(track, 1)
            track.last_batch_index = batch_index

        published: list[Track] = []
        # The frozen Track contract has no systematic field, so the bound
        # travels beside the published objects in this per-batch map
        # (D0 forbids contract edits; D6.1 forbids hiding the channel).
        self.published_systematic = {}
        for track in self.live_tracks():
            if track.suppressed:
                continue
            if track.status not in (TrackState.CONFIRMED, TrackState.REACQUIRED):
                continue
            # Publish-time prediction (working plan §10: the filter "predicts
            # to publish time"): a track without an accepted update this
            # batch would otherwise publish a STALE state — at gate speed
            # that is ~1 m of pure timestamp-mismatch error per missed
            # batch, found by the D5.2 decomposition. The stored state is
            # NOT mutated; prediction happens on copies, and the next
            # accepted update still predicts from the true last update.
            x_pub, p_pub = track.x, track.p
            dt = (ts_ns - track.last_update_ts_ns) * 1e-9
            if dt > 0.0:
                f = _transition(dt)
                x_pub = f @ track.x
                p_pub = f @ track.p @ f.T + _process_noise(dt, self._config.accel_psd)
            self.published_systematic[track.track_id] = (
                track.systematic_bound, track.systematic_sources
            )
            published.append(
                Track(
                    track_id=track.track_id,
                    session_uuid=self._session,
                    state=tuple(float(v) for v in x_pub),
                    covariance=tuple(float(v) for v in p_pub.reshape(-1)),
                    status=track.status,
                    created_ts_ns=track.created_ts_ns,
                    last_update_ts_ns=track.last_update_ts_ns,
                    update_count=track.update_count,
                    miss_count=track.miss_count,
                )
            )
        return published
