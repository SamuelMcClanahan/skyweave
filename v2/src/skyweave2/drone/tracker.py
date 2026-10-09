"""Tracker (DRONE_CONTRACTS_D0.md [K1]-[K4], [P2], [P2a]). PROVISIONAL.

This is a reimplementation of the tracker v0 *spec on record* (brief §2:
per-axis 2-state KF [pos, vel], constant velocity; IoU association; M-of-N
confirm; coasting with a miss cap). The tracker v0 SOURCE is missing from the
repo (brief §0), so nothing here is v0 code and nothing here inherits v0's
evidence: v0's numbers (1.4 px RMS, zero ghosts on 5 synthetic tests) are NOT
claims about this module. Every number in it is Provisional and must be
re-earned in the harness (contract §8, [S7] probe seeds only).

Why the shape:

- [K1] Four independent scalar Kalman filters per track, one per axis
  (box center ``u``, ``v``, width ``w``, height ``h``), each with state
  ``[pos, rate]`` and a constant-rate model. Process noise is the discrete
  piecewise-constant white-acceleration model, ``q = sigma_a^2``, so the
  ``*_px_s2`` config values read as acceleration standard deviations. The
  filters are plain float arithmetic: no numpy scalars leak into packets and
  the result is bit-identical run to run.
- Time: ``dt`` is the difference of consecutive accepted ``t_cap`` values
  (board ms, [C1]); the tracker never reads a clock. A detection whose
  ``t_cap`` is not newer than the last accepted one is ignored and counted in
  :attr:`Tracker.stale_dropped` ([P1]: consumers order by ``t_cap``).
- [K2] Association is greedy by descending IoU between each track's predicted
  box and each detection box, pairs below ``iou_min`` never associate, and
  ties break by (lower ``track_id``, lower box index), so the same packets
  give the same tracks. Unmatched boxes spawn tentative tracks in box order.
  A track confirms once it has ``m_confirm`` hits (not necessarily
  consecutive) within its first ``n_confirm`` frames, the birth frame being
  frame 1 and hit 1. A tentative track is deleted, with no packet in that
  frame, as soon as it can no longer reach ``m_confirm`` hits by frame
  ``n_confirm`` ([P2a]).
- [K3] / [P2a] A confirmed track coasts on a miss. While ``misses <=
  coast_cap`` it is emitted as ``coasting``; in the frame where ``misses``
  reaches ``coast_cap + 1`` it is emitted once more as ``coasting`` with
  that count and then deleted; its id never appears again. A hit while
  coasting returns it to ``confirmed`` under the same id.
- [K4] / [P2] Ids are ``id_base + n`` with ``n`` counting spawned tracks from
  1. Live, ``id_base = start_t_ms * ID_BASE_PER_MS``
  (:func:`id_base_from_start`), so a restarted tracker starts above every id
  its predecessor could have issued unless that predecessor spawned more than
  ``ID_BASE_PER_MS`` tracks per millisecond of its uptime.
- [P2] Output per accepted frame: one packet per live track, the frame's
  ``t_cap`` on every packet, sorted by ``track_id``. ``hits`` and ``misses``
  are consecutive counts ending at this frame.
- [P2] requires ``w, h > 0``; a constant-rate size filter can predict a
  non-positive size while coasting. When a predicted size falls below
  ``min_size_px`` the size is held at that floor and a shrinking size rate is
  zeroed (contract gap, reported; Provisional).

Not here, on purpose: no confidence threshold or duplicate-box suppression
(percepd's job), no track scoring, no Mahalanobis gating (E1-D2, deferred
with a trigger).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from skyweave2.drone.packets import Box, DetectionPacket, TrackPacket, TrackState

ID_BASE_PER_MS = 1000
"""[K4] id space reserved per millisecond of board clock between two tracker
starts (E1 build plan: live ``id_base = start_t_ms * 1000``)."""


def id_base_from_start(start_t_ms: int) -> int:
    """[K4] the live ``id_base`` for a tracker started at ``start_t_ms`` board ms."""
    if isinstance(start_t_ms, bool) or not isinstance(start_t_ms, int) or start_t_ms < 0:
        raise ValueError(f"start_t_ms must be an int >= 0, got {start_t_ms!r}")
    return start_t_ms * ID_BASE_PER_MS


_INT_FIELDS = ("id_base", "m_confirm", "n_confirm", "coast_cap")


@dataclass(frozen=True, kw_only=True)
class TrackerConfig:
    """Tracker constants. Every number is Provisional (brief §6 and E1).

    ``coast_cap`` must equal the companion's ([P2a]); it travels in the
    recording's ``meta.config`` ([R2]).
    """

    id_base: int  # required; live: id_base_from_start(start_t_ms) ([K4])
    m_confirm: int = 3  # brief §6, [K2]
    n_confirm: int = 5  # brief §6, [K2]
    coast_cap: int = 20  # brief §6, [P2a]
    iou_min: float = 0.1  # E1, [K2]
    accel_sigma_px_s2: float = 1000.0  # E1, [K1] u/v process noise
    size_rate_sigma_px_s2: float = 200.0  # E1, [K1] w/h process noise
    meas_sigma_px: float = 2.0  # E1, [K1] box center and size noise
    init_vel_sigma_px_s: float = 1000.0  # E1, [K1] u/v rate prior at birth
    init_size_rate_sigma_px_s: float = 100.0  # E1, [K1] w/h rate prior at birth
    min_size_px: float = 1.0  # E1, [P2] w, h > 0 floor while coasting

    def __post_init__(self) -> None:
        for f in fields(self):
            val = getattr(self, f.name)
            if f.name in _INT_FIELDS:
                if isinstance(val, bool) or not isinstance(val, int):
                    raise ValueError(f"tracker {f.name} must be an int, got {val!r}")
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"tracker {f.name} must be a number, got {val!r}")
            if not (math.isfinite(val) and val > 0.0):
                raise ValueError(f"tracker {f.name} must be finite and > 0, got {val!r}")
        if self.id_base < 0:
            raise ValueError(f"tracker id_base must be >= 0, got {self.id_base}")
        if not 1 <= self.m_confirm <= self.n_confirm:
            raise ValueError("tracker needs 1 <= m_confirm <= n_confirm")
        if self.coast_cap < 0:
            raise ValueError(f"tracker coast_cap must be >= 0, got {self.coast_cap}")
        if self.iou_min > 1.0:
            raise ValueError(f"tracker iou_min must be in (0, 1], got {self.iou_min}")

    def to_obj(self) -> dict[str, Any]:
        """Recording / scorecard form."""
        out: dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            out[f.name] = val if f.name in _INT_FIELDS else float(val)
        return out

    @classmethod
    def from_obj(cls, obj: Mapping[str, Any]) -> TrackerConfig:
        """Inverse of :meth:`to_obj`; every key is required, unknown keys refused."""
        if not isinstance(obj, Mapping):
            raise ValueError("tracker config must be an object")
        names = {f.name for f in fields(cls)}
        if set(obj) != names:
            missing = sorted(names - set(obj))
            unknown = sorted(set(obj) - names)
            raise ValueError(f"tracker config keys: missing {missing}, unknown {unknown}")
        return cls(**{name: obj[name] for name in names})


class _Axis:
    """[K1] one scalar constant-rate Kalman filter, state ``[pos, rate]``."""

    __slots__ = ("x", "r", "p00", "p01", "p11")

    def __init__(self, x: float, rate_sigma: float, meas_var: float) -> None:
        self.x = x
        self.r = 0.0
        self.p00 = meas_var
        self.p01 = 0.0
        self.p11 = rate_sigma * rate_sigma

    def predict(self, dt: float, accel_var: float) -> None:
        dt2 = dt * dt
        self.x += self.r * dt
        self.p00 += 2.0 * dt * self.p01 + dt2 * self.p11 + accel_var * dt2 * dt2 / 4.0
        self.p01 += dt * self.p11 + accel_var * dt2 * dt / 2.0
        self.p11 += accel_var * dt2

    def update(self, z: float, meas_var: float) -> None:
        s = self.p00 + meas_var
        k0 = self.p00 / s
        k1 = self.p01 / s
        y = z - self.x
        self.x += k0 * y
        self.r += k1 * y
        p00, p01, p11 = self.p00, self.p01, self.p11
        self.p00 = (1.0 - k0) * p00
        self.p01 = (1.0 - k0) * p01
        self.p11 = p11 - k1 * p01

    def floor(self, minimum: float) -> None:
        """Hold a size axis at ``minimum`` and stop it shrinking ([P2] w, h > 0)."""
        if self.x < minimum:
            self.x = minimum
            if self.r < 0.0:
                self.r = 0.0


class _Track:
    __slots__ = ("track_id", "u", "v", "w", "h", "state", "hits", "misses", "total_hits", "age")

    def __init__(self, track_id: int, box: Box, cfg: TrackerConfig) -> None:
        mv = cfg.meas_sigma_px * cfg.meas_sigma_px
        cu, cv = box.center
        self.track_id = track_id
        self.u = _Axis(float(cu), cfg.init_vel_sigma_px_s, mv)
        self.v = _Axis(float(cv), cfg.init_vel_sigma_px_s, mv)
        self.w = _Axis(float(box.w), cfg.init_size_rate_sigma_px_s, mv)
        self.h = _Axis(float(box.h), cfg.init_size_rate_sigma_px_s, mv)
        self.hits = 1
        self.misses = 0
        self.total_hits = 1
        self.age = 1
        self.state = TrackState.CONFIRMED if cfg.m_confirm <= 1 else TrackState.TENTATIVE

    def predict(self, dt: float, cfg: TrackerConfig) -> None:
        a_var = cfg.accel_sigma_px_s2 * cfg.accel_sigma_px_s2
        s_var = cfg.size_rate_sigma_px_s2 * cfg.size_rate_sigma_px_s2
        self.u.predict(dt, a_var)
        self.v.predict(dt, a_var)
        self.w.predict(dt, s_var)
        self.h.predict(dt, s_var)
        self.w.floor(cfg.min_size_px)
        self.h.floor(cfg.min_size_px)

    def update(self, box: Box, cfg: TrackerConfig) -> None:
        mv = cfg.meas_sigma_px * cfg.meas_sigma_px
        cu, cv = box.center
        self.u.update(float(cu), mv)
        self.v.update(float(cv), mv)
        self.w.update(float(box.w), mv)
        self.h.update(float(box.h), mv)
        self.w.floor(cfg.min_size_px)
        self.h.floor(cfg.min_size_px)

    def packet(self, t_cap: int) -> TrackPacket:
        return TrackPacket(
            t_cap=t_cap,
            track_id=self.track_id,
            state=self.state,
            u=self.u.x,
            v_px=self.v.x,
            du=self.u.r,
            dv=self.v.r,
            w=self.w.x,
            h=self.h.x,
            hits=self.hits,
            misses=self.misses,
            age_frames=self.age,
        )


def _iou(track: _Track, box: Box) -> float:
    tw, th = track.w.x, track.h.x
    tx0, ty0 = track.u.x - tw / 2.0, track.v.x - th / 2.0
    ix = min(tx0 + tw, box.x + box.w) - max(tx0, box.x)
    iy = min(ty0 + th, box.y + box.h) - max(ty0, box.y)
    if ix <= 0.0 or iy <= 0.0:
        return 0.0
    inter = ix * iy
    return inter / (tw * th + box.w * box.h - inter)


class Tracker:
    """[K1]-[K4] Provisional tracker: detection packets in, track packets out.

    One instance per tracker process run. Feed every detection packet, in
    arrival order, to :meth:`update`; it returns that frame's track packets.
    """

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config
        self.stale_dropped = 0
        """Detection packets ignored because ``t_cap`` <= the last accepted one."""
        self.last_t_cap: int | None = None
        self._tracks: list[_Track] = []  # always sorted by track_id (spawn order)
        self._spawned = 0

    def update(self, det: DetectionPacket) -> list[TrackPacket]:
        """Process one detection frame ([P1]) and return its track packets ([P2])."""
        cfg = self.config
        t_cap = det.t_cap
        if self.last_t_cap is not None:
            if t_cap <= self.last_t_cap:
                self.stale_dropped += 1
                return []
            dt = (t_cap - self.last_t_cap) / 1000.0
            for trk in self._tracks:
                trk.predict(dt, cfg)
        self.last_t_cap = t_cap

        boxes = det.boxes
        pairs = []
        for trk in self._tracks:
            for j, box in enumerate(boxes):
                iou = _iou(trk, box)
                if iou >= cfg.iou_min:
                    pairs.append((-iou, trk.track_id, j, trk))
        pairs.sort(key=lambda p: (p[0], p[1], p[2]))
        matched: dict[int, int] = {}  # track_id -> box index
        used_boxes: set[int] = set()
        for _, tid, j, trk in pairs:
            if tid in matched or j in used_boxes:
                continue
            matched[tid] = j
            used_boxes.add(j)
            trk.update(boxes[j], cfg)

        out: list[TrackPacket] = []
        survivors: list[_Track] = []
        for trk in self._tracks:
            trk.age += 1
            if trk.track_id in matched:
                trk.hits += 1
                trk.misses = 0
                trk.total_hits += 1
            else:
                trk.hits = 0
                trk.misses += 1
            if trk.state is TrackState.TENTATIVE:
                if trk.total_hits >= cfg.m_confirm:
                    trk.state = TrackState.CONFIRMED
                elif trk.total_hits + (cfg.n_confirm - trk.age) < cfg.m_confirm:
                    continue  # [K2], [P2a]: fails confirmation, deleted without a packet
            elif trk.misses == 0:
                trk.state = TrackState.CONFIRMED
            else:
                trk.state = TrackState.COASTING
                if trk.misses > cfg.coast_cap:
                    out.append(trk.packet(t_cap))  # [P2a] final packet, then deleted
                    continue
            out.append(trk.packet(t_cap))
            survivors.append(trk)

        for j, box in enumerate(boxes):
            if j in used_boxes:
                continue
            self._spawned += 1
            trk = _Track(cfg.id_base + self._spawned, box, cfg)
            survivors.append(trk)
            out.append(trk.packet(t_cap))

        self._tracks = survivors
        return out
