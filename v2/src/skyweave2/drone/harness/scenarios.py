"""Closed-loop scenarios S1-S6 (DRONE_CONTRACTS_D0.md §8): seeded worlds and scripts.

A scenario is two things, both pure (no clock, no I/O, randomness only from
the run's seed):

- A :class:`World`: the trial's prime parameters, the synthetic camera's
  error model, and the truth objects, drawn from the seed ([S7]).
- A :class:`Script`: the scripted human at the ground UI (prime, approve,
  mark complete, abort, sent as command packets through the core's command
  path) and the scripted pilot at the radio (the 3-position mode switch,
  brief 3.11), plus the scenario's fault injections (detection dropout, FC
  link blackout). It reacts only to what a human could see: the mission
  state packets and acks the companion publishes ([P3], [P5]) and the GCS
  readiness messages on the pilot link. It never reads truth or the core.

The closed loop that executes the actions is ``sitl_loop``.

Choices made before any run, and why (all Provisional, E1 harness inputs;
never tuned on gate seeds):

- Camera error model: :data:`NOMINAL_CAMERA` is ``camera_sim.PROVISIONAL_NOISE``
  (1 px center jitter, 3 % log-normal size jitter, 0.5 deg pitch and yaw
  boresight error) with its random per-frame dropout set to zero. Dropout is
  the fault S3 injects on a script; it is not part of a "nominal" world. A
  random dropout also makes S1 unreachable by construction: [G7] restarts the
  hold on any coasting packet, so a hold of ``hold_time_s`` at 60 fps needs
  600 consecutive hits, which at the preset's 2 % per-frame rate happens with
  probability 0.98^600 (about 5e-6). That interaction is a finding, not a
  harness setting to hide.
- Geometry: the target is ahead within +-90 deg of the initial heading (north)
  at 11-26 m and 1-5 m above the search altitude, so it is above the drone's
  horizon during search (brief 3.9) and inside the 60 m geofence.
- The human approves ``approve_settle_ms + HUMAN_REACTION_MS`` after the
  candidate appears in a mission state packet ([M8], contract §8: "waiting at
  least approve_settle_ms after the candidate appears").
- The pilot holds the mode switch in MANUAL until the human's prime is
  accepted, then moves it to GUIDED (T03 is edge-gated, E1-D4).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

import numpy as np

from skyweave2.drone.harness.camera_sim import PROVISIONAL_NOISE, CameraSimConfig
from skyweave2.drone.harness.scorecard import (
    Check,
    RunTrace,
    check_lock_retained,
    check_reacquired,
)
from skyweave2.drone.harness.seeds import SCENARIOS
from skyweave2.drone.harness.targets import CrossingBird, SlowKite, StaticBalloon, Trajectory
from skyweave2.drone.packets import (
    AckPacket,
    AckResult,
    CommandName,
    MissionState,
    MissionStatePacket,
    PrimeParams,
    TrialType,
)

# ---------------------------------------------------------------------------
# Constants (Provisional, E1 harness; chosen before any run)
# ---------------------------------------------------------------------------

NOMINAL_CAMERA = replace(PROVISIONAL_NOISE, dropout_p=0.0)
"""The nominal detection world (see the module docstring)."""

READY_MARGIN_MS = 3_000
"""The human primes this long after the GCS shows the EKF using GPS on both
cores and the pre-arm checks passing (ArduCopter refuses a GUIDED arm before
the EKF uses GPS; sitl.py)."""

SWITCH_DELAY_MS = 1_000
"""The pilot moves the switch into GUIDED this long after the prime's ack, so
a tick sees the MANUAL mode after the prime (T03 edge gate, E1-D4)."""

HUMAN_REACTION_MS = 200
"""Added to ``approve_settle_ms`` before the human's approve ([M8])."""

END_AFTER_LAND_MS = 1_000
"""A run ends this long after the mission enters LAND (RETURN then LAND is the
last row any scenario checks)."""

LAUNCH_STUCK_MS = 30_000
"""A run that stays in LAUNCH this long ends: the arm or takeoff was refused
and the mission has no LAUNCH timeout (E1-F11)."""

# S3 detection dropout, windows on t_cap relative to the first T07.
S3_SHORT_AT_MS = 3_000
S3_SHORT_MS = 160  # 9-10 frames at 60 fps: < coast_cap (20 frames)
S3_LONG_AT_MS = 7_000
S3_LONG_MS = 700  # 42 frames at 60 fps: > coast_cap + 1 (21 frames)
S3_COMPLETE_AFTER_REACQUIRE_MS = 3_000

