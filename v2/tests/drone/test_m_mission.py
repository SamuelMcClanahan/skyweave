"""M series: mission state machine (DRONE_CONTRACTS_D0.md §4).

Expectations come from two places only: the contract text (the §4.2 table,
the [M4] ranking, the [M1] set `A`, the §9 tone table, and the [M5] validity
sets are parsed from the doc) and the mission's own outputs (acks, mission
state packets, FC requests, the transition log). Inputs are real packets and
vehicle snapshots fed through the real :class:`Mission` the way the core
feeds it ([M2]); nothing is mocked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from skyweave2.drone.mission import (
    AIRBORNE_STATES,
    PRECEDENCE,
    TRANSITIONS,
    Mission,
    MissionConfig,
    Source,
)
from skyweave2.drone.packets import (
    AckPacket,
    AckResult,
    CommandName,
    CommandPacket,
    Event,
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
ACC = AckResult.ACCEPTED
REJ = AckResult.REJECTED_STATE

# ---------------------------------------------------------------------------
# The contract, parsed
# ---------------------------------------------------------------------------

DOC_TEXT = (Path(__file__).resolve().parents[2] / "docs" / "DRONE_CONTRACTS_D0.md").read_text(
    encoding="utf-8"
)


def _backticked(text: str) -> list[str]:
    return re.findall(r"`([A-Z_]+)`", text)


def _doc_match(pattern: str) -> str:
    m = re.search(pattern, DOC_TEXT, re.S)
    assert m is not None, f"contract text not found: {pattern}"
    return m.group(1)


DOC_A = frozenset(S(n) for n in _backticked(_doc_match(r"Airborne set `A` = \{(.*?)\}")))
DOC_PRECEDENCE = tuple(
    tuple(re.findall(r"T\d\d", group))
    for group in _doc_match(r"ranked highest here:(.*?)When several").split(">")
)
DOC_TONES = tuple(
    S(n) for n in _backticked(_doc_match(r"Tone table \(E1, \[F8\], \[M12\]\):(.*?)each have"))
)


class DocRow(NamedTuple):
    from_states: frozenset[MissionState | None]
    to_state: MissionState
    inputs: frozenset[str]
    source: Source


def _doc_states(cell: str) -> frozenset[MissionState | None]:
    if cell == "unprimed":
        return frozenset({None})
    if cell == "every state":
        return frozenset(S)
    if cell == "`A`":
        return frozenset(DOC_A)
    return frozenset(S(n) for n in _backticked(cell))


def _doc_rows() -> dict[str, DocRow]:
    table = DOC_TEXT.split("### 4.2 Transition table", 1)[1].split("- **[M5]", 1)[0]
    rows: dict[str, DocRow] = {}
    for line in table.splitlines():
        if not re.match(r"\| T\d\d \|", line):
            continue
        tid, frm, to, inputs, _trigger, source = (
            c.strip() for c in line.strip().strip("|").split("|")
        )
        rows[tid] = DocRow(
            from_states=_doc_states(frm),
            to_state=S(to.strip("`")),
            inputs=frozenset(x.strip() for x in inputs.split(",")),
            source=Source(source.split()[0]),
        )
    return rows


DOC_ROWS = _doc_rows()

# [M5] validity per command: the From sets of the rows each command fires.
VALID_STATES: dict[str, frozenset[MissionState | None]] = {
    "prime": DOC_ROWS["T01"].from_states
    | DOC_ROWS["T02"].from_states
    | DOC_ROWS["T24"].from_states,
    "approve_engage": DOC_ROWS["T07"].from_states,
    "mark_complete": DOC_ROWS["T13"].from_states,
    "abort": DOC_ROWS["T19"].from_states,
}
AUTO_FROM = frozenset(
    st for row in DOC_ROWS.values() if row.source is Source.AUTO for st in row.from_states
)

# ---------------------------------------------------------------------------
# Driving the real mission
# ---------------------------------------------------------------------------

GROUND: dict[str, Any] = {
    "fc_link_up": True,
    "mode": "STABILIZE",
    "armed": False,
    "landed_state": LandedState.ON_GROUND,
    "rel_alt_m": 0.0,
    "home_dist_m": 0.0,
    "battery_pct": 95.0,
    "attitude_age_ms": 10,
    "attitude_degraded": False,
    "rc_seen": True,
}
FLYING: dict[str, Any] = {
    **GROUND,
    "mode": "GUIDED",
    "armed": True,
    "landed_state": LandedState.IN_AIR,
    "rel_alt_m": 10.0,
    "home_dist_m": 5.0,
    "battery_pct": 80.0,
}
UI_TOKEN = "fixture-ui-token"
_DEFAULT: Any = object()


class Run:
    """One Mission fed the way the core feeds it ([M2]): one ``on_*`` call per
    input, then ``maybe_publish`` and ``take_requests`` at that input's stamp."""

    def __init__(self, config: MissionConfig | None = None) -> None:
        self.cfg = config if config is not None else MissionConfig()
        self.m = Mission(self.cfg)
        self.t = 1_000
        self.vehicle: dict[str, Any] = dict(GROUND)
        self.packets: list[MissionStatePacket] = []
        self.requests: list[tuple[int, FcRequest]] = []
        self.acks: list[AckPacket] = []
        self.cmd_log: list[tuple[int, str, AckResult]] = []
        self.stamps: list[int] = []
        self.kind: str | None = None
        self._ids = 0

    def _at(self, dt: int, at: int | None) -> int:
        self.t = self.t + dt if at is None else at
        self.stamps.append(self.t)
        return self.t

    def _after(self, kind: str) -> None:
        self.kind = kind
        pkt = self.m.maybe_publish(self.t)
        if pkt is not None:
            self.packets.append(pkt)
        self.requests.extend((self.t, req) for req in self.m.take_requests())

    def tick(self, dt: int = 50, *, at: int | None = None, **vehicle: Any) -> None:
        t = self._at(dt, at)
        self.vehicle.update(vehicle)
        self.m.on_tick(VehicleSnapshot(t_ms=t, **self.vehicle), t)
        self._after("TICK")

    def cmd(
        self,
        name: str,
        params: Any = _DEFAULT,
        *,
        dt: int = 10,
        at: int | None = None,
        cmd_id: str | None = None,
        auth: bool = True,
        from_ground: bool = True,
    ) -> AckResult:
        t = self._at(dt, at)
        if cmd_id is None:
            self._ids += 1
            cmd_id = f"ui-{self._ids:04d}"
        if params is _DEFAULT:
            params = PrimeParams().to_obj() if name == "prime" else None
        pkt = CommandPacket(cmd_id=cmd_id, token=UI_TOKEN, command=CommandName(name), params=params)
        ack = self.m.on_command(pkt, auth, t, from_ground=from_ground)
        assert ack.cmd_id == cmd_id
        self.acks.append(ack)
        self.cmd_log.append((t, name, ack.result))
        self._after("CMD")
        return ack.result

    def trk(
        self,
        track_id: int = 7,
        state: str = "confirmed",
        *,
        dt: int = 10,
        at: int | None = None,
        hits: int = 6,
        misses: int = 0,
        w: float = 40.0,
    ) -> None:
        t = self._at(dt, at)
        pkt = TrackPacket(
            t_cap=t - 30,
            track_id=track_id,
            state=TrackState(state),
            u=960.0,
            v_px=600.0,
            du=0.0,
            dv=0.0,
            w=w,
            h=w,
            hits=hits,
            misses=misses,
            age_frames=max(1, hits + misses),
        )
        self.m.on_track(pkt, t)
        self._after("TRK")

    def gde(
        self,
        kind: str,
        *,
        dt: int = 10,
        at: int | None = None,
        track_id: int = 7,
        t_cap: int = 1234,
        miss_m: tuple[float, float] = (0.1, -0.2),
        z_m: float = 12.0,
    ) -> None:
        t = self._at(dt, at)
        k = GuidanceEventKind(kind)
        if k is GuidanceEventKind.COMMIT:
            ev = GuidanceEvent(
                kind=k, t_ms=t, track_id=track_id, t_cap=t_cap, miss_m=miss_m, z_m=z_m
            )
        else:
            ev = GuidanceEvent(kind=k, t_ms=t)
        self.m.on_guidance(ev)
        self._after("GDE")

    def hb(self, dt: int = 10, *, at: int | None = None) -> None:
        t = self._at(dt, at)
        self.m.on_ground_heartbeat(t)
        self._after("HB")

    def flush(self) -> None:
        """A tick one publish period later: it publishes the pending events."""
        self.tick(dt=self.cfg.publish_period_ms)

    @property
    def state(self) -> MissionState | None:
        return self.m.state

    @property
    def log(self) -> list[tuple[int, str, MissionState | None, MissionState, Source]]:
        return self.m.transition_log

    def events(self) -> list[Event]:
        return [e for p in self.packets for e in p.events]

    def names(self) -> list[str]:
        return [e.name for e in self.events()]

    def requested(self, kind: FcRequestKind) -> list[tuple[int, Any]]:
        return [(t, req.value) for t, req in self.requests if req.kind is kind]


