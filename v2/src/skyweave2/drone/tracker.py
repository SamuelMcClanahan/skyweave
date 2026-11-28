"""Tracker (DRONE_CONTRACTS_D0.md [K1]-[K4], [P2], [P2a]; V-D10). PROVISIONAL.

This is a reimplementation of the tracker v0 *spec on record* (brief §2:
per-axis 2-state KF [pos, vel], constant velocity; IoU association; M-of-N
confirm; coasting with a miss cap). The tracker v0 SOURCE is missing from the
repo (brief §0), so nothing here is v0 code and nothing here inherits v0's
evidence: v0's numbers (1.4 px RMS, zero ghosts on 5 synthetic tests) are NOT
claims about this module. Every number in it is Provisional and must be
re-earned in the harness (contract §8, [S7] probe seeds only).

World angles (V-D10, E2a-4). The tracker filters where a target is in the
world, not where it is in the image, so the vehicle's own rotation drops out
of prediction and association:

- Each box center is undistorted into the pinhole image (V-D11) and
  de-rotated with the attitude at the frame's ``t_cap`` ([G1]:
  ``los = R_ned_body(att) R_body_cam normalize(ray)``). The attitude comes
  from the companion's attitude history (``AttitudeSource``; E2-D5).
- Azimuth and elevation are not filtered: with the ``cone_up`` mount the view
  is centered on the zenith, where azimuth is singular. Each track keeps a
  reference direction (its first line of sight) and two axes across it, all
  fixed in NED, and filters the gnomonic coordinates of its line of sight in
  that tangent plane: ``x = f (los . e1) / (los . d0)``,
  ``y = f (los . e2) / (los . d0)``. The factor ``f`` (the lens ``f_px``)
  makes the unit a pixel at the image center ("px-equivalent": 1 unit is
  ``1 / f`` rad at the reference direction), so the ``*_px*`` constants below
  keep their E1 meaning and values.
- When a track's filtered direction is more than ``recenter_deg`` from its
  reference, the reference moves to that direction. The line of sight and
  its rate do not change; the filter covariances are carried over as they
  are (the plane's scale changes by under 1 % at 5 deg).
- Box size stays in raw pixels ([K1]): width and height are filtered as the
  detector reports them, because [G2], [G4] and the E2a-5 width rate read
  pixels. (Record: E2a4-F2.)

Why the shape:

- [K1] Four independent scalar Kalman filters per track, one per axis
  (tangent-plane ``x``, ``y``, box width ``w``, height ``h``), each with state
  ``[pos, rate]`` and a constant-rate model. Process noise is the discrete
  piecewise-constant white-acceleration model, ``q = sigma_a^2``, so the
  ``*_px_s2`` config values read as acceleration standard deviations. The
  filters and the geometry are plain float arithmetic: no numpy scalars leak
  into packets and the result is bit-identical run to run on one platform.
- Time: ``dt`` is the difference of consecutive accepted ``t_cap`` values
  (board ms, [C1]); the tracker never reads a clock. A detection whose
  ``t_cap`` is not newer than the last accepted one is ignored and counted in
  :attr:`Tracker.stale_dropped` ([P1]: consumers order by ``t_cap``).
- Attitude-degraded frames ([G1]: no attitude sample within
  ``attitude_bound_ms`` of ``t_cap``): the whole frame is dropped. Without
  the attitude a box has no world direction and a track has no image
  position, so nothing can be associated or published. The frame changes no
  track, produces no packet, and is counted in
  :attr:`Tracker.degraded_dropped`; the next good frame predicts over the
  real time gap. It is the dropped-frame path the tracker already has
  ([S11]); the mission's ``track_timeout_ms`` bounds the gap ([P2a]).
- [K2] Association is greedy by descending IoU between each track's predicted
  box and each detection box, compared in that track's tangent plane: the
  centers are tangent-plane points, the sizes are the pixel sizes (1 px = 1
  unit). Pairs below ``iou_min`` never associate, and ties break by (lower
  ``track_id``, lower box index), so the same packets give the same tracks.
  Unmatched boxes spawn tentative tracks in box order. A track confirms once
  it has ``m_confirm`` hits (not necessarily consecutive) within its first
  ``n_confirm`` frames, the birth frame being frame 1 and hit 1. A tentative
  track is deleted, with no packet in that frame, as soon as it can no
  longer reach ``m_confirm`` hits by frame ``n_confirm`` ([P2a]).
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
  are consecutive counts ending at this frame. Each packet carries the
  filtered line of sight in NED and its rate (``los_*``, ``dlos_*``).
  ``u``, ``v_px`` are that line of sight projected back into the raw image
  with the attitude at ``t_cap``; ``du``, ``dv`` are the image rate of the
  line-of-sight rate with the camera held at that attitude (the target's
  own motion; the vehicle's rotation is not in them). When the line of sight
  has no image at ``t_cap`` (it lies behind the image plane or outside the
  lens model), ``u`` and ``v_px`` keep their last values and ``du``, ``dv``
  are 0.
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

from skyweave2.drone.camera import AttitudeSource, CameraModel
from skyweave2.drone.packets import Box, DetectionPacket, TrackPacket, TrackState
from skyweave2.drone.types import Attitude

ID_BASE_PER_MS = 1000
"""[K4] id space reserved per millisecond of board clock between two tracker
starts (E1 build plan: live ``id_base = start_t_ms * 1000``)."""

Vec3 = tuple[float, float, float]
Rows3 = tuple[Vec3, Vec3, Vec3]

_MIN_COS = 1e-6
"""A direction at 90 deg or more from a track's reference has no point in its
tangent plane; it never associates with that track."""


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
    recording's ``meta.config`` ([R2]). The ``*_px*`` numbers apply in the
    track's tangent plane, in px-equivalent units (module docstring, V-D10).
    """

    id_base: int  # required; live: id_base_from_start(start_t_ms) ([K4])
    m_confirm: int = 3  # brief §6, [K2]
    n_confirm: int = 5  # brief §6, [K2]
    coast_cap: int = 20  # brief §6, [P2a]
    iou_min: float = 0.1  # E1, [K2]
    accel_sigma_px_s2: float = 1000.0  # E1, [K1] x/y process noise
    size_rate_sigma_px_s2: float = 200.0  # E1, [K1] w/h process noise
    meas_sigma_px: float = 2.0  # E1, [K1] box center and size noise
    init_vel_sigma_px_s: float = 1000.0  # E1, [K1] x/y rate prior at birth
    init_size_rate_sigma_px_s: float = 100.0  # E1, [K1] w/h rate prior at birth
    min_size_px: float = 1.0  # E1, [P2] w, h > 0 floor while coasting
    recenter_deg: float = 5.0  # E2a-4, [K1] re-center the tangent plane past this (V-D10)

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
        if self.recenter_deg > 45.0:
            raise ValueError(f"tracker recenter_deg must be in (0, 45], got {self.recenter_deg}")

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


