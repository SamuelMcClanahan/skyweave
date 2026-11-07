"""R series: the companion core and replay (DRONE_CONTRACTS_D0.md §3, [R2]-[R4]).

The fixture ``fixtures/r_touch_trial.jsonl`` is a whole-flight recording of one
scripted touch trial, made by the code path a live companion uses: the real
:class:`CompanionCore` with a real :class:`FcLink`, which writes the
``mavlink`` rx and tx records itself (velocity setpoints included), against a
loopback TCP peer that plays the FC with pymavlink-encoded MAVLink2 frames as
system 1 / component 1. Time is an injected step clock ([C1]); nothing reads
the wall clock. The script: UI polls throughout; prime; the pilot cycles the
mode switch into GUIDED; takeoff telemetry; a confirmed track; a radio approve
after the settle time; the approach; commit; the pass; MISS; RETURN; landing.
It also sends a UI approve that is rejected, an abort with the wrong token,
and one malformed command datagram.

The committed fixture pins replay against an older recording; the fresh
recording made at test time pins the live path against replay. Regenerate the
committed fixture only with a reason:

    SKYWEAVE_REGENERATE_DRONE_FIXTURES=1 uv run pytest -q tests/drone/test_r_replay.py

Expected values are the recording's own output records (or, for [R4], its
logged ``miss:`` event); nothing here re-derives mission or guidance logic.
"""

from __future__ import annotations

import base64
import dataclasses
import hmac
import io
import json
import math
import os
import select
import shutil
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.core import (
    CompanionCore,
    CoreConfig,
    CoreOutput,
    ReplayResult,
    miss_vectors_from_recording,
    recorded_setpoint_keys,
    replay,
    setpoint_key,
)
from skyweave2.drone.fc_link import FcLink
from skyweave2.drone.guidance import GuidanceConfig
from skyweave2.drone.mission import MissionConfig
from skyweave2.drone.packets import (
    AckPacket,
    AckResult,
    CommandName,
    CommandPacket,
    MissionState,
    PrimeParams,
    TrackPacket,
    TrackState,
    TrialType,
    canonical_json,
    strict_json_loads,
)
from skyweave2.drone.recording import Record, Recorder, Stream, read_records
from skyweave2.drone.types import LandedState, VelocityCommand
from skyweave2.drone.vehicle_state import LinkConfig, parse_frames

S = MissionState

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "r_touch_trial.jsonl"
REGENERATE = os.environ.get("SKYWEAVE_REGENERATE_DRONE_FIXTURES") == "1"

# -- the scripted trial's facts ----------------------------------------------

T0 = 1_000  # board ms at the start of the recording
TICK_MS = 50  # contract §9 tick_hz = 20 (Provisional)
BUNDLE_EVERY = 5  # the fake FC sends its [M2a] telemetry every 5th tick (250 ms)
FC_BOOT_OFFSET_MS = 7_000  # FC time_boot_ms is its own clock ([F2]: stored, not mapped)
TRACK_ID = 1_000_001  # [K4]: id_base = start_t_ms * 1000 for a tracker started at 1000 ms
UI_TOKEN = "ui-fixture-token"  # not a credential; must never reach a recording ([P5c])
WRONG_TOKEN = "wrong-ui-token"
RC_LOW, RC_HIGH = 1000, 2000  # approve channel PWM, inside the [F7] valid range
GUIDED, LOITER, RTL = 4, 5, 6  # ArduCopter custom_mode numbers (fixture facts)
CLIMB_MPS, DESCENT_MPS = 5.0, 5.0  # the fake FC's takeoff and RTL-landing rates

CONFIG = CoreConfig(link=LinkConfig(setpoints_enabled=True))  # gate on: the peer proves SITL
PRIME = PrimeParams(trial_type=TrialType.TOUCH)  # contract §9 prime defaults otherwise
MALFORMED = (
    b'{"v":1,"cmd_id":"ui-0002-bad","token":"' + UI_TOKEN.encode() + b'","command":"launch"}'
)

TRIAL_PATH = [
    "transition:UNPRIMED->PRIMED",
    "transition:PRIMED->LAUNCH",
    "transition:LAUNCH->SEARCH",
    "transition:SEARCH->ACQUIRING",
    "transition:ACQUIRING->ENGAGED",
    "transition:ENGAGED->TOUCH",
    "transition:TOUCH->MISS",
    "transition:MISS->RETURN",
    "transition:RETURN->LAND",
]


