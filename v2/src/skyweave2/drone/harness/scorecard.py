"""[S8] scorecard: metrics, safety floors, and pass/fail per check, from one run's trace.

A run trace has three parts:

- The run's recording records ([R1], [R2]), in file order: what the
  companion saw and did.
- The synthetic camera's per-frame image truth (``camera_sim.FrameTruth``),
  keyed by ``t_cap``.
- Truth samples: the vehicle's true pose and velocity (SITL ``SIM_STATE``,
  read on the harness's own connection, [S0]) and every truth object's
  position at the same instants.

Truth never enters the recording ([S0]: it feeds only the camera and this
scorer).

Times ([S8]): every time is a recording ``t_rx`` on the SITL clock. "The
state at ``t``" means the state after every input stamped at or before
``t``: the newest ``mission_state`` packet with ``t <= t``. Every transition
publishes at once with its input's stamp ([P3]), so this is exact for state,
and exact for ``engaged_track_id`` inside the lock window. An [M9] clear,
which publishes only on the next periodic packet, happens only outside that
window.

Metric definitions follow [S8] literally. Where it leaves a choice, the
choice is written at the function and was made before any run:

- p95 is the nearest-rank percentile: the ``ceil(0.95 n)``-th smallest
  value, which is always an observed value.
- The commit-plane miss uses the vehicle's true velocity at its closest
  approach as "the terminal velocity".
- Attribution ties go to the earlier object in scene order.

The scorer computes its geometry (the standoff point, the plane crossing)
itself, from the contract formulas. It does not import guidance's code: a
judge should not share a bug with the code it judges.

Output is canonical JSON ([C5], ``packets.canonical_json``) with no wall-clock
values: every number comes from the trace, the seed, and the declared
thresholds. Thresholds are the contract §9 numbers (Provisional, E1, declared
before the first run).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.harness.camera_sim import FrameTruth, Pose
from skyweave2.drone.harness.seeds import SeedSet, check_seed, gate_seeds
from skyweave2.drone.harness.targets import Vec3
from skyweave2.drone.packets import (
    AckPacket,
    AckResult,
    CommandName,
    CommandPacket,
    DetectionPacket,
    InvalidParams,
    MissionState,
    MissionStatePacket,
    PrimeParams,
    TrackPacket,
    canonical_json,
)
from skyweave2.drone.recording import Record, Stream
from skyweave2.drone.vehicle_state import mode_name, parse_frames

FORMAT = "skyweave-drone-scorecard"
FORMAT_V = 1

S = MissionState
LOCK_STATES = frozenset({S.ENGAGED, S.COASTING})
FLOOR_STATES = frozenset({S.SEARCH, S.ACQUIRING, S.ENGAGED, S.COASTING, S.TOUCH, S.LOST})
"""[S8] altitude floor: ``A`` minus ``LAUNCH``."""
_LOCK_WINDOW_END = frozenset({S.COMPLETE, S.RETURN, S.ABORT, S.LAND})
_SETPOINT_REL_TOL = 1e-6  # setpoints travel as float32 ([R3]); a 2.5 m/s vector may read 2.5000001
_RTL = "RTL"


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TruthSample:
    """Vehicle truth plus every truth object's NED position at one SITL instant."""

    t_ms: int
    pose: Pose
    vel_ned: Vec3
    objects: Mapping[str, Vec3]


@dataclass(frozen=True, kw_only=True)
class RunTrace:
    """One run: its recording, the camera's frame truth, and the truth samples.

    ``target`` names the truth object that is the trial's target; the others
    are distractors.
    """

    records: tuple[Record, ...]
    frames: tuple[FrameTruth, ...]
    truth: tuple[TruthSample, ...]
    target: str


@dataclass(frozen=True, kw_only=True)
class Thresholds:
    """Contract §9 pass/fail numbers (all Provisional, E1, declared before the first run).

    ``hold_tol_m`` and ``hold_time_s`` are the [G7] guidance constants; pass
    the run's guidance config values when they differ from §9.
    """

    miss_p95_max_m: float = 0.5
    abort_latency_max_ms: int = 500
    lock_retention_min: float = 0.95
    alt_floor_m: float = 2.0
    hold_tol_m: float = 1.0
    hold_time_s: float = 10.0

    def to_obj(self) -> dict[str, Any]:
        return {
            "miss_p95_max_m": float(self.miss_p95_max_m),
            "abort_latency_max_ms": int(self.abort_latency_max_ms),
            "lock_retention_min": float(self.lock_retention_min),
            "alt_floor_m": float(self.alt_floor_m),
            "hold_tol_m": float(self.hold_tol_m),
            "hold_time_s": float(self.hold_time_s),
        }


@dataclass(frozen=True, kw_only=True)
class Check:
    """One pass/fail line of a scorecard."""

    name: str
    passed: bool
    value: float | int | str | None = None
    limit: float | int | str | None = None

    def to_obj(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": bool(self.passed),
            "value": self.value,
            "limit": self.limit,
        }


