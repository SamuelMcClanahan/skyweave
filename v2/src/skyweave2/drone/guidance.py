"""Guidance v1 (DRONE_CONTRACTS_D0.md §5; brief 3.4-3.6). Pure logic, no clock.

Guidance turns the one track the mission lets it see into velocity setpoints
and three events (``commit``, ``pass_done``, ``hold_complete``):

- [G1] de-rotation: the track's pixel center becomes a NED line of sight with
  the FC attitude at the frame's capture time ``t_cap``.
- [G2] size range ``Z = f W / w`` (assume-and-bound fallback below
  ``w_min_px``). Onboard scope is bearing plus size range; no 3D estimation.
- [G3] the pluggable law (v1: pure pursuit to a standoff point level with the
  target), plus the yaw law ``yaw_rate = clamp(K_yaw * az)``.
- [G4] the image-space commit gate (touch trials), [G5] the open-loop terminal
  segment, [G6] the miss vector logged at commit, [G7] the standoff hold.
- [G8]-[G11] degraded attitude, the lock, per-state outputs, cadence.

Time ([M2], [R3]): guidance's "now" is the stamp of the input it processes,
the record ``t_rx`` of a track packet or the stamp of a setpoint step.
``t_cap`` is geometry only: it picks the attitude sample for de-rotation and
never times anything. The terminal segment and the hold are timed on input
stamps.

How the core drives it ([R3]): for a TRK input, ``mission.on_track`` first,
then :meth:`Guidance.on_track` with the mission view after that input, then
each returned event to the mission as its own GDE input. For a TICK input,
:meth:`Guidance.step` on every setpoint step (:meth:`Guidance.step_due`,
[G11]) in every state, including states that send nothing: the step keeps
guidance's resets (new trial, T07/T09 limiter reset, terminal reset) in line
with the mission.

Lock ([G9], [M7]): guidance stores only the newest packet of the engaged
track and of the candidate. It never chooses a track; the mission view names
the ids and guidance ignores every other packet.

Numbers are Provisional (contract §9, E1); they are tuned in the harness on
probe seeds only, inside a campaign file ([S7]).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Protocol

import numpy as np

from skyweave2.drone.camera import AttitudeSource, CameraModel, r_ned_body, wrap_pi
from skyweave2.drone.packets import MissionState, PrimeParams, TrackPacket, TrackState, TrialType
from skyweave2.drone.types import (
    Attitude,
    GuidanceEvent,
    GuidanceEventKind,
    VehicleSnapshot,
    VelocityCommand,
)

ZERO_COMMAND = VelocityCommand(vn=0.0, ve=0.0, vd=0.0, yaw_rate=0.0)
"""Zero velocity with an explicit zero yaw rate ([G10], [F6])."""

_NO_SETPOINT_STATES = (None, MissionState.PRIMED, MissionState.LAUNCH)
_EXIT_STATES = frozenset(
    {
        MissionState.ABORT,
        MissionState.COMPLETE,
        MissionState.MISS,
        MissionState.RETURN,
        MissionState.LAND,
    }
)


class RangeSource(str, Enum):
    """[G2] where a range came from."""

    SIZE = "size"
    ASSUMED = "assumed"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_POSITIVE = frozenset(
    {
        "Kp",
        "a_max",
        "K_yaw",
        "yaw_rate_max",
        "K_alt",
        "v_alt_max",
        "w_min_px",
        "r_assume",
        "r_min",
        "r_max",
        "hold_tol_m",
        "hold_time_s",
        "setpoint_hz",
    }
)
_ANY_SIGN = frozenset({"search_yaw_rate"})  # the scan direction is a choice


@dataclass(frozen=True, kw_only=True)
class GuidanceConfig:
    """Guidance constants. All Provisional (contract §9, E1).

    Names follow the contract symbols. ``setpoint_hz`` is the [G11] cadence
    (§9: 10 Hz); the core asks :meth:`Guidance.step_due` before each step.
    """

    Kp: float = 0.8  # 1/s, [G3]
    deadband_m: float = 0.25  # [G3]
    a_max: float = 2.0  # m/s^2, [G3] rate limit
    K_yaw: float = 1.5  # 1/s, [G3]
    yaw_rate_max: float = math.radians(90.0)  # [G3]
    K_alt: float = 0.5  # 1/s, [G10]
    v_alt_max: float = 1.0  # m/s, [G10]
    search_yaw_rate: float = math.radians(45.0)  # [G10], brief §6
    w_min_px: float = 4.0  # [G2]
    r_assume: float = 15.0  # [G2]
    r_min: float = 1.0  # [G2]
    r_max: float = 60.0  # [G2]
    margin_m: float = 0.05  # [G3] s_touch
    nose_clearance_m: float = 0.3  # [G3] s_touch
    t_overrun_s: float = 0.5  # [G5]
    t_brake_s: float = 1.0  # [G5]
    v_climb: float = 1.5  # m/s, [G5]
    t_climb_s: float = 2.0  # [G5]
    hold_tol_m: float = 1.0  # [G7]
    hold_time_s: float = 10.0  # [G7]
    setpoint_hz: float = 10.0  # [G11]

    def __post_init__(self) -> None:
        for f in fields(self):
            val = getattr(self, f.name)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"guidance {f.name} must be a number, got {val!r}")
            if not math.isfinite(val):
                raise ValueError(f"guidance {f.name} must be finite, got {val!r}")
            if f.name in _POSITIVE and not val > 0.0:
                raise ValueError(f"guidance {f.name} must be > 0, got {val!r}")
            if f.name not in _POSITIVE and f.name not in _ANY_SIGN and val < 0.0:
                raise ValueError(f"guidance {f.name} must be >= 0, got {val!r}")
        if self.r_min > self.r_max:
            raise ValueError("guidance r_min must be <= r_max")

    def to_obj(self) -> dict[str, Any]:
        """Recording ``meta.config`` form ([R2])."""
        return {f.name: float(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_obj(cls, obj: Mapping[str, Any]) -> GuidanceConfig:
        """Inverse of :meth:`to_obj`; every key is required, unknown keys refused."""
        if not isinstance(obj, Mapping):
            raise ValueError("guidance config must be an object")
        names = {f.name for f in fields(cls)}
        if set(obj) != names:
            missing = sorted(names - set(obj))
            unknown = sorted(set(obj) - names)
            raise ValueError(f"guidance config keys: missing {missing}, unknown {unknown}")
        return cls(**{name: obj[name] for name in names})


# ---------------------------------------------------------------------------
# Inputs guidance reads
# ---------------------------------------------------------------------------


class MissionViewLike(Protocol):
    """What guidance reads from the mission (``mission.MissionView``)."""

    @property
    def state(self) -> MissionState | None: ...

    @property
    def trial(self) -> PrimeParams | None: ...

    @property
    def engaged_track_id(self) -> int | None: ...

    @property
    def candidate_id(self) -> int | None: ...


@dataclass(frozen=True, kw_only=True)
class LosObservation:
    """One de-rotated observation of the engaged track, as a law consumes it.

    ``az_body`` is the [G3] azimuth: the line of sight's bearing minus
    ``yaw_now``, wrapped to [-pi, pi], positive with the target right of the
    nose. ``el_body`` is the elevation above the local horizontal, positive
    up. Both are measured in the level frame against the heading, not in the
    tilted body frame. ``t_ms`` is the step stamp ``yaw_now`` was taken at;
    ``t_cap`` is the packet's capture time the line of sight belongs to.
    """

    t_ms: int
    t_cap: int
    track_id: int
    los_ned: tuple[float, float, float]
    r_m: float
    range_source: RangeSource
    z_m: float
    az_body: float
    el_body: float
    yaw_now: float

    def p_ned(self) -> np.ndarray:
        """[G3] the target's position relative to the vehicle, ``p = r * los``."""
        return self.r_m * np.asarray(self.los_ned, dtype=float)