# State builders: real inputs from unprimed to the named state.


def unprimed() -> Run:
    r = Run()
    r.tick()
    return r


def primed(**prime: Any) -> Run:
    r = unprimed()
    assert r.cmd("prime", PrimeParams(**prime).to_obj()) is ACC
    return r


def launched(**prime: Any) -> Run:
    r = primed(**prime)
    r.tick()  # a known mode other than GUIDED after the prime (T03 edge)
    r.tick(mode="GUIDED")
    assert r.state is S.LAUNCH
    return r


def searching(**prime: Any) -> Run:
    r = launched(**prime)
    r.tick(**FLYING)
    assert r.state is S.SEARCH
    return r


def acquiring(**prime: Any) -> Run:
    r = searching(**prime)
    r.trk(7)
    assert r.state is S.ACQUIRING
    return r


def engaged(**prime: Any) -> Run:
    r = acquiring(**prime)
    t5 = r.t
    r.trk(7, at=t5 + 500)
    r.trk(7, at=t5 + 990)
    assert r.cmd("approve_engage", at=t5 + r.cfg.approve_settle_ms) is ACC
    assert r.state is S.ENGAGED
    return r


def coasting(**prime: Any) -> Run:
    r = engaged(**prime)
    r.trk(7, "coasting", hits=0, misses=1)
    assert r.state is S.COASTING
    return r


def touching() -> Run:
    r = engaged(trial_type=TrialType.TOUCH)
    r.gde("commit")
    assert r.state is S.TOUCH
    return r


def completed(**prime: Any) -> Run:
    r = engaged(**prime)
    assert r.cmd("mark_complete") is ACC
    assert r.state is S.COMPLETE
    return r


def missed() -> Run:
    r = touching()
    r.gde("pass_done")
    assert r.state is S.MISS
    return r


def lost(**prime: Any) -> Run:
    r = engaged(**prime)
    r.trk(7, "coasting", hits=0, misses=r.cfg.coast_cap + 1)
    assert r.state is S.LOST
    return r


def returning(**prime: Any) -> Run:
    r = searching(**prime)
    r.tick(battery_pct=10.0)
    assert r.state is S.RETURN
    return r


def landed(**prime: Any) -> Run:
    r = returning(**prime)
    r.tick(landed_state=LandedState.ON_GROUND, rel_alt_m=0.0)
    assert r.state is S.LAND
    return r


def aborted(**prime: Any) -> Run:
    r = primed(**prime)
    assert r.cmd("abort") is ACC
    assert r.state is S.ABORT
    return r


BUILDERS: dict[MissionState | None, Callable[[], Run]] = {
    None: unprimed,
    S.PRIMED: primed,
    S.LAUNCH: launched,
    S.SEARCH: searching,
    S.ACQUIRING: acquiring,
    S.ENGAGED: engaged,
    S.COASTING: coasting,
    S.TOUCH: touching,
    S.COMPLETE: completed,
    S.MISS: missed,
    S.LOST: lost,
    S.RETURN: returning,
    S.LAND: landed,
    S.ABORT: aborted,
}


# ---------------------------------------------------------------------------
# (1) Contract-anchored data
# ---------------------------------------------------------------------------


def test_m_transitions_equal_the_contract_table_row_by_row() -> None:
    """[M1], T01-T24: TRANSITIONS is the §4.2 table (From incl. `A` and
    "every state", To, Input, Source), so an edit to either side fails here."""
    assert [spec.tid for spec in TRANSITIONS] == list(DOC_ROWS)
    assert list(DOC_ROWS) == [f"T{n:02d}" for n in range(1, 25)]
    for spec in TRANSITIONS:
        row = DOC_ROWS[spec.tid]
        assert spec.from_states == row.from_states, spec.tid
        assert spec.to_state is row.to_state, spec.tid
        assert spec.inputs == row.inputs, spec.tid
        assert spec.source is row.source, spec.tid
    assert AIRBORNE_STATES == DOC_A


def test_m_precedence_equals_the_contract_ranking() -> None:
    """[M4]: the ranking is the contract's, and ranks every row exactly once."""
    assert PRECEDENCE == DOC_PRECEDENCE
    ranked = [tid for group in PRECEDENCE for tid in group]
    assert sorted(ranked) == sorted(DOC_ROWS)


def test_m_config_round_trips_for_the_recording_meta() -> None:
    """[R2], [R3]: the mission constants survive meta.config; nothing coerced."""
    cfg = MissionConfig(coast_cap=7, takeoff_done_frac=0.8, tone_states=(S.ABORT,))
    obj = json.loads(canonical_json(cfg.to_obj()))
    assert MissionConfig.from_obj(obj) == cfg
    assert MissionConfig.from_obj(MissionConfig().to_obj()) == MissionConfig()
    with pytest.raises(ValueError):
        MissionConfig.from_obj({**obj, "coast_cap": 7.0})
    with pytest.raises(ValueError):
        MissionConfig.from_obj({k: v for k, v in obj.items() if k != "track_timeout_ms"})


# ---------------------------------------------------------------------------
# (2) Every transition, driven by real inputs
# ---------------------------------------------------------------------------


def _t02() -> Run:
    r = primed()
    assert r.cmd("prime", PrimeParams(d_s=7.0).to_obj()) is ACC
    assert r.m.trial is not None and r.m.trial.d_s == 7.0  # replaces the trial
    return r