# S4 FC-link blackout, from the commit.
S4_BLACKOUT_MS = 3_000  # > fc_link_bound_ms (1000), so T18 fires; GUID_TIMEOUT is 3 s

# S5 late abort, after the commit (the [G5] fly phase lasts about 1 s).
S5_ABORT_AFTER_COMMIT_MS = 400

# S6 maneuver and distractor, relative to the first T07.
S6_KITE_START_MS = 1_000
S6_KITE_OUT_MS = 4_000
S6_KITE_TURN_S = 1.0
S6_BIRD_CROSS_MS = 7_500
S6_BIRD_HALF_WINDOW_MS = 2_000
S6_BIRD_SPEED_MPS = 4.0
S6_BIRD_STANDOFF_FRAC = 0.5  # the bird crosses halfway between the drone's standoff and the kite
S6_COMPLETE_MS = 13_000

TRIAL_TYPES: Mapping[str, TrialType] = {
    "S1": TrialType.STANDOFF,
    "S2": TrialType.TOUCH,
    "S3": TrialType.STANDOFF,
    "S4": TrialType.TOUCH,
    "S5": TrialType.TOUCH,
    "S6": TrialType.STANDOFF,
}

TARGET_NAME = "target"
DISTRACTOR_NAME = "bird"
_FLYING = frozenset(
    {
        MissionState.SEARCH,
        MissionState.ACQUIRING,
        MissionState.ENGAGED,
        MissionState.COASTING,
        MissionState.TOUCH,
        MissionState.LOST,
    }
)
_WORLD_STREAM = 1  # SeedSequence spawn keys: the camera uses default_rng([seed, k])
_COMMAND_STREAM = 2


class SwitchPosition(str, Enum):
    """The radio's 3-position switch (brief 3.11)."""

    MANUAL = "MANUAL"
    GUIDED = "GUIDED"
    RTL = "RTL"


SWITCH_PWM: Mapping[SwitchPosition, int] = {
    SwitchPosition.MANUAL: 1000,
    SwitchPosition.GUIDED: 1500,
    SwitchPosition.RTL: 2000,
}
"""Channel 5 (``FLTMODE_CH`` in copter.parm) PWM per switch position."""

FLTMODE_PARAMS: Mapping[str, float] = {
    "FLTMODE1": 0.0,  # STABILIZE: "MANUAL" on a copter (position 1, <= 1230 us)
    "FLTMODE2": 0.0,
    "FLTMODE3": 0.0,
    "FLTMODE4": 4.0,  # GUIDED (position 4, 1491-1620 us: the 1500 us centre)
    "FLTMODE5": 4.0,
    "FLTMODE6": 6.0,  # RTL (position 6, >= 1750 us)
}
"""SITL defaults-file entries (never a parameter write) that make the switch
MANUAL / GUIDED / RTL at 1000 / 1500 / 2000 us (brief 3.11)."""

APPROVE_IDLE_PWM = 1000
"""The radio approve switch rests low: a valid sample below ``approve_pwm_high``
keeps the [F7] detector armed. The scripted human approves through the UI
command path (contract §8), so the switch is never flipped."""


def stream_rng(seed: int, stream: int) -> np.random.Generator:
    """An independent generator for one use of the run seed."""
    return np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(stream,)))


# ---------------------------------------------------------------------------
# Truth objects the script changes during a run
# ---------------------------------------------------------------------------


class ManeuveringKite:
    """S6 target: held at its anchor until the script starts its maneuver.

    The maneuver is a :class:`targets.SlowKite` from the anchor, started at a
    future time, so positions already rendered never change.
    """

    def __init__(self, anchor_ned: Sequence[float], *, width_m: float, height_m: float) -> None:
        self.name = TARGET_NAME
        self.width_m = width_m
        self.height_m = height_m
        self.anchor = np.asarray(anchor_ned, dtype=float)
        self.motion: SlowKite | None = None

    def start(self, motion: SlowKite) -> None:
        if self.motion is not None:
            raise RuntimeError("the kite maneuver starts once")
        self.motion = motion

    def position(self, t_ms: int) -> np.ndarray:
        if self.motion is None or t_ms < self.motion.t0_ms:
            return self.anchor.copy()
        return self.motion.position(t_ms)

    def present(self, t_ms: int) -> bool:
        return True