@dataclass(frozen=True, kw_only=True)
class MissVector:
    """[G6] predicted miss of a pass flown along the boresight."""

    eps_px: tuple[float, float]  # (u - cx, v_px - cy)
    z_m: float  # [G2] depth
    miss_m: tuple[float, float]  # camera right, down
    range_source: RangeSource


@dataclass(frozen=True, kw_only=True)
class CommitRecord:
    """Everything decided at the commit ([G4]-[G6]), for logs and the harness."""

    t_ms: int  # commit stamp: the committing packet's input stamp
    track_id: int
    t_cap: int
    eps_px: tuple[float, float]
    z_m: float
    r_m: float
    range_source: RangeSource
    miss_m: tuple[float, float]
    contact_ned: tuple[float, float, float]  # [G5] c = r * los, relative to the vehicle
    v_close: float  # [G5] v_c = trial v_max
    t_fly_s: float  # [G5] |c| / v_c + t_overrun_s


# ---------------------------------------------------------------------------
# Geometry ([G1], [G2], [G3], [G6])
# ---------------------------------------------------------------------------


def derotate(track: TrackPacket, cam: CameraModel, att: Attitude) -> np.ndarray:
    """[G1] unit line of sight in NED: ``R_ned_body(att) R_body_cam normalize(ray)``.

    ``att`` is the [G1] sample for ``track.t_cap`` (see ``VehicleState.attitude_at``).
    """
    ray = cam.ray(track.u, track.v_px)
    return r_ned_body(att) @ cam.rotation() @ (ray / np.linalg.norm(ray))