def _t05() -> Run:
    r = acquiring()
    assert r.m.candidate_id == 7
    return r


def _t06_trk() -> Run:
    r = acquiring()
    r.trk(7, "coasting", hits=0, misses=r.cfg.coast_cap + 1)
    assert r.m.candidate_id is None
    return r


def _t06_tick() -> Run:
    r = acquiring()
    r.tick(dt=r.cfg.track_timeout_ms + 1)
    return r


def _t07_any() -> Run:
    r = acquiring(engage_preauthorized=True)
    r.hb()
    assert r.m.engaged_track_id == 7
    return r


def _t09() -> Run:
    r = coasting()
    r.trk(7)
    return r


def _t10_tick() -> Run:
    r = coasting()
    r.tick(dt=r.cfg.track_timeout_ms + 1)
    assert r.m.engaged_track_id is None
    return r


def _t12() -> Run:
    r = engaged()
    r.gde("hold_complete")
    return r


def _t15() -> Run:
    r = lost()
    r.hb()
    return r


def _t16() -> Run:
    r = completed()
    r.trk(9)
    return r


def _t18() -> Run:
    r = searching()
    r.tick(fc_link_up=False)
    return r


def _t19() -> Run:
    r = searching()
    r.cmd("abort")
    return r


def _t20() -> Run:
    r = searching()
    r.tick(mode="RTL")
    return r


def _t21() -> Run:
    r = searching()
    r.cmd("abort")
    r.tick()
    return r


def _t22() -> Run:
    r = aborted()
    r.gde("pass_done")
    return r


def _t24() -> Run:
    r = landed()
    r.tick(armed=False)
    r.cmd("prime")
    return r


ROW_CASES: dict[tuple[str, str], Callable[[], Run]] = {
    ("T01", "CMD"): primed,
    ("T02", "CMD"): _t02,
    ("T03", "TICK"): launched,
    ("T04", "TICK"): searching,
    ("T05", "TRK"): _t05,
    ("T06", "TRK"): _t06_trk,
    ("T06", "TICK"): _t06_tick,
    ("T07", "CMD"): engaged,
    ("T07", "HB"): _t07_any,
    ("T08", "TRK"): coasting,
    ("T09", "TRK"): _t09,
    ("T10", "TRK"): lost,
    ("T10", "TICK"): _t10_tick,
    ("T11", "GDE"): touching,
    ("T12", "GDE"): _t12,
    ("T13", "CMD"): completed,
    ("T14", "GDE"): missed,
    ("T15", "HB"): _t15,
    ("T16", "TRK"): _t16,
    ("T17", "TICK"): returning,
    ("T18", "TICK"): _t18,
    ("T19", "CMD"): _t19,
    ("T20", "TICK"): _t20,
    ("T21", "TICK"): _t21,
    ("T22", "GDE"): _t22,
    ("T23", "TICK"): landed,
    ("T24", "CMD"): _t24,
}


@pytest.mark.parametrize(("tid", "kind"), sorted(ROW_CASES), ids=lambda x: str(x))
def test_m_each_row_fires_on_its_input(tid: str, kind: str) -> None:
    """T01-T24, [M3]: the last transition is the row, from one of its From
    states, to its To state, with its Source, stamped with the input's stamp,
    on an input kind its Input column allows."""
    r = ROW_CASES[(tid, kind)]()
    row = DOC_ROWS[tid]
    t, logged_tid, src, dst, source = r.log[-1]
    assert (logged_tid, source) == (tid, row.source)
    assert src in row.from_states and dst is row.to_state
    assert t == r.t and r.kind == kind
    assert kind in row.inputs or "any" in row.inputs


def test_m_row_cases_cover_every_row_and_input_kind() -> None:
    """T01-T24: every row has a case per listed input kind, and every "any"
    row is driven by an input kind outside its own column."""
    for tid, row in DOC_ROWS.items():
        kinds = {k for (t, k) in ROW_CASES if t == tid}
        assert row.inputs - {"any"} <= kinds, tid
        if "any" in row.inputs:
            assert kinds - row.inputs, tid


# ---------------------------------------------------------------------------
# (3) Every rejection ([M5], [P5a], [P5b])
# ---------------------------------------------------------------------------

INVALID = [
    (state, command)
    for state in [None, *S]
    for command in VALID_STATES
    if state not in VALID_STATES[command]
]


@pytest.mark.parametrize(
    ("state", "command"),
    INVALID,
    ids=[f"{s.value if s else 'unprimed'}-{c}" for s, c in INVALID],
)
def test_m_command_invalid_in_state_is_rejected_and_logged(
    state: MissionState | None, command: str
) -> None:
    """[M5], [P5b], [P3a], [M4a]: acked rejected_state and logged as a cmd:
    event (none while unprimed); its own row never fires, and an AUTO row
    pending in the state takes the input instead."""
    r = BUILDERS[state]()
    before = len(r.log)
    assert r.cmd(command) is REJ
    t_cmd = r.t
    rows = r.log[before:]
    assert all(row[4] is Source.AUTO for row in rows)
    assert len(rows) == (1 if state in AUTO_FROM else 0)
    r.flush()
    if state is None:
        assert r.packets == [] and r.state is None
    else:
        assert Event(t=t_cmd, name=f"cmd:{command}:rejected_state") in r.events()


def test_m_prime_in_launch_is_rejected_by_state_on_the_ground() -> None:
    """[M5]: prime is refused outside unprimed/PRIMED/LAND even while the
    vehicle is still disarmed on the ground."""
    r = launched()
    assert r.vehicle["armed"] is False and r.vehicle["landed_state"] is LandedState.ON_GROUND
    assert r.cmd("prime") is REJ
    assert r.state is S.LAUNCH


@pytest.mark.parametrize(
    "vehicle",
    [
        {"armed": True},
        {"landed_state": LandedState.UNDEFINED},
        {"landed_state": LandedState.IN_AIR},
        {"fc_link_up": False},
        {"armed": None, "mode": None},
    ],
    ids=["armed", "landed_undefined", "in_air", "link_down", "no_heartbeat"],
)
def test_m_prime_needs_disarmed_and_on_ground(vehicle: dict[str, Any]) -> None:
    """[M5], [M2a], T01: prime only while disarmed and on_ground; a link-down
    snapshot makes both unknown."""
    r = Run()
    assert r.cmd("prime") is REJ  # no snapshot yet: unknown
    r.tick(**vehicle)
    assert r.cmd("prime") is REJ
    assert r.state is None and r.packets == []
    r.tick(**GROUND)
    assert r.cmd("prime") is ACC
    assert r.state is S.PRIMED


@pytest.mark.parametrize("builder", [primed, landed], ids=["T02", "T24"])
def test_m_reprime_needs_disarmed_and_on_ground(builder: Callable[[], Run]) -> None:
    """[M5], T02, T24: an armed vehicle refuses the next trial."""
    r = builder()
    r.tick(armed=True, landed_state=LandedState.ON_GROUND)
    state = r.state
    assert r.cmd("prime") is REJ
    assert r.state is state