# ---------------------------------------------------------------------------
# Geometry in plain floats ([G1], [C3])
# ---------------------------------------------------------------------------


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _unit(a: Vec3) -> Vec3:
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


def _across(a: Vec3, d: Vec3) -> Vec3:
    """The unit vector along the part of ``a`` that is perpendicular to unit ``d``."""
    k = _dot(a, d)
    return _unit((a[0] - k * d[0], a[1] - k * d[1], a[2] - k * d[2]))


def r_ned_cam(att: Attitude, cam: CameraModel) -> Rows3:
    """[C3], [G1]: ``R_ned_body(att) R_body_cam`` as rows of plain floats, with
    ``R_ned_body = Rz(yaw) Ry(pitch) Rx(roll)``."""
    cr, sr = math.cos(att.roll), math.sin(att.roll)
    cp, sp = math.cos(att.pitch), math.sin(att.pitch)
    cy, sy = math.cos(att.yaw), math.sin(att.yaw)
    b = (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )
    m = cam.r_body_cam
    rows = tuple(
        (
            row[0] * m[0][0] + row[1] * m[1][0] + row[2] * m[2][0],
            row[0] * m[0][1] + row[1] * m[1][1] + row[2] * m[2][1],
            row[0] * m[0][2] + row[1] * m[1][2] + row[2] * m[2][2],
        )
        for row in b
    )
    return (rows[0], rows[1], rows[2])


