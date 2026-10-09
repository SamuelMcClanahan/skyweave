"""Mission state machine (DRONE_CONTRACTS_D0.md §4; brief 3.2-3.4). Pure logic.

The mission decides what the drone is doing, never how it flies. It is the
part of the companion core ([R3]) that must replay bit for bit, so it holds no
clock, does no I/O, and draws no randomness: every input carries its own
board-ms stamp ([M2]), and the same inputs in the same order give the same
acks, mission state packets, FC requests, and transition log.

Why the shape:

- ``TRANSITIONS`` is the contract's §4.2 table as data and ``PRECEDENCE`` is
  the [M4] ranking. Every state change goes through
  :meth:`Mission._transition`, which refuses a row whose From set does not
  hold the current state, so a transition the table does not list cannot
  happen. A test parses the contract's table and compares it row by row, so a
  doc edit that forgets the code (or the reverse) fails.
- One transition per input ([M4], [M4a]): an input selects at most one row,
  the highest-ranked whose input kind, From state, and trigger all hold. AUTO
  rows and the preauthorized T07 fire only on the first input after the
  transition that entered their From state, and consume that input.
- Lock stickiness ([M7]-[M9], brief 3.2): the candidate and the engaged id
  are written only by the rows the contract names (T05, T06, T07, T10, the
  prime rows) and by the [M9] death clear. No size, hit count, or recency of
  another track is ever compared.
- Exits are requests ([M10], [M11]): RTL / LAND go to the FC only while the
  newest snapshot says link up and GUIDED, and stop for the rest of that state
  once the pilot or an FC failsafe picked another mode.

Usage by the core: call one ``on_*`` method per input, then
:meth:`Mission.maybe_publish` with that input's stamp and
:meth:`Mission.take_requests`. ``maybe_publish`` emits on any transition and,
after a TICK, when ``publish_period_ms`` has passed since the last packet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any

from skyweave2.drone.packets import (
    AckPacket,
    AckResult,
    CommandName,
    CommandPacket,
    Event,
    InvalidParams,
    MissionState,
    MissionStatePacket,
    PrimeParams,
    TrackPacket,
    TrackState,
    TrialType,
    canonical_json,
)
from skyweave2.drone.types import (
    FcRequest,
    FcRequestKind,
    GuidanceEvent,
    GuidanceEventKind,
    LandedState,
    VehicleSnapshot,
)

S = MissionState

# Input kinds ([M2]) as the §4.2 Input column spells them. HB (ground
# heartbeat) has no row of its own: it matches only rows whose Input is "any".
CMD = "CMD"
TRK = "TRK"
TICK = "TICK"
GDE = "GDE"
HB = "HB"
ANY = "any"

GUIDED_MODE = "GUIDED"  # ArduCopter mode names, as the vehicle snapshot carries them
LAND_MODE = "LAND"

AIRBORNE_STATES: frozenset[MissionState] = frozenset(
    {S.LAUNCH, S.SEARCH, S.ACQUIRING, S.ENGAGED, S.COASTING, S.TOUCH, S.LOST}
)  # [M1] the airborne set `A`
DEATH_CLEAR_STATES: frozenset[MissionState] = frozenset(
    {S.TOUCH, S.COMPLETE, S.MISS, S.RETURN, S.LAND, S.ABORT}
)  # [M9]: engaged-track death clears the id here and causes no transition
TONE_STATES: tuple[MissionState, ...] = (
    S.ACQUIRING,
    S.ENGAGED,
    S.TOUCH,
    S.LOST,
    S.RETURN,
    S.ABORT,
)  # contract §9 tone table (E1)


class Source(str, Enum):
    """[M3] who may trigger a transition."""

    HUMAN = "HUMAN"
    TRACKER = "TRACKER"
    GUIDANCE = "GUIDANCE"
    VEHICLE = "VEHICLE"
    BUDGET = "BUDGET"
    FAILSAFE = "FAILSAFE"
    AUTO = "AUTO"


@dataclass(frozen=True, kw_only=True)
class TransitionSpec:
    """One row of contract §4.2. ``None`` in ``from_states`` is unprimed."""

    tid: str
    from_states: frozenset[MissionState | None]
    to_state: MissionState
    inputs: frozenset[str]  # subset of {"CMD", "TRK", "TICK", "GDE", "any"}
    source: Source


def _row(
    tid: str,
    from_states: frozenset[MissionState] | set[MissionState | None],
    to_state: MissionState,
    inputs: set[str],
    source: Source,
) -> TransitionSpec:
    return TransitionSpec(
        tid=tid,
        from_states=frozenset(from_states),
        to_state=to_state,
        inputs=frozenset(inputs),
        source=source,
    )


_A = AIRBORNE_STATES
_EVERY: frozenset[MissionState] = frozenset(MissionState)  # "every state": not unprimed [M5]

TRANSITIONS: tuple[TransitionSpec, ...] = (
    _row("T01", {None}, S.PRIMED, {CMD}, Source.HUMAN),
    _row("T02", {S.PRIMED}, S.PRIMED, {CMD}, Source.HUMAN),
    _row("T03", {S.PRIMED}, S.LAUNCH, {TICK}, Source.HUMAN),
    _row("T04", {S.LAUNCH}, S.SEARCH, {TICK}, Source.VEHICLE),
    _row("T05", {S.SEARCH}, S.ACQUIRING, {TRK}, Source.TRACKER),
    _row("T06", {S.ACQUIRING}, S.SEARCH, {TRK, TICK}, Source.TRACKER),
    _row("T07", {S.ACQUIRING}, S.ENGAGED, {CMD, ANY}, Source.HUMAN),
    _row("T08", {S.ENGAGED}, S.COASTING, {TRK}, Source.TRACKER),
    _row("T09", {S.COASTING}, S.ENGAGED, {TRK}, Source.TRACKER),
    _row("T10", {S.ENGAGED, S.COASTING}, S.LOST, {TRK, TICK}, Source.TRACKER),
    _row("T11", {S.ENGAGED}, S.TOUCH, {GDE}, Source.GUIDANCE),
    _row("T12", {S.ENGAGED}, S.COMPLETE, {GDE}, Source.GUIDANCE),
    _row(
        "T13",
        {S.SEARCH, S.ACQUIRING, S.ENGAGED, S.COASTING, S.TOUCH, S.LOST},
        S.COMPLETE,
        {CMD},
        Source.HUMAN,
    ),
    _row("T14", {S.TOUCH}, S.MISS, {GDE}, Source.GUIDANCE),
    _row("T15", {S.LOST}, S.SEARCH, {ANY}, Source.AUTO),
    _row("T16", {S.COMPLETE, S.MISS}, S.RETURN, {ANY}, Source.AUTO),
    _row("T17", _A, S.RETURN, {TICK}, Source.BUDGET),
    _row("T18", _A, S.RETURN, {TICK}, Source.FAILSAFE),
    _row("T19", _EVERY, S.ABORT, {CMD}, Source.HUMAN),
    _row("T20", _A, S.ABORT, {TICK}, Source.FAILSAFE),
    _row("T21", {S.ABORT}, S.RETURN, {ANY}, Source.AUTO),
    _row("T22", {S.ABORT}, S.LAND, {ANY}, Source.AUTO),
    _row("T23", {S.RETURN}, S.LAND, {TICK}, Source.VEHICLE),
    _row("T24", {S.LAND}, S.PRIMED, {CMD}, Source.HUMAN),
)

# [M4], highest first. Rows sharing a rank never compete (disjoint From states
# or disjoint triggers).
PRECEDENCE: tuple[tuple[str, ...], ...] = (
    ("T19",),
    ("T20",),
    ("T18",),
    ("T17",),
    ("T13",),
    ("T06", "T10"),
    ("T07",),
    ("T11", "T12", "T14"),
    ("T08", "T09"),
    ("T05",),
    ("T03", "T04", "T23"),
    ("T01", "T02", "T24"),
    ("T15", "T16", "T21", "T22"),
)

_SPEC: dict[str, TransitionSpec] = {spec.tid: spec for spec in TRANSITIONS}
_RANK: dict[str, int] = {tid: n for n, group in enumerate(PRECEDENCE) for tid in group}
_RANKED: tuple[TransitionSpec, ...] = tuple(sorted(TRANSITIONS, key=lambda s: _RANK[s.tid]))

# [M5]: the rows each command fires. Its valid states are their From sets.
_COMMAND_ROWS: dict[CommandName, tuple[str, ...]] = {
    CommandName.PRIME: ("T01", "T02", "T24"),
    CommandName.APPROVE_ENGAGE: ("T07",),
    CommandName.MARK_COMPLETE: ("T13",),
    CommandName.ABORT: ("T19",),
}

_EXIT_EVENT = {FcRequestKind.MODE_RTL: "RTL", FcRequestKind.MODE_LAND: "LAND"}


@dataclass(frozen=True, kw_only=True)
class MissionConfig:
    """Mission constants (contract §9; every number Provisional, E1).

    ``coast_cap`` must equal the tracker's ([P2a]); the recording's
    ``meta.config`` carries both ([R2]).
    """

    coast_cap: int = 20
    track_timeout_ms: int = 500
    reacquire_window_ms: int = 10_000
    ground_link_timeout_ms: int = 5_000
    takeoff_done_frac: float = 0.9
    publish_period_ms: int = 200  # 5 Hz on ticks, plus one packet per transition [P3]
    approve_settle_ms: int = 1_000  # [M8]
    exit_retry_ms: int = 1_000  # [M10]
    tone_states: tuple[MissionState, ...] = TONE_STATES

    def to_obj(self) -> dict[str, Any]:
        """Plain JSON object for the recording's ``meta.config`` ([R2])."""
        out: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self)}
        out["tone_states"] = [s.value for s in self.tone_states]
        return out

    @classmethod
    def from_obj(cls, obj: Any) -> MissionConfig:
        """Inverse of :meth:`to_obj`. Every field is required; nothing is coerced."""
        if not isinstance(obj, Mapping):
            raise ValueError("mission config must be a JSON object")
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in obj:
                raise ValueError(f"mission config is missing {f.name!r}")
            val = obj[f.name]
            if f.name == "tone_states":
                if not isinstance(val, list):
                    raise ValueError("mission config tone_states must be a list")
                kwargs[f.name] = tuple(MissionState(v) for v in val)
            elif f.name == "takeoff_done_frac":
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise ValueError("mission config takeoff_done_frac must be a number")
                kwargs[f.name] = float(val)
            else:
                if isinstance(val, bool) or not isinstance(val, int):
                    raise ValueError(f"mission config {f.name!r} must be an integer")
                kwargs[f.name] = val
        return cls(**kwargs)