# ---------------------------------------------------------------------------
# Timeline: the recording, parsed once
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Track:
    idx: int  # record index (file order)
    t_rx: int
    pkt: TrackPacket


@dataclass(frozen=True)
class _Engaged:
    """An engaged-track packet: its track id is the engaged id at its ``t_rx``."""

    idx: int
    t_rx: int
    pkt: TrackPacket
    state: MissionState | None
    attribution: str | None


@dataclass
class _Timeline:
    events: list[tuple[int, str]] = field(default_factory=list)  # (t, name), file order
    transitions: list[tuple[int, str, str]] = field(default_factory=list)  # (t, from, to)
    pub_t: list[int] = field(default_factory=list)  # mission_state packets, file order
    pub_state: list[MissionState] = field(default_factory=list)
    pub_engaged: list[int | None] = field(default_factory=list)
    primes: list[tuple[int, int, PrimeParams]] = field(default_factory=list)  # (idx, t, params)
    aborts: list[tuple[int, int]] = field(default_factory=list)  # (idx, t) accepted aborts
    tracks: list[_Track] = field(default_factory=list)
    detections: list[tuple[int, int]] = field(default_factory=list)  # (t_rx, t_cap)
    mavlink: list[tuple[int, int, str, bytes]] = field(default_factory=list)  # idx, t, dir, raw
    frames: dict[int, FrameTruth] = field(default_factory=dict)

    # -- state over time ------------------------------------------------------

    def _pub_index(self, t: int) -> int:
        return bisect.bisect_right(self.pub_t, t) - 1

    def state_at(self, t: int) -> MissionState | None:
        i = self._pub_index(t)
        return None if i < 0 else self.pub_state[i]

    def engaged_at(self, t: int) -> int | None:
        i = self._pub_index(t)
        return None if i < 0 else self.pub_engaged[i]

    def prime_at(self, t: int) -> PrimeParams | None:
        """The trial in force at ``t``: the newest accepted prime at or before it."""
        times = [p[1] for p in self.primes]
        i = bisect.bisect_right(times, t) - 1
        return None if i < 0 else self.primes[i][2]

    def first_event(self, pred: Callable[[str], bool], start: int = 0) -> int | None:
        for i in range(start, len(self.events)):
            if pred(self.events[i][1]):
                return i
        return None

    def truth_of(self, t_cap: int) -> FrameTruth:
        try:
            return self.frames[t_cap]
        except KeyError:
            raise ValueError(f"trace has no frame truth for t_cap={t_cap}") from None


def _parse(trace: RunTrace) -> _Timeline:
    tl = _Timeline()
    for ft in trace.frames:
        if ft.t_cap in tl.frames:
            raise ValueError(f"two frame truths share t_cap={ft.t_cap}")
        tl.frames[ft.t_cap] = ft
    pending: dict[str, tuple[int, Record]] = {}
    for idx, rec in enumerate(trace.records):
        stream = rec.stream
        if stream is Stream.COMMAND:
            assert isinstance(rec.packet, CommandPacket)
            pending[rec.packet.cmd_id] = (idx, rec)
        elif stream is Stream.ACK:
            assert isinstance(rec.packet, AckPacket)
            hit = pending.pop(rec.packet.cmd_id, None)
            if hit is None or rec.packet.result is not AckResult.ACCEPTED:
                continue  # radio approvals and malformed datagrams have no command record
            cmd_idx, cmd_rec = hit
            cmd = cmd_rec.packet
            assert isinstance(cmd, CommandPacket)
            if cmd.command is CommandName.PRIME:
                try:
                    params = PrimeParams.from_obj(cmd.params)
                except InvalidParams as exc:
                    raise ValueError(f"accepted prime at record {cmd_idx} is invalid") from exc
                tl.primes.append((cmd_idx, cmd_rec.t_rx, params))
            elif cmd.command is CommandName.ABORT:
                tl.aborts.append((cmd_idx, cmd_rec.t_rx))
        elif stream is Stream.MISSION_STATE:
            pkt = rec.packet
            assert isinstance(pkt, MissionStatePacket)
            for ev in pkt.events:
                tl.events.append((ev.t, ev.name))
                if ev.name.startswith("transition:"):
                    frm, _, to = ev.name[len("transition:") :].partition("->")
                    tl.transitions.append((ev.t, frm, to))
            tl.pub_t.append(pkt.t)
            tl.pub_state.append(pkt.mission_state)
            tl.pub_engaged.append(pkt.engaged_track_id)
        elif stream is Stream.TRACK:
            assert isinstance(rec.packet, TrackPacket)
            tl.tracks.append(_Track(idx=idx, t_rx=rec.t_rx, pkt=rec.packet))
        elif stream is Stream.DETECTION:
            assert isinstance(rec.packet, DetectionPacket)
            tl.detections.append((rec.t_rx, rec.packet.t_cap))
        elif stream is Stream.MAVLINK:
            assert rec.direction is not None and rec.raw is not None
            tl.mavlink.append((idx, rec.t_rx, rec.direction, rec.raw))
    if any(b < a for a, b in zip(tl.pub_t, tl.pub_t[1:], strict=False)):
        raise ValueError("mission_state packet stamps go backwards in the recording")
    return tl