@pytest.mark.parametrize(
    "bad",
    [
        {"pass_budget": 2},
        {"v_max": 5.5},
        {"k": 0},
        {"alpha": 1.5},
        {"trial_type": "kite"},
        {"engage_preauthorized": 1},
        {"search_alt": "10"},
    ],
    ids=lambda b: next(iter(b)),
)
def test_m_invalid_prime_is_rejected_params_never_partly_applied(bad: dict[str, Any]) -> None:
    """[P5], [M6]: an invalid set is acked rejected_params; the trial stands."""
    r = primed(d_s=7.0)
    trial, before = r.m.trial, len(r.log)
    assert r.cmd("prime", {**PrimeParams().to_obj(), **bad}) is AckResult.REJECTED_PARAMS
    assert r.m.trial == trial and len(r.log) == before


def test_m_prime_with_missing_params_is_rejected_params_while_unprimed() -> None:
    """[P5], [P3a]: missing params or a missing field reject; nothing publishes."""
    r = unprimed()
    params = PrimeParams().to_obj()
    del params["geofence_radius_m"]
    assert r.cmd("prime", params) is AckResult.REJECTED_PARAMS
    assert r.cmd("prime", None) is AckResult.REJECTED_PARAMS
    assert r.state is None and r.packets == []


def test_m_rejected_auth_is_not_stored() -> None:
    """[P5a]: a command that fails authentication is acked rejected_auth, is
    logged, and is not stored: the same id authenticated later executes."""
    r = primed()
    assert r.cmd("abort", cmd_id="u-1", auth=False) is AckResult.REJECTED_AUTH
    t_bad = r.t
    assert r.state is S.PRIMED
    assert r.cmd("abort", cmd_id="u-1") is ACC
    assert r.state is S.ABORT
    assert Event(t=t_bad, name="cmd:abort:rejected_auth") in r.events()


def test_m_true_retry_returns_the_stored_ack_and_executes_nothing() -> None:
    """[P5a]: same cmd_id and fingerprint is the stored ack, no execution and
    no second event; abort is not exempt."""
    r = primed()
    assert r.cmd("abort", cmd_id="a-1") is ACC
    t_abort, rows = r.t, len(r.log)
    assert r.cmd("abort", cmd_id="a-1", dt=1) is ACC  # an ABORT->ABORT T19 if it ran
    assert r.log[rows:] == [] or all(row[4] is Source.AUTO for row in r.log[rows:])
    assert [row[1] for row in r.log].count("T19") == 1
    r.flush()
    assert [e for e in r.events() if e.name.startswith("cmd:abort")] == [
        Event(t=t_abort, name="cmd:abort:accepted")
    ]


def test_m_reused_id_with_another_fingerprint_is_rejected_duplicate_id() -> None:
    """[P5a]: same cmd_id, different command or params: rejected_duplicate_id,
    nothing executes, and the stored entry is unchanged."""
    r = primed()
    p7 = PrimeParams(d_s=7.0).to_obj()
    assert r.cmd("prime", p7, cmd_id="dup") is ACC
    rows = len(r.log)
    assert r.cmd("abort", cmd_id="dup") is AckResult.REJECTED_DUPLICATE_ID
    assert r.cmd("prime", {**p7, "d_s": 8.0}, cmd_id="dup") is AckResult.REJECTED_DUPLICATE_ID
    assert r.state is S.PRIMED and r.m.trial is not None and r.m.trial.d_s == 7.0
    assert r.cmd("prime", p7, cmd_id="dup") is ACC  # the stored entry still answers
    assert len(r.log) == rows
    r.flush()
    dup = [e.name for e in r.events() if e.name.startswith("cmd:") and "duplicate" in e.name]
    assert dup == ["cmd:abort:rejected_duplicate_id", "cmd:prime:rejected_duplicate_id"]
    assert r.names().count("cmd:prime:accepted") == 2  # T01 and the one T02


def test_m_rejected_command_is_stored_like_any_other() -> None:
    """[P5a]: a rejection is stored; its retry later returns it unexecuted."""
    r = primed()
    assert r.cmd("approve_engage", cmd_id="ap-1") is REJ
    r.tick()
    r.tick(mode="GUIDED")
    r.tick(**FLYING)
    r.trk(7)
    t5 = r.t
    r.trk(7, at=t5 + 990)
    assert r.cmd("approve_engage", cmd_id="ap-1", at=t5 + 1000) is REJ
    assert r.state is S.ACQUIRING and r.m.engaged_track_id is None


# ---------------------------------------------------------------------------
# (4) Precedence ([M4]) and AUTO consumption ([M4a])
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode_rtl", "ground_lost", "battery_low", "tid", "event"),
    [
        (True, True, True, "T20", "failsafe:mode"),
        (False, True, True, "T18", "failsafe:ground_link"),
        (False, False, True, "T17", "budget:battery"),
        (False, False, False, "T10", "track_dead:7"),
    ],
)
def test_m_tick_precedence_ladder(
    mode_rtl: bool, ground_lost: bool, battery_low: bool, tid: str, event: str
) -> None:
    """[M4]: T20 > T18 > T17 > T10 on one tick where all their triggers hold
    (the engaged track has timed out on every rung)."""
    r = engaged()
    t_hb = r.t
    limit = t_hb + r.cfg.ground_link_timeout_ms
    if not ground_lost:
        r.hb(at=limit)
    r.tick(
        at=limit + 1,
        mode="RTL" if mode_rtl else "GUIDED",
        battery_pct=10.0 if battery_low else 80.0,
    )
    assert r.log[-1][1] == tid
    assert len([row for row in r.log if row[0] == limit + 1]) == 1
    assert event in r.names()


@pytest.mark.parametrize(
    ("late", "vehicle", "event"),
    [
        (True, {"battery_pct": 10.0, "home_dist_m": 99.0}, "budget:flight_time"),
        (False, {"battery_pct": 10.0, "home_dist_m": 99.0}, "budget:battery"),
        (False, {"home_dist_m": 99.0}, "budget:geofence"),
        (False, {"fc_link_up": False}, "failsafe:fc_link"),
    ],
)
def test_m_event_names_the_first_condition(late: bool, vehicle: dict[str, Any], event: str) -> None:
    """[M4]: several T17 (T18) conditions name the first of flight_time,
    battery, geofence, reacquire (fc_link, ground_link)."""
    r = searching(flight_time_cap_s=2.0)
    t_launch = next(row[0] for row in r.log if row[1] == "T03")
    t = t_launch + (2001 if late else 1000)
    if vehicle.get("fc_link_up") is False:
        t = r.t + r.cfg.ground_link_timeout_ms + 1  # ground link lost as well
    else:
        r.hb(at=t - 1)
    r.tick(at=t, **vehicle)
    assert r.state is S.RETURN
    assert event in r.names()


# Finding DT-1: the T17 / T18 thresholds are safety budgets; each gets a
# just-passes / just-fails pair against the primed or configured value.