@dataclass(frozen=True, kw_only=True)
class MissionView:
    """What guidance reads ([G9], [G10]). ``candidate_id`` is set only in
    ``ACQUIRING``; ``engaged_track_id`` from T07 until death or re-prime."""

    state: MissionState | None
    trial: PrimeParams | None
    engaged_track_id: int | None
    candidate_id: int | None


TransitionRecord = tuple[int, str, MissionState | None, MissionState, Source]
"""(t, tid, from, to, source); ``from`` is ``None`` for T01."""


@dataclass(frozen=True, kw_only=True)
class _Input:
    kind: str
    t: int
    first: bool  # [M4a]: the first input after the latest transition
    command: CommandName | None = None  # set only for a command that may execute
    valid: bool = False  # the command's own trigger holds ([M5], [M8])
    params: PrimeParams | None = None
    trk: TrackPacket | None = None
    gde: GuidanceEvent | None = None


@dataclass(frozen=True)
class _Firing:
    """What a row does if it fires: events before the transition event, and
    the row's side effects ([M4]: they happen only when the row fires)."""

    events: tuple[str, ...] = ()
    apply: Callable[[], None] | None = None
    by_command: bool = False  # the input's command is this row's trigger


def _fmt_m(x: float) -> str:
    out = f"{x:.3f}"
    return "0.000" if out == "-0.000" else out  # §4.6: -0.000 is written 0.000