def _is_transition_to(*states: MissionState) -> Callable[[str], bool]:
    suffixes = tuple(f"->{s.value}" for s in states)
    return lambda name: name.startswith("transition:") and name.endswith(suffixes)


def _is_transition(frm: MissionState, to: MissionState) -> Callable[[str], bool]:
    target = f"transition:{frm.value}->{to.value}"
    return lambda name: name == target


def _is_commit(name: str) -> bool:
    return name.startswith("commit:")


_IS_T07 = _is_transition(S.ACQUIRING, S.ENGAGED)


# ---------------------------------------------------------------------------
# Metric primitives
# ---------------------------------------------------------------------------


def p95(values: Sequence[float]) -> float:
    """Nearest-rank 95th percentile: the ``ceil(0.95 n)``-th smallest value.

    Always an observed value; for 20 values it is the 19th smallest.
    ``inf`` stands for a run with no value (it sorts last).
    """
    if not values:
        raise ValueError("p95 of an empty set")
    ordered = sorted(float(v) for v in values)
    if any(math.isnan(v) for v in ordered):
        raise ValueError("p95 of a set containing NaN")
    rank = math.ceil(0.95 * len(ordered))
    return ordered[rank - 1]


def attribute(pkt: TrackPacket, truth: FrameTruth) -> str | None:
    """[S8] attribution of one track packet: the truth object whose projected
    center lies inside the box ``u +- w/2``, ``v_px +- h/2`` (edges count);
    the nearest to the box center if several (ties: scene order); ``None``
    ("none") if no object."""
    best: tuple[float, str] | None = None
    for obj in truth.objects:
        if obj.center_px is None:
            continue
        cu, cv = obj.center_px
        if abs(cu - pkt.u) <= pkt.w / 2.0 and abs(cv - pkt.v_px) <= pkt.h / 2.0:
            d = math.hypot(cu - pkt.u, cv - pkt.v_px)
            if best is None or d < best[0]:
                best = (d, obj.name)
    return None if best is None else best[1]


def _engaged_packets(tl: _Timeline) -> list[_Engaged]:
    out: list[_Engaged] = []
    for tr in tl.tracks:
        if tl.engaged_at(tr.t_rx) != tr.pkt.track_id:
            continue
        out.append(
            _Engaged(
                idx=tr.idx,
                t_rx=tr.t_rx,
                pkt=tr.pkt,
                state=tl.state_at(tr.t_rx),
                attribution=attribute(tr.pkt, tl.truth_of(tr.pkt.t_cap)),
            )
        )
    return out


def _t07s(tl: _Timeline) -> list[tuple[int, int | None]]:
    """Each T07 as ``(t, engaged id after it)``, in order."""
    return [(t, tl.engaged_at(t)) for (t, name) in tl.events if _IS_T07(name)]


# ---------------------------------------------------------------------------
# [S8] metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Retargets:
    count: int
    switches: tuple[tuple[int, int, str], ...]  # (t_rx, track_id, attributed object)

    def to_obj(self) -> dict[str, Any]:
        return {"count": self.count, "switches": [list(s) for s in self.switches]}


def _retargets(tl: _Timeline, engaged: list[_Engaged]) -> Retargets:
    """[S8] retarget. Reference: the attribution of the engaged id's newest
    packet at or before the latest T07. A packet attributed to another
    object counts once per switch; "none" never counts and never switches;
    a switch back to the reference is not a retarget."""
    t07s = _t07s(tl)
    t07_times = [t for t, _ in t07s]
    refs: list[str | None] = []
    for t, eid in t07s:
        last = None
        for tr in tl.tracks:
            if tr.t_rx > t:
                break
            if tr.pkt.track_id == eid:
                last = tr
        refs.append(None if last is None else attribute(last.pkt, tl.truth_of(last.pkt.t_cap)))
    count = 0
    switches: list[tuple[int, int, str]] = []
    governing = -1
    reference: str | None = None
    current: str | None = None
    for ep in engaged:
        g = bisect.bisect_right(t07_times, ep.t_rx) - 1
        if g < 0:
            continue
        if g != governing:
            governing, reference = g, refs[g]
            current = reference
        a = ep.attribution
        if a is None or a == current:
            continue
        if a != reference:
            count += 1
            switches.append((ep.t_rx, ep.pkt.track_id, a))
        current = a
    return Retargets(count=count, switches=tuple(switches))


@dataclass(frozen=True, kw_only=True)
class LockRetention:
    fraction: float | None  # None when no frame qualifies (no T07, or target never in view)
    frames: int
    retained: int

    def to_obj(self) -> dict[str, Any]:
        return {"fraction": self.fraction, "frames": self.frames, "retained": self.retained}