@pytest.mark.parametrize(
    ("field", "limit", "past", "event"),
    [
        ("battery_pct", "battery_floor_pct", -0.01, "budget:battery"),
        ("home_dist_m", "geofence_radius_m", +0.01, "budget:geofence"),
    ],
    ids=["battery", "geofence"],
)
def test_m_t17_battery_and_geofence_boundaries(
    field: str, limit: str, past: float, event: str
) -> None:
    """T17, [M2a], DT-1: battery < battery_floor_pct and home_dist >
    geofence_radius_m are strict. With the primed defaults (30 %, 60 m) a tick
    exactly at the limit stays in SEARCH with no transition; 0.01 past it
    (29.99 %, 60.01 m) fires T17 with the named budget event."""
    r = searching()
    bound = getattr(r.m.trial, limit)
    rows = len(r.log)
    r.tick(**{field: bound})
    assert r.state is S.SEARCH and len(r.log) == rows
    r.tick(**{field: bound + past})
    assert r.log[-1][:2] == (r.t, "T17") and r.state is S.RETURN
    assert event in r.names()


def test_m_t17_flight_time_cap_boundary() -> None:
    """T17, DT-1: flight time since LAUNCH > flight_time_cap_s is strict.
    Primed cap 2 s: a tick exactly 2000 ms after the T03 stamp stays in SEARCH;
    1 ms later fires T17 budget:flight_time."""
    r = searching(flight_time_cap_s=2.0)
    trial = r.m.trial
    assert trial is not None
    cap_ms = round(trial.flight_time_cap_s * 1000.0)
    t_launch = next(row[0] for row in r.log if row[1] == "T03")
    rows = len(r.log)
    r.tick(at=t_launch + cap_ms)
    assert r.state is S.SEARCH and len(r.log) == rows
    r.tick(at=t_launch + cap_ms + 1)
    assert r.log[-1][:2] == (r.t, "T17") and r.state is S.RETURN
    assert "budget:flight_time" in r.names()


def test_m_t18_ground_link_timeout_boundary() -> None:
    """T18, [U6], DT-1: a ground-heartbeat silence of exactly
    ground_link_timeout_ms is not link loss; 1 ms more fires T18
    failsafe:ground_link."""
    r = searching()
    r.hb()
    t_hb, timeout = r.t, r.cfg.ground_link_timeout_ms
    rows = len(r.log)
    r.tick(at=t_hb + timeout)
    assert r.state is S.SEARCH and len(r.log) == rows
    r.tick(at=t_hb + timeout + 1)
    assert r.log[-1][:2] == (r.t, "T18") and r.state is S.RETURN
    assert "failsafe:ground_link" in r.names()


def test_m_auto_row_consumes_its_input() -> None:
    """[M4a], T15, T05: the confirmed packet right after LOST fires T15 only;
    the next one fires T05."""
    r = lost()
    r.trk(9)
    assert r.log[-1][1] == "T15" and r.state is S.SEARCH and r.m.candidate_id is None
    r.trk(9)
    assert r.log[-1][1] == "T05" and r.m.candidate_id == 9


def test_m_preauthorized_t07_fires_on_the_input_after_t05() -> None:
    """T07, [M4a]: with engage_preauthorized the input after T05 engages; the
    T05 input itself does not."""
    r = searching(engage_preauthorized=True)
    r.trk(7)
    assert r.state is S.ACQUIRING and r.m.engaged_track_id is None
    r.tick()
    assert r.log[-1][1:] == ("T07", S.ACQUIRING, S.ENGAGED, Source.HUMAN)
    assert r.m.engaged_track_id == 7


@pytest.mark.parametrize("death", ["final_packet", "timeout"])
def test_m_candidate_death_beats_preauthorized_t07(death: str) -> None:
    """[M4] T06 > T07: the candidate's death on the input after T05 wins."""
    r = acquiring(engage_preauthorized=True)
    if death == "final_packet":
        r.trk(7, "coasting", hits=0, misses=r.cfg.coast_cap + 1)
    else:
        r.tick(dt=r.cfg.track_timeout_ms + 1)
    assert r.log[-1][1] == "T06" and r.state is S.SEARCH
    assert r.m.engaged_track_id is None


@pytest.mark.parametrize(
    ("builder", "command", "tid"),
    [(completed, "abort", "T19"), (lost, "mark_complete", "T13"), (missed, "abort", "T19")],
)
def test_m_higher_row_beats_the_pending_auto_row(
    builder: Callable[[], Run], command: str, tid: str
) -> None:
    """[M4], [M4a]: T19 and T13 outrank AUTO on the input after entry."""
    r = builder()
    rows = len(r.log)
    assert r.cmd(command) is ACC
    assert [row[1] for row in r.log[rows:]] == [tid]


def test_m_one_transition_per_input() -> None:
    """[M4]: the abort enters ABORT only; T21 waits for the next input."""
    r = searching()
    rows = len(r.log)
    r.cmd("abort")
    assert [row[1] for row in r.log[rows:]] == ["T19"]
    r.tick()
    assert [row[1] for row in r.log[rows:]] == ["T19", "T21"]


# ---------------------------------------------------------------------------
# (5) T03 edge gate (E1-D4), T04 takeoff floor
# ---------------------------------------------------------------------------


def test_m_t03_waits_for_a_non_guided_snapshot_after_the_prime() -> None:
    """T03, [M2a]: primed while in GUIDED stays PRIMED until a link-up
    snapshot with a known non-GUIDED mode, then GUIDED launches."""
    r = unprimed()
    r.tick(mode="GUIDED")
    assert r.cmd("prime") is ACC
    for _ in range(3):
        r.tick()
    r.tick(fc_link_up=False, mode="STABILIZE")  # mode unknown while the link is down
    r.tick(fc_link_up=True, mode="GUIDED")
    assert r.state is S.PRIMED
    r.tick(mode="STABILIZE")
    assert r.state is S.PRIMED
    r.tick(mode="GUIDED")
    assert r.log[-1][1] == "T03"


def test_m_reprime_resets_the_t03_latch() -> None:
    """T02, T03: a re-prime needs a fresh switch movement."""
    r = primed()
    r.tick()  # STABILIZE: latch set for the first trial
    assert r.cmd("prime", PrimeParams(d_s=6.0).to_obj()) is ACC
    r.tick(mode="GUIDED")
    assert r.state is S.PRIMED
    r.tick(mode="STABILIZE")
    r.tick(mode="GUIDED")
    assert r.log[-1][1] == "T03"


@pytest.mark.parametrize(
    "blocker",
    [{"attitude_degraded": True}, {"armed": True}, {"landed_state": LandedState.UNDEFINED}],
    ids=["attitude_degraded", "armed", "not_on_ground"],
)
def test_m_t03_needs_fresh_attitude_disarmed_on_ground(blocker: dict[str, Any]) -> None:
    """T03: each condition holds the launch."""
    r = primed()
    r.tick()
    r.tick(mode="GUIDED", **blocker)
    assert r.state is S.PRIMED
    r.tick(**{k: GROUND[k] for k in blocker})
    assert r.log[-1][1] == "T03"


def test_m_t04_takeoff_done_boundary() -> None:
    """T04, DT-8: airborne and rel_alt >= takeoff_done_frac x search_alt. With
    the defaults (frac 0.9, search_alt 10) the floor is 9.0 m: rel_alt 8.99
    stays in LAUNCH, exactly 9.0 enters SEARCH."""
    r = launched()
    trial = r.m.trial
    assert trial is not None
    floor = r.cfg.takeoff_done_frac * trial.search_alt
    r.tick(**{**FLYING, "rel_alt_m": floor - 0.01})
    assert r.state is S.LAUNCH and r.log[-1][1] == "T03"
    r.tick(rel_alt_m=floor)
    assert r.log[-1][:2] == (r.t, "T04") and r.state is S.SEARCH


