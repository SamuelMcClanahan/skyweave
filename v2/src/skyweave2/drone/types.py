"""Internal types shared by the drone stack's modules. NOT a wire contract.

These carry data between mission, guidance, fc_link, and the harness inside
one companion process. Nothing here crosses a socket; the wire is
``packets.py`` alone. Time is always board-monotonic integer milliseconds
([C1]); code obtains it only through an injected :data:`Clock`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, IntEnum

Clock = Callable[[], int]
"""Board-monotonic milliseconds ([C1]). Live: ``time.monotonic_ns() // 1e6``;
SITL harness and replay: simulation or recorded time."""


class LandedState(IntEnum):
    """MAVLink ``MAV_LANDED_STATE`` values, as ``EXTENDED_SYS_STATE`` reports."""

    UNDEFINED = 0
    ON_GROUND = 1
    IN_AIR = 2
    TAKEOFF = 3
    LANDING = 4


@dataclass(frozen=True, kw_only=True)
class Attitude:
    """One ``ATTITUDE`` sample ([F2]). Angles in radians, ArduPilot convention."""

    t_ms: int  # board-ms receive time
    time_boot_ms: int  # FC time
    roll: float
    pitch: float
    yaw: float


@dataclass(frozen=True, kw_only=True)
class VehicleSnapshot:
    """What the mission needs from the FC at one instant ([M2]).

    Built by fc_link from the MAVLink stream; replay rebuilds it from the
    recording's ``mavlink`` rx records with the same code ([R3]).
    """

    t_ms: int
    fc_link_up: bool
    mode: str | None  # ArduCopter mode name ("GUIDED", "RTL", ...); None = never seen
    armed: bool | None  # None = no HEARTBEAT seen yet ([M2a])
    landed_state: LandedState
    rel_alt_m: float | None  # above home
    home_dist_m: float | None  # horizontal distance from home
    battery_pct: float | None  # None when the FC reports it unknown
    attitude_age_ms: int | None
    attitude_degraded: bool
    rc_seen: bool

    @property
    def airborne(self) -> bool:
        """[M2a]: TAKEOFF, IN_AIR or LANDING. UNDEFINED is unknown: neither this
        nor :attr:`on_ground`."""
        return self.landed_state in (LandedState.IN_AIR, LandedState.TAKEOFF, LandedState.LANDING)

    @property
    def on_ground(self) -> bool:
        """[M2a]: landed_state ON_GROUND."""
        return self.landed_state is LandedState.ON_GROUND


@dataclass(frozen=True, kw_only=True)
class VelocityCommand:
    """Velocity setpoint in NED, m/s, and yaw rate in rad/s (+ = nose right).

    The yaw rate is always explicit ([F6], [G10]): a setpoint that left yaw
    uncommanded would hand the heading to ArduCopter's auto-yaw.
    """

    vn: float
    ve: float
    vd: float
    yaw_rate: float


class FcRequestKind(str, Enum):
    """The only FC requests the mission makes ([M12])."""

    ARM_AND_TAKEOFF = "arm_and_takeoff"  # value: takeoff altitude, m (gated, [F5])
    MODE_RTL = "mode_rtl"
    MODE_LAND = "mode_land"
    TONE = "tone"  # value: tone name (contract §9 tone table)


@dataclass(frozen=True, kw_only=True)
class FcRequest:
    kind: FcRequestKind
    value: float | str | None = None


class GuidanceEventKind(str, Enum):
    COMMIT = "commit"
    PASS_DONE = "pass_done"
    HOLD_COMPLETE = "hold_complete"


@dataclass(frozen=True, kw_only=True)
class GuidanceEvent:
    """A guidance event, processed by the mission as its own input ([M2]).

    ``t_ms`` is the stamp of the input that made guidance emit it. A commit
    carries the committing packet's id and ``t_cap`` and the [G6] miss vector
    (camera right, down, metres) and range ``z_m``, for the ``commit:`` and
    ``miss:`` events (contract §4.6).
    """

    kind: GuidanceEventKind
    t_ms: int
    track_id: int | None = None
    t_cap: int | None = None
    miss_m: tuple[float, float] | None = None
    z_m: float | None = None