def _lock_window(tl: _Timeline) -> tuple[int, int | None] | None:
    """``[first T07, first of commit / COMPLETE / RETURN / ABORT / LAND)``."""
    i07 = tl.first_event(_IS_T07)
    if i07 is None:
        return None
    end_pred = _is_transition_to(*_LOCK_WINDOW_END)
    i_end = tl.first_event(lambda n: _is_commit(n) or end_pred(n), i07 + 1)
    return tl.events[i07][0], (None if i_end is None else tl.events[i_end][0])


def _lock_retention(tl: _Timeline, engaged: list[_Engaged], target: str) -> LockRetention:
    """[S8] lock retention: over detection frames (by ``t_rx``) in the lock
    window where the target is in the camera's view, the fraction where the
    state is ENGAGED or COASTING and the frame's engaged packet is attributed
    to the target."""
    window = _lock_window(tl)
    if window is None:
        return LockRetention(fraction=None, frames=0, retained=0)
    t0, t1 = window
    by_tcap: dict[int, _Engaged] = {}
    for ep in engaged:
        by_tcap.setdefault(ep.pkt.t_cap, ep)
    frames = retained = 0
    for t_rx, t_cap in tl.detections:
        if t_rx < t0 or (t1 is not None and t_rx >= t1):
            continue
        if not tl.truth_of(t_cap).get(target).in_fov:
            continue
        frames += 1
        ep = by_tcap.get(t_cap)
        if ep is not None and ep.state in LOCK_STATES and ep.attribution == target:
            retained += 1
    return LockRetention(
        fraction=None if frames == 0 else retained / frames, frames=frames, retained=retained
    )


@dataclass(frozen=True, kw_only=True)
class HoldError:
    p95_m: float
    max_m: float
    samples: int
    t_hold_complete: int

    def to_obj(self) -> dict[str, Any]:
        return {
            "p95_m": self.p95_m,
            "max_m": self.max_m,
            "samples": self.samples,
            "t_hold_complete": self.t_hold_complete,
        }


def _standoff_error(s: TruthSample, target: str, d_s: float) -> float:
    """True ``|q|``: the vehicle's distance to the [G3] standoff point, level
    with the target and ``d_s`` short of it horizontally."""
    p = np.asarray(s.objects[target], dtype=float) - np.asarray(s.pose.pos_ned, dtype=float)
    n = math.hypot(p[0], p[1])
    if n < 0.01:
        u_h = np.array([math.cos(s.pose.yaw), math.sin(s.pose.yaw), 0.0])
    else:
        u_h = np.array([p[0] / n, p[1] / n, 0.0])
    return float(np.linalg.norm(p - d_s * u_h))


def _hold_error(
    tl: _Timeline, truth: Sequence[TruthSample], target: str, hold_time_s: float
) -> HoldError | None:
    """[S8] hold error: the true standoff error over the [G7] hold window,
    ``[t_hold_complete - hold_time_s, t_hold_complete]``."""
    i = tl.first_event(lambda n: n == "hold_complete")
    if i is None:
        return None
    t_hc = tl.events[i][0]
    trial = tl.prime_at(t_hc)
    if trial is None:
        return None
    t_from = t_hc - round(hold_time_s * 1000.0)
    errors = [_standoff_error(s, target, trial.d_s) for s in truth if t_from <= s.t_ms <= t_hc]
    if not errors:
        return None
    return HoldError(
        p95_m=p95(errors), max_m=max(errors), samples=len(errors), t_hold_complete=t_hc
    )


@dataclass(frozen=True, kw_only=True)
class CommitPlaneMiss:
    miss_m: float | None  # None: the vehicle never crossed the plane
    closest_approach_m: float
    t_closest: int

    def to_obj(self) -> dict[str, Any]:
        return {
            "miss_m": self.miss_m,
            "closest_approach_m": self.closest_approach_m,
            "t_closest": self.t_closest,
        }


def _commit_plane_miss(
    tl: _Timeline, truth: Sequence[TruthSample], target: str
) -> CommitPlaneMiss | None:
    """[S8] commit-plane miss over the terminal segment (first commit to the
    first transition out of TOUCH).

    Closest approach: the truth sample nearest the target. The plane passes
    through the target there, normal to the vehicle's true velocity at that
    sample (the terminal velocity). The crossing is interpolated linearly
    between the two samples where the vehicle passes the plane along the
    normal, and the miss is the distance from the target center to the
    crossing point.
    """
    ic = tl.first_event(_is_commit)
    if ic is None:
        return None
    t_c = tl.events[ic][0]
    i_out = tl.first_event(lambda n: n.startswith("transition:TOUCH->"), ic + 1)
    t_end = None if i_out is None else tl.events[i_out][0]
    window = [s for s in truth if s.t_ms >= t_c and (t_end is None or s.t_ms <= t_end)]
    if not window:
        return None
    pos = np.array([s.pose.pos_ned for s in window], dtype=float)
    tgt = np.array([s.objects[target] for s in window], dtype=float)
    dist = np.linalg.norm(pos - tgt, axis=1)
    k = int(np.argmin(dist))
    closest = CommitPlaneMiss(
        miss_m=None, closest_approach_m=float(dist[k]), t_closest=window[k].t_ms
    )
    vel = np.asarray(window[k].vel_ned, dtype=float)
    speed = float(np.linalg.norm(vel))
    if speed < 1e-9:
        return closest
    n = vel / speed
    c = tgt[k]
    side = (pos - c) @ n
    best: tuple[int, float] | None = None
    for i in range(len(window) - 1):
        if side[i] < 0.0 <= side[i + 1]:
            gap = min(abs(i - k), abs(i + 1 - k))
            if best is None or gap < best[0]:
                frac = -side[i] / (side[i + 1] - side[i])
                x = pos[i] + frac * (pos[i + 1] - pos[i])
                best = (gap, float(np.linalg.norm(x - c)))
    if best is None:
        return closest
    return CommitPlaneMiss(
        miss_m=best[1], closest_approach_m=closest.closest_approach_m, t_closest=closest.t_closest
    )


