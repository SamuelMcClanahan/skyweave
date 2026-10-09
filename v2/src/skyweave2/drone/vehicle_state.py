"""Vehicle state from the FC's MAVLink stream (DRONE_CONTRACTS_D0.md §6, [M2a]).

Pure: no socket, no clock. Every frame comes in with the board-ms receive time
its caller stamped ([C1], [F11]), and every query names its own "now". The
live link (``fc_link.FcLink``) and replay (the companion core, [R3]) feed the
same frames through this one class, so the mission sees the same snapshots in
both.

What lives here and why:

* The [M2a] vehicle predicates. Only frames from the FC's system id AND
  autopilot component count: the companion shares the vehicle's system id
  (component 191), and ArduPilot forwards other components' and the GCS's
  frames between its ports, so a HEARTBEAT from anyone else must never set the
  mode or the armed flag.
* Link loss forgets. ``fc_link_up`` is "any FC frame within
  ``link_bound_ms``" ([P4]); every other predicate is unknown while the link is
  down, and a mode seen before a link loss does not count afterwards ([F9]).
  So when a frame arrives more than ``link_bound_ms`` after the previous one,
  everything learned before the gap is dropped before the new frame is used.
  Attitude samples are kept: they stay true at their own receive times, and
  [G1] judges them by distance from ``t_cap``.
* The [F2] / [G1] attitude store and sampler, and the [F3] staleness term.
* The [F7] radio approve detector.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.types import Attitude, LandedState, VehicleSnapshot

ARDUCOPTER_MODES: dict[int, str] = dict(mavutil.mode_mapping_acm)
"""ArduCopter ``custom_mode`` number -> mode name, from pymavlink's table."""

GUIDED = "GUIDED"
ATTITUDE_KEEP_MS = 1000  # [F2]: at least 1 s of samples is kept for [G1]


def wrap_pi(a: float) -> float:
    """Wrap an angle to [-pi, pi)."""
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def mode_name(custom_mode: int) -> str:
    """[M2a] mode: the ArduCopter name for ``custom_mode``.

    A number pymavlink does not name is still a known mode that is not
    ``GUIDED`` (T20 must see it), so it gets a stable placeholder name.
    """
    return ARDUCOPTER_MODES.get(custom_mode, f"MODE_{custom_mode}")


def rc_approve_cmd_id(t_rx: int) -> str:
    """[F7] ``cmd_id`` of the in-process ``approve_engage`` for one approve."""
    return f"rc:approve:{t_rx}"