class _Clock:
    """The injected board clock ([C1], [F11]); the bench moves it by hand."""

    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


class _FakeFc:
    """The FC: a loopback TCP peer speaking real MAVLink2 as system 1 /
    component 1, with just enough vehicle behaviour for one trial. It sends
    the SITL proof once ([F1] (b)), ATTITUDE every tick, the [M2a] telemetry
    every 250 ms, and obeys the arm, takeoff, mode, and setpoint frames
    fc_link writes."""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.endpoint = f"tcp:127.0.0.1:{self.listener.getsockname()[1]}"
        self.conn: socket.socket | None = None
        self._mav = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)
        self._parser = mavlink2.MAVLink(None)
        self._parser.robust_parsing = True
        self._tx_seen = 0
        self._n = 0
        self.mode = LOITER
        self.armed = False
        self.landed = LandedState.ON_GROUND
        self.alt = 0.0
        self.yaw = 0.4
        self.ch8 = RC_LOW
        self._takeoff_alt: float | None = None
        self._rtl_t: int | None = None
        self._setpoint: Any = None  # newest SET_POSITION_TARGET_LOCAL_NED

    def accept(self) -> None:
        self.conn, _ = self.listener.accept()
        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # no Nagle stalls

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.listener.close()

    def advance(self, t: int) -> None:
        """One tick of vehicle motion."""
        dt = TICK_MS / 1000.0
        if self.armed and self._takeoff_alt is not None:
            self.landed = LandedState.TAKEOFF
            self.alt = min(self._takeoff_alt, self.alt + CLIMB_MPS * dt)
            if self.alt >= self._takeoff_alt:
                self.landed = LandedState.IN_AIR
                self._takeoff_alt = None
        elif self.armed and self.mode == RTL and self._rtl_t is not None:
            if t - self._rtl_t >= 1_000:  # the return leg, then the descent
                self.landed = LandedState.LANDING
                self.alt = max(0.0, self.alt - DESCENT_MPS * dt)
                if self.alt == 0.0:
                    self.landed = LandedState.ON_GROUND
                    self.armed = False
        elif self.armed and self.mode == GUIDED and self._setpoint is not None:
            self.yaw = math.remainder(self.yaw + self._setpoint.yaw_rate * dt, 2.0 * math.pi)
            self.alt = max(0.0, self.alt - self._setpoint.vz * dt)

    def telemetry(self, link: FcLink, t: int) -> None:
        mav, boot = self._mav, t + FC_BOOT_OFFSET_MS
        msgs: list[Any] = []
        if self._n == 0:
            msgs.append(mav.simstate_encode(0, 0, 0, 0, 0, -9.8, 0, 0, 0, 370000000, -1220000000))
        msgs.append(mav.attitude_encode(boot, 0.01, -0.03, self.yaw, 0.0, 0.0, 0.0))
        if self._n % BUNDLE_EVERY == 0:
            base = mavlink2.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            if self.armed:
                base |= mavlink2.MAV_MODE_FLAG_SAFETY_ARMED
            chans = [1500] * 18
            chans[7] = self.ch8
            alt_mm = round(self.alt * 1000)
            msgs += [
                mav.heartbeat_encode(
                    mavlink2.MAV_TYPE_QUADROTOR,
                    mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
                    base,
                    self.mode,
                    mavlink2.MAV_STATE_ACTIVE,
                ),
                mav.extended_sys_state_encode(0, int(self.landed)),
                mav.global_position_int_encode(
                    boot, 370000000, -1220000000, 10_000 + alt_mm, alt_mm, 0, 0, 0, 0
                ),
                mav.local_position_ned_encode(boot, 0.0, 0.0, -self.alt, 0.0, 0.0, 0.0),
                mav.sys_status_encode(0, 0, 0, 0, 12000, 100, 87, 0, 0, 0, 0, 0, 0),
                mav.rc_channels_encode(boot, 16, *chans, 255),
            ]
        self._n += 1
        assert self.conn is not None
        self.conn.sendall(b"".join(bytes(m.pack(mav)) for m in msgs))
        want = link.rx_frames + len(msgs)
        for _ in range(400):
            fd = link.fileno()
            assert fd is not None
            select.select([fd], [], [], 0.05)
            link.poll()
            if link.rx_frames >= want:
                return
        raise AssertionError("fc_link did not read the FC's frames")

    def react(self, link: FcLink, t: int) -> None:
        """Read every frame fc_link wrote this tick and obey it."""
        assert self.conn is not None
        want = link.tx_frames - self._tx_seen
        frames: list[Any] = []
        while len(frames) < want:
            if not select.select([self.conn], [], [], 2.0)[0]:
                raise AssertionError("fc_link's writes did not reach the FC")
            got = self._parser.parse_buffer(self.conn.recv(65536)) or []
            frames += [m for m in got if m.get_type() != "BAD_DATA"]
        self._tx_seen += len(frames)
        for m in frames:
            kind = m.get_type()
            if kind == "SET_POSITION_TARGET_LOCAL_NED":
                self._setpoint = m
            elif kind == "COMMAND_LONG":
                if m.command == mavlink2.MAV_CMD_COMPONENT_ARM_DISARM and m.param1 == 1:
                    if self.mode == GUIDED and self.landed is LandedState.ON_GROUND:
                        self.armed = True
                elif m.command == mavlink2.MAV_CMD_NAV_TAKEOFF and self.armed:
                    self._takeoff_alt = float(m.param7)
                elif m.command == mavlink2.MAV_CMD_DO_SET_MODE and self.mode == GUIDED:
                    self.mode = int(m.param2)
                    self._rtl_t = t if self.mode == RTL else None