def _fc_rx(tl: _Timeline, after_idx: int, fc_ids: tuple[int, int]) -> Iterator[tuple[int, Any]]:
    """FC frames received after record ``after_idx``, as ``(t_rx, msg)``."""
    for idx, t, direction, raw in tl.mavlink:
        if idx <= after_idx or direction != "rx":
            continue
        for msg in parse_frames(raw):
            if (msg.get_srcSystem(), msg.get_srcComponent()) == fc_ids:
                yield t, msg


def _abort_latency(tl: _Timeline, fc_ids: tuple[int, int]) -> int | None:
    """[S8] abort latency: from the first accepted abort's ``command`` record
    to the first FC frame after it that confirms RTL: a ``COMMAND_ACK``
    accepted for a ``DO_SET_MODE`` RTL request sent after the abort, or a
    ``HEARTBEAT`` in RTL."""
    if not tl.aborts:
        return None
    a_idx, a_t = tl.aborts[0]
    rtl_requested = False
    for idx, t, direction, raw in tl.mavlink:
        if idx <= a_idx:
            continue
        for msg in parse_frames(raw):
            kind = msg.get_type()
            if direction == "tx":
                if (
                    kind == "COMMAND_LONG"
                    and msg.command == mavlink2.MAV_CMD_DO_SET_MODE
                    and mode_name(int(round(msg.param2))) == _RTL
                ):
                    rtl_requested = True
                continue
            if (msg.get_srcSystem(), msg.get_srcComponent()) != fc_ids:
                continue
            if kind == "HEARTBEAT" and mode_name(msg.custom_mode) == _RTL:
                return t - a_t
            if (
                kind == "COMMAND_ACK"
                and rtl_requested
                and msg.command == mavlink2.MAV_CMD_DO_SET_MODE
                and msg.result == mavlink2.MAV_RESULT_ACCEPTED
            ):
                return t - a_t
    return None


def _fc_mode_seen(tl: _Timeline, mode: str, t_from: int, fc_ids: tuple[int, int]) -> bool:
    """An FC ``HEARTBEAT`` in ``mode`` received at or after ``t_from``."""
    for t, msg in _fc_rx(tl, -1, fc_ids):
        if t >= t_from and msg.get_type() == "HEARTBEAT" and mode_name(msg.custom_mode) == mode:
            return True
    return False


@dataclass(frozen=True, kw_only=True)
class SafetyFloors:
    """[S8] safety floors. Each ``*_ok`` is the floor's verdict over the whole run."""

    geofence_ok: bool
    max_horizontal_m: float | None
    geofence_breaches: int
    alt_ok: bool
    min_alt_m: float | None  # over samples in SEARCH..LOST only
    alt_breaches: int
    setpoint_ok: bool
    max_setpoint_mps: float | None
    setpoint_breaches: int
    setpoints: int

    def to_obj(self) -> dict[str, Any]:
        return {
            "geofence": {
                "ok": self.geofence_ok,
                "max_horizontal_m": self.max_horizontal_m,
                "breaches": self.geofence_breaches,
            },
            "alt_floor": {
                "ok": self.alt_ok,
                "min_alt_m": self.min_alt_m,
                "breaches": self.alt_breaches,
            },
            "setpoint_speed": {
                "ok": self.setpoint_ok,
                "max_mps": self.max_setpoint_mps,
                "breaches": self.setpoint_breaches,
                "setpoints": self.setpoints,
            },
        }