def size_range(
    track: TrackPacket, cam: CameraModel, target_width_m: float, cfg: GuidanceConfig
) -> tuple[float, float, RangeSource]:
    """[G2] ``(Z, r, source)``: ``Z = f W / w`` and ``r = Z |ray|`` when
    ``w >= w_min_px``; otherwise ``r = clamp(r_assume, r_min, r_max)`` and the
    depth is the matching ``Z = r / |ray|``."""
    ray_norm = float(np.linalg.norm(cam.ray(track.u, track.v_px)))
    if track.w >= cfg.w_min_px:
        z = cam.f_px * target_width_m / track.w
        return z, z * ray_norm, RangeSource.SIZE
    r = min(max(cfg.r_assume, cfg.r_min), cfg.r_max)
    return r / ray_norm, r, RangeSource.ASSUMED


def geometry_usable(
    track: TrackPacket, cam: CameraModel, target_width_m: float, cfg: GuidanceConfig
) -> bool:
    """[G1a]: the packet's ray and [G2] range are finite and positive.

    The wire bounds nothing about ``u``/``v_px`` beyond finiteness; a far
    out-of-frame value (|u| above about 1e157) overflows ``|ray|`` and would
    turn the line of sight and the [G3] command into NaN (bug-hunt finding 3).
    Such a packet is treated as attitude-degraded.
    """
    ray_norm = float(np.linalg.norm(cam.ray(track.u, track.v_px)))
    if not (math.isfinite(ray_norm) and ray_norm > 0.0):
        return False
    z, r, _ = size_range(track, cam, target_width_m, cfg)
    return math.isfinite(z) and math.isfinite(r) and z > 0.0 and r > 0.0


def miss_vector(
    track: TrackPacket,
    cam: CameraModel,
    target_width_m: float,
    cfg: GuidanceConfig | None = None,
) -> MissVector:
    """[G6] ``eps = (u - cx, v_px - cy)``; ``miss = Z eps / f`` (camera right, down).

    ``cfg`` matters only below ``w_min_px`` (the [G2] fallback); replay passes
    the recorded guidance config ([R4]).
    """
    z, _, source = size_range(track, cam, target_width_m, cfg or GuidanceConfig())
    eps = (track.u - cam.cx, track.v_px - cam.cy)
    return MissVector(
        eps_px=eps,
        z_m=z,
        miss_m=(z * eps[0] / cam.f_px, z * eps[1] / cam.f_px),
        range_source=source,
    )


def standoff_distance(trial: PrimeParams, cam: CameraModel, cfg: GuidanceConfig) -> float:
    """[G3] horizontal standoff ``s``: ``d_s`` in standoff trials; in touch trials
    ``s_touch = max(f W / (1920 alpha) - deadband_m - margin_m, W/2 + nose_clearance_m)``."""
    if trial.trial_type is TrialType.STANDOFF:
        return trial.d_s
    fill_range = cam.f_px * trial.target_width_m / (cam.width * trial.alpha)
    return max(
        fill_range - cfg.deadband_m - cfg.margin_m,
        trial.target_width_m / 2.0 + cfg.nose_clearance_m,
    )