def _forward(link: FcLink, out: CoreOutput) -> None:
    """What the live process does with a core output: requests, then the setpoint."""
    for req in out.requests:
        link.request(req)
    if out.setpoint is not None:
        link.send_velocity(out.setpoint)


def _ui_command(cmd_id: str, command: CommandName, token: str = UI_TOKEN) -> CommandPacket:
    params = PRIME.to_obj() if command is CommandName.PRIME else None
    return CommandPacket(cmd_id=cmd_id, token=token, command=command, params=params)


def _auth(cmd: CommandPacket) -> bool:
    """The receiver's [P5c] compare; the core only ever sees the result."""
    return hmac.compare_digest(cmd.token.encode("ascii"), UI_TOKEN.encode("ascii"))


class _Script:
    """The humans and the tracker, reacting to what the companion shows."""

    def __init__(self) -> None:
        self.prime_t: int | None = None
        self.search_t: int | None = None
        self.acq_t: int | None = None
        self.eng_t: int | None = None
        self.landed_t: int | None = None
        self.n_trk = 0
        self.k_eng = 0
        self.done = False

    def inputs(self, core: CompanionCore, link: FcLink, t: int) -> None:
        """Inputs that arrive before this tick: UI polls and commands, tracks."""
        state = core.mission.state
        if t % 1_000 == 0:
            _forward(link, core.on_ground_heartbeat(t))  # a UI poll [U6]
        if self.prime_t is None and t >= T0 + 300:
            cmd = _ui_command("ui-0001-prime", CommandName.PRIME)
            _forward(link, core.on_command(cmd, _auth(cmd), t))
            self.prime_t = t
        if self.search_t is None:
            return
        since = t - self.search_t
        if since == 100:
            core.on_malformed_command(MALFORMED, t)  # [P5b]
        elif since == 150:
            cmd = _ui_command("ui-0003-approve", CommandName.APPROVE_ENGAGE)  # rejected_state
            _forward(link, core.on_command(cmd, _auth(cmd), t))
        elif since == 200:
            cmd = _ui_command("ui-0004-abort", CommandName.ABORT, token=WRONG_TOKEN)
            _forward(link, core.on_command(cmd, _auth(cmd), t))  # rejected_auth
        if since >= 300 and state in (S.SEARCH, S.ACQUIRING, S.ENGAGED):
            _forward(link, core.on_track(self._track(t, state), t))

    def _track(self, t: int, state: MissionState | None) -> TrackPacket:
        """One tracker packet: far and steady until ENGAGED, then closing."""
        self.n_trk += 1
        n = self.n_trk
        if state is S.ENGAGED:
            self.k_eng += 1
        k = self.k_eng
        w = 40.0 + 60.0 * k
        return TrackPacket(
            t_cap=t - 20,
            track_id=TRACK_ID,
            state=TrackState.TENTATIVE if n < 3 else TrackState.CONFIRMED,
            u=1010.0 - 2.0 * k,
            v_px=640.0 - 2.5 * k,
            du=-40.0,
            dv=-50.0,
            w=w,
            h=1.2 * w,
            hits=n,
            misses=0,
            age_frames=n,
        )

    def after_tick(self, core: CompanionCore, fc: _FakeFc, t: int) -> None:
        """The pilot, reacting to the state the tick left."""
        state = core.mission.state
        if state is S.PRIMED and self.prime_t is not None and t >= self.prime_t + 500:
            fc.mode = GUIDED  # the pilot moves the switch into GUIDED after priming (E1-D4)
        if state is S.SEARCH and self.search_t is None:
            self.search_t = t
        if state is S.ACQUIRING:
            if self.acq_t is None:
                self.acq_t = t
            elif t >= self.acq_t + 1_100:  # [M8]: past approve_settle_ms
                fc.ch8 = RC_HIGH
        if state is S.ENGAGED and self.eng_t is None:
            self.eng_t = t
        if self.eng_t is not None and t >= self.eng_t + 300:
            fc.ch8 = RC_LOW
        if state is S.LAND and not fc.armed:
            if self.landed_t is None:
                self.landed_t = t
            elif t >= self.landed_t + 500:
                self.done = True