def _safety_floors(tl: _Timeline, truth: Sequence[TruthSample], alt_floor_m: float) -> SafetyFloors:
    """[S8] floors.

    - Geofence: true horizontal distance from home ``<= geofence_radius_m`` of
      the trial in force, at every truth sample after the first accepted prime.
    - Altitude: true altitude above home ``>= alt_floor_m`` at every truth
      sample whose state is in SEARCH..LOST (``A`` minus LAUNCH).
    - Setpoints: every sent ``SET_POSITION_TARGET_LOCAL_NED`` has a 3-D speed
      no faster than the ``v_max`` of the trial in force (a setpoint with no
      trial in force is a breach). The float32 wire precision is allowed for.
    """
    geo_breaches = alt_breaches = 0
    max_h: float | None = None
    min_alt: float | None = None
    for s in truth:
        n, e, d = (float(x) for x in s.pose.pos_ned)
        trial = tl.prime_at(s.t_ms)
        if trial is not None:
            h = math.hypot(n, e)
            max_h = h if max_h is None else max(max_h, h)
            if h > trial.geofence_radius_m:
                geo_breaches += 1
        if tl.state_at(s.t_ms) in FLOOR_STATES:
            alt = -d
            min_alt = alt if min_alt is None else min(min_alt, alt)
            if alt < alt_floor_m:
                alt_breaches += 1
    sp_breaches = sp_count = 0
    max_sp: float | None = None
    for _idx, t, direction, raw in tl.mavlink:
        if direction != "tx":
            continue
        for msg in parse_frames(raw):
            if msg.get_type() != "SET_POSITION_TARGET_LOCAL_NED":
                continue
            sp_count += 1
            speed = math.sqrt(msg.vx * msg.vx + msg.vy * msg.vy + msg.vz * msg.vz)
            max_sp = speed if max_sp is None else max(max_sp, speed)
            trial = tl.prime_at(t)
            if trial is None or speed > trial.v_max * (1.0 + _SETPOINT_REL_TOL):
                sp_breaches += 1
    return SafetyFloors(
        geofence_ok=geo_breaches == 0,
        max_horizontal_m=max_h,
        geofence_breaches=geo_breaches,
        alt_ok=alt_breaches == 0,
        min_alt_m=min_alt,
        alt_breaches=alt_breaches,
        setpoint_ok=sp_breaches == 0,
        max_setpoint_mps=max_sp,
        setpoint_breaches=sp_breaches,
        setpoints=sp_count,
    )


# ---------------------------------------------------------------------------
# Public metric entry points (each parses the trace once)
# ---------------------------------------------------------------------------


def retargets(trace: RunTrace) -> Retargets:
    tl = _parse(trace)
    return _retargets(tl, _engaged_packets(tl))


def lock_retention(trace: RunTrace) -> LockRetention:
    tl = _parse(trace)
    return _lock_retention(tl, _engaged_packets(tl), trace.target)


def hold_error(trace: RunTrace, *, hold_time_s: float) -> HoldError | None:
    return _hold_error(_parse(trace), trace.truth, trace.target, hold_time_s)


def commit_plane_miss(trace: RunTrace) -> CommitPlaneMiss | None:
    return _commit_plane_miss(_parse(trace), trace.truth, trace.target)


def abort_latency_ms(trace: RunTrace, *, fc_ids: tuple[int, int] = (1, 1)) -> int | None:
    return _abort_latency(_parse(trace), fc_ids)


def safety_floors(trace: RunTrace, *, alt_floor_m: float) -> SafetyFloors:
    return _safety_floors(_parse(trace), trace.truth, alt_floor_m)


# ---------------------------------------------------------------------------
# Scenario checks (contract §8 table)
# ---------------------------------------------------------------------------


def _sequence(tl: _Timeline, preds: Sequence[Callable[[str], bool]], start: int = 0) -> bool:
    """The events contain ``preds`` as an ordered subsequence from ``start``."""
    i = start
    for pred in preds:
        hit = tl.first_event(pred, i)
        if hit is None:
            return False
        i = hit + 1
    return True


def _seq_check(name: str, tl: _Timeline, preds: Sequence[Callable[[str], bool]]) -> Check:
    return Check(name=name, passed=_sequence(tl, preds))


def check_lock_retained(
    trace: RunTrace, *, t0_ms: int, t1_ms: int, name: str = "lock_retained"
) -> Check:
    """S3 short dropout: the lock survives a dropout window ``[t0, t1)``.

    Passes when an engaged id exists at ``t0``, a packet of that id is engaged
    again with a fresh hit (``misses = 0``) at or after ``t1``, and no
    transition into LOST happens in between. ``name`` keeps checks for several
    windows apart in one scorecard.
    """
    tl = _parse(trace)
    eid = tl.engaged_at(t0_ms)
    if eid is None:
        return Check(name=name, passed=False, value=None)
    rehit = next(
        (
            ep
            for ep in _engaged_packets(tl)
            if ep.t_rx >= t1_ms and ep.pkt.track_id == eid and ep.pkt.misses == 0
        ),
        None,
    )
    if rehit is None:
        return Check(name=name, passed=False, value=eid)
    to_lost = _is_transition_to(S.LOST)
    lost = any(t0_ms <= t <= rehit.t_rx and to_lost(n) for t, n in tl.events)
    return Check(name=name, passed=not lost, value=eid)