def standoff_offset(p_ned: np.ndarray, s: float, yaw_now: float) -> np.ndarray:
    """[G3] ``q = p - s p_h / |p_h|``, the standoff point level with the target.

    ``p_h = (p_N, p_E, 0)``; below 0.01 m the current heading stands in for it.
    """
    p = np.asarray(p_ned, dtype=float)
    p_h = np.array([p[0], p[1], 0.0])
    n = float(np.linalg.norm(p_h))
    if n < 0.01:
        u_h = np.array([math.cos(yaw_now), math.sin(yaw_now), 0.0])
    else:
        u_h = p_h / n
    return p - s * u_h


def azimuth(los_ned: np.ndarray | tuple[float, float, float], yaw_now: float) -> float:
    """[G3] ``az = wrap_pi(atan2(los_E, los_N) - yaw_now)``; positive = right of the nose."""
    return wrap_pi(math.atan2(los_ned[1], los_ned[0]) - yaw_now)


def elevation(los_ned: np.ndarray | tuple[float, float, float]) -> float:
    """Elevation above the local horizontal, positive up (NED down is negative)."""
    return math.atan2(-los_ned[2], math.hypot(los_ned[0], los_ned[1]))


def yaw_rate_command(az: float, cfg: GuidanceConfig) -> float:
    """[G3] yaw law: ``clamp(K_yaw * az, +-yaw_rate_max)``; positive turns the nose right [C4]."""
    return max(-cfg.yaw_rate_max, min(cfg.yaw_rate_max, cfg.K_yaw * az))


def make_observation(
    track: TrackPacket,
    cam: CameraModel,
    att_cap: Attitude,
    yaw_now: float,
    target_width_m: float,
    cfg: GuidanceConfig,
    t_ms: int,
) -> LosObservation:
    """[G1]-[G3] one observation: line of sight at ``t_cap``, size range, and the
    azimuth against ``yaw_now`` (the newest attitude at the step, ``t_ms``)."""
    los = derotate(track, cam, att_cap)
    z, r, source = size_range(track, cam, target_width_m, cfg)
    return LosObservation(
        t_ms=t_ms,
        t_cap=track.t_cap,
        track_id=track.track_id,
        los_ned=(float(los[0]), float(los[1]), float(los[2])),
        r_m=r,
        range_source=source,
        z_m=z,
        az_body=azimuth(los, yaw_now),
        el_body=elevation(los),
        yaw_now=yaw_now,
    )


# ---------------------------------------------------------------------------
# Laws ([G3]): pluggable, ranked by the harness scorecard
# ---------------------------------------------------------------------------


class GuidanceLaw(Protocol):
    """The ENGAGED law. Takes the de-rotated observation, returns a command.

    ``obs`` is ``None`` when guidance has no usable packet of the engaged
    track. ``dt_s`` is the time since the previous setpoint step, from board-ms
    stamps. ``reset`` zeroes any memory (the [G3] limiter's ``v_(k-1)``);
    guidance calls it whenever the mission is not in ``ENGAGED`` (so T07 and
    T09 start from zero) and on every degraded step.
    """

    name: str

    def reset(self) -> None: ...

    def command(
        self, obs: LosObservation | None, standoff_m: float, v_max: float, dt_s: float
    ) -> VelocityCommand: ...


class PurePursuit:
    """[G3] v1 law: velocity toward the standoff point, P control.

    ``v = Kp q`` (zero inside the deadband), clamped to ``v_max``, then rate
    limited to ``|v_k - v_(k-1)| <= a_max dt``. Yaw by the [G3] yaw law.
    """

    name = "pure_pursuit"

    def __init__(self, config: GuidanceConfig) -> None:
        self.config = config
        self._v_prev = np.zeros(3)

    def reset(self) -> None:
        self._v_prev = np.zeros(3)

    def command(
        self, obs: LosObservation | None, standoff_m: float, v_max: float, dt_s: float
    ) -> VelocityCommand:
        if obs is None:
            self.reset()
            return ZERO_COMMAND
        cfg = self.config
        q = standoff_offset(obs.p_ned(), standoff_m, obs.yaw_now)
        v = np.zeros(3) if float(np.linalg.norm(q)) <= cfg.deadband_m else cfg.Kp * q
        speed = float(np.linalg.norm(v))
        if speed > v_max:
            v = v * (v_max / speed)
        dv = v - self._v_prev
        dv_norm = float(np.linalg.norm(dv))
        dv_max = cfg.a_max * max(dt_s, 0.0)
        if dv_norm > dv_max:
            v = self._v_prev + dv * (dv_max / dv_norm)
        self._v_prev = v
        return VelocityCommand(
            vn=float(v[0]),
            ve=float(v[1]),
            vd=float(v[2]),
            yaw_rate=yaw_rate_command(obs.az_body, cfg),
        )