def record_touch_trial(path: Path) -> None:
    """Fly the scripted touch trial through the live path and record it."""
    clock = _Clock(T0)
    fc = _FakeFc()
    rec = Recorder(path)
    link = FcLink(fc.endpoint, clock, CONFIG.link, recorder=rec)
    try:
        core = CompanionCore(CONFIG, vehicle=link.state, recorder=rec, t_start_ms=T0)
        link.connect(timeout_s=2.0)
        fc.accept()
        script = _Script()
        t = T0
        while not script.done:
            if t > T0 + 30_000:
                raise AssertionError(f"the scripted trial stalled in {core.mission.state}")
            clock.t = t
            fc.advance(t)
            fc.telemetry(link, t)
            script.inputs(core, link, t)
            _forward(link, core.on_tick(t))
            link.maybe_health()
            fc.react(link, t)
            script.after_tick(core, fc, t)
            t += TICK_MS
    finally:
        link.close()
        fc.close()
        rec.close()


# -- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def fresh(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The trial recorded now, through the live path."""
    path = tmp_path_factory.mktemp("r_replay") / "touch_trial.jsonl"
    record_touch_trial(path)
    if REGENERATE:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, FIXTURE)
    return path


@pytest.fixture(scope="module")
def committed(fresh: Path) -> list[str]:
    """The committed fixture's lines (rewritten first when regenerating)."""
    if not FIXTURE.exists():
        pytest.fail(f"{FIXTURE} is missing; regenerate with SKYWEAVE_REGENERATE_DRONE_FIXTURES=1")
    return FIXTURE.read_text(encoding="ascii").splitlines()


@pytest.fixture(scope="module")
def committed_replay(committed: list[str]) -> ReplayResult:
    return replay(committed)


def _events(result: ReplayResult) -> list[tuple[int, str]]:
    return [(e.t, e.name) for _, pkt in result.recorded_mission_states for e in pkt.events]


def _commit_key(result: ReplayResult) -> tuple[int, int]:
    names = [n for _, n in _events(result) if n.startswith("commit:")]
    assert len(names) == 1
    _, track_id, t_cap = names[0].split(":")
    return int(track_id), int(t_cap)


def _frames(obj: dict[str, Any]) -> list[Any]:
    return parse_frames(base64.b64decode(obj["raw"]))


def _feed(core: CompanionCore, records: Iterable[Record]) -> None:
    """Drive ``core`` with a recording's input records in file order, the way
    the companion received them ([R3]); output records are skipped."""
    for r in records:
        if r.stream is Stream.MAVLINK and r.direction == "rx":
            assert r.raw is not None
            core.on_mavlink_rx(r.raw, r.t_rx)
        elif r.stream is Stream.TRACK:
            assert isinstance(r.packet, TrackPacket)
            core.on_track(r.packet, r.t_rx)
        elif r.stream is Stream.COMMAND:
            assert isinstance(r.packet, CommandPacket) and r.auth_ok is not None
            core.on_command(r.packet, r.auth_ok, r.t_rx)
        elif r.stream is Stream.TICK:
            core.on_tick(r.t_rx)
        elif r.stream is Stream.GROUND_HB:
            core.on_ground_heartbeat(r.t_rx)


def _recorded(buf: io.StringIO, stream: str) -> list[dict[str, Any]]:
    return [o for o in map(json.loads, buf.getvalue().splitlines()) if o["stream"] == stream]


# -- [R3] replay determinism ---------------------------------------------------