def _fingerprint(cmd: CommandPacket) -> tuple[str, str | None]:
    """[P5a]: the command and its canonical params, never the token. Params
    count only for ``prime``; the other three ignore them ([P5])."""
    if cmd.command is not CommandName.PRIME or cmd.params is None:
        return (cmd.command.value, None)
    params = dict(cmd.params) if isinstance(cmd.params, Mapping) else cmd.params
    return (cmd.command.value, canonical_json(params).decode("ascii"))


class Mission:
    """Contract §4 state machine. One ``on_*`` call per input ([M2])."""

    def __init__(self, config: MissionConfig) -> None:
        self._cfg = config
        self._state: MissionState | None = None
        self._trial: PrimeParams | None = None
        self._engaged: int | None = None
        self._engaged_seen: int | None = None  # stamp of the newest engaged-id packet
        self._candidate: int | None = None
        self._candidate_since: int | None = None  # T05 stamp, for [M8]
        self._candidate_seen: int | None = None
        self._snap: VehicleSnapshot | None = None  # newest tick's snapshot [M2]
        self._last_ground_hb: int | None = None
        self._saw_non_guided = False  # T03 edge gate (E1-D4)
        self._launch_t: int | None = None
        self._reacquire_open_t: int | None = None
        self._follow_on = False  # [M4a]
        self._acks: dict[str, tuple[tuple[str, str | None], AckResult]] = {}
        self._events: list[Event] = []
        self._requests: list[FcRequest] = []
        self._dirty = False
        self._last_pub_t: int | None = None
        self._last_input_kind: str | None = None
        self._exit_kind: FcRequestKind | None = None  # [M10]
        self._exit_last_t: int | None = None
        self._exit_stopped = False
        self.transition_log: list[TransitionRecord] = []

    # -- read-only view -----------------------------------------------------

    @property
    def config(self) -> MissionConfig:
        return self._cfg

    @property
    def state(self) -> MissionState | None:
        return self._state

    @property
    def trial(self) -> PrimeParams | None:
        return self._trial

    @property
    def engaged_track_id(self) -> int | None:
        return self._engaged

    @property
    def candidate_id(self) -> int | None:
        return self._candidate

    def view(self) -> MissionView:
        return MissionView(
            state=self._state,
            trial=self._trial,
            engaged_track_id=self._engaged,
            candidate_id=self._candidate,
        )

    # -- inputs ([M2]) --------------------------------------------------------

    def on_command(
        self, cmd: CommandPacket, auth_ok: bool, t_ms: int, from_ground: bool = True
    ) -> AckPacket:
        """CMD input. ``from_ground=False`` is the radio approve switch ([F7]),
        which is not a ground heartbeat. An authenticated ground command is."""
        first = self._begin(CMD)
        if auth_ok and from_ground:
            self._last_ground_hb = t_ms
        inert = _Input(kind=CMD, t=t_ms, first=first)  # a command that executes nothing
        if not auth_ok:
            return self._command_done(cmd, inert, AckResult.REJECTED_AUTH)  # never stored
        fingerprint = _fingerprint(cmd)
        stored = self._acks.get(cmd.cmd_id)
        if stored is not None:
            if stored[0] == fingerprint:
                # [P5a] true retry: the stored ack, nothing executes, no second
                # event. It is still an input, so a pending AUTO row may fire.
                self._run(inert)
                return AckPacket(cmd_id=cmd.cmd_id, result=stored[1])
            return self._command_done(cmd, inert, AckResult.REJECTED_DUPLICATE_ID)
        verdict, params = self._verdict(cmd, t_ms)
        inp = _Input(
            kind=CMD,
            t=t_ms,
            first=first,
            command=cmd.command,
            valid=verdict is AckResult.ACCEPTED,
            params=params,
        )
        return self._command_done(cmd, inp, verdict, store=fingerprint)

    def on_track(self, pkt: TrackPacket, t_ms: int) -> None:
        """TRK input, stamped with its record ``t_rx`` (``t_cap`` is guidance
        geometry only, [M2])."""
        first = self._begin(TRK)
        if self._engaged is not None and pkt.track_id == self._engaged:
            self._engaged_seen = t_ms
        if self._candidate is not None and pkt.track_id == self._candidate:
            self._candidate_seen = t_ms
        engaged_died = pkt.track_id == self._engaged and self._is_final(pkt)
        self._run(_Input(kind=TRK, t=t_ms, first=first, trk=pkt))
        self._clear_dead_engaged(engaged_died, t_ms)

    def on_tick(self, snap: VehicleSnapshot, t_ms: int) -> None:
        """TICK input: the only place a vehicle snapshot is taken ([M2]); drives
        vehicle rows, budgets, link loss, timeout deaths, and exit retries."""
        first = self._begin(TICK)
        self._snap = snap
        if self._state is S.PRIMED and snap.fc_link_up and snap.mode not in (None, GUIDED_MODE):
            self._saw_non_guided = True  # T03: the switch left GUIDED after the prime
        engaged_died = self._engaged is not None and self._timed_out(self._engaged_seen, t_ms)
        self._run(_Input(kind=TICK, t=t_ms, first=first))
        self._clear_dead_engaged(engaged_died, t_ms)
        self._exit_step(t_ms, on_tick=True)

    def on_guidance(self, ev: GuidanceEvent) -> None:
        """GDE input, stamped ``ev.t_ms``; the core feeds it right after the
        input that made guidance emit it ([M2])."""
        if ev.kind is GuidanceEventKind.COMMIT and (
            ev.track_id is None or ev.t_cap is None or ev.miss_m is None or ev.z_m is None
        ):
            raise ValueError("a commit event carries track_id, t_cap, miss_m and z_m (§4.6)")
        first = self._begin(GDE)
        self._run(_Input(kind=GDE, t=ev.t_ms, first=first, gde=ev))

    def on_ground_heartbeat(self, t_ms: int) -> None:
        """HB input: a UI state poll ([U6]). It matches only "any" rows."""
        first = self._begin(HB)
        self._last_ground_hb = t_ms
        self._run(_Input(kind=HB, t=t_ms, first=first))

    # -- outputs --------------------------------------------------------------

    def take_requests(self) -> list[FcRequest]:
        """Drain the FC requests made since the last call ([M10], [M12])."""
        out, self._requests = self._requests, []
        return out

    def maybe_publish(self, t_ms: int) -> MissionStatePacket | None:
        """The mission state packet for the input just processed, if one is due.

        Due after any transition, or after a TICK when ``publish_period_ms`` has
        passed since the previous packet ([P3], §9). Never before the first
        accepted prime ([P3a]). Pending events go into this packet and no other.
        ``t_ms`` is the stamp of the input that caused the publish.
        """
        if self._state is None or self._trial is None:
            return None
        periodic = self._last_input_kind == TICK and (
            self._last_pub_t is None or t_ms - self._last_pub_t >= self._cfg.publish_period_ms
        )
        if not (self._dirty or periodic):
            return None
        pkt = MissionStatePacket(
            t=t_ms,
            mission_state=self._state,
            engaged_track_id=self._engaged,
            trial=self._trial.echo(),
            events=tuple(self._events),
        )
        self._events = []
        self._dirty = False
        self._last_pub_t = t_ms
        return pkt

    # -- evaluation -----------------------------------------------------------

    def _begin(self, kind: str) -> bool:
        first = self._follow_on
        self._follow_on = False
        self._last_input_kind = kind
        return first

    def _select(self, inp: _Input) -> tuple[TransitionSpec, _Firing] | None:
        """[M4]: the highest-ranked row whose input kind, From state, and
        trigger hold. Pure: nothing changes until :meth:`_fire`."""
        for spec in _RANKED:
            if self._state not in spec.from_states:
                continue
            if inp.kind not in spec.inputs and ANY not in spec.inputs:
                continue
            firing = _TRIGGERS[spec.tid](self, inp)
            if firing is not None:
                return spec, firing
        return None

    def _run(self, inp: _Input) -> tuple[TransitionSpec, _Firing] | None:
        selected = self._select(inp)
        if selected is not None:
            self._fire(selected[0], selected[1], inp.t)
        return selected

    def _fire(self, spec: TransitionSpec, firing: _Firing, t: int) -> None:
        for name in firing.events:
            self._emit(t, name)
        if firing.apply is not None:
            firing.apply()
        self._transition(spec, t)

    def _command_done(
        self,
        cmd: CommandPacket,
        inp: _Input,
        verdict: AckResult,
        store: tuple[str, str | None] | None = None,
    ) -> AckPacket:
        selected = self._select(inp)
        if selected is not None and selected[1].by_command:
            result = AckResult.ACCEPTED
        elif verdict is AckResult.ACCEPTED:
            result = AckResult.REJECTED_STATE  # [M4a]: its own row did not fire
        else:
            result = verdict
        if store is not None:
            self._acks[cmd.cmd_id] = (store, result)  # [P5a]: rejections are stored too
        if self._state is not None or selected is not None:
            self._emit(inp.t, f"cmd:{cmd.command.value}:{result.value}")  # [P5b], [P3a]
        if selected is not None:
            self._fire(selected[0], selected[1], inp.t)
        return AckPacket(cmd_id=cmd.cmd_id, result=result)

    def _verdict(self, cmd: CommandPacket, t: int) -> tuple[AckResult, PrimeParams | None]:
        """[M5] whether the command's own trigger holds now."""
        if not any(self._state in _SPEC[tid].from_states for tid in _COMMAND_ROWS[cmd.command]):
            return AckResult.REJECTED_STATE, None
        if cmd.command is CommandName.PRIME:
            snap = self._link_snap()
            if snap is None or snap.armed is not False or not snap.on_ground:
                return AckResult.REJECTED_STATE, None
            try:
                return AckResult.ACCEPTED, PrimeParams.from_obj(cmd.params)
            except InvalidParams:
                return AckResult.REJECTED_PARAMS, None  # never partly applied
        if cmd.command is CommandName.APPROVE_ENGAGE:
            since = self._candidate_since
            if since is None or t - since < self._cfg.approve_settle_ms:
                return AckResult.REJECTED_STATE, None  # [M8]
        return AckResult.ACCEPTED, None

    def _transition(self, spec: TransitionSpec, t: int) -> None:
        """The only place the state changes. It enforces the §4.2 row."""
        src = self._state
        if src not in spec.from_states:
            raise RuntimeError(f"{spec.tid} does not leave {src}; not a §4.2 transition")
        dst = spec.to_state
        self.transition_log.append((t, spec.tid, src, dst, spec.source))
        self._emit(t, f"transition:{'UNPRIMED' if src is None else src.value}->{dst.value}")
        self._state = dst
        self._follow_on = True
        self._dirty = True
        if dst is not S.ACQUIRING:  # the candidate role exists only in ACQUIRING
            self._candidate = None
            self._candidate_since = None
            self._candidate_seen = None
        self._exit_kind = None
        if dst is S.LAUNCH:
            search_alt = self._require_trial().search_alt
            self._requests.append(FcRequest(kind=FcRequestKind.ARM_AND_TAKEOFF, value=search_alt))
            self._emit(t, "fc_request:ARM_AND_TAKEOFF")  # [M12]
        elif dst is S.RETURN:
            self._start_exit(FcRequestKind.MODE_RTL, t)
        elif dst is S.LAND and spec.tid == "T22":
            self._start_exit(FcRequestKind.MODE_LAND, t)
        if dst in self._cfg.tone_states:
            self._requests.append(FcRequest(kind=FcRequestKind.TONE, value=dst.value))
            self._emit(t, f"tone:{dst.value}")  # [M12]

    # -- [M10] exits ----------------------------------------------------------

    def _start_exit(self, kind: FcRequestKind, t: int) -> None:
        self._exit_kind = kind
        self._exit_last_t = None
        self._exit_stopped = False
        self._exit_step(t, on_tick=False)  # the first request immediately on entry

    def _exit_step(self, t: int, *, on_tick: bool) -> None:
        kind = self._exit_kind
        if kind is None or self._exit_stopped:
            return
        snap = self._link_snap()
        if snap is None:
            return  # link down: no request, and no mode to judge
        if on_tick and (
            snap.mode not in (None, GUIDED_MODE)
            or (kind is FcRequestKind.MODE_LAND and snap.armed is False)
        ):
            self._exit_stopped = True  # never override the pilot or an FC failsafe
            return
        if snap.mode != GUIDED_MODE:
            return
        if kind is FcRequestKind.MODE_LAND and snap.armed is not True:
            return
        if self._exit_last_t is not None and t - self._exit_last_t < self._cfg.exit_retry_ms:
            return
        self._exit_last_t = t
        self._requests.append(FcRequest(kind=kind))
        self._emit(t, f"fc_request:{_EXIT_EVENT[kind]}")

    # -- helpers --------------------------------------------------------------

    def _emit(self, t: int, name: str) -> None:
        # Callers keep [P3a]: nothing is emitted while unprimed except by T01.
        self._events.append(Event(t=t, name=name))

    def _require_trial(self) -> PrimeParams:
        if self._trial is None:
            raise RuntimeError("no trial: the mission is unprimed")
        return self._trial

    def _link_snap(self) -> VehicleSnapshot | None:
        """[M2a]: the newest snapshot when the FC link is up; every vehicle
        predicate is unknown while the link is down."""
        snap = self._snap
        return snap if snap is not None and snap.fc_link_up else None

    def _is_final(self, pkt: TrackPacket) -> bool:
        """[P2a] the final packet of a dying confirmed track."""
        return pkt.state is TrackState.COASTING and pkt.misses > self._cfg.coast_cap

    def _timed_out(self, seen: int | None, t: int) -> bool:
        """[P2a] no packet for the id within ``track_timeout_ms``."""
        return seen is not None and t - seen > self._cfg.track_timeout_ms

    def _died(self, track_id: int, seen: int | None, inp: _Input) -> bool:
        if inp.kind == TRK and inp.trk is not None:
            return inp.trk.track_id == track_id and self._is_final(inp.trk)
        if inp.kind == TICK:
            return self._timed_out(seen, inp.t)
        return False

    def _clear_dead_engaged(self, died: bool, t: int) -> None:
        """[M9]: death outside ENGAGED / COASTING clears the id, no transition."""
        if died and self._engaged is not None and self._state in DEATH_CLEAR_STATES:
            self._emit(t, f"track_dead:{self._engaged}")
            self._engaged = None
            self._engaged_seen = None

    def _budget_breach(self, t: int) -> str | None:
        """T17, first in [M4] order: flight_time, battery, geofence, reacquire."""
        trial = self._require_trial()
        if self._launch_t is not None and t - self._launch_t > trial.flight_time_cap_s * 1000.0:
            return "flight_time"
        snap = self._link_snap()
        if snap is not None:
            if snap.battery_pct is not None and snap.battery_pct < trial.battery_floor_pct:
                return "battery"
            if snap.home_dist_m is not None and snap.home_dist_m > trial.geofence_radius_m:
                return "geofence"
        opened = self._reacquire_open_t
        if opened is not None and t - opened > self._cfg.reacquire_window_ms:
            return "reacquire"
        return None

    def _link_loss(self, t: int) -> str | None:
        """T18, first in [M4] order: fc_link, ground_link."""
        if self._link_snap() is None:
            return "fc_link"
        last = self._last_ground_hb
        if last is None or t - last > self._cfg.ground_link_timeout_ms:
            return "ground_link"
        return None

    # -- row triggers (pure: they read state and say what firing would do) ---

    def _trig_prime(self, inp: _Input) -> _Firing | None:  # T01, T02, T24
        params = inp.params
        if inp.command is not CommandName.PRIME or not inp.valid or params is None:
            return None

        def apply() -> None:
            self._trial = params
            self._engaged = None  # [M7]: replaced only by an accepted re-prime
            self._engaged_seen = None
            self._launch_t = None
            self._reacquire_open_t = None
            self._saw_non_guided = False  # T03 latch restarts with each prime

        return _Firing(apply=apply, by_command=True)

    def _trig_t03(self, inp: _Input) -> _Firing | None:
        snap = self._link_snap()
        if not (
            snap is not None
            and self._saw_non_guided
            and snap.mode == GUIDED_MODE
            and snap.on_ground
            and snap.armed is False
            and not snap.attitude_degraded
        ):
            return None
        t = inp.t

        def apply() -> None:
            self._launch_t = t

        return _Firing(apply=apply)

    def _trig_t04(self, inp: _Input) -> _Firing | None:
        snap = self._link_snap()
        if snap is None or not snap.airborne or snap.rel_alt_m is None:
            return None
        floor = self._cfg.takeoff_done_frac * self._require_trial().search_alt
        return _Firing() if snap.rel_alt_m >= floor else None

    def _trig_t05(self, inp: _Input) -> _Firing | None:
        pkt = inp.trk
        if pkt is None or pkt.state is not TrackState.CONFIRMED:
            return None
        track_id, t = pkt.track_id, inp.t

        def apply() -> None:
            self._candidate = track_id
            self._candidate_since = t
            self._candidate_seen = t

        return _Firing(events=(f"candidate:{track_id}",), apply=apply)

    def _trig_t06(self, inp: _Input) -> _Firing | None:
        cand = self._candidate
        if cand is None or not self._died(cand, self._candidate_seen, inp):
            return None
        return _Firing(events=(f"track_dead:{cand}",))  # leaving ACQUIRING clears it

    def _trig_t07(self, inp: _Input) -> _Firing | None:
        cand = self._candidate
        if cand is None:
            return None
        by_command = inp.command is CommandName.APPROVE_ENGAGE and inp.valid
        if not by_command and not (inp.first and self._require_trial().engage_preauthorized):
            return None
        seen = self._candidate_seen

        def apply() -> None:
            self._engaged = cand  # [M7]: only T07 sets it
            self._engaged_seen = seen
            self._reacquire_open_t = None  # ENGAGED reached: the window closes

        return _Firing(events=(f"engaged:{cand}",), apply=apply, by_command=by_command)

    def _trig_t08(self, inp: _Input) -> _Firing | None:
        pkt = inp.trk
        if pkt is None or self._engaged is None or pkt.track_id != self._engaged:
            return None
        coasting = pkt.state is TrackState.COASTING and pkt.misses <= self._cfg.coast_cap
        return _Firing() if coasting else None

    def _trig_t09(self, inp: _Input) -> _Firing | None:
        pkt = inp.trk
        if pkt is None or self._engaged is None or pkt.track_id != self._engaged:
            return None
        return _Firing() if pkt.state is TrackState.CONFIRMED else None

    def _trig_t10(self, inp: _Input) -> _Firing | None:
        eng = self._engaged
        if eng is None or not self._died(eng, self._engaged_seen, inp):
            return None

        def apply() -> None:
            self._engaged = None
            self._engaged_seen = None

        return _Firing(events=(f"track_dead:{eng}",), apply=apply)

    def _trig_t11(self, inp: _Input) -> _Firing | None:
        ev = inp.gde
        if ev is None or ev.kind is not GuidanceEventKind.COMMIT:
            return None
        if self._require_trial().trial_type is not TrialType.TOUCH:
            return None
        if ev.miss_m is None or ev.z_m is None:
            return None  # on_guidance refuses this; kept for the type checker
        x, y = ev.miss_m
        return _Firing(
            events=(
                f"commit:{ev.track_id}:{ev.t_cap}",
                f"miss:{_fmt_m(x)}:{_fmt_m(y)}:{_fmt_m(ev.z_m)}",
            )
        )

    def _trig_t12(self, inp: _Input) -> _Firing | None:
        ev = inp.gde
        if ev is None or ev.kind is not GuidanceEventKind.HOLD_COMPLETE:
            return None
        if self._require_trial().trial_type is not TrialType.STANDOFF:
            return None
        return _Firing(events=("hold_complete",))

    def _trig_t13(self, inp: _Input) -> _Firing | None:
        if inp.command is CommandName.MARK_COMPLETE and inp.valid:
            return _Firing(by_command=True)
        return None

    def _trig_t14(self, inp: _Input) -> _Firing | None:
        ev = inp.gde
        if ev is None or ev.kind is not GuidanceEventKind.PASS_DONE:
            return None
        return _Firing(events=("pass_done",))

    def _trig_t15(self, inp: _Input) -> _Firing | None:
        if not inp.first:
            return None
        t = inp.t

        def apply() -> None:
            self._reacquire_open_t = t  # opens the reacquire window

        return _Firing(apply=apply)

    def _trig_t16(self, inp: _Input) -> _Firing | None:
        return _Firing() if inp.first else None

    def _trig_t17(self, inp: _Input) -> _Firing | None:
        name = self._budget_breach(inp.t)
        return None if name is None else _Firing(events=(f"budget:{name}",))

    def _trig_t18(self, inp: _Input) -> _Firing | None:
        name = self._link_loss(inp.t)
        return None if name is None else _Firing(events=(f"failsafe:{name}",))

    def _trig_t19(self, inp: _Input) -> _Firing | None:
        if inp.command is CommandName.ABORT and inp.valid:
            return _Firing(by_command=True)
        return None

    def _trig_t20(self, inp: _Input) -> _Firing | None:
        snap = self._link_snap()
        if snap is None or snap.mode in (None, GUIDED_MODE):
            return None
        return _Firing(events=("failsafe:mode",))

    def _trig_t21(self, inp: _Input) -> _Firing | None:
        snap = self._link_snap()
        on_ground = snap is not None and snap.on_ground
        return _Firing() if inp.first and not on_ground else None  # airborne or unknown

    def _trig_t22(self, inp: _Input) -> _Firing | None:
        snap = self._link_snap()
        return _Firing() if inp.first and snap is not None and snap.on_ground else None

    def _trig_t23(self, inp: _Input) -> _Firing | None:
        snap = self._link_snap()
        if snap is None:
            return None
        down = snap.landed_state in (LandedState.LANDING, LandedState.ON_GROUND)
        return _Firing() if down or snap.mode == LAND_MODE else None


_TRIGGERS: dict[str, Callable[[Mission, _Input], _Firing | None]] = {
    "T01": Mission._trig_prime,
    "T02": Mission._trig_prime,
    "T03": Mission._trig_t03,
    "T04": Mission._trig_t04,
    "T05": Mission._trig_t05,
    "T06": Mission._trig_t06,
    "T07": Mission._trig_t07,
    "T08": Mission._trig_t08,
    "T09": Mission._trig_t09,
    "T10": Mission._trig_t10,
    "T11": Mission._trig_t11,
    "T12": Mission._trig_t12,
    "T13": Mission._trig_t13,
    "T14": Mission._trig_t14,
    "T15": Mission._trig_t15,
    "T16": Mission._trig_t16,
    "T17": Mission._trig_t17,
    "T18": Mission._trig_t18,
    "T19": Mission._trig_t19,
    "T20": Mission._trig_t20,
    "T21": Mission._trig_t21,
    "T22": Mission._trig_t22,
    "T23": Mission._trig_t23,
    "T24": Mission._trig_prime,
}