@pytest.mark.parametrize(
    "landed", [LandedState.ON_GROUND, LandedState.UNDEFINED], ids=["on_ground", "unknown"]
)
def test_m_t04_needs_airborne_at_full_altitude(landed: LandedState) -> None:
    """T04, [M2a], DT-8: rel_alt at the full search_alt is not enough without
    airborne (on_ground, or landed_state UNDEFINED, which is unknown): LAUNCH
    holds. The same altitude with IN_AIR enters SEARCH."""
    r = launched()
    trial = r.m.trial
    assert trial is not None
    r.tick(**{**FLYING, "landed_state": landed, "rel_alt_m": trial.search_alt})
    assert r.state is S.LAUNCH and r.log[-1][1] == "T03"
    r.tick(landed_state=LandedState.IN_AIR)
    assert r.log[-1][:2] == (r.t, "T04") and r.state is S.SEARCH


# ---------------------------------------------------------------------------
# (6) Approve settle ([M8])
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("from_ground", [True, False], ids=["ui", "radio"])
def test_m_approve_settle_boundary(from_ground: bool) -> None:
    """[M8], [F7]: approve_settle_ms after T05 is accepted, 1 ms less is
    rejected_state; the radio approve follows the same rule."""
    r = acquiring()
    t5, settle = r.t, r.cfg.approve_settle_ms

    def approve(t: int) -> AckResult:
        cmd_id = None if from_ground else f"rc:approve:{t}"
        return r.cmd("approve_engage", at=t, cmd_id=cmd_id, from_ground=from_ground)

    assert approve(t5 + settle - 1) is REJ
    assert r.state is S.ACQUIRING
    assert approve(t5 + settle) is ACC
    assert r.log[-1] == (t5 + settle, "T07", S.ACQUIRING, S.ENGAGED, Source.HUMAN)


# ---------------------------------------------------------------------------
# (7) Lock stickiness ([M7]-[M9])
# ---------------------------------------------------------------------------


def test_m_lock_is_sticky_against_a_better_track() -> None:
    """[M7], [M8], brief 3.2: a bigger, longer-hit, newer confirmed track
    never becomes the candidate or the engaged track."""
    r = acquiring()
    t5 = r.t
    r.trk(8, w=300.0, hits=40, at=t5 + 100)
    assert r.m.candidate_id == 7
    r.trk(7, at=t5 + 600)
    assert r.cmd("approve_engage", at=t5 + 1000) is ACC
    assert r.m.engaged_track_id == 7
    r.trk(8, w=400.0, hits=60)
    r.trk(9, w=500.0, hits=80)
    assert r.state is S.ENGAGED and r.m.engaged_track_id == 7
    r.trk(7, "coasting", hits=0, misses=2)
    r.trk(8, w=400.0, hits=61)
    assert r.state is S.COASTING and r.m.engaged_track_id == 7
    r.trk(7)
    assert r.log[-1][1] == "T09"
    r.flush()
    t07 = next(row[0] for row in r.log if row[1] == "T07")
    assert {p.engaged_track_id for p in r.packets if p.t >= t07} == {7}
    assert not [n for n in r.names() if n.endswith((":8", ":9"))]


def test_m_engaged_death_in_touch_clears_without_transition() -> None:
    """[M9]: the final packet in TOUCH clears the id; no transition."""
    r = touching()
    rows = len(r.log)
    r.trk(7, "coasting", hits=0, misses=r.cfg.coast_cap + 1)
    t_dead = r.t
    assert r.state is S.TOUCH and r.m.engaged_track_id is None and len(r.log) == rows
    r.flush()
    assert Event(t=t_dead, name="track_dead:7") in r.events()


def test_m_engaged_timeout_in_return_clears_without_transition() -> None:
    """[M9], [P2a]: a timeout death in RETURN clears the id; no transition."""
    r = engaged()
    seen = r.t - 10  # the last engaged packet
    r.tick(battery_pct=10.0)
    assert r.state is S.RETURN and r.m.engaged_track_id == 7
    rows = len(r.log)
    r.tick(at=seen + r.cfg.track_timeout_ms)
    assert r.m.engaged_track_id == 7
    r.tick(at=seen + r.cfg.track_timeout_ms + 1)
    assert r.m.engaged_track_id is None and r.state is S.RETURN and len(r.log) == rows


# ---------------------------------------------------------------------------
# (8) Track death ([P2a])
# ---------------------------------------------------------------------------


def test_m_final_packet_is_death_at_coast_cap_plus_one() -> None:
    """[P2a], T08, T10: misses == coast_cap still coasts; coast_cap + 1 is
    the final packet."""
    r = engaged()
    cap = r.cfg.coast_cap
    r.trk(7, "coasting", hits=0, misses=cap)
    assert r.log[-1][1] == "T08" and r.m.engaged_track_id == 7
    r.trk(7, "coasting", hits=0, misses=cap + 1)
    assert r.log[-1][1] == "T10" and r.m.engaged_track_id is None
    assert r.packets[-1].events[0] == Event(t=r.t, name="track_dead:7")


def test_m_timeout_death_at_the_boundary() -> None:
    """[P2a], T10: no packet within track_timeout_ms is death; exactly
    track_timeout_ms is not."""
    r = engaged()
    seen = r.t - 10
    r.tick(at=seen + r.cfg.track_timeout_ms)
    assert r.state is S.ENGAGED
    r.tick(at=seen + r.cfg.track_timeout_ms + 1)
    assert r.log[-1][1] == "T10" and r.kind == "TICK"


def test_m_timeout_dead_id_resumes_as_an_ordinary_packet() -> None:
    """[P2a]: after a timeout death the same id is ordinary; T05 may pick it."""
    r = engaged()
    r.tick(dt=r.cfg.track_timeout_ms + 20)
    assert r.state is S.LOST
    r.trk(7)
    assert r.log[-1][1] == "T15"
    r.trk(7)
    assert r.log[-1][1] == "T05" and r.m.candidate_id == 7


def test_m_reacquire_window_expires_before_engaged() -> None:
    """T15, T17: the window opens at T15; reacquire_window_ms later it has
    not expired, 1 ms after it has."""
    r = lost()
    r.hb()
    t_open, window = r.t, r.cfg.reacquire_window_ms
    r.hb(at=t_open + window - 4000)
    r.tick(at=t_open + window)
    assert r.state is S.SEARCH
    r.tick(at=t_open + window + 1)
    assert r.log[-1][1] == "T17" and "budget:reacquire" in r.names()


def test_m_reacquire_window_closes_on_engagement() -> None:
    """T07, T17: ENGAGED before the window expires closes it."""
    r = lost()
    r.hb()
    t_open, window = r.t, r.cfg.reacquire_window_ms
    r.trk(9)
    t5 = r.t
    r.trk(9, at=t5 + 990)
    assert r.cmd("approve_engage", at=t5 + 1000) is ACC
    r.hb(at=t_open + window - 1000)
    r.trk(9, at=t_open + window - 100)
    r.tick(at=t_open + window + 1)
    assert r.state is S.ENGAGED


