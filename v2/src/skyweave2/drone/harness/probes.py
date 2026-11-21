"""Probe scenarios P1-P7 ([S9]; contract decisions V-F3, V-D14, V-D15, V-D16, V-F2).

They cover the sim gaps the 2026-11-20 review found (V-F3). They are not gate
scenes: they have no gate seeds, their probe seeds use the ``V-probe``
namespace (``seeds``), and nothing here touches S1-S6. Each world is a pure
function of (scenario, seed), drawn from the same world stream as the gate
worlds (``scenarios.stream_rng``). Each is scored against one or more §8
table rows (``World.checks_as``).

| ID | What it probes | Trial | Scored as |
| --- | --- | --- | --- |
| P1 | static balloon 20-60 deg above the vehicle | touch | S2 |
| P2 | tethered balloon blown by the wind and swaying (V-D14), SITL wind | touch | S2 |
| P3 | kite flying figure-8s in wind, SITL wind | standoff | S6 |
| P4 | thrown paper plane through the yaw scan (``ThrownPlane``) | touch, preauthorized | S2, S6 |
| P5 | false-box clutter and a circling bird (V-D16) | standoff | S1, S6 |
| P6 | detector misses, frame drops, bursts, latency jitter (V-D16) | touch | S2 |
| P7 | the cone-tower X airframe (V-F2) | touch | S2 |

Choices made before any run (all Provisional; probe seeds only):

- P1: slant range 12-20 m from the search point, elevation 20-60 deg. The
  [C3] mount sees +-31 deg vertically at f = 1000 px, so a level scan cannot
  see the upper part of that band. That gap is what P1 shows.
- P2: the balloon's mean position is drawn like S2's. The wind comes from
  the balloon's bearing +-30 deg, so the string leans toward home and stays off
  the far side (V-D14: approach from downwind). Lean from vertical is
  ``atan(D / B)`` with drag ``D = 0.5 rho V^2 Cd A`` (rho 1.225, Cd 0.47, 1 m
  sphere) and net lift ``B`` = 4 N; about 13 deg at 2 m/s and 42 deg at 4 m/s.
- P3: kite 13-17 m up at a mean line elevation of 40-55 deg (+-4-8 deg in
  the figure-8, so it stays above the 10 m search altitude), 1.0 x 0.8 m,
  sweeping +-15-25 deg across the wind with a 10-14 s period. Wind 4-6 m/s.
- P4: the drone hovers low (search altitude 3 m) and scans clockwise at
  ``search_yaw_rate`` from north. The thrower throws 2-5 s after SEARCH
  starts, from 1.8 m, so the plane crosses the line of sight 0.8 s later, 8-12
  m out, about where the scan points then (+-15 deg). Engage is
  preauthorized: there is no time for a human approve. The flight budget is
  60 s, so a missed plane ends the run.
- P5: S1's balloon, 0.5 clutter boxes per frame, and a bird circling 20-35 m
  out within 40 deg of the balloon's bearing. The scripted human cannot tell
  the bird from the balloon (no image on the UI), so it approves whatever
  candidate comes first (E1-F13).
- P6: S2's balloon; 2 % detector misses, 2 % frame drops, 0.2 bursts per
  second of 100-600 ms, and exponential jitter of mean 10 ms (cap 80 ms).
- P7: S2's world on :data:`environment.CONE_X`, nominal camera, no wind.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import numpy as np

from skyweave2.drone.guidance import GuidanceConfig
from skyweave2.drone.harness.camera_sim import CameraSimConfig
from skyweave2.drone.harness.environment import CONE_X, Wind
from skyweave2.drone.harness.scenarios import (
    _WORLD_STREAM,
    DISTRACTOR_NAME,
    NOMINAL_CAMERA,
    TARGET_NAME,
    World,
    _anchor,
    stream_rng,
)
from skyweave2.drone.harness.seeds import PROBE_SCENARIOS
from skyweave2.drone.harness.targets import (
    StaticBalloon,
    TetheredBalloon,
    ThrownPlane,
    WanderingBird,
    WindKite,
)
from skyweave2.drone.packets import PrimeParams, TrialType

RHO = 1.225  # kg/m^3, sea level
BALLOON_CD = 0.47  # sphere
BALLOON_LIFT_N = 4.0  # net lift of a 1 m helium balloon minus envelope (Provisional)
P4_SEARCH_ALT_M = 3.0
P4_FLIGHT_CAP_S = 60.0
P4_HAND_HEIGHT_M = 1.8
P4_CROSS_S = 0.8  # throw to line-of-sight crossing
P4_DRAG_PER_S = 0.3

P6_CAMERA = replace(
    NOMINAL_CAMERA,
    dropout_p=0.02,
    frame_drop_p=0.02,
    burst_per_s=0.2,
    burst_ms=(100, 600),
    latency_jitter_ms=10.0,
    latency_jitter_max_ms=80.0,
)
P5_CAMERA = replace(NOMINAL_CAMERA, clutter_per_frame=0.5, clutter_w_px=(8.0, 80.0))


class ScriptedThrow:
    """P4 target: a paper plane the thrower holds (not present) until the
    script throws it at a future time; then a :class:`targets.ThrownPlane`."""

    def __init__(
        self,
        launch_ned: Sequence[float],
        v0_ned: Sequence[float],
        *,
        drag_per_s: float,
        width_m: float = 0.3,
        height_m: float = 0.1,
    ) -> None:
        self.name = TARGET_NAME
        self.width_m = width_m
        self.height_m = height_m
        self.launch = tuple(float(x) for x in launch_ned)
        self.v0 = tuple(float(x) for x in v0_ned)
        self.drag_per_s = drag_per_s
        self.plane: ThrownPlane | None = None

    def throw(self, t_ms: int) -> None:
        if self.plane is not None:
            raise RuntimeError("the plane is thrown once")
        self.plane = ThrownPlane(
            launch_ned=self.launch,  # type: ignore[arg-type]
            v0_ned=self.v0,  # type: ignore[arg-type]
            t_throw_ms=t_ms,
            drag_per_s=self.drag_per_s,
            name=self.name,
            width_m=self.width_m,
            height_m=self.height_m,
        )

    def position(self, t_ms: int) -> np.ndarray:
        if self.plane is None:
            return np.asarray(self.launch, dtype=float)
        return self.plane.position(t_ms)

    def present(self, t_ms: int) -> bool:
        return self.plane is not None and self.plane.present(t_ms)


def _wrap360(deg: float) -> float:
    return deg % 360.0


def balloon_lean_rad(wind_mps: float) -> float:
    """Tether lean from vertical, ``atan(D / B)`` (module docstring)."""
    drag = 0.5 * RHO * wind_mps**2 * BALLOON_CD * math.pi * 0.5**2
    return math.atan2(drag, BALLOON_LIFT_N)


def _p1(rng: np.random.Generator, prime: PrimeParams) -> tuple[list[Any], dict[str, float]]:
    slant = float(rng.uniform(12.0, 20.0))
    elev = math.radians(float(rng.uniform(20.0, 60.0)))
    brg = math.radians(float(rng.uniform(-90.0, 90.0)))
    r = slant * math.cos(elev)
    alt = prime.search_alt + slant * math.sin(elev)
    ned = (r * math.cos(brg), r * math.sin(brg), -alt)
    w = prime.target_width_m
    geo = {"slant_m": slant, "elevation_rad": elev, "bearing_rad": brg, "alt_m": alt}
    return [StaticBalloon(anchor_ned=ned, name=TARGET_NAME, width_m=w, height_m=w)], geo


def _p2(rng: np.random.Generator, prime: PrimeParams) -> tuple[list[Any], dict[str, float], Wind]:
    mean, geo = _anchor(rng, (15.0, 22.0), 90.0, (11.0, 14.0))
    speed = float(rng.uniform(2.0, 4.0))
    from_deg = _wrap360(math.degrees(geo["bearing_rad"]) + float(rng.uniform(-30.0, 30.0)))
    wind = Wind(speed_mps=speed, from_deg=from_deg, turbulence_mps=0.5)
    lean = balloon_lean_rad(speed)
    tether = geo["alt_m"] / math.cos(lean)
    d = np.asarray(wind.downwind_ned())
    anchor = np.array([mean[0], mean[1], 0.0]) - tether * math.sin(lean) * d
    downwind = math.atan2(d[1], d[0])
    geo.update(
        {
            "wind_mps": speed,
            "wind_from_deg": from_deg,
            "lean_rad": lean,
            "tether_m": tether,
            "lean_period_s": float(rng.uniform(4.0, 7.0)),
            "lean_phase_rad": float(rng.uniform(0.0, 2.0 * math.pi)),
            "swing_period_s": float(rng.uniform(6.0, 10.0)),
            "swing_phase_rad": float(rng.uniform(0.0, 2.0 * math.pi)),
        }
    )
    w = prime.target_width_m
    balloon = TetheredBalloon(
        anchor_ned=(float(anchor[0]), float(anchor[1]), 0.0),
        tether_m=tether,
        downwind_heading_rad=downwind,
        lean_rad=lean,
        lean_amp_rad=math.radians(4.0),
        lean_period_s=geo["lean_period_s"],
        lean_phase_rad=geo["lean_phase_rad"],
        swing_amp_rad=math.radians(8.0),
        swing_period_s=geo["swing_period_s"],
        swing_phase_rad=geo["swing_phase_rad"],
        name=TARGET_NAME,
        width_m=w,
        height_m=w,
    )
    return [balloon], geo, wind


def _p3(rng: np.random.Generator, prime: PrimeParams) -> tuple[list[Any], dict[str, float], Wind]:
    rng_m = float(rng.uniform(15.0, 22.0))
    brg = math.radians(float(rng.uniform(-60.0, 60.0)))
    height = float(rng.uniform(13.0, 17.0))
    elev = math.radians(float(rng.uniform(40.0, 55.0)))
    speed = float(rng.uniform(4.0, 6.0))
    from_deg = _wrap360(math.degrees(brg) + float(rng.uniform(-30.0, 30.0)))
    wind = Wind(speed_mps=speed, from_deg=from_deg, turbulence_mps=1.0)
    line = height / math.sin(elev)
    d = np.asarray(wind.downwind_ned())
    mean_h = np.array([rng_m * math.cos(brg), rng_m * math.sin(brg), 0.0])
    anchor = mean_h - line * math.cos(elev) * d
    geo = {
        "range_m": rng_m,
        "bearing_rad": brg,
        "alt_m": height,
        "elev_rad": elev,
        "line_m": line,
        "wind_mps": speed,
        "wind_from_deg": from_deg,
        "sweep_amp_rad": math.radians(float(rng.uniform(15.0, 25.0))),
        "elev_amp_rad": math.radians(float(rng.uniform(4.0, 8.0))),
        "period_s": float(rng.uniform(10.0, 14.0)),
        "phase_rad": float(rng.uniform(0.0, 2.0 * math.pi)),
    }
    kite = WindKite(
        anchor_ned=(float(anchor[0]), float(anchor[1]), 0.0),
        line_m=line,
        downwind_heading_rad=math.atan2(d[1], d[0]),
        elev_rad=elev,
        sweep_amp_rad=geo["sweep_amp_rad"],
        elev_amp_rad=geo["elev_amp_rad"],
        period_s=geo["period_s"],
        phase_rad=geo["phase_rad"],
        name=TARGET_NAME,
        width_m=prime.target_width_m,
        height_m=0.8,
    )
    return [kite], geo, wind


def _p4(rng: np.random.Generator, yaw_rate: float) -> tuple[list[Any], dict[str, float]]:
    delay_s = float(rng.uniform(2.0, 5.0))
    scan_deg = math.degrees(yaw_rate) * (delay_s + P4_CROSS_S)
    cross_brg = math.radians(scan_deg + float(rng.uniform(-15.0, 15.0)))
    cross_r = float(rng.uniform(8.0, 12.0))
    v_h = float(rng.uniform(5.0, 7.0))
    v_up = float(rng.uniform(4.0, 6.0))
    side = float(rng.choice((-1.0, 1.0)))
    los = np.array([math.cos(cross_brg), math.sin(cross_brg), 0.0])
    lateral = side * np.array([-los[1], los[0], 0.0])
    v0 = v_h * lateral + np.array([0.0, 0.0, -v_up])
    # Where the plane is P4_CROSS_S after a throw from the origin (closed form).
    rel = ThrownPlane(
        launch_ned=(0.0, 0.0, 0.0),
        v0_ned=tuple(float(x) for x in v0),  # type: ignore[arg-type]
        t_throw_ms=0,
        drag_per_s=P4_DRAG_PER_S,
    ).position(round(P4_CROSS_S * 1000.0))
    cross_h = cross_r * los
    launch = np.array([cross_h[0] - rel[0], cross_h[1] - rel[1], -P4_HAND_HEIGHT_M])
    geo = {
        "throw_delay_ms": 1000.0 * delay_s,
        "cross_bearing_rad": cross_brg,
        "cross_range_m": cross_r,
        "cross_alt_m": P4_HAND_HEIGHT_M - float(rel[2]),
        "v_h_mps": v_h,
        "v_up_mps": v_up,
        "side": side,
    }
    plane = ScriptedThrow(launch, v0, drag_per_s=P4_DRAG_PER_S)
    return [plane], geo


def _p5(rng: np.random.Generator, prime: PrimeParams) -> tuple[list[Any], dict[str, float]]:
    ned, geo = _anchor(rng, (18.0, 26.0), 90.0, (12.0, 15.0))
    w = prime.target_width_m
    bird_r = float(rng.uniform(20.0, 35.0))
    bird_brg = geo["bearing_rad"] + math.radians(float(rng.uniform(-40.0, 40.0)))
    bird_alt = float(rng.uniform(12.0, 20.0))
    geo.update(
        {
            "bird_range_m": bird_r,
            "bird_bearing_rad": bird_brg,
            "bird_alt_m": bird_alt,
            "bird_radius_m": float(rng.uniform(3.0, 6.0)),
            "bird_speed_mps": float(rng.uniform(4.0, 7.0)),
            "bird_phase_rad": float(rng.uniform(0.0, 2.0 * math.pi)),
        }
    )
    bird = WanderingBird(
        center_ned=(bird_r * math.cos(bird_brg), bird_r * math.sin(bird_brg), -bird_alt),
        radius_m=geo["bird_radius_m"],
        speed_mps=geo["bird_speed_mps"],
        phase_rad=geo["bird_phase_rad"],
        bob_m=0.5,
        name=DISTRACTOR_NAME,
    )
    balloon = StaticBalloon(anchor_ned=ned, name=TARGET_NAME, width_m=w, height_m=w)
    return [balloon, bird], geo


def _s2_balloon(rng: np.random.Generator, prime: PrimeParams) -> tuple[list[Any], dict]:
    ned, geo = _anchor(rng, (15.0, 22.0), 90.0, (11.0, 14.0))
    w = prime.target_width_m
    return [StaticBalloon(anchor_ned=ned, name=TARGET_NAME, width_m=w, height_m=w)], geo


def build_probe_world(scenario: str, seed: int) -> World:
    """The probe scenario's world for ``seed`` ([S9]); deterministic in both."""
    if scenario not in PROBE_SCENARIOS:
        raise ValueError(f"{scenario!r} is not a probe scenario")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError(f"seed must be a 32-bit unsigned int, got {seed!r}")
    rng = stream_rng(seed, _WORLD_STREAM)
    touch = PrimeParams(trial_type=TrialType.TOUCH)
    standoff = PrimeParams(trial_type=TrialType.STANDOFF)
    camera: CameraSimConfig = NOMINAL_CAMERA
    kw: dict[str, Any] = {}
    if scenario == "P1":
        prime, checks = touch, ("S2",)
        scene, geo = _p1(rng, prime)
    elif scenario == "P2":
        prime, checks = touch, ("S2",)
        scene, geo, kw["wind"] = _p2(rng, prime)
    elif scenario == "P3":
        prime, checks = standoff, ("S6",)
        scene, geo, kw["wind"] = _p3(rng, prime)
    elif scenario == "P4":
        prime = replace(
            touch,
            engage_preauthorized=True,
            search_alt=P4_SEARCH_ALT_M,
            flight_time_cap_s=P4_FLIGHT_CAP_S,
        )
        checks = ("S2", "S6")
        scene, geo = _p4(rng, GuidanceConfig().search_yaw_rate)
    elif scenario == "P5":
        prime, checks, camera = standoff, ("S1", "S6"), P5_CAMERA
        scene, geo = _p5(rng, prime)
    elif scenario == "P6":
        prime, checks, camera = touch, ("S2",), P6_CAMERA
        scene, geo = _s2_balloon(rng, prime)
    else:  # P7
        prime, checks = touch, ("S2",)
        scene, geo = _s2_balloon(rng, prime)
        kw["airframe"] = CONE_X
    return World(
        scenario=scenario,
        seed=seed,
        prime=prime,
        camera=camera,
        target=TARGET_NAME,
        scene=tuple(scene),
        geometry=geo,
        checks_as=checks,
        **kw,
    )