def _to_ned(rot: Rows3, v_cam: Vec3) -> Vec3:
    return (_dot(rot[0], v_cam), _dot(rot[1], v_cam), _dot(rot[2], v_cam))


def _to_cam(rot: Rows3, v_ned: Vec3) -> Vec3:
    return (
        rot[0][0] * v_ned[0] + rot[1][0] * v_ned[1] + rot[2][0] * v_ned[2],
        rot[0][1] * v_ned[0] + rot[1][1] * v_ned[1] + rot[2][1] * v_ned[2],
        rot[0][2] * v_ned[0] + rot[1][2] * v_ned[1] + rot[2][2] * v_ned[2],
    )


def los_of_pixel(u: float, v: float, cam: CameraModel, rot: Rows3) -> Vec3 | None:
    """[G1] the NED unit line of sight of the raw pixel ``(u, v)``: undistorted
    (V-D11), then ``rot . normalize(ray)``. ``None`` when the pixel has no
    finite ray (outside the lens model's domain, or too far out of frame)."""
    u_p, v_p = cam.undistort_px(u, v)
    x = (u_p - cam.cx) / cam.f_px
    y = (v_p - cam.cy) / cam.f_px
    n = math.sqrt(x * x + y * y + 1.0)
    if not math.isfinite(n):
        return None
    return _to_ned(rot, (x / n, y / n, 1.0 / n))