@dataclass(frozen=True, kw_only=True)
class LinkConfig:
    """fc_link constants (contract §9; every number Provisional, E1).

    ``setpoints_enabled`` is the [F5] gate setting; only an explicit ``True``
    here unlocks it. ``link_bound_ms`` is the contract's ``fc_link_bound_ms``.
    ``telemetry_hz`` and ``heartbeat_hz`` are the [F2] request rates for the
    [M2a] telemetry (Provisional, E1).
    """

    attitude_hz: float = 50.0
    attitude_bound_ms: int = 100
    link_bound_ms: int = 1000
    health_period_ms: int = 1000
    telemetry_hz: float = 10.0
    heartbeat_hz: float = 2.0
    setpoints_enabled: bool = False
    v_xy_hard: float = 5.0
    v_z_hard: float = 2.0
    yaw_rate_hard: float = math.radians(90.0)
    approve_channel: int = 8
    approve_pwm_high: int = 1700
    approve_pwm_valid_min: int = 800
    approve_pwm_valid_max: int = 2200
    companion_sysid: int = 1
    companion_compid: int = 191
    fc_sysid: int = 1
    fc_compid: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.approve_channel <= 18:
            raise ValueError("approve_channel must be an RC_CHANNELS channel, 1..18")
        if not (self.approve_pwm_valid_min <= self.approve_pwm_high <= self.approve_pwm_valid_max):
            raise ValueError("need approve_pwm_valid_min <= approve_pwm_high <= valid_max")
        for name in ("attitude_bound_ms", "link_bound_ms", "health_period_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        for name in ("attitude_hz", "telemetry_hz", "heartbeat_hz"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be > 0")
        for name in ("v_xy_hard", "v_z_hard", "yaw_rate_hard"):
            if not getattr(self, name) >= 0:
                raise ValueError(f"{name} must be >= 0")

    def to_obj(self) -> dict[str, Any]:
        """Plain JSON object for the recording's ``meta.config`` ([R2])."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_obj(cls, obj: Any) -> LinkConfig:
        """Inverse of :meth:`to_obj`. Every field is required, unknown keys are
        refused, and nothing is coerced (an int field never takes a bool)."""
        if not isinstance(obj, Mapping):
            raise ValueError("link config must be a JSON object")
        names = {f.name for f in fields(cls)}
        if set(obj) != names:
            missing = sorted(names - set(obj))
            unknown = sorted(set(obj) - names)
            raise ValueError(f"link config keys: missing {missing}, unknown {unknown}")
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            val = obj[f.name]
            if f.type == "bool":
                if not isinstance(val, bool):
                    raise ValueError(f"link config {f.name!r} must be a bool")
            elif f.type == "int":
                if isinstance(val, bool) or not isinstance(val, int):
                    raise ValueError(f"link config {f.name!r} must be an integer")
            else:
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise ValueError(f"link config {f.name!r} must be a number")
                val = float(val)
            kwargs[f.name] = val
        return cls(**kwargs)


def parse_frames(raw: bytes) -> list[Any]:
    """MAVLink2 bytes -> pymavlink messages (one fresh parser per call).

    For replay of ``mavlink`` records ([R2], [R3]): each record is whole frames.
    Bytes that do not form a valid frame are dropped (never turned into a
    message).
    """
    parser = mavlink2.MAVLink(None)
    parser.robust_parsing = True
    msgs = parser.parse_buffer(raw) or []
    return [m for m in msgs if m.get_type() != "BAD_DATA"]


class VehicleState:
    """The FC as the companion knows it, from frames stamped with receive times."""

    def __init__(self, config: LinkConfig) -> None:
        self.config = config
        self.foreign_frames = 0  # frames from any source other than the FC autopilot
        self._last_rx: int | None = None
        self._att_t: list[int] = []
        self._att: list[Attitude] = []
        self._approvals: list[int] = []
        self._forget()

    def _forget(self) -> None:
        """Drop everything a link loss makes unknown ([M2a], [F9])."""
        self._mode: str | None = None
        self._armed: bool | None = None
        self._landed = LandedState.UNDEFINED
        self._rel_alt: float | None = None
        self._home_dist: float | None = None
        self._velocity: tuple[float, float, float] | None = None
        self._battery: float | None = None
        self._rc_t: int | None = None
        self._approve_armed = False  # [F7] rc_seen false disarms

    # -- input --------------------------------------------------------------

    def ingest(self, msg: Any, t_rx_ms: int) -> None:
        """One pymavlink message received at board ms ``t_rx_ms``."""
        cfg = self.config
        if msg.get_srcSystem() != cfg.fc_sysid or msg.get_srcComponent() != cfg.fc_compid:
            self.foreign_frames += 1
            return
        if self._last_rx is not None:
            if t_rx_ms < self._last_rx:
                raise ValueError(f"receive times must not go back ({t_rx_ms} < {self._last_rx})")
            if t_rx_ms - self._last_rx > cfg.link_bound_ms:
                self._forget()  # the link was down in between
        self._last_rx = t_rx_ms
        kind = msg.get_type()
        if kind == "ATTITUDE":
            self._add_attitude(
                Attitude(
                    t_ms=t_rx_ms,
                    time_boot_ms=int(msg.time_boot_ms),
                    roll=float(msg.roll),
                    pitch=float(msg.pitch),
                    yaw=float(msg.yaw),
                )
            )
        elif kind == "HEARTBEAT":
            self._mode = mode_name(int(msg.custom_mode))
            self._armed = bool(msg.base_mode & mavlink2.MAV_MODE_FLAG_SAFETY_ARMED)
        elif kind == "EXTENDED_SYS_STATE":
            try:
                self._landed = LandedState(int(msg.landed_state))
            except ValueError:
                self._landed = LandedState.UNDEFINED
        elif kind == "GLOBAL_POSITION_INT":
            self._rel_alt = msg.relative_alt / 1000.0
        elif kind == "LOCAL_POSITION_NED":
            self._home_dist = math.hypot(msg.x, msg.y)
            self._velocity = (float(msg.vx), float(msg.vy), float(msg.vz))
        elif kind == "SYS_STATUS":
            pct = int(msg.battery_remaining)
            self._battery = None if pct == -1 else float(pct)
        elif kind == "RC_CHANNELS":
            self._rc(msg, t_rx_ms)

    def _add_attitude(self, att: Attitude) -> None:
        if self._att_t and att.t_ms == self._att_t[-1]:
            self._att[-1] = att  # same receive time (one read): the newer one wins
            return
        self._att_t.append(att.t_ms)
        self._att.append(att)
        cut = bisect.bisect_right(self._att_t, att.t_ms - ATTITUDE_KEEP_MS) - 1
        if cut > 0:  # keep one sample at or before the cut so the window still interpolates
            del self._att_t[:cut]
            del self._att[:cut]

    def _rc(self, msg: Any, t: int) -> None:
        """[F7] detector and the [P4] ``rc_seen`` clock."""
        cfg = self.config
        seen_before = self._rc_t is not None and t - self._rc_t <= cfg.link_bound_ms
        if msg.chancount > 0:
            self._rc_t = t
        if not seen_before:
            self._approve_armed = False  # rc_seen was false: a recovery is never an approve
        ch = cfg.approve_channel
        value = getattr(msg, f"chan{ch}_raw")
        valid = (
            msg.chancount >= ch and cfg.approve_pwm_valid_min <= value <= cfg.approve_pwm_valid_max
        )
        if not valid:
            self._approve_armed = False
        elif value < cfg.approve_pwm_high:
            self._approve_armed = True
        elif self._approve_armed:
            self._approvals.append(t)
            self._approve_armed = False

    # -- queries ------------------------------------------------------------

    def link_up(self, t_ms: int) -> bool:
        """[P4] ``fc_link_up``: an FC frame within ``link_bound_ms``."""
        return self._last_rx is not None and t_ms - self._last_rx <= self.config.link_bound_ms

    def attitude_age_ms(self, t_ms: int) -> int | None:
        """[F3]: ``t_ms`` minus the newest ``ATTITUDE`` receive time."""
        return None if not self._att_t else t_ms - self._att_t[-1]

    def attitude_degraded(self, t_ms: int) -> bool:
        """[F3]: no sample, or the newest is older than ``attitude_bound_ms``."""
        age = self.attitude_age_ms(t_ms)
        return age is None or age > self.config.attitude_bound_ms

    def rc_seen(self, t_ms: int) -> bool:
        """[P4]: ``RC_CHANNELS`` with ``chancount > 0`` within ``link_bound_ms``."""
        return (
            self.link_up(t_ms)
            and self._rc_t is not None
            and t_ms - self._rc_t <= self.config.link_bound_ms
        )

    def mode(self, t_ms: int) -> str | None:
        """[M2a] mode of the newest HEARTBEAT; unknown while the link is down."""
        return self._mode if self.link_up(t_ms) else None

    def velocity_ned(self, t_ms: int) -> tuple[float, float, float] | None:
        """Newest ``LOCAL_POSITION_NED`` velocity (m/s); unknown while the link is down."""
        return self._velocity if self.link_up(t_ms) else None

    def snapshot(self, t_ms: int) -> VehicleSnapshot:
        """[M2a] predicates at ``t_ms``; everything but the link is unknown while it is down."""
        up = self.link_up(t_ms)
        return VehicleSnapshot(
            t_ms=t_ms,
            fc_link_up=up,
            mode=self._mode if up else None,
            armed=self._armed if up else None,
            landed_state=self._landed if up else LandedState.UNDEFINED,
            rel_alt_m=self._rel_alt if up else None,
            home_dist_m=self._home_dist if up else None,
            battery_pct=self._battery if up else None,
            attitude_age_ms=self.attitude_age_ms(t_ms),
            attitude_degraded=self.attitude_degraded(t_ms),
            rc_seen=self.rc_seen(t_ms),
        )

    def newest_attitude(self) -> Attitude | None:
        return self._att[-1] if self._att else None

    def attitude_at(self, t_ms: int) -> Attitude | None:
        """[G1] attitude for ``t_ms``; ``None`` means attitude-degraded.

        Between two samples roll, pitch, and unwrapped yaw are linear in
        receive time (the result is stamped ``t_ms``); outside the stored span
        the nearest sample is held; nothing is extrapolated. The "sample used"
        for the ``attitude_bound_ms`` test is the held sample, or the nearer of
        the two bracketing samples.
        """
        if not self._att_t:
            return None
        bound = self.config.attitude_bound_ms
        i = bisect.bisect_right(self._att_t, t_ms)
        if i == 0:
            first = self._att[0]
            return first if first.t_ms - t_ms <= bound else None
        if i == len(self._att_t):
            last = self._att[-1]
            return last if t_ms - last.t_ms <= bound else None
        a, b = self._att[i - 1], self._att[i]
        if min(t_ms - a.t_ms, b.t_ms - t_ms) > bound:
            return None
        frac = (t_ms - a.t_ms) / (b.t_ms - a.t_ms)
        return Attitude(
            t_ms=t_ms,
            time_boot_ms=a.time_boot_ms + round(frac * (b.time_boot_ms - a.time_boot_ms)),
            roll=a.roll + frac * (b.roll - a.roll),
            pitch=a.pitch + frac * (b.pitch - a.pitch),
            yaw=wrap_pi(a.yaw + frac * wrap_pi(b.yaw - a.yaw)),
        )

    def take_approvals(self) -> list[int]:
        """[F7]: the receive time of each radio approve since the last call."""
        out, self._approvals = self._approvals, []
        return out