class WindowedObject:
    """A distractor that is in the world only inside ``[t_from, t_to)``, once started."""

    def __init__(self, name: str, *, width_m: float, height_m: float) -> None:
        self.name = name
        self.width_m = width_m
        self.height_m = height_m
        self.inner: Trajectory | None = None
        self.t_from = 0
        self.t_to = 0

    def start(self, inner: Trajectory, t_from: int, t_to: int) -> None:
        if self.inner is not None:
            raise RuntimeError(f"{self.name} starts once")
        self.inner, self.t_from, self.t_to = inner, t_from, t_to

    def position(self, t_ms: int) -> np.ndarray:
        if self.inner is None:
            return np.zeros(3)
        return self.inner.position(t_ms)

    def present(self, t_ms: int) -> bool:
        return self.inner is not None and self.t_from <= t_ms < self.t_to


# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class World:
    """One run's world: trial, camera error model, truth objects (seeded)."""

    scenario: str
    seed: int
    prime: PrimeParams
    camera: CameraSimConfig
    target: str
    scene: tuple[Any, ...]  # Trajectory objects; S6 holds mutable ones
    geometry: Mapping[str, float]  # the drawn numbers, for the run log

    def to_obj(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "seed": self.seed,
            "prime": self.prime.to_obj(),
            "camera": self.camera.to_obj(),
            "target": self.target,
            "objects": [o.name for o in self.scene],
            "geometry": {k: float(v) for k, v in self.geometry.items()},
        }


def _anchor(
    rng: np.random.Generator, r: tuple[float, float], bearing_deg: float, alt: tuple[float, float]
):
    rng_m = float(rng.uniform(*r))
    brg = math.radians(float(rng.uniform(-bearing_deg, bearing_deg)))
    alt_m = float(rng.uniform(*alt))
    ned = (rng_m * math.cos(brg), rng_m * math.sin(brg), -alt_m)
    return ned, {"range_m": rng_m, "bearing_rad": brg, "alt_m": alt_m}