# ---------------------------------------------------------------------------
# Filters and tracks ([K1])
# ---------------------------------------------------------------------------


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
    """One track: a tangent plane fixed in NED (V-D10) and four [K1] filters."""

    __slots__ = (
        "track_id",
        "f",
        "d0",
        "e1",
        "e2",
        "x",
        "y",
        "w",
        "h",
        "state",
        "hits",
        "misses",
        "total_hits",
        "age",
        "uv",
    )

    def __init__(
        self, track_id: int, box: Box, los: Vec3, cam_x_ned: Vec3, f_px: float, cfg: TrackerConfig
    ) -> None:
        mv = cfg.meas_sigma_px * cfg.meas_sigma_px
        self.track_id = track_id
        self.f = f_px
        # The reference direction is the first line of sight. The plane's x
        # axis lies along the camera's X axis at birth, so with no rotation
        # (x, y) run along the image's (u, v).
        self.d0 = los
        self.e1 = _across(cam_x_ned, los)
        self.e2 = _cross(los, self.e1)
        # The birth point through the same map as every later measurement (it
        # is 0 within rounding), so a target that does not move gives exact
        # zero innovations and exact zero rates.
        x0, y0 = self._plane(los)
        self.x = _Axis(x0, cfg.init_vel_sigma_px_s, mv)
        self.y = _Axis(y0, cfg.init_vel_sigma_px_s, mv)
        self.w = _Axis(float(box.w), cfg.init_size_rate_sigma_px_s, mv)
        self.h = _Axis(float(box.h), cfg.init_size_rate_sigma_px_s, mv)
        self.hits = 1
        self.misses = 0
        self.total_hits = 1
        self.age = 1
        self.state = TrackState.CONFIRMED if cfg.m_confirm <= 1 else TrackState.TENTATIVE
        self.uv = box.center  # last (u, v_px) that had an image ([P2])

    def _plane(self, los: Vec3) -> tuple[float, float]:
        c = _dot(los, self.d0)
        return (self.f * _dot(los, self.e1) / c, self.f * _dot(los, self.e2) / c)

    def plane(self, los: Vec3) -> tuple[float, float] | None:
        """Gnomonic px-equivalent coordinates of a NED direction in this track's
        tangent plane; ``None`` at 90 deg or more from the reference."""
        if not _dot(los, self.d0) > _MIN_COS:
            return None
        return self._plane(los)

    def direction(self) -> tuple[Vec3, Vec3]:
        """The filtered line of sight in NED (unit) and its rate (1/s)."""
        a, b = self.x.x / self.f, self.y.x / self.f
        da, db = self.x.r / self.f, self.y.r / self.f
        d0, e1, e2 = self.d0, self.e1, self.e2
        g = (
            d0[0] + a * e1[0] + b * e2[0],
            d0[1] + a * e1[1] + b * e2[1],
            d0[2] + a * e1[2] + b * e2[2],
        )
        n = math.sqrt(_dot(g, g))
        d = (g[0] / n, g[1] / n, g[2] / n)
        gd = (
            da * e1[0] + db * e2[0],
            da * e1[1] + db * e2[1],
            da * e1[2] + db * e2[2],
        )
        k = _dot(d, gd)
        rate = ((gd[0] - k * d[0]) / n, (gd[1] - k * d[1]) / n, (gd[2] - k * d[2]) / n)
        return d, rate

    def predict(self, dt: float, cfg: TrackerConfig) -> None:
        a_var = cfg.accel_sigma_px_s2 * cfg.accel_sigma_px_s2
        s_var = cfg.size_rate_sigma_px_s2 * cfg.size_rate_sigma_px_s2
        self.x.predict(dt, a_var)
        self.y.predict(dt, a_var)
        self.w.predict(dt, s_var)
        self.h.predict(dt, s_var)
        self.w.floor(cfg.min_size_px)
        self.h.floor(cfg.min_size_px)

    def update(self, xy: tuple[float, float], box: Box, cfg: TrackerConfig) -> None:
        mv = cfg.meas_sigma_px * cfg.meas_sigma_px
        self.x.update(xy[0], mv)
        self.y.update(xy[1], mv)
        self.w.update(float(box.w), mv)
        self.h.update(float(box.h), mv)
        self.w.floor(cfg.min_size_px)
        self.h.floor(cfg.min_size_px)

    def recenter(self, limit: float) -> None:
        """V-D10: move the reference to the filtered direction once it is more
        than ``limit`` (px-equivalent) from the old one. The line of sight and
        its rate are kept; the covariances are carried over unchanged."""
        if not math.hypot(self.x.x, self.y.x) > limit:
            return
        d, rate = self.direction()
        self.d0 = d
        self.e1 = _across(self.e1, d)
        self.e2 = _cross(d, self.e1)
        self.x.x = 0.0
        self.y.x = 0.0
        self.x.r = self.f * _dot(rate, self.e1)
        self.y.r = self.f * _dot(rate, self.e2)

    def packet(self, t_cap: int, rot: Rows3, cam: CameraModel) -> TrackPacket:
        d, rate = self.direction()
        du = dv = 0.0
        p = _to_cam(rot, d)
        if p[2] > 0.0:
            # [P2]: the line of sight back in the raw image at this frame's
            # attitude, and the image rate of the line-of-sight rate.
            dp = _to_cam(rot, rate)
            u_p = cam.cx + cam.f_px * p[0] / p[2]
            v_p = cam.cy + cam.f_px * p[1] / p[2]
            du_p = cam.f_px * (dp[0] * p[2] - p[0] * dp[2]) / (p[2] * p[2])
            dv_p = cam.f_px * (dp[1] * p[2] - p[1] * dp[2]) / (p[2] * p[2])
            u, v = cam.distort_px(u_p, v_p)
            du_r, dv_r = cam.distort_rate_px(u_p, v_p, du_p, dv_p)
            if all(math.isfinite(c) for c in (u, v, du_r, dv_r)):
                self.uv = (u, v)
                du, dv = du_r, dv_r
        return TrackPacket(
            t_cap=t_cap,
            track_id=self.track_id,
            state=self.state,
            u=self.uv[0],
            v_px=self.uv[1],
            du=du + 0.0,  # + 0.0: never a negative zero on the wire
            dv=dv + 0.0,
            w=self.w.x,
            h=self.h.x,
            hits=self.hits,
            misses=self.misses,
            age_frames=self.age,
            los_n=d[0],
            los_e=d[1],
            los_d=d[2],
            dlos_n=rate[0] + 0.0,
            dlos_e=rate[1] + 0.0,
            dlos_d=rate[2] + 0.0,
        )