def test_r3_live_recording_replays_exactly(fresh: Path) -> None:
    """[R3], [R2], [F10]: a recording made by the live path (the core plus a real
    fc_link writing the mavlink records) replays in a fresh core to the same
    mission_state records, acks (other than rejected_malformed), and setpoint
    sequence (frame, type_mask, velocity, yaw rate at float32)."""
    result = replay(fresh)
    assert result.mismatch() is None
    assert result.recorded_mission_states and result.recorded_acks and result.recorded_setpoints


def test_r3_committed_fixture_replays_exactly(committed_replay: ReplayResult) -> None:
    """[R3]: the committed recording replays exactly under the current code."""
    assert committed_replay.mismatch() is None


def test_r3_fixture_covers_the_whole_touch_trial(
    committed: list[str], committed_replay: ReplayResult
) -> None:
    """[R3], [F7], [M9], [M8], [P5c]: the replay check spans every phase of the
    trial. The recording's own mission_state records walk the scripted path
    with commit, miss, pass_done and the [M9] death clear in TOUCH; the radio
    approve is acked accepted under an ``rc:approve:`` id and is never a
    command record (replay re-derives it from the mavlink rx record); the UI
    acks are the scripted accepted prime, rejected_state approve, and
    rejected_auth abort."""
    events = _events(committed_replay)
    names = [n for _, n in events]
    assert [n for n in names if n.startswith("transition:")] == TRIAL_PATH
    assert any(n.startswith("commit:") for n in names)
    assert any(n.startswith("miss:") for n in names)
    assert "pass_done" in names
    touch_t = next(t for t, n in events if n == "transition:ENGAGED->TOUCH")
    miss_t = next(t for t, n in events if n == "transition:TOUCH->MISS")
    assert any(touch_t < t < miss_t and n == f"track_dead:{TRACK_ID}" for t, n in events)
    acks = {ack.cmd_id: ack.result for _, ack in committed_replay.recorded_acks}
    rc_ids = [cmd_id for cmd_id in acks if cmd_id.startswith("rc:approve:")]
    assert len(rc_ids) == 1 and acks[rc_ids[0]] is AckResult.ACCEPTED
    assert acks["ui-0001-prime"] is AckResult.ACCEPTED
    assert acks["ui-0003-approve"] is AckResult.REJECTED_STATE
    assert acks["ui-0004-abort"] is AckResult.REJECTED_AUTH
    commands = [json.loads(line) for line in committed if '"stream":"command"' in line]
    assert {c["pkt"]["cmd_id"] for c in commands} == {
        "ui-0001-prime",
        "ui-0003-approve",
        "ui-0004-abort",
    }


def _prime_record(result: ReplayResult) -> Callable[[dict[str, Any]], bool]:
    return lambda o: o["stream"] == "command" and o["pkt"]["command"] == "prime"


def _commit_track_record(result: ReplayResult) -> Callable[[dict[str, Any]], bool]:
    key = _commit_key(result)
    return lambda o: o["stream"] == "track" and (o["pkt"]["track_id"], o["pkt"]["t_cap"]) == key


def _approve_rc_record(result: ReplayResult) -> Callable[[dict[str, Any]], bool]:
    """The RC_CHANNELS rx record whose receive time the ``rc:approve:<t>`` id names."""
    (cmd_id,) = [a.cmd_id for _, a in result.recorded_acks if a.cmd_id.startswith("rc:")]
    t_sample = int(cmd_id.split(":")[2])

    def is_approve(o: dict[str, Any]) -> bool:
        if o["stream"] != "mavlink" or o["dir"] != "rx" or o["t_rx"] != t_sample:
            return False
        return any(f.get_type() == "RC_CHANNELS" and f.chan8_raw >= RC_HIGH for f in _frames(o))

    return is_approve


def _launch_tick_record(result: ReplayResult) -> Callable[[dict[str, Any]], bool]:
    t_launch = next(t for t, n in _events(result) if n == "transition:PRIMED->LAUNCH")
    return lambda o: o["stream"] == "tick" and o["t_rx"] == t_launch