def build_world(scenario: str, seed: int) -> World:
    """The scenario's world for ``seed`` ([S7]); deterministic in both."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError(f"seed must be a 32-bit unsigned int, got {seed!r}")
    rng = stream_rng(seed, _WORLD_STREAM)
    prime = PrimeParams(trial_type=TRIAL_TYPES[scenario])  # contract §9 defaults
    width = prime.target_width_m
    scene: list[Any]
    if scenario in ("S1", "S3"):
        ned, geo = _anchor(rng, (18.0, 26.0), 90.0, (12.0, 15.0))
        scene = [StaticBalloon(anchor_ned=ned, name=TARGET_NAME, width_m=width, height_m=width)]
    elif scenario in ("S2", "S4", "S5"):
        ned, geo = _anchor(rng, (15.0, 22.0), 90.0, (11.0, 14.0))
        scene = [StaticBalloon(anchor_ned=ned, name=TARGET_NAME, width_m=width, height_m=width)]
    else:  # S6
        ned, geo = _anchor(rng, (15.0, 20.0), 60.0, (12.0, 14.0))
        geo["kite_speed_mps"] = float(rng.uniform(1.0, 1.5))
        geo["kite_side"] = float(rng.choice((-1.0, 1.0)))
        geo["bird_side"] = float(rng.choice((-1.0, 1.0)))
        scene = [
            ManeuveringKite(ned, width_m=width, height_m=width),
            WindowedObject(DISTRACTOR_NAME, width_m=0.5, height_m=0.2),
        ]
    return World(
        scenario=scenario,
        seed=seed,
        prime=prime,
        camera=NOMINAL_CAMERA,
        target=TARGET_NAME,
        scene=tuple(scene),
        geometry=geo,
    )


# ---------------------------------------------------------------------------
# Script actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SendCommand:
    """The human sends one UI command packet ([P5]) at ``t_ms``."""

    t_ms: int
    command: CommandName
    cmd_id: str
    params: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class SetSwitch:
    """The pilot moves the 3-position mode switch at ``t_ms``."""

    t_ms: int
    position: SwitchPosition


@dataclass(frozen=True, kw_only=True)
class SetBlackout:
    """The FC link (SERIAL1 relay) drops every byte, both ways, while ``on``."""

    t_ms: int
    on: bool


Action = SendCommand | SetSwitch | SetBlackout


@dataclass(frozen=True, kw_only=True)
class DropoutWindow:
    """A scripted detection dropout on ``t_cap``, ``[start_ms, end_ms)``."""

    start_ms: int
    end_ms: int
    kind: str  # "short" or "long"


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    action: Action
    key: str


@dataclass
class ScriptLog:
    """What the script did, for the run log (no wall clock)."""

    notes: list[tuple[int, str]] = field(default_factory=list)


class Script:
    """The scripted human (UI) and pilot (radio) of one run, plus fault injection.

    The loop calls :meth:`on_ready` once the GCS shows the vehicle ready,
    :meth:`on_output` with every mission state packet and ack the companion
    publishes, and drains :meth:`pop_due` in time order with its other
    inputs. :attr:`dropouts` and the S6 objects change the world only at
    future times.
    """

    def __init__(self, world: World, *, approve_settle_ms: int, camera_latency_ms: int) -> None:
        self.world = world
        self.settle_ms = approve_settle_ms
        self.latency_ms = camera_latency_ms
        self.initial_switch = SwitchPosition.MANUAL
        self.dropouts: list[DropoutWindow] = []
        self.end_at: int | None = None
        self.log = ScriptLog()
        self._rng = stream_rng(world.seed, _COMMAND_STREAM)
        self._queue: list[_Pending] = []
        self._state: MissionState | None = None
        self._ready = False
        self._prime_id: str | None = None
        self._primed = False
        self._t07: list[int] = []
        self._commit_t: int | None = None
        self._launch_t: int | None = None

    # -- queue ----------------------------------------------------------------

    def _cmd_id(self) -> str:
        """A fresh random id per command (the UI rule, [P5]), drawn from the seed."""
        return "h-" + self._rng.bytes(12).hex()

    def _push(self, action: Action, key: str = "") -> None:
        self._queue.append(_Pending(action=action, key=key))
        self._queue.sort(key=lambda p: p.action.t_ms)

    def _cancel(self, key: str) -> None:
        self._queue = [p for p in self._queue if p.key != key]

    def _command(self, t: int, command: CommandName, key: str = "", **kw: Any) -> str:
        cmd_id = self._cmd_id()
        self._push(SendCommand(t_ms=t, command=command, cmd_id=cmd_id, **kw), key)
        self.log.notes.append((t, f"schedule {command.value} {cmd_id}"))
        return cmd_id

    def next_time(self) -> int | None:
        return self._queue[0].action.t_ms if self._queue else None

    def pop_due(self, t_ms: int) -> list[Action]:
        """Actions due at or before ``t_ms``, in time order."""
        due = [p.action for p in self._queue if p.action.t_ms <= t_ms]
        self._queue = [p for p in self._queue if p.action.t_ms > t_ms]
        return due

    # -- observations ---------------------------------------------------------

    def on_ready(self, t: int) -> None:
        """The GCS shows the EKF on GPS and the pre-arm checks passing."""
        if self._ready:
            return
        self._ready = True
        self._prime_id = self._command(
            t + READY_MARGIN_MS, CommandName.PRIME, params=self.world.prime.to_obj()
        )

    def on_output(
        self, t: int, states: Sequence[MissionStatePacket], acks: Sequence[AckPacket]
    ) -> None:
        """What the UI shows after one input, in production order."""
        for ack in acks:
            self._on_ack(t, ack)
        for pkt in states:
            for ev in pkt.events:
                self._on_event(ev.t, ev.name)
            self._state = pkt.mission_state

    def flying(self) -> bool:
        """The published state is in SEARCH..LOST (the [S8] altitude-floor states)."""
        return self._state in _FLYING

    def stuck(self, t: int) -> str | None:
        """A reason to end the run early, or ``None``."""
        if self._launch_t is not None and self._state is MissionState.LAUNCH:
            if t - self._launch_t > LAUNCH_STUCK_MS:
                return "launch_stuck"
        return None

    def _on_ack(self, t: int, ack: AckPacket) -> None:
        if ack.cmd_id == self._prime_id and not self._primed:
            if ack.result is AckResult.ACCEPTED:
                self._primed = True
                self._push(SetSwitch(t_ms=t + SWITCH_DELAY_MS, position=SwitchPosition.GUIDED))
            else:
                self.log.notes.append((t, f"prime {ack.result.value}"))

    def _on_event(self, t: int, name: str) -> None:
        sc = self.world.scenario
        if name.startswith("candidate:"):
            # [M8], §8: no sooner than approve_settle_ms after the candidate appears.
            self._cancel("approve")
            self._command(
                t + self.settle_ms + HUMAN_REACTION_MS, CommandName.APPROVE_ENGAGE, key="approve"
            )
        elif name.startswith("transition:ACQUIRING->"):
            self._cancel("approve")  # the candidate is gone (T06) or engaged (T07)
        if name == "transition:PRIMED->LAUNCH":
            self._launch_t = t
        if name == "transition:ACQUIRING->ENGAGED":
            self._t07.append(t)
            self._on_t07(t, len(self._t07))
        if name.startswith("commit:") and self._commit_t is None:
            self._commit_t = t
            if sc == "S4":
                self._push(SetBlackout(t_ms=t, on=True))
                self._push(SetBlackout(t_ms=t + S4_BLACKOUT_MS, on=False))
            elif sc == "S5":
                self._command(t + S5_ABORT_AFTER_COMMIT_MS, CommandName.ABORT)
        if name.startswith("transition:") and name.endswith("->LAND") and self.end_at is None:
            self.end_at = t + END_AFTER_LAND_MS

    def _on_t07(self, t: int, n: int) -> None:
        sc = self.world.scenario
        if sc == "S3":
            if n == 1:
                self.dropouts.append(
                    DropoutWindow(
                        start_ms=t + S3_SHORT_AT_MS,
                        end_ms=t + S3_SHORT_AT_MS + S3_SHORT_MS,
                        kind="short",
                    )
                )
                self.dropouts.append(
                    DropoutWindow(
                        start_ms=t + S3_LONG_AT_MS,
                        end_ms=t + S3_LONG_AT_MS + S3_LONG_MS,
                        kind="long",
                    )
                )
            elif n == 2:
                self._command(t + S3_COMPLETE_AFTER_REACQUIRE_MS, CommandName.MARK_COMPLETE)
        elif sc == "S6" and n == 1:
            self._start_s6(t)
            self._command(t + S6_COMPLETE_MS, CommandName.MARK_COMPLETE)

    def _start_s6(self, t7: int) -> None:
        """Kite maneuver and crossing bird, timed from the first T07 (future times only)."""
        kite, bird = self.world.scene
        geo = self.world.geometry
        anchor = kite.anchor
        u = np.array([anchor[0], anchor[1], 0.0])  # home -> kite: the approach direction
        u /= float(np.linalg.norm(u))
        lateral = np.array([-u[1], u[0], 0.0])  # 90 deg right of the approach direction
        v_kite = geo["kite_speed_mps"] * geo["kite_side"] * lateral
        t_move = t7 + S6_KITE_START_MS
        kite.start(
            SlowKite(
                start_ned=tuple(float(x) for x in anchor),
                velocity_ned=tuple(float(x) for x in v_kite),
                t0_ms=t_move,
                t_reverse_ms=t_move + S6_KITE_OUT_MS,
                turn_s=S6_KITE_TURN_S,
                name=TARGET_NAME,
                width_m=kite.width_m,
                height_m=kite.height_m,
            )
        )
        t_cross = t7 + S6_BIRD_CROSS_MS
        d_s = self.world.prime.d_s
        cross = kite.position(t_cross) - (S6_BIRD_STANDOFF_FRAC * d_s) * u
        v_bird = S6_BIRD_SPEED_MPS * geo["bird_side"] * lateral
        start = cross - v_bird * (S6_BIRD_HALF_WINDOW_MS / 1000.0)
        bird.start(
            CrossingBird(
                start_ned=tuple(float(x) for x in start),
                velocity_ned=tuple(float(x) for x in v_bird),
                t0_ms=t_cross - S6_BIRD_HALF_WINDOW_MS,
                name=DISTRACTOR_NAME,
                width_m=bird.width_m,
                height_m=bird.height_m,
            ),
            t_cross - S6_BIRD_HALF_WINDOW_MS,
            t_cross + S6_BIRD_HALF_WINDOW_MS,
        )
        self.log.notes.append((t7, f"s6 kite moves at {t_move}, bird crosses at {t_cross}"))

    # -- scenario checks the scorecard cannot build by itself -----------------

    def extra_checks(self, trace: RunTrace) -> list[Check]:
        """S3's two dropout checks ([S8] table), on delivery-time (``t_rx``) windows."""
        if self.world.scenario != "S3":
            return []
        windows = {w.kind: w for w in self.dropouts}
        lat = self.latency_ms
        checks: list[Check] = []
        short = windows.get("short")
        if short is None:
            checks.append(Check(name="lock_retained", passed=False))
        else:
            checks.append(
                check_lock_retained(trace, t0_ms=short.start_ms + lat, t1_ms=short.end_ms + lat)
            )
        long = windows.get("long")
        if long is None:
            checks.append(Check(name="reacquired_before_budget", passed=False))
        else:
            checks.append(check_reacquired(trace, after_ms=long.start_ms + lat))
        return checks