# ---------------------------------------------------------------------------
# (9) Exit requests ([M10])
# ---------------------------------------------------------------------------


def test_m_rtl_on_entry_then_every_retry_while_guided_then_stops_for_good() -> None:
    """[M10], [F9]: RTL on entry, again every exit_retry_ms while link up and
    GUIDED, none while the link is down, none after a non-GUIDED mode."""
    r = searching()
    r.tick(battery_pct=10.0)
    t0, retry = r.t, r.cfg.exit_retry_ms
    assert r.state is S.RETURN
    assert r.requested(FcRequestKind.MODE_RTL) == [(t0, None)]
    for _ in range(retry // 50 - 1):
        r.tick()
    assert r.t == t0 + retry - 50 and len(r.requested(FcRequestKind.MODE_RTL)) == 1
    r.tick()
    r.tick(at=t0 + 2 * retry, fc_link_up=False)
    r.tick(at=t0 + 2 * retry + 50, fc_link_up=True)
    r.tick(at=t0 + 3 * retry + 100, mode="LOITER")
    r.tick(at=t0 + 4 * retry + 200, mode="GUIDED")
    r.tick(at=t0 + 6 * retry)
    rtl = [t for t, _ in r.requested(FcRequestKind.MODE_RTL)]
    assert rtl == [t0, t0 + retry, t0 + 2 * retry + 50]
    r.flush()
    assert [e.t for e in r.events() if e.name == "fc_request:RTL"] == rtl


def test_m_rtl_waits_for_link_and_guided_at_entry() -> None:
    """[M10]: entry with the link down requests nothing; the first link-up
    GUIDED tick requests at once."""
    r = searching()
    r.tick(fc_link_up=False)
    assert r.state is S.RETURN and r.requested(FcRequestKind.MODE_RTL) == []
    r.tick(fc_link_up=True)
    assert r.requested(FcRequestKind.MODE_RTL) == [(r.t, None)]


def test_m_rtl_on_entry_by_a_non_tick_input() -> None:
    """[M10], T16: entry on any input requests RTL at that input's stamp."""
    r = completed()
    r.hb()
    assert r.log[-1][1] == "T16"
    assert r.requested(FcRequestKind.MODE_RTL) == [(r.t, None)]


def test_m_no_rtl_over_a_mode_the_pilot_chose() -> None:
    """[M10], [M11], T20, T21: the radio hard abort's mode is never fought."""
    r = searching()
    r.tick(mode="RTL")
    r.tick()
    assert [row[1] for row in r.log[-2:]] == ["T20", "T21"]
    r.tick(dt=r.cfg.exit_retry_ms, mode="GUIDED")
    r.tick(dt=r.cfg.exit_retry_ms)
    assert r.requested(FcRequestKind.MODE_RTL) == []


def test_m_land_requests_after_t22_while_armed() -> None:
    """[M10], T22: LAND on entry and every exit_retry_ms while armed and
    GUIDED; it stops for good on disarm."""
    r = launched()
    r.tick(armed=True)  # armed on the stand, not yet airborne
    assert r.cmd("abort") is ACC
    r.tick()
    t0, retry = r.t, r.cfg.exit_retry_ms
    assert r.log[-1][1] == "T22"
    r.tick(at=t0 + retry - 1)
    r.tick(at=t0 + retry)
    r.tick(at=t0 + retry + 500, armed=False)
    r.tick(at=t0 + 3 * retry, armed=True)
    assert [t for t, _ in r.requested(FcRequestKind.MODE_LAND)] == [t0, t0 + retry]


def _aborted_on_the_stand() -> Run:
    r = launched()  # GUIDED, disarmed, on the ground
    assert r.cmd("abort") is ACC
    return r


@pytest.mark.parametrize("builder", [landed, _aborted_on_the_stand], ids=["T23", "T22_disarmed"])
def test_m_no_land_request_unless_t22_and_armed(builder: Callable[[], Run]) -> None:
    """[M10]: LAND entered by T23 (armed, GUIDED), or by T22 while disarmed
    (GUIDED), requests nothing."""
    r = builder()
    assert r.vehicle["mode"] == "GUIDED"
    for _ in range(3):
        r.tick(dt=r.cfg.exit_retry_ms)
    assert r.state is S.LAND
    assert r.requested(FcRequestKind.MODE_LAND) == []


# ---------------------------------------------------------------------------
# (10) Publishing ([P3], [P3a])
# ---------------------------------------------------------------------------


def test_m_first_packet_only_after_the_first_prime() -> None:
    """[P3a]: nothing before the first accepted prime; the first packet's
    events are cmd:prime:accepted then transition:UNPRIMED->PRIMED."""
    r = unprimed()
    r.cmd("abort")
    r.cmd("approve_engage")
    r.cmd("prime", {**PrimeParams().to_obj(), "k": 0})
    r.hb()
    for _ in range(10):
        r.tick()
    assert r.packets == []
    assert r.cmd("prime", PrimeParams(d_s=9.0).to_obj()) is ACC
    (first,) = r.packets
    assert first.t == r.t and first.mission_state is S.PRIMED
    assert first.engaged_track_id is None and first.trial == PrimeParams(d_s=9.0).echo()
    assert first.events == (
        Event(t=r.t, name="cmd:prime:accepted"),
        Event(t=r.t, name="transition:UNPRIMED->PRIMED"),
    )


def test_m_publish_cadence() -> None:
    """[P3], §9 mission_publish_hz: a packet per transition at once, and at
    publish_period_ms on ticks only; pending events ride the next packet."""
    r = primed()
    t_p, period = r.t, r.cfg.publish_period_ms
    for k in range(1, 9):
        r.tick(at=t_p + 50 * k)
    assert [p.t for p in r.packets] == [t_p, t_p + period, t_p + 2 * period]
    r.hb(at=t_p + 700)
    r.cmd("approve_engage", at=t_p + 710)
    assert len(r.packets) == 3  # non-tick inputs without a transition
    r.tick(at=t_p + 720)
    assert r.packets[-1].t == t_p + 720
    assert r.packets[-1].events == (Event(t=t_p + 710, name="cmd:approve_engage:rejected_state"),)
    r.cmd("abort", at=t_p + 730)
    assert r.packets[-1].t == t_p + 730 and r.packets[-1].mission_state is S.ABORT


def test_m_commit_and_miss_events() -> None:
    """T11, §4.6, [G6]: commit:<id>:<t_cap> then miss:%.3f with -0.000
    written 0.000, at the GDE stamp, before the transition."""
    r = engaged(trial_type=TrialType.TOUCH)
    r.gde("commit", track_id=7, t_cap=4321, miss_m=(-0.0004, 0.25), z_m=12.0)
    assert r.packets[-1].events == (
        Event(t=r.t, name="commit:7:4321"),
        Event(t=r.t, name="miss:0.000:0.250:12.000"),
        Event(t=r.t, name="transition:ENGAGED->TOUCH"),
        Event(t=r.t, name="tone:TOUCH"),
    )


def test_m_guidance_rows_respect_the_trial_type() -> None:
    """T11, T12: commit only in touch trials, hold_complete only in standoff."""
    r = engaged()  # standoff
    r.gde("commit")
    assert r.state is S.ENGAGED
    r = engaged(trial_type=TrialType.TOUCH)
    r.gde("hold_complete")
    assert r.state is S.ENGAGED


# A two-flight script through all 13 states, with every input kind, a
# rejection, an unauthenticated command, a true retry, and a reused id.
TOUCH_PARAMS = PrimeParams(trial_type=TrialType.TOUCH).to_obj()
STANDOFF_PARAMS = PrimeParams(engage_preauthorized=True, d_s=6.0, search_alt=12.0).to_obj()
SCRIPT: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
    ("tick", (), {}),
    ("cmd", ("approve_engage",), {}),  # unprimed: no event
    ("cmd", ("prime", TOUCH_PARAMS), {"cmd_id": "p-1"}),  # T01
    ("cmd", ("prime", TOUCH_PARAMS), {"cmd_id": "p-1"}),  # true retry: no event
    ("tick", (), {}),
    ("tick", (), {"mode": "GUIDED"}),  # T03
    ("tick", (), FLYING),  # T04
    ("trk", (7,), {}),  # T05
    ("cmd", ("approve_engage",), {"dt": 500}),  # not settled
    ("trk", (7,), {"dt": 480}),
    ("cmd", ("approve_engage",), {"dt": 20}),  # T07, exactly approve_settle_ms after T05
    ("trk", (7, "coasting"), {"hits": 0, "misses": 1}),  # T08
    ("trk", (7,), {}),  # T09
    ("trk", (7, "coasting"), {"hits": 0, "misses": 21}),  # T10
    ("hb", (), {}),  # T15
    ("trk", (8,), {}),  # T05
    ("trk", (8,), {"dt": 500}),
    ("cmd", ("abort",), {"auth": False, "cmd_id": "x-1"}),
    ("trk", (8,), {"dt": 480}),
    ("cmd", ("approve_engage",), {"dt": 10, "from_ground": False, "cmd_id": "rc:approve:1"}),
    ("gde", ("commit",), {"track_id": 8}),  # T11
    ("cmd", ("abort",), {"cmd_id": "p-1"}),  # rejected_duplicate_id
    ("gde", ("pass_done",), {"dt": 100}),  # T14
    ("tick", (), {}),  # T16
    ("tick", (), {"dt": 1000}),
    ("tick", (), {"mode": "RTL"}),
    ("tick", (), {"landed_state": LandedState.LANDING}),  # T23
    ("tick", (), {"landed_state": LandedState.ON_GROUND, "armed": False, "rel_alt_m": 0.0}),
    ("cmd", ("prime", STANDOFF_PARAMS), {}),  # T24
    ("tick", (), {"mode": "STABILIZE"}),
    ("tick", (), {"mode": "GUIDED"}),  # T03
    ("tick", (), {**FLYING, "rel_alt_m": 12.0}),  # T04
    ("trk", (9,), {}),  # T05
    ("tick", (), {}),  # T07 preauthorized
    ("gde", ("hold_complete",), {"track_id": 9}),  # T12
    ("hb", (), {}),  # T16
    ("cmd", ("abort",), {}),  # T19
    ("tick", (), {}),  # T21
    ("tick", (), {"landed_state": LandedState.ON_GROUND, "rel_alt_m": 0.0}),  # T23
    ("cmd", ("abort",), {}),  # T19
    ("hb", (), {}),  # T22, armed: LAND requested
    ("tick", (), {"dt": 1000}),
    ("tick", (), {"armed": False}),
    ("tick", (), {"dt": 200}),
]