@pytest.mark.parametrize(
    "select_record",
    [_prime_record, _commit_track_record, _approve_rc_record, _launch_tick_record],
    ids=["command:prime", "track:commit", "mavlink_rx:rc_approve", "tick:launch"],
)
def test_r3_removing_one_input_record_changes_the_output(
    committed: list[str],
    committed_replay: ReplayResult,
    select_record: Callable[[ReplayResult], Callable[[dict[str, Any]], bool]],
) -> None:
    """[R3] discrimination: replay is driven by the input records. Dropping one
    load-bearing input record of each kind (the prime command, the committing
    track packet, the RC frame carrying the radio approve, the tick that
    launched) makes the replay differ from the recorded outputs. A single UI
    poll is not one of the cases: polls are 1 s apart against the 5 s ground
    link timeout, so dropping one rightly changes nothing."""
    pred = select_record(committed_replay)
    hits = [i for i, line in enumerate(committed) if pred(json.loads(line))]
    assert len(hits) == 1
    lines = committed[: hits[0]] + committed[hits[0] + 1 :]
    assert replay(lines).mismatch() is not None


def test_r3_p5b_malformed_ack_is_not_replayed(
    committed: list[str], committed_replay: ReplayResult
) -> None:
    """[R3], [P5b]: the recording holds the receiver's rejected_malformed ack
    (written with no input record); replay skips it, produces no ack for that
    id, and still matches."""
    malformed = [
        json.loads(line)["pkt"]["cmd_id"]
        for line in committed
        if '"stream":"ack"' in line and '"result":"rejected_malformed"' in line
    ]
    assert malformed == ["ui-0002-bad"]
    assert committed_replay.skipped_malformed == 1
    assert all(ack.cmd_id != "ui-0002-bad" for _, ack in committed_replay.acks)
    assert all(ack.cmd_id != "ui-0002-bad" for _, ack in committed_replay.recorded_acks)
    assert committed_replay.mismatch() is None


def test_r3_f5_locked_gate_recording_replays_without_setpoints(
    committed: list[str], committed_replay: ReplayResult
) -> None:
    """[R3], [F5]: the gate setting in meta.config decides whether setpoints
    reach the mavlink log. The same flight recorded with the gate locked (no
    setpoint tx records) replays exactly with no setpoints and the same
    mission states."""
    meta = json.loads(committed[0])
    meta["config"]["link"]["setpoints_enabled"] = False
    meta["config"]["gate"] = "locked"
    lines = [canonical_json(meta).decode("ascii")]
    for line in committed[1:]:
        o = json.loads(line)
        if o["stream"] == "mavlink" and o["dir"] == "tx":
            if any(f.get_type() == "SET_POSITION_TARGET_LOCAL_NED" for f in _frames(o)):
                continue
        lines.append(line)
    result = replay(lines)
    assert result.mismatch() is None
    assert result.setpoints == () and committed_replay.setpoints != ()
    assert result.mission_states == committed_replay.mission_states


def test_r3_f6_replay_setpoint_key_is_what_fc_link_writes() -> None:
    """[R3], [F6], [F10]: replay's key for a setpoint is the one in the tx record
    fc_link itself writes, hard limits included. The command exceeds
    v_xy_hard, v_z_hard and yaw_rate_hard, so a replay that skipped the [F6]
    limits would key it differently (the trial never reaches the limits)."""
    buf = io.StringIO()
    fc = _FakeFc()
    link = FcLink(fc.endpoint, _Clock(T0), CONFIG.link, recorder=Recorder(buf))
    cmd = VelocityCommand(vn=6.0, ve=-8.0, vd=-3.0, yaw_rate=-4.0)
    try:
        link.connect(timeout_s=2.0)
        fc.accept()
        fc.telemetry(link, T0)  # carries the SITL proof
        assert link.send_velocity(cmd)
    finally:
        link.close()
        fc.close()
    keys = [
        key
        for r in read_records(buf.getvalue().splitlines())
        if r.stream is Stream.MAVLINK and r.direction == "tx" and r.raw is not None
        for key in recorded_setpoint_keys(r.raw)
    ]
    unlimited = LinkConfig(v_xy_hard=1e3, v_z_hard=1e3, yaw_rate_hard=1e3)
    assert keys == [setpoint_key(cmd, CONFIG.link)]
    assert keys[0] != setpoint_key(cmd, unlimited)


# -- CC-1: a radio approve is a CMD input at its sample's stamp ([F7], [M8]) -----


def _rc_channels(t: int, ch8: int) -> bytes:
    """One real MAVLink2 RC_CHANNELS frame from the FC (system 1 / component 1)
    with the approve channel ([F7], RC 8) at ``ch8``."""
    mav = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)
    chans = [1500] * 18
    chans[7] = ch8
    return bytes(mav.rc_channels_encode(t + FC_BOOT_OFFSET_MS, 16, *chans, 255).pack(mav))


