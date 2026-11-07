"""Synthetic truth trajectories for the closed-loop harness ([S0]; brief 1, 3.9, 3.10).

Each truth object has a name, a physical size (width and height, m), and a
position in local NED (origin at home, [C3]) as a function of board time
(integer ms, [C1]). The synthetic camera turns that position and size into a
box. Every trajectory is a closed form with no integration step, so its value
at any time is exact and does not depend on how often the harness samples
it. Closed-loop SITL is not bit-reproducible ([S0]), and the world should not
add a second source of drift.

The four kinds follow the demo plan:

- :class:`StaticBalloon`, the first target: tethered, with an optional small
  sinusoidal sway along one horizontal heading.
- :class:`SlowKite`: constant velocity with one direction reversal (S6
  "target maneuver"). The reversal is instant or a constant-deceleration turn
  of ``turn_s``, and the position stays continuous.
- :class:`ThrownPlane`, the last target: ballistic under gravity with linear
  drag, from the throw instant until it reaches the ground. A real paper plane
  glides; lift is not modelled (finding).
- :class:`CrossingBird`: a straight constant-velocity distractor (S6).

Demo geometry keeps the target above the vehicle's horizon (brief 3.9).
:func:`require_above_horizon` lets a scenario check that over its window.

All sizes and speeds are Provisional (E1) model inputs. The 1.0 m balloon
width matches the contract §9 ``target_width_m`` default.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

Vec3 = tuple[float, float, float]
G_MPS2 = 9.80665  # standard gravity, NED +D


def _vec3(v: Sequence[float], what: str) -> np.ndarray:
    arr = np.asarray(v, dtype=float)
    if arr.shape != (3,) or not np.all(np.isfinite(arr)):
        raise ValueError(f"{what} must be 3 finite numbers, got {v!r}")
    return arr


def _positive(x: float, what: str) -> None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
        raise ValueError(f"{what} must be a finite number > 0, got {x!r}")


def _non_negative(x: float, what: str) -> None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
        raise ValueError(f"{what} must be a finite number >= 0, got {x!r}")


def _finite(x: float, what: str) -> None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ValueError(f"{what} must be a finite number, got {x!r}")


def _int_ms(x: int, what: str) -> None:
    if isinstance(x, bool) or not isinstance(x, int):
        raise ValueError(f"{what} must be an integer ms, got {x!r}")


def _check_size(name: str, width_m: float, height_m: float) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("a truth object needs a non-empty name")
    _positive(width_m, f"{name} width_m")
    _positive(height_m, f"{name} height_m")


class Trajectory(Protocol):
    """A truth object: a name, a size, and a NED position over board time."""

    @property
    def name(self) -> str: ...

    @property
    def width_m(self) -> float: ...

    @property
    def height_m(self) -> float: ...

    def position(self, t_ms: int) -> np.ndarray:
        """NED position relative to home (m) at board time ``t_ms``."""
        ...

    def present(self, t_ms: int) -> bool:
        """Whether the object is in the world at ``t_ms``. An object that is
        not present produces no box."""
        ...


@dataclass(frozen=True, kw_only=True)
class StaticBalloon:
    """Tethered balloon at ``anchor_ned`` with an optional horizontal sway.

    ``position(t) = anchor + sway_amp_m * sin(2 pi t / sway_period_s + phase) *
    (cos h, sin h, 0)``, with ``h = sway_heading_rad`` and ``t`` in seconds.
    With ``sway_amp_m = 0`` the balloon is static.
    """

    anchor_ned: Vec3
    name: str = "balloon"
    width_m: float = 1.0
    height_m: float = 1.0
    sway_amp_m: float = 0.0
    sway_period_s: float = 8.0
    sway_heading_rad: float = 0.0
    sway_phase_rad: float = 0.0

    def __post_init__(self) -> None:
        _vec3(self.anchor_ned, "anchor_ned")
        _check_size(self.name, self.width_m, self.height_m)
        _non_negative(self.sway_amp_m, "sway_amp_m")
        _positive(self.sway_period_s, "sway_period_s")
        _finite(self.sway_heading_rad, "sway_heading_rad")
        _finite(self.sway_phase_rad, "sway_phase_rad")

    def position(self, t_ms: int) -> np.ndarray:
        _int_ms(t_ms, "t_ms")
        anchor = np.asarray(self.anchor_ned, dtype=float)
        if self.sway_amp_m == 0.0:
            return anchor
        t_s = t_ms / 1000.0
        a = self.sway_amp_m * math.sin(
            2.0 * math.pi * t_s / self.sway_period_s + self.sway_phase_rad
        )
        h = self.sway_heading_rad
        return anchor + a * np.array([math.cos(h), math.sin(h), 0.0])

    def present(self, t_ms: int) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class SlowKite:
    """Constant velocity with one direction reversal at ``t_reverse_ms``.

    With ``tau = (t - t0) / 1000`` and ``tau_r = (t_reverse - t0) / 1000``:

    - ``tau < tau_r``: ``p = start + v tau``. This also holds before ``t0``,
      so the formula runs backwards in time.
    - turn, ``0 <= sigma = tau - tau_r < turn_s``: the velocity ramps linearly
      from ``v`` to ``-v``, so ``p = p_r + v (sigma - sigma^2 / turn_s)``. The
      kite runs out and comes back to ``p_r`` at the end of the turn.
    - after: ``p = p_r - v (tau - tau_r - turn_s)``.

    ``turn_s = 0`` is an instant reversal.
    """

    start_ned: Vec3
    velocity_ned: Vec3
    t_reverse_ms: int
    t0_ms: int = 0
    turn_s: float = 0.0
    name: str = "kite"
    width_m: float = 1.0
    height_m: float = 1.0

    def __post_init__(self) -> None:
        _vec3(self.start_ned, "start_ned")
        _vec3(self.velocity_ned, "velocity_ned")
        _int_ms(self.t0_ms, "t0_ms")
        _int_ms(self.t_reverse_ms, "t_reverse_ms")
        if self.t_reverse_ms < self.t0_ms:
            raise ValueError("t_reverse_ms must be >= t0_ms")
        _non_negative(self.turn_s, "turn_s")
        _check_size(self.name, self.width_m, self.height_m)

    def position(self, t_ms: int) -> np.ndarray:
        _int_ms(t_ms, "t_ms")
        p0 = np.asarray(self.start_ned, dtype=float)
        v = np.asarray(self.velocity_ned, dtype=float)
        tau = (t_ms - self.t0_ms) / 1000.0
        tau_r = (self.t_reverse_ms - self.t0_ms) / 1000.0
        if tau < tau_r:
            return p0 + v * tau
        p_r = p0 + v * tau_r
        sigma = tau - tau_r
        if sigma < self.turn_s:
            return p_r + v * (sigma - sigma * sigma / self.turn_s)
        return p_r - v * (sigma - self.turn_s)

    def present(self, t_ms: int) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class ThrownPlane:
    """Ballistic paper plane: gravity plus linear drag, from ``t_throw_ms``.

    With ``tau = (t - t_throw) / 1000`` and drag coefficient ``k`` (1/s):

    - ``k = 0``: ``p = p0 + v0 tau + (0, 0, g tau^2 / 2)``.
    - ``k > 0``: ``p = p0 + v_T tau + (v0 - v_T)(1 - exp(-k tau)) / k`` with
      terminal velocity ``v_T = (0, 0, g / k)``.

    Before the throw the plane is at ``launch_ned`` and not present (the
    thrower holds it). It is present while ``p_D < 0``, i.e. above the home
    ground plane. After it lands it is gone; the formula keeps going below
    ground, but no box is produced.
    """

    launch_ned: Vec3
    v0_ned: Vec3
    t_throw_ms: int
    drag_per_s: float = 0.0
    g_mps2: float = G_MPS2
    name: str = "plane"
    width_m: float = 0.3
    height_m: float = 0.1

    def __post_init__(self) -> None:
        _vec3(self.launch_ned, "launch_ned")
        _vec3(self.v0_ned, "v0_ned")
        _int_ms(self.t_throw_ms, "t_throw_ms")
        _non_negative(self.drag_per_s, "drag_per_s")
        _non_negative(self.g_mps2, "g_mps2")
        _check_size(self.name, self.width_m, self.height_m)

    def position(self, t_ms: int) -> np.ndarray:
        _int_ms(t_ms, "t_ms")
        p0 = np.asarray(self.launch_ned, dtype=float)
        if t_ms <= self.t_throw_ms:
            return p0
        v0 = np.asarray(self.v0_ned, dtype=float)
        tau = (t_ms - self.t_throw_ms) / 1000.0
        k = self.drag_per_s
        if k == 0.0:
            return p0 + v0 * tau + np.array([0.0, 0.0, 0.5 * self.g_mps2 * tau * tau])
        v_term = np.array([0.0, 0.0, self.g_mps2 / k])
        return p0 + v_term * tau + (v0 - v_term) * (-math.expm1(-k * tau) / k)

    def present(self, t_ms: int) -> bool:
        _int_ms(t_ms, "t_ms")
        return t_ms >= self.t_throw_ms and float(self.position(t_ms)[2]) < 0.0


@dataclass(frozen=True, kw_only=True)
class CrossingBird:
    """Distractor on a straight line: ``p = start + v (t - t0) / 1000`` for all t."""

    start_ned: Vec3
    velocity_ned: Vec3
    t0_ms: int = 0
    name: str = "bird"
    width_m: float = 0.5
    height_m: float = 0.2

    def __post_init__(self) -> None:
        _vec3(self.start_ned, "start_ned")
        _vec3(self.velocity_ned, "velocity_ned")
        _int_ms(self.t0_ms, "t0_ms")
        _check_size(self.name, self.width_m, self.height_m)

    def position(self, t_ms: int) -> np.ndarray:
        _int_ms(t_ms, "t_ms")
        tau = (t_ms - self.t0_ms) / 1000.0
        return (
            np.asarray(self.start_ned, dtype=float)
            + np.asarray(self.velocity_ned, dtype=float) * tau
        )

    def present(self, t_ms: int) -> bool:
        return True


def above_horizon(target_ned: Sequence[float], vehicle_ned: Sequence[float]) -> bool:
    """Brief 3.9: the target is above the vehicle's local horizontal plane.

    NED down is positive, so "above" means a smaller D.
    """
    return float(target_ned[2]) < float(vehicle_ned[2])


def require_above_horizon(target: Trajectory, *, vehicle_alt_m: float, t_ms: Iterable[int]) -> None:
    """Raise ``ValueError`` if the target is present at or below ``vehicle_alt_m``
    (above home) at any of the given times (brief 3.9 demo geometry)."""
    _finite(vehicle_alt_m, "vehicle_alt_m")
    vehicle = (0.0, 0.0, -float(vehicle_alt_m))
    for t in t_ms:
        if target.present(t) and not above_horizon(target.position(t), vehicle):
            alt = -float(target.position(t)[2])
            raise ValueError(
                f"{target.name} at t={t} ms is {alt:.3f} m above home, not above the "
                f"vehicle's horizon at {vehicle_alt_m:.3f} m (brief 3.9)"
            )