def check_reacquired(
    trace: RunTrace, *, after_ms: int, name: str = "reacquired_before_budget"
) -> Check:
    """S3 long dropout: after ``after_ms`` the mission goes LOST, then SEARCH,
    then re-engages (T07) before any ``budget:reacquire`` event."""
    tl = _parse(trace)
    start = next((i for i, (t, _) in enumerate(tl.events) if t >= after_ms), len(tl.events))
    i_lost = tl.first_event(_is_transition_to(S.LOST), start)
    i_search = (
        None if i_lost is None else tl.first_event(_is_transition(S.LOST, S.SEARCH), i_lost + 1)
    )
    i_t07 = None if i_search is None else tl.first_event(_IS_T07, i_search + 1)
    if i_lost is None or i_t07 is None:
        return Check(name=name, passed=False)
    i_budget = tl.first_event(lambda n: n == "budget:reacquire", i_lost + 1)
    ok = i_budget is None or i_budget > i_t07
    return Check(name=name, passed=ok, value=tl.events[i_t07][0])


def _scenario_checks(
    scenario: str,
    tl: _Timeline,
    metrics: Mapping[str, Any],
    thresholds: Thresholds,
    fc_ids: tuple[int, int],
) -> list[Check]:
    to_return = _is_transition_to(S.RETURN)
    return_then_land = _seq_check(
        "return_then_land", tl, [to_return, _is_transition(S.RETURN, S.LAND)]
    )
    checks: list[Check] = []
    if scenario == "S1":
        hold = metrics["hold_error"]
        p = None if hold is None else hold.p95_m
        checks += [
            _seq_check(
                "complete_by_hold",
                tl,
                [lambda n: n == "hold_complete", _is_transition(S.ENGAGED, S.COMPLETE)],
            ),
            Check(
                name="hold_error_p95",
                passed=p is not None and p <= thresholds.hold_tol_m,
                value=p,
                limit=thresholds.hold_tol_m,
            ),
            return_then_land,
        ]
    elif scenario == "S2":
        checks += [
            _seq_check("commit_then_touch", tl, [_is_commit, _is_transition(S.ENGAGED, S.TOUCH)]),
            _seq_check(
                "pass_ends_miss_or_complete",
                tl,
                [
                    _is_commit,
                    _is_transition(S.ENGAGED, S.TOUCH),
                    lambda n: n in ("transition:TOUCH->MISS", "transition:TOUCH->COMPLETE"),
                ],
            ),
            return_then_land,
        ]
    elif scenario == "S4":
        ic = tl.first_event(_is_commit)
        i_ret = None if ic is None else tl.first_event(to_return, ic + 1)
        rtl = i_ret is not None and _fc_mode_seen(tl, _RTL, tl.events[i_ret][0], fc_ids)
        checks += [
            _seq_check("commit_then_touch", tl, [_is_commit, _is_transition(S.ENGAGED, S.TOUCH)]),
            Check(name="return_after_commit", passed=i_ret is not None),
            Check(name="fc_rtl_after_return", passed=rtl),
            return_then_land,
        ]
    elif scenario == "S5":
        latency = metrics["abort_latency_ms"]
        rtl = bool(tl.aborts) and _fc_mode_seen(tl, _RTL, tl.aborts[0][1], fc_ids)
        checks += [
            _seq_check("abort_in_touch", tl, [_is_transition(S.TOUCH, S.ABORT)]),
            _seq_check(
                "abort_then_return",
                tl,
                [_is_transition(S.TOUCH, S.ABORT), _is_transition(S.ABORT, S.RETURN)],
            ),
            Check(name="fc_rtl_after_abort", passed=rtl),
            Check(
                name="abort_latency",
                passed=latency is not None and latency <= thresholds.abort_latency_max_ms,
                value=latency,
                limit=thresholds.abort_latency_max_ms,
            ),
        ]
    elif scenario == "S6":
        lr = metrics["lock_retention"]
        rt = metrics["retargets"]
        checks += [
            Check(
                name="lock_retention",
                passed=lr.fraction is not None and lr.fraction >= thresholds.lock_retention_min,
                value=lr.fraction,
                limit=thresholds.lock_retention_min,
            ),
            Check(
                name="zero_retargets",
                passed=lr.fraction is not None and rt.count == 0,
                value=rt.count,
                limit=0,
            ),
        ]
    # S3's checks need the scripted dropout windows: the runner passes them as
    # extra_checks built with check_lock_retained / check_reacquired.
    return checks


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------