@pytest.mark.parametrize(
    ("offset_ms", "result", "state"),
    [(-5, AckResult.REJECTED_STATE, S.ACQUIRING), (0, AckResult.ACCEPTED, S.ENGAGED)],
    ids=["settle-5ms:rejected_state", "settle:accepted"],
)
def test_cc1_f7_m8_radio_approve_judged_at_its_sample_stamp(
    committed: list[str],
    committed_replay: ReplayResult,
    offset_ms: int,
    result: AckResult,
    state: MissionState,
) -> None:
    """CC-1, [F7], [M8], [M2], [R2], [R3]: a radio approve is its own CMD input
    stamped with its RC_CHANNELS sample's t_rx, so [M8] settles at the sample,
    not at the next tick. The committed flight is fed to a fresh core up to
    ACQUIRING; a low then a high RC sample arrive, the high one at
    ``approve_settle_ms`` + offset after T05, and the next input is a tick past
    the settle boundary. 5 ms short of it: ``rc:approve:<t>`` is acked
    rejected_state at the sample stamp and the state stays ACQUIRING; exactly
    at it: accepted, with T07 at the sample stamp. Replay of what the core
    recorded reproduces its mission states and acks (the approve drains at the
    same input in both)."""
    t05 = next(t for t, n in _events(committed_replay) if n == "transition:SEARCH->ACQUIRING")
    settle = committed_replay.config.mission.approve_settle_ms
    t_low, t_high, t_tick = t05 + settle - 20, t05 + settle + offset_ms, t05 + settle + 45
    buf = io.StringIO()
    core = CompanionCore(committed_replay.config, recorder=Recorder(buf), t_start_ms=T0)
    _feed(core, (r for r in read_records(committed) if r.t_rx < t_low))
    assert core.mission.state is S.ACQUIRING and core.mission.candidate_id == TRACK_ID
    core.on_mavlink_rx(_rc_channels(t_low, RC_LOW), t_low)
    core.on_mavlink_rx(_rc_channels(t_high, RC_HIGH), t_high)
    out = core.on_tick(t_tick)
    ack = AckPacket(cmd_id=f"rc:approve:{t_high}", result=result)
    assert out.rc_acks == ((t_high, ack),) and out.acks == ()
    rc_records = [o for o in _recorded(buf, "ack") if o["pkt"]["cmd_id"].startswith("rc:")]
    assert [(o["t_rx"], o["pkt"]["result"]) for o in rc_records] == [(t_high, result.value)]
    assert core.mission.state is state
    t07 = [(t, src, dst) for t, tid, src, dst, _ in core.mission.transition_log if tid == "T07"]
    if result is AckResult.ACCEPTED:
        assert t07 == [(t_high, S.ACQUIRING, S.ENGAGED)]
        names = [(e.t, e.name) for pkt in out.mission_states for e in pkt.events]
        assert (t_high, "transition:ACQUIRING->ENGAGED") in names
    else:
        assert t07 == []
    replayed = replay(buf.getvalue().splitlines())
    assert replayed.acks == replayed.recorded_acks
    assert replayed.mission_states == replayed.recorded_mission_states


# -- [P5c] / [R2] the token -----------------------------------------------------


def _keys(obj: Any) -> list[str]:
    """Every object key at any depth."""
    out: list[str] = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            out.append(key)
            out += _keys(val)
    elif isinstance(obj, list):
        for val in obj:
            out += _keys(val)
    return out


def test_p5c_r2_token_never_recorded(fresh: Path, committed: list[str]) -> None:
    """[P5c], [R2]: neither the UI token nor a wrong one appears anywhere in a
    recording (the malformed datagram carried the token too); no record has a
    token field, and meta.config names none."""
    for text in (fresh.read_text(encoding="ascii"), "\n".join(committed)):
        assert UI_TOKEN not in text and WRONG_TOKEN not in text
        objs = [json.loads(line) for line in text.splitlines()]
        assert all("token" not in k.lower() for o in objs for k in _keys(o))


# -- [R4] miss vector offline ----------------------------------------------------


def test_r4_offline_miss_vector_equals_logged(
    committed: list[str], committed_replay: ReplayResult
) -> None:
    """[R4], [G6]: the miss vector recomputed from the recording alone (the
    committing track record, the accepted prime's target_width_m, the camera in
    meta.config) equals the logged ``miss:`` event within its %.3f rounding."""
    misses = miss_vectors_from_recording(committed)
    assert len(misses) == 1
    m = misses[0]
    assert (m.track_id, m.t_cap) == _commit_key(committed_replay)
    assert m.target_width_m == PRIME.target_width_m
    assert m.logged_m is not None
    got = (*m.vector.miss_m, m.vector.z_m)
    assert all(abs(a - b) <= 0.0005 + 1e-9 for a, b in zip(got, m.logged_m, strict=True))
    assert any(abs(v) > 0.001 for v in m.logged_m[:2])  # a real offset, not 0 == 0