LAWS: dict[str, Callable[[GuidanceConfig], GuidanceLaw]] = {PurePursuit.name: PurePursuit}
"""Law registry; the recording's ``meta.config`` names the law ([R2])."""


def make_law(name: str, config: GuidanceConfig) -> GuidanceLaw:
    try:
        factory = LAWS[name]
    except KeyError:
        raise ValueError(f"unknown guidance law {name!r}; known: {sorted(LAWS)}") from None
    return factory(config)


# ---------------------------------------------------------------------------
# Guidance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seen:
    """The newest packet of one role, with its [G1] attitude sample."""

    pkt: TrackPacket
    t_ms: int
    att_cap: Attitude | None  # None: the packet is attitude-degraded [G1]


@dataclass
class _Terminal:
    """[G5] the open-loop segment, timed from the commit stamp."""

    t0_ms: int
    v_ned: tuple[float, float, float]
    fly_end_s: float
    brake_end_s: float
    climb_end_s: float
    done: bool = False


class Guidance:
    """Guidance v1 (§5). Pure: every time is an argument ([C1], [R3])."""

    def __init__(self, cam: CameraModel, config: GuidanceConfig, law: GuidanceLaw) -> None:
        self.cam = cam
        self.config = config
        self.law = law
        self.last_commit: CommitRecord | None = None
        self._last_step_t: int | None = None
        self._seen: dict[int, _Seen] = {}
        self._committed = False
        self._terminal: _Terminal | None = None
        self._hold_start: int | None = None
        self._hold_done = False

    # -- trial lifecycle ----------------------------------------------------

    def reset(self) -> None:
        """New trial: drop packets, latches, the terminal segment, the hold, the law.

        Runs whenever the view is unprimed, ``PRIMED``, or ``LAUNCH``.
        ``last_commit`` is kept for logs until the next commit.
        """
        self._seen.clear()
        self._committed = False
        self._terminal = None
        self._hold_start = None
        self._hold_done = False
        self.law.reset()

    def _observe(self, view: MissionViewLike) -> None:
        state = view.state
        if state in _NO_SETPOINT_STATES:
            self.reset()
            return
        if view.trial is None:
            raise ValueError(f"mission view in {state} carries no trial")
        if state is not MissionState.ENGAGED:
            self.law.reset()  # T07 / T09 enter ENGAGED with v_(k-1) = 0 [G3]
            self._hold_start = None  # [G7] the hold is evaluated in ENGAGED only
        if state is not MissionState.TOUCH:
            self._terminal = None  # [G5] resets outside TOUCH
        keep = {view.engaged_track_id, view.candidate_id}
        for tid in [tid for tid in self._seen if tid not in keep]:
            del self._seen[tid]

    # -- inputs -------------------------------------------------------------

    def on_track(
        self, pkt: TrackPacket, t_ms: int, view: MissionViewLike, att: AttitudeSource
    ) -> list[GuidanceEvent]:
        """One TRK input ([M2]), stamped ``t_ms`` (its record ``t_rx``).

        ``view`` is the mission's view after it processed this packet. Returns
        the [G4] commit or the [G7] hold_complete event, stamped ``t_ms``.
        """
        self._observe(view)
        state = view.state
        if state in (MissionState.ENGAGED, MissionState.COASTING):
            role_id = view.engaged_track_id
        elif state is MissionState.ACQUIRING:
            role_id = view.candidate_id  # yaw only [G9]
        else:
            return []  # [G9]; in TOUCH the segment is open loop [G5]
        if role_id is None or pkt.track_id != role_id:
            return []  # [G9], [M7]
        att_cap = att.attitude_at(pkt.t_cap)
        trial = view.trial
        assert trial is not None  # _observe
        if att_cap is not None and not geometry_usable(
            pkt, self.cam, trial.target_width_m, self.config
        ):
            att_cap = None  # [G1a]: unusable geometry counts as a degraded packet
        self._seen[pkt.track_id] = _Seen(pkt=pkt, t_ms=t_ms, att_cap=att_cap)
        if state is not MissionState.ENGAGED:
            return []
        att_now = att.attitude_at(t_ms)
        if att_cap is None or att_now is None:  # [G1a] at this input's stamp
            self._hold_start = None
            return []
        if trial.trial_type is TrialType.TOUCH:
            return self._commit_gate(pkt, t_ms, trial, att_cap)
        return self._hold(pkt, t_ms, trial, att_cap, att_now.yaw)

    def _commit_gate(
        self, pkt: TrackPacket, t_ms: int, trial: PrimeParams, att_cap: Attitude
    ) -> list[GuidanceEvent]:
        """[G4]; attitude was checked by the caller ([G1a])."""
        cam, cfg = self.cam, self.config
        if self._committed:
            return []  # at most one commit per pass
        fill_ok = pkt.w / cam.width >= trial.alpha
        center_ok = math.hypot(pkt.u - cam.cx, pkt.v_px - cam.cy) <= trial.beta * cam.width
        if not (
            fill_ok and center_ok and pkt.state is TrackState.CONFIRMED and pkt.hits >= trial.k
        ):
            return []
        mv = miss_vector(pkt, cam, trial.target_width_m, cfg)
        _, r, _ = size_range(pkt, cam, trial.target_width_m, cfg)
        los = derotate(pkt, cam, att_cap)
        v_c = trial.v_max
        fly_end = r / v_c + cfg.t_overrun_s
        self._terminal = _Terminal(
            t0_ms=t_ms,
            v_ned=(float(v_c * los[0]), float(v_c * los[1]), float(v_c * los[2])),
            fly_end_s=fly_end,
            brake_end_s=fly_end + cfg.t_brake_s,
            climb_end_s=fly_end + cfg.t_brake_s + cfg.t_climb_s,
        )
        self._committed = True
        self.last_commit = CommitRecord(
            t_ms=t_ms,
            track_id=pkt.track_id,
            t_cap=pkt.t_cap,
            eps_px=mv.eps_px,
            z_m=mv.z_m,
            r_m=r,
            range_source=mv.range_source,
            miss_m=mv.miss_m,
            contact_ned=(float(r * los[0]), float(r * los[1]), float(r * los[2])),
            v_close=v_c,
            t_fly_s=fly_end,
        )
        return [
            GuidanceEvent(
                kind=GuidanceEventKind.COMMIT,
                t_ms=t_ms,
                track_id=pkt.track_id,
                t_cap=pkt.t_cap,
                miss_m=mv.miss_m,
                z_m=mv.z_m,
            )
        ]

    def _hold(
        self, pkt: TrackPacket, t_ms: int, trial: PrimeParams, att_cap: Attitude, yaw_now: float
    ) -> list[GuidanceEvent]:
        """[G7]: ``|q| <= hold_tol_m`` without a break for ``hold_time_s``."""
        cfg = self.config
        if self._hold_done:
            return []
        if pkt.state is TrackState.COASTING:
            self._hold_start = None
            return []
        obs = make_observation(pkt, self.cam, att_cap, yaw_now, trial.target_width_m, cfg, t_ms)
        q = standoff_offset(obs.p_ned(), trial.d_s, yaw_now)
        if not float(np.linalg.norm(q)) <= cfg.hold_tol_m:  # NaN breaks the hold too
            self._hold_start = None
            return []
        if self._hold_start is None:
            self._hold_start = t_ms
        if (t_ms - self._hold_start) / 1000.0 < cfg.hold_time_s:
            return []
        self._hold_done = True
        return [GuidanceEvent(kind=GuidanceEventKind.HOLD_COMPLETE, t_ms=t_ms)]

    def step_due(self, t_ms: int) -> bool:
        """[G11]: a tick is a setpoint step when it is at least ``1000 / setpoint_hz``
        ms after the previous step (the first tick always is)."""
        if self._last_step_t is None:
            return True
        return t_ms - self._last_step_t >= 1000.0 / self.config.setpoint_hz

    def step(
        self, t_ms: int, view: MissionViewLike, att: AttitudeSource, snap: VehicleSnapshot
    ) -> tuple[VelocityCommand | None, list[GuidanceEvent]]:
        """One setpoint step at stamp ``t_ms`` ([G10], [G11]).

        Returns the setpoint (``None`` when the state sends none) and the
        events it emits (``pass_done`` at the end of the [G5] segment).
        """
        if self._last_step_t is not None and t_ms < self._last_step_t:
            raise ValueError(f"step stamps must not decrease ({t_ms} < {self._last_step_t})")
        dt_s = 0.0 if self._last_step_t is None else (t_ms - self._last_step_t) / 1000.0
        self._last_step_t = t_ms
        self._observe(view)
        state = view.state
        if state in _NO_SETPOINT_STATES:
            return None, []
        if state is MissionState.TOUCH:
            return self._terminal_step(t_ms)
        if state in _EXIT_STATES:
            guided = snap.fc_link_up and snap.mode == "GUIDED"
            return (ZERO_COMMAND if guided else None), []
        if state is MissionState.LOST:
            return ZERO_COMMAND, []
        return self._tracking_step(t_ms, dt_s, view, att, snap), []

    def _tracking_step(
        self,
        t_ms: int,
        dt_s: float,
        view: MissionViewLike,
        att: AttitudeSource,
        snap: VehicleSnapshot,
    ) -> VelocityCommand:
        """SEARCH, ACQUIRING, ENGAGED, COASTING ([G8], [G10])."""
        cfg, state, trial = self.config, view.state, view.trial
        assert trial is not None  # _observe
        if state is MissionState.ACQUIRING:
            role_id = view.candidate_id
        elif state is MissionState.SEARCH:
            role_id = None
        else:
            role_id = view.engaged_track_id
        seen = self._seen.get(role_id) if role_id is not None else None
        att_now = att.attitude_at(t_ms)
        if snap.attitude_degraded or att_now is None or (seen is not None and seen.att_cap is None):
            self.law.reset()  # [G3]: v_(k-1) restarts at zero once degradation clears
            return ZERO_COMMAND  # [G8], [G1a]
        if state is MissionState.SEARCH:
            return VelocityCommand(
                vn=0.0, ve=0.0, vd=self._altitude_hold(snap, trial), yaw_rate=cfg.search_yaw_rate
            )
        obs = None
        if seen is not None and seen.att_cap is not None:
            obs = make_observation(
                seen.pkt, self.cam, seen.att_cap, att_now.yaw, trial.target_width_m, cfg, t_ms
            )
        if state is MissionState.ENGAGED:
            s = standoff_distance(trial, self.cam, cfg)
            return self.law.command(obs, s, trial.v_max, dt_s)
        yaw_rate = 0.0 if obs is None else yaw_rate_command(obs.az_body, cfg)
        vd = self._altitude_hold(snap, trial) if state is MissionState.ACQUIRING else 0.0
        return VelocityCommand(vn=0.0, ve=0.0, vd=vd, yaw_rate=yaw_rate)

    def _altitude_hold(self, snap: VehicleSnapshot, trial: PrimeParams) -> float:
        """[G10]: ``v_D = clamp(K_alt (rel_alt - search_alt), +-v_alt_max)``; 0 when
        ``rel_alt`` is unknown (never seen, or the FC link is down, [M2a])."""
        cfg = self.config
        if not snap.fc_link_up or snap.rel_alt_m is None:
            return 0.0
        vd = cfg.K_alt * (snap.rel_alt_m - trial.search_alt)
        return max(-cfg.v_alt_max, min(cfg.v_alt_max, vd))

    def _terminal_step(self, t_ms: int) -> tuple[VelocityCommand, list[GuidanceEvent]]:
        """[G5]: fly, brake, climb, then ``pass_done`` once. Yaw rate 0 throughout;
        no limiter, no tracker input, attitude degradation ignored."""
        term = self._terminal
        if term is None:
            return ZERO_COMMAND, []
        elapsed = (t_ms - term.t0_ms) / 1000.0
        if elapsed < term.fly_end_s:
            vn, ve, vd = term.v_ned
            return VelocityCommand(vn=vn, ve=ve, vd=vd, yaw_rate=0.0), []
        if elapsed < term.brake_end_s:
            return ZERO_COMMAND, []
        if elapsed < term.climb_end_s:
            return VelocityCommand(vn=0.0, ve=0.0, vd=-self.config.v_climb, yaw_rate=0.0), []
        if term.done:
            return ZERO_COMMAND, []
        term.done = True
        return ZERO_COMMAND, [GuidanceEvent(kind=GuidanceEventKind.PASS_DONE, t_ms=t_ms)]