def _iou(track: _Track, xy: tuple[float, float], box: Box) -> float:
    """[K2] IoU of the track's predicted box and a detection box whose center is
    at ``xy`` in the track's tangent plane; sizes are the pixel sizes."""
    tw, th = track.w.x, track.h.x
    tx0, ty0 = track.x.x - tw / 2.0, track.y.x - th / 2.0
    bx0, by0 = xy[0] - box.w / 2.0, xy[1] - box.h / 2.0
    ix = min(tx0 + tw, bx0 + box.w) - max(tx0, bx0)
    iy = min(ty0 + th, by0 + box.h) - max(ty0, by0)
    if ix <= 0.0 or iy <= 0.0:
        return 0.0
    inter = ix * iy
    return inter / (tw * th + box.w * box.h - inter)


class Tracker:
    """[K1]-[K4] Provisional world-angle tracker (V-D10): detection packets in,
    track packets out.

    One instance per companion process run (E2-D5: the companion core owns
    it). ``cam`` is the configured camera (mount, lens, distortion; V-D1,
    V-D11) and ``attitude`` the companion's attitude history, sampled at each
    frame's ``t_cap`` ([G1]). Feed every detection packet, in arrival order,
    to :meth:`update`; it returns that frame's track packets.
    """

    def __init__(self, config: TrackerConfig, cam: CameraModel, attitude: AttitudeSource) -> None:
        self.config = config
        self.cam = cam
        self.stale_dropped = 0
        """Detection packets ignored because ``t_cap`` <= the last accepted one."""
        self.degraded_dropped = 0
        """Detection packets dropped whole because they were attitude-degraded
        ([G1]): no attitude sample within the bound of their ``t_cap``."""
        self.boxes_unusable = 0
        """Boxes ignored because their center has no finite ray (V-D11)."""
        self.last_t_cap: int | None = None
        self._attitude = attitude
        self._recenter = cam.f_px * math.tan(math.radians(config.recenter_deg))
        self._tracks: list[_Track] = []  # always sorted by track_id (spawn order)
        self._spawned = 0

    def update(self, det: DetectionPacket) -> list[TrackPacket]:
        """Process one detection frame ([P1]) and return its track packets ([P2])."""
        cfg, cam = self.config, self.cam
        t_cap = det.t_cap
        if self.last_t_cap is not None and t_cap <= self.last_t_cap:
            self.stale_dropped += 1
            return []
        att = self._attitude.attitude_at(t_cap)
        if att is None:
            self.degraded_dropped += 1  # V-D10: no attitude at t_cap, no world direction
            return []
        rot = r_ned_cam(att, cam)
        if self.last_t_cap is not None:
            dt = (t_cap - self.last_t_cap) / 1000.0
            for trk in self._tracks:
                trk.predict(dt, cfg)
        self.last_t_cap = t_cap

        boxes = det.boxes
        sights = [los_of_pixel(*box.center, cam, rot) for box in boxes]
        self.boxes_unusable += sum(1 for los in sights if los is None)
        pairs = []
        for trk in self._tracks:
            for j, box in enumerate(boxes):
                los = sights[j]
                xy = None if los is None else trk.plane(los)
                if xy is None:
                    continue
                iou = _iou(trk, xy, box)
                if iou >= cfg.iou_min:
                    pairs.append((-iou, trk.track_id, j, trk, xy))
        pairs.sort(key=lambda p: (p[0], p[1], p[2]))
        matched: dict[int, int] = {}  # track_id -> box index
        used_boxes: set[int] = set()
        for _, tid, j, trk, xy in pairs:
            if tid in matched or j in used_boxes:
                continue
            matched[tid] = j
            used_boxes.add(j)
            trk.update(xy, boxes[j], cfg)

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
                    out.append(trk.packet(t_cap, rot, cam))  # [P2a] final packet, then deleted
                    continue
            trk.recenter(self._recenter)
            out.append(trk.packet(t_cap, rot, cam))
            survivors.append(trk)

        cam_x_ned = (rot[0][0], rot[1][0], rot[2][0])
        for j, box in enumerate(boxes):
            los = sights[j]
            if j in used_boxes or los is None:
                continue
            self._spawned += 1
            trk = _Track(cfg.id_base + self._spawned, box, los, cam_x_ned, cam.f_px, cfg)
            survivors.append(trk)
            out.append(trk.packet(t_cap, rot, cam))

        self._tracks = survivors
        return out