def test_cc2_r4_p5a_retry_of_an_older_prime_does_not_replace_the_trial(
    committed: list[str], committed_replay: ReplayResult
) -> None:
    """CC-2, [R4], [P5a], T02: a true retry of an older prime is acked
    ``accepted`` again but executes nothing, so it is not the trial in force.
    The committed flight is fed to a fresh core with a recorder; right after
    prime A it gets prime B with a different ``target_width_m`` (accepted, T02),
    then A re-sent with the same id and body (the page's retry path); the
    flight then runs on to its commit. The offline miss vector uses B's width
    and equals the logged ``miss:`` event within its %.3f rounding."""
    records = list(read_records(committed))
    i_a = next(i for i, r in enumerate(records) if r.stream is Stream.COMMAND)
    prime_a = records[i_a]
    assert isinstance(prime_a.packet, CommandPacket)
    assert prime_a.packet.command is CommandName.PRIME and prime_a.auth_ok is True
    width_b = 2.0 * PRIME.target_width_m
    prime_b = CommandPacket(
        cmd_id="ui-0005-prime-b",
        token=UI_TOKEN,
        command=CommandName.PRIME,
        params=dataclasses.replace(PRIME, target_width_m=width_b).to_obj(),
    )
    buf = io.StringIO()
    core = CompanionCore(committed_replay.config, recorder=Recorder(buf), t_start_ms=T0)
    _feed(core, records[: i_a + 1])
    t = prime_a.t_rx
    out_b = core.on_command(prime_b, True, t)
    out_retry = core.on_command(prime_a.packet, True, t)  # same id, same body
    _feed(core, records[i_a + 1 :])
    assert [a.result for a in (*out_b.acks, *out_retry.acks)] == [AckResult.ACCEPTED] * 2
    assert out_retry.acks[0].cmd_id == prime_a.packet.cmd_id
    tids = [tid for _, tid, _, _, _ in core.mission.transition_log]
    assert tids[:2] == ["T01", "T02"] and tids.count("T02") == 1  # the retry executed nothing
    misses = miss_vectors_from_recording(buf.getvalue().splitlines())
    assert len(misses) == 1
    m = misses[0]
    assert m.target_width_m == width_b
    assert m.logged_m is not None
    got = (*m.vector.miss_m, m.vector.z_m)
    assert all(abs(a - b) <= 0.0005 + 1e-9 for a, b in zip(got, m.logged_m, strict=True))
    (original,) = miss_vectors_from_recording(committed)  # flown on A's width
    assert original.logged_m is not None and abs(m.logged_m[2] - original.logged_m[2]) > 0.01


# -- [R2] meta.config -------------------------------------------------------------


def test_r2_core_config_round_trips_and_refuses_strays() -> None:
    """[R2], [P5c]: meta.config carries mission, guidance, camera, link, law,
    coast_cap, track_timeout_ms and the gate setting and reads back equal; an
    unknown key (a token among them) or a top-level copy that disagrees with
    the nested value is refused."""
    config = CoreConfig(
        mission=MissionConfig(coast_cap=7, track_timeout_ms=400),
        guidance=GuidanceConfig(Kp=0.6),
        link=LinkConfig(setpoints_enabled=True),
    )
    obj = config.to_obj()
    assert (obj["coast_cap"], obj["track_timeout_ms"], obj["gate"]) == (7, 400, "enabled")
    assert CoreConfig.from_obj(strict_json_loads(canonical_json(obj).decode("ascii"))) == config
    for stray in ({"token": "x"}, {"coast_cap": 20}, {"gate": "locked"}):
        with pytest.raises(ValueError):
            CoreConfig.from_obj({**obj, **stray})


def test_m2_core_input_stamps_must_not_decrease() -> None:
    """[M2], [R3]: the core's now is the input stamp and the file order is the
    input order, so an input stamped before the previous one is refused."""
    core = CompanionCore(CoreConfig())
    core.on_tick(100)
    core.on_ground_heartbeat(100)  # equal stamps are fine
    with pytest.raises(ValueError):
        core.on_tick(99)