def play(*runs: Run) -> None:
    for method, args, kwargs in SCRIPT:
        for r in runs:
            getattr(r, method)(*args, **kwargs)


def test_m_script_visits_every_state() -> None:
    """[M1]: the shared script reaches all 13 states (it backs the tests below)."""
    r = Run()
    play(r)
    assert {row[3] for row in r.log} == set(S)


def test_m_every_event_in_exactly_one_packet() -> None:
    """[P3], §4.6: transitions, FC requests, and commands each appear as
    exactly one event, in order, at their input's stamp; nothing is left over."""
    r = Run()
    play(r)
    r.flush()
    assert r.packets[-1].events == ()
    events = r.events()
    assert [e for e in events if e.name.startswith("transition:")] == [
        Event(t=t, name=f"transition:{src.value if src else 'UNPRIMED'}->{dst.value}")
        for t, _, src, dst, _ in r.log
    ]
    request_name = {
        FcRequestKind.ARM_AND_TAKEOFF: "fc_request:ARM_AND_TAKEOFF",
        FcRequestKind.MODE_RTL: "fc_request:RTL",
        FcRequestKind.MODE_LAND: "fc_request:LAND",
    }
    assert [e for e in events if e.name.startswith(("fc_request:", "tone:"))] == [
        Event(t=t, name=request_name.get(q.kind) or f"tone:{q.value}") for t, q in r.requests
    ]
    cmd_events = [e for e in events if e.name.startswith("cmd:")]
    assert [e.name for e in cmd_events] == [
        "cmd:prime:accepted",
        "cmd:approve_engage:rejected_state",
        "cmd:approve_engage:accepted",
        "cmd:abort:rejected_auth",
        "cmd:approve_engage:accepted",
        "cmd:abort:rejected_duplicate_id",
        "cmd:prime:accepted",
        "cmd:abort:accepted",
        "cmd:abort:accepted",
    ]
    sent = [t for i, (t, _, _) in enumerate(r.cmd_log) if i not in (0, 2)]  # unprimed, retry
    assert [e.t for e in cmd_events] == sent
    for p in r.packets:
        assert all(e.t <= p.t and e.t in r.stamps for e in p.events)


# ---------------------------------------------------------------------------
# (11) Effects ([M12])
# ---------------------------------------------------------------------------


def test_m_effects_are_takeoff_on_launch_and_tones_on_tone_states() -> None:
    """[M12], §9 tone table: ARM_AND_TAKEOFF to search_alt on each LAUNCH
    entry; a tone exactly on entering a tone-table state."""
    assert MissionConfig().tone_states == DOC_TONES
    r = Run()
    play(r)
    assert r.requested(FcRequestKind.ARM_AND_TAKEOFF) == [
        (t, alt)
        for (t, tid, *_), alt in zip(
            [row for row in r.log if row[1] == "T03"], (10.0, 12.0), strict=True
        )
    ]
    assert r.requested(FcRequestKind.TONE) == [
        (t, dst.value) for t, _, _, dst, _ in r.log if dst in DOC_TONES
    ]
    kinds = {req.kind for _, req in r.requests}
    assert kinds <= {
        FcRequestKind.ARM_AND_TAKEOFF,
        FcRequestKind.MODE_RTL,
        FcRequestKind.MODE_LAND,
        FcRequestKind.TONE,
    }


# ---------------------------------------------------------------------------
# (12) Determinism ([R3], [M2])
# ---------------------------------------------------------------------------


def test_m_same_inputs_give_same_outputs() -> None:
    """[R3], [M2]: the same input list fed to two missions (interleaved, so
    no state can leak between instances) gives identical acks, packets,
    requests, and transition logs."""
    a, b = Run(), Run()
    play(a, b)
    assert a.acks == b.acks and a.packets == b.packets
    assert a.requests == b.requests and a.log == b.log
    assert len(a.packets) > 10 and len(a.log) > 20