def score_run(
    trace: RunTrace,
    *,
    scenario: str,
    seed: int,
    seed_set: SeedSet | str,
    backend: str,
    versions: Mapping[str, str],
    law: str,
    replay_ok: bool,
    thresholds: Thresholds | None = None,
    extra_checks: Sequence[Check] = (),
    fc_ids: tuple[int, int] = (1, 1),
) -> dict[str, Any]:
    """One run's scorecard ([S8] "Scorecard JSON per run") as a JSON-ready object.

    Carries: scenario, seed and seed set, backend and versions, law, the
    [S8] metrics (``null`` where undefined), the safety floors, the final
    state, the transition list, the [R3] replay result, and pass/fail per
    check. ``passed`` is true only when every check passed.
    """
    parsed_set = check_seed(seed_set, scenario, seed)
    th = thresholds or Thresholds()
    if not isinstance(backend, str) or not backend:
        raise ValueError("backend must be a non-empty string")
    if not isinstance(law, str) or not law:
        raise ValueError("law must be a non-empty string")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in versions.items()):
        raise ValueError("versions must map strings to strings")
    if not isinstance(replay_ok, bool):
        raise ValueError("replay_ok must be a bool")
    tl = _parse(trace)
    engaged = _engaged_packets(tl)
    metrics: dict[str, Any] = {
        "abort_latency_ms": _abort_latency(tl, fc_ids),
        "lock_retention": _lock_retention(tl, engaged, trace.target),
        "retargets": _retargets(tl, engaged),
        "hold_error": _hold_error(tl, trace.truth, trace.target, th.hold_time_s),
        "commit_plane_miss": _commit_plane_miss(tl, trace.truth, trace.target),
    }
    attributed: dict[str, int] = {}
    unattributed = 0
    for ep in engaged:
        if ep.attribution is None:
            unattributed += 1
        else:
            attributed[ep.attribution] = attributed.get(ep.attribution, 0) + 1
    floors = _safety_floors(tl, trace.truth, th.alt_floor_m)
    checks = _scenario_checks(scenario, tl, metrics, th, fc_ids)
    checks += [
        Check(
            name="floor_geofence",
            passed=floors.geofence_ok,
            value=floors.geofence_breaches,
            limit=0,
        ),
        Check(name="floor_alt", passed=floors.alt_ok, value=floors.alt_breaches, limit=0),
        Check(
            name="floor_setpoint_speed",
            passed=floors.setpoint_ok,
            value=floors.setpoint_breaches,
            limit=0,
        ),
        Check(name="replay", passed=replay_ok),
    ]
    checks += list(extra_checks)
    names = [c.name for c in checks]
    if len(set(names)) != len(names):
        raise ValueError(f"check names must be unique, got {names}")
    final_state = tl.transitions[-1][2] if tl.transitions else None
    return {
        "format": FORMAT,
        "format_v": FORMAT_V,
        "scenario": scenario,
        "seed": seed,
        "seed_set": parsed_set.value,
        "backend": backend,
        "versions": dict(versions),
        "law": law,
        "target": trace.target,
        "thresholds": th.to_obj(),
        "metrics": {
            "abort_latency_ms": metrics["abort_latency_ms"],
            "lock_retention": metrics["lock_retention"].to_obj(),
            "retargets": metrics["retargets"].to_obj(),
            "attribution": {"attributed": attributed, "unattributed": unattributed},
            "hold_error": None if metrics["hold_error"] is None else metrics["hold_error"].to_obj(),
            "commit_plane_miss": (
                None
                if metrics["commit_plane_miss"] is None
                else metrics["commit_plane_miss"].to_obj()
            ),
        },
        "safety_floors": floors.to_obj(),
        "final_state": final_state,
        "transitions": [[t, f"{frm}->{to}"] for t, frm, to in tl.transitions],
        "replay": {"ok": replay_ok},
        "checks": [c.to_obj() for c in checks],
        "passed": all(c.passed for c in checks),
    }


def scorecard_json(card: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes ([C5]): sorted keys, compact, ASCII, finite numbers only."""
    return canonical_json(card)


def miss_p95(
    cards: Sequence[Mapping[str, Any]], *, thresholds: Thresholds | None = None
) -> dict[str, Any]:
    """S2 green over a seed set: p95 commit-plane miss ``<= miss_p95_max_m``.

    All cards must be S2 runs from one seed set with distinct seeds. A run
    with no crossing counts as an infinite miss. For the gate set, the result
    passes only when the seeds are exactly the 20 S2 gate seeds ([S7]).
    """
    th = thresholds or Thresholds()
    if not cards:
        raise ValueError("no scorecards")
    sets = {c["seed_set"] for c in cards}
    if {c["scenario"] for c in cards} != {"S2"} or len(sets) != 1:
        raise ValueError("miss p95 takes S2 scorecards from one seed set")
    seeds = [c["seed"] for c in cards]
    if len(set(seeds)) != len(seeds):
        raise ValueError("duplicate seeds in the set")
    values: list[float | None] = []
    for c in cards:
        cpm = c["metrics"]["commit_plane_miss"]
        values.append(None if cpm is None else cpm["miss_m"])
    p = p95([math.inf if v is None else v for v in values])
    seed_set = sets.pop()
    complete = seed_set != SeedSet.GATE.value or sorted(seeds) == sorted(gate_seeds("S2"))
    return {
        "format": FORMAT,
        "format_v": FORMAT_V,
        "scenario": "S2",
        "seed_set": seed_set,
        "seeds": seeds,
        "miss_m": values,
        "p95_m": None if math.isinf(p) else p,
        "limit_m": float(th.miss_p95_max_m),
        "complete": complete,
        "passed": complete and p <= th.miss_p95_max_m,
    }
