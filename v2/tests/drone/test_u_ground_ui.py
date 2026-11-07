"""U series: the ground UI and the companion process (DRONE_CONTRACTS_D0.md §7, [P5], [C10]).

Headless and fast. A real :class:`GroundUiServer` on an ephemeral loopback
port, real HTTP requests (``http.client``), the real :class:`CompanionCore`
with a real :class:`Recorder`. The vehicle is real MAVLink2 frames
(pymavlink-encoded, system 1 / component 1) fed through the core's own
``on_mavlink_rx``, so the mission's prime precondition (disarmed, on the
ground) comes from the same parser a live process uses. Nothing we own is
mocked; the time is an injected step clock ([C1]).

Expected values come from the system's own outputs (acks, recording records,
the mission's transition log, ``PrimeParams`` defaults) or, for [U5], from the
C agent's committed recording fixture ``fixtures/r_touch_trial.jsonl``.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import select
import socket
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.companiond import Companion, CompanionConfig
from skyweave2.drone.core import CompanionCore, CoreConfig, CoreOutput
from skyweave2.drone.fc_link import EndpointRefused
from skyweave2.drone.ground_ui import (
    CommandReceiver,
    GroundUiServer,
    GroundUiService,
    RecordTap,
    ViewBuilder,
    render_page,
    render_status,
    view_from_recording,
)
from skyweave2.drone.packets import (
    AckPacket,
    AckResult,
    CommandName,
    FcLinkHealthPacket,
    MissionState,
    MissionStatePacket,
    PacketKind,
    PrimeParams,
    TrackPacket,
    TrackState,
    canonical_json,
    decode,
    encode,
)
from skyweave2.drone.recording import Record, Recorder, Stream, read_records
from skyweave2.drone.types import LandedState
from skyweave2.drone.vehicle_state import parse_frames

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "r_touch_trial.jsonl"

T0 = 10_000  # board ms
UI_TOKEN = "ui-test-token"  # not a credential; must never reach a recording, page, or log
WRONG_TOKEN = "ui-wrong-token"
LOITER = 5  # ArduCopter custom_mode (fixture fact)
BATTERY_PCT = 87


class _Clock:
    """The injected board clock ([C1]); the test moves it by hand."""

    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def _fc_frames(boot_ms: int) -> bytes:
    """The FC on the ground: disarmed, LOITER, ON_GROUND, battery 87 %."""
    mav = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)
    msgs = [
        mav.heartbeat_encode(
            mavlink2.MAV_TYPE_QUADROTOR,
            mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
            mavlink2.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            LOITER,
            mavlink2.MAV_STATE_STANDBY,
        ),
        mav.extended_sys_state_encode(0, int(LandedState.ON_GROUND)),
        mav.sys_status_encode(0, 0, 0, 0, 12000, 100, BATTERY_PCT, 0, 0, 0, 0, 0, 0),
        mav.attitude_encode(boot_ms, 0.0, 0.0, 0.3, 0.0, 0.0, 0.0),
    ]
    return b"".join(bytes(m.pack(mav)) for m in msgs)


def _body(
    cmd_id: str,
    command: str,
    *,
    token: str = UI_TOKEN,
    params: dict[str, Any] | None = None,
    v: int = 1,
) -> bytes:
    """A command packet as the page sends it ([P5])."""
    obj: dict[str, Any] = {"v": v, "cmd_id": cmd_id, "token": token, "command": command}
    if params is not None:
        obj["params"] = params
    return canonical_json(obj)


PRIME_PARAMS = PrimeParams().to_obj()


class _Bench:
    """The UI as the companion serves it, minus the FC link: core, recorder
    (through the view tap), service, and a started HTTP server."""

    def __init__(self) -> None:
        self.clock = _Clock(T0)
        self.sink = io.StringIO()
        self.builder = ViewBuilder()
        self.core = CompanionCore(
            CoreConfig(), recorder=Recorder(RecordTap(self.builder, self.sink)), t_start_ms=T0
        )
        self.outputs: list[CoreOutput] = []
        self.service = GroundUiService(
            self.core, self.builder, self.clock, UI_TOKEN, forward=self.outputs.append
        )
        self.server = GroundUiServer(self.service)
        self.server.start()

    def close(self) -> None:
        self.server.close()

    def fc_tick(self, t: int) -> None:
        """What the companion loop does at ``t``: FC frames in, then a tick."""
        self.clock.t = t
        with self.service.lock:
            self.core.on_mavlink_rx(_fc_frames(t), t)
            self.outputs.append(self.core.on_tick(t))

    def http(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        host, port = self.server.address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def post(self, t: int, body: bytes) -> tuple[int, bytes]:
        self.clock.t = t
        return self.http("POST", "/command", body)

    def ack(self, t: int, body: bytes) -> AckPacket:
        status, data = self.post(t, body)
        assert status == 200
        ack = decode(PacketKind.ACK, data)
        assert isinstance(ack, AckPacket)
        return ack

    def poll(self, t: int) -> dict[str, Any]:
        self.clock.t = t
        status, data = self.http("GET", "/state")
        assert status == 200
        return json.loads(data)

    def page(self, t: int) -> str:
        self.clock.t = t
        status, data = self.http("GET", "/")
        assert status == 200
        return data.decode("utf-8")

    def lines(self) -> list[str]:
        return self.sink.getvalue().splitlines()

    def records(self, stream: Stream) -> list[Record]:
        return [r for r in read_records(self.lines()) if r.stream is stream]

    def tids(self) -> list[str]:
        return [rec[1] for rec in self.core.mission.transition_log]


@pytest.fixture
def bench() -> Any:
    b = _Bench()
    try:
        yield b
    finally:
        b.close()


class _Doc(HTMLParser):
    """The page or status block, parsed: elements by id, buttons, the prime
    form's inputs, forms, and script text."""

    _VOID = frozenset({"input", "meta", "br", "img", "link", "hr"})

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.attrs_by_id: dict[str, dict[str, Any]] = {}
        self.text_by_id: dict[str, str] = {}
        self.buttons: list[dict[str, Any]] = []
        self.params: dict[str, dict[str, Any]] = {}
        self.forms = 0
        self.script = ""
        self._stack: list[tuple[str, str | None]] = []
        self._select: dict[str, Any] | None = None
        self.feed(page)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self.forms += 1
        if tag == "button":
            self.buttons.append(a)
        if "data-param" in a:
            self.params[str(a["data-param"])] = a
            if tag == "select":
                self._select = a
        if tag == "option" and self._select is not None and "selected" in a:
            self._select["selected"] = a["value"]
        eid = a.get("id")
        if eid is not None:
            self.attrs_by_id[eid] = a
            self.text_by_id[eid] = ""
        if tag not in self._VOID:
            self._stack.append((tag, eid))

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None
        while self._stack:
            if self._stack.pop()[0] == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1][0] == "script":
            self.script += data
        for _, eid in self._stack:
            if eid is not None:
                self.text_by_id[eid] += data

    def value(self, eid: str) -> Any:
        return json.loads(self.attrs_by_id[eid]["data-value"])

    def text(self, eid: str) -> str:
        return self.text_by_id[eid].strip()


# -- [P5a] idempotency, [P5b] rejections, [P5c] auth -----------------------------


def test_u3_p5a_same_id_and_body_executes_once_other_body_refused(bench: _Bench) -> None:
    """[U3], [P5a], [U1]: the page's ack-by-id retry is safe. POST /command
    twice with the same cmd_id and body: one execution (one T01 in the
    mission's transition log) and a byte-identical ack. The same cmd_id with
    a different body (an abort, which would fire T19) executes nothing and is
    acked rejected_duplicate_id. Each HTTP ack is the one the core recorded."""
    bench.fc_tick(T0)
    prime = _body("ui-retry-1", "prime", params=PRIME_PARAMS)
    first = bench.post(T0 + 100, prime)
    again = bench.post(T0 + 200, prime)
    assert first[0] == again[0] == 200
    assert first[1] == again[1]
    assert decode(PacketKind.ACK, first[1]) == AckPacket(
        cmd_id="ui-retry-1", result=AckResult.ACCEPTED
    )
    assert bench.tids() == ["T01"]

    reused = bench.ack(T0 + 300, _body("ui-retry-1", "abort"))
    assert reused.result is AckResult.REJECTED_DUPLICATE_ID
    assert bench.tids() == ["T01"]
    assert bench.core.mission.state is MissionState.PRIMED
    recorded = [r.packet for r in bench.records(Stream.ACK)]
    assert recorded == [decode(PacketKind.ACK, first[1]), decode(PacketKind.ACK, again[1]), reused]


def test_u4_p5b_approve_before_acquiring_rejected_state_and_logged(bench: _Bench) -> None:
    """[P5b], [M5], [U4]: approve_engage in PRIMED (before ACQUIRING) is acked
    rejected_state, changes nothing, and is logged as a cmd: event: in the
    recorded mission_state stream and on the page's recent events."""
    bench.fc_tick(T0)
    assert bench.ack(T0 + 100, _body("ui-p1", "prime", params=PRIME_PARAMS)).result is (
        AckResult.ACCEPTED
    )
    early = bench.ack(T0 + 200, _body("ui-a1", "approve_engage"))
    assert early == AckPacket(cmd_id="ui-a1", result=AckResult.REJECTED_STATE)
    bench.fc_tick(T0 + 400)  # the periodic mission_state publish carries the event
    state = bench.poll(T0 + 450)

    name = "cmd:approve_engage:rejected_state"
    recorded = [
        (e.t, e.name)
        for r in bench.records(Stream.MISSION_STATE)
        if isinstance(r.packet, MissionStatePacket)
        for e in r.packet.events
    ]
    assert (T0 + 200, name) in recorded
    assert {"t": T0 + 200, "name": name} in state["view"]["events"]
    assert name in _Doc(state["html"]).text("events")
    assert state["view"]["mission_state"] == "PRIMED"
    assert bench.tids() == ["T01"]


def test_u3_p5c_wrong_token_rejected_auth_and_not_stored(bench: _Bench) -> None:
    """[P5c], [P5a], [U3]: a wrong token is acked rejected_auth, executes
    nothing, and is not stored: the same cmd_id with the right token then
    executes (accepted, not rejected_duplicate_id). The command records keep
    only the authentication result."""
    bench.fc_tick(T0)
    wrong = bench.ack(T0 + 100, _body("ui-tok-1", "prime", token=WRONG_TOKEN, params=PRIME_PARAMS))
    assert wrong.result is AckResult.REJECTED_AUTH
    assert bench.tids() == []
    right = bench.ack(T0 + 200, _body("ui-tok-1", "prime", params=PRIME_PARAMS))
    assert right.result is AckResult.ACCEPTED
    assert bench.tids() == ["T01"]
    assert [r.auth_ok for r in bench.records(Stream.COMMAND)] == [False, True]
    assert bench.service.receiver.auth_failed == 1


def test_u2_unknown_command_name_refused(bench: _Bench) -> None:
    """[U2], [C7], [P5b]: a command name outside the four (here a manual
    velocity) fails decoding: acked rejected_malformed under its salvaged
    cmd_id, recorded as an ack only, never reaching the mission and never
    stored (the same cmd_id then primes)."""
    bench.fc_tick(T0)
    refused = bench.ack(T0 + 100, _body("ui-vel-1", "set_velocity"))
    assert refused == AckPacket(cmd_id="ui-vel-1", result=AckResult.REJECTED_MALFORMED)
    assert bench.records(Stream.COMMAND) == []
    assert [r.packet for r in bench.records(Stream.ACK)] == [refused]
    assert bench.tids() == []
    assert bench.ack(T0 + 200, _body("ui-vel-1", "prime", params=PRIME_PARAMS)).result is (
        AckResult.ACCEPTED
    )


@pytest.mark.parametrize(
    ("body", "cmd_id"),
    [
        (_body("ui-v2-1", "abort", v=2), "ui-v2-1"),
        (b"\xff not a command", None),
    ],
    ids=["salvageable", "unreadable"],
)
def test_p5b_malformed_body_acked_when_cmd_id_readable(
    bench: _Bench, body: bytes, cmd_id: str | None
) -> None:
    """[P5b]: a body that fails decoding is acked rejected_malformed when its
    cmd_id can be salvaged, and that ack is recorded; otherwise there is no ack
    (HTTP 400). Either way it is counted, never a command record, and not a
    ground heartbeat."""
    status, data = bench.post(T0 + 100, body)
    acks = [r.packet for r in bench.records(Stream.ACK)]
    if cmd_id is None:
        assert status == 400
        assert acks == []
    else:
        assert status == 200
        ack = AckPacket(cmd_id=cmd_id, result=AckResult.REJECTED_MALFORMED)
        assert decode(PacketKind.ACK, data) == ack
        assert acks == [ack]
    assert bench.service.receiver.malformed == 1
    assert bench.records(Stream.COMMAND) == []
    assert bench.records(Stream.GROUND_HB) == []


def test_p5_ground_command_with_reserved_rc_prefix_refused(bench: _Bench) -> None:
    """[P5], [F7], [P5b]: the rc: cmd_id prefix is reserved for radio
    approvals, so a ground command using it is refused as malformed and never
    stored; a later radio approve sampled at that time cannot be swallowed as
    a [P5a] retry."""
    bench.fc_tick(T0)
    ack = bench.ack(T0 + 100, _body(f"rc:approve:{T0 + 50}", "approve_engage"))
    assert ack.result is AckResult.REJECTED_MALFORMED
    assert bench.records(Stream.COMMAND) == []


def test_p5c_token_never_recorded_rendered_or_logged(
    bench: _Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """[P5c], [U3], [R2]: after accepted, wrong-token, and malformed commands
    that carried the token, the token is absent from the recording (meta
    included), the page source, the /state responses, the HTTP acks, and the
    process log; the malformed one is still logged ([P5b])."""
    caplog.set_level(logging.DEBUG, logger="skyweave2")
    bench.fc_tick(T0)
    answers = [
        bench.post(T0 + 100, _body("ui-s1", "prime", params=PRIME_PARAMS))[1],
        bench.post(T0 + 150, _body("ui-s2", "abort", token=WRONG_TOKEN))[1],
        bench.post(T0 + 200, _body("ui-s3", "abort", v=2))[1],
        bench.post(T0 + 250, _body("ui-s4", "approve_engage"))[1],
    ]
    texts = [
        bench.sink.getvalue(),
        bench.page(T0 + 300),
        json.dumps(bench.poll(T0 + 350)),
        caplog.text,
        *(a.decode("ascii") for a in answers),
    ]
    assert "malformed command" in caplog.text
    assert all(UI_TOKEN not in text for text in texts)
    assert all(WRONG_TOKEN not in text for text in texts)


def test_p5c_configured_token_must_match_the_field_pattern() -> None:
    """[P5c]: the configured token satisfies the token field's pattern; a
    token outside it is refused at construction, without echoing it."""
    core = CompanionCore(CoreConfig())
    bad = "has space"
    with pytest.raises(ValueError) as err:
        CommandReceiver(core, bad, lambda out: None)
    assert bad not in str(err.value)
    CommandReceiver(core, UI_TOKEN, lambda out: None)


# -- [U2] the page's controls ----------------------------------------------------


def test_u2_page_has_four_command_controls_and_two_endpoints(bench: _Bench) -> None:
    """[U2], [U1], [U3]: the page has exactly four command buttons, one per
    contract command; its script talks to /state and /command only; there is
    no form (nothing can submit the token in a URL) and no browser storage.
    The server answers no other endpoint."""
    page = bench.page(T0)
    doc = _Doc(page)
    assert len(doc.buttons) == 4
    assert sorted(b["data-command"] for b in doc.buttons) == sorted(c.value for c in CommandName)
    fetches = [
        line.split("fetch(", 1)[1].split(",", 1)[0].strip()
        for line in doc.script.splitlines()
        if "fetch(" in line
    ]
    assert sorted(fetches) == ['"/command"', '"/state"']
    assert doc.forms == 0
    for api in ("localStorage", "sessionStorage", "document.cookie", "WebSocket", "XMLHttpRequest"):
        assert api not in page
    assert doc.attrs_by_id["ui-token"]["type"] == "password"
    assert bench.http("GET", "/joystick")[0] == 404
    assert bench.http("POST", "/velocity", b"{}")[0] == 404
    assert bench.http("PUT", "/command", b"{}")[0] == 501


def test_u2_prime_form_defaults_are_prime_params(bench: _Bench) -> None:
    """[U2], [P5], contract §9: the prime form has one input per prime param,
    each defaulting to PrimeParams()."""
    doc = _Doc(bench.page(T0))
    assert set(doc.params) == set(PRIME_PARAMS)
    for name, want in PRIME_PARAMS.items():
        el = doc.params[name]
        kind = el["data-kind"]
        if kind == "bool":
            assert ("checked" in el) is want, name
        elif kind == "enum":
            assert el["selected"] == want, name
        else:
            assert json.loads(el["value"]) == want, name


# -- [U5] render from a recorded stream, [U6] polls are heartbeats ----------------


def _fixture_at(lines: list[str], until: int) -> dict[str, Any]:
    """The fixture's own newest records at or before ``until``."""
    out: dict[str, Any] = {"candidates": []}
    for r in read_records(lines):
        if r.t_rx > until:
            break
        if isinstance(r.packet, MissionStatePacket):
            out["state"] = r.packet
            out["candidates"] += [
                e.name for e in r.packet.events if e.name.startswith("candidate:")
            ]
        elif isinstance(r.packet, FcLinkHealthPacket):
            out["health"] = r.packet
        elif r.stream is Stream.MAVLINK and r.direction == "rx" and r.raw is not None:
            for m in parse_frames(r.raw):
                if m.get_type() == "SYS_STATUS":
                    out["battery"] = m.battery_remaining
    return out


@pytest.mark.parametrize("entered", ["SEARCH->ACQUIRING", "ACQUIRING->ENGAGED"])
def test_u5_page_renders_from_the_recorded_fixture(entered: str) -> None:
    """[U5], [U4]: the page renders from a recorded packet stream (the
    committed touch-trial recording): banner, engaged track, the candidate
    awaiting approval (in ACQUIRING), battery, and link health equal the
    recording's own newest records at that time."""
    lines = FIXTURE.read_text(encoding="ascii").splitlines()
    enter_t = next(
        e.t
        for r in read_records(lines)
        if isinstance(r.packet, MissionStatePacket)
        for e in r.packet.events
        if e.name == f"transition:{entered}"
    )
    until = enter_t + 100
    want = _fixture_at(lines, until)
    ms: MissionStatePacket = want["state"]
    health: FcLinkHealthPacket = want["health"]
    doc = _Doc(render_page(view_from_recording(lines, until_t=until)))

    assert doc.text("banner") == ms.mission_state.value
    assert doc.value("engaged") == ms.engaged_track_id
    if ms.mission_state is MissionState.ACQUIRING:
        candidate = int(want["candidates"][-1].split(":")[1])
        assert doc.value("candidate") == candidate
        assert str(candidate) in doc.text("candidate")
    else:
        assert ms.engaged_track_id is not None
        assert str(ms.engaged_track_id) in doc.text("engaged")
        assert doc.value("candidate") is None
    assert doc.value("battery") == want["battery"]
    assert f"{want['battery']} %" in doc.text("battery")
    assert doc.value("fc-link") is health.fc_link_up
    assert doc.value("attitude-age") == health.attitude_age_ms
    assert doc.value("rc") is health.rc_seen
    assert doc.value("gate") == health.gate_state.value
    assert doc.value("last-setpoint") == health.last_setpoint_t


def test_u5_live_status_is_the_render_of_its_own_recording(bench: _Bench) -> None:
    """[U5]: one render function. After a prime, a rejected approve, ticks and
    polls, the status block and view GET /state returns are exactly what the
    recording the live core wrote renders to."""
    bench.fc_tick(T0)
    bench.post(T0 + 100, _body("ui-l1", "prime", params=PRIME_PARAMS))
    bench.post(T0 + 200, _body("ui-l2", "approve_engage"))
    bench.fc_tick(T0 + 400)
    live = bench.poll(T0 + 450)
    replayed = view_from_recording(bench.lines())
    assert live["html"] == render_status(replayed)
    assert live["view"] == json.loads(canonical_json(replayed.to_obj()))
    assert live["view"]["battery_pct"] == BATTERY_PCT
    assert live["view"]["ground_link"] == {"last_hb_t": T0 + 450, "up": True}


def test_u6_each_state_poll_is_one_ground_hb_record(bench: _Bench) -> None:
    """[U6], [R2], [U1]: every GET /state is one ground heartbeat, recorded
    as a ground_hb record at the poll's stamp, including polls that arrive in
    parallel (the request threads are serialized into the core by its lock)."""
    for k in range(3):
        bench.poll(T0 + 100 * k)
    bench.clock.t = T0 + 500
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = [s for s, _ in pool.map(lambda _: bench.http("GET", "/state"), range(8))]
    assert statuses == [200] * 8
    hbs = [r.t_rx for r in bench.records(Stream.GROUND_HB)]
    assert hbs == [T0, T0 + 100, T0 + 200] + [T0 + 500] * 8
    assert bench.service.polls == 11


# -- [C10] the companion process ----------------------------------------------------


def test_c10_companion_refuses_a_non_loopback_fc_before_opening_anything(tmp_path: Path) -> None:
    """[F1] (a), [C10]: the companion process refuses a non-loopback FC
    endpoint before it creates the recording or binds a socket."""
    rec = tmp_path / "flight.jsonl"
    config = CompanionConfig(fc_endpoint="tcp:192.168.1.10:5762", recording=rec, ui_port=0)
    with pytest.raises(EndpointRefused):
        Companion(config, UI_TOKEN)
    assert not rec.exists()


def _udp(timeout_s: float = 2.0) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(timeout_s)
    return s


def test_c10_companion_process_wires_udp_fc_ui_and_recording(tmp_path: Path) -> None:
    """[C10], [C8], [P5], [P5b], [F4], [U6], [R2]: one companion process. FC
    frames arrive on its link; a track and commands arrive on their UDP ports;
    each command's ack goes back to its sender (accepted prime, rejected_auth,
    rejected_malformed); the mission_state and fc_link_health packets reach
    their listeners; an undecodable track datagram is counted, not recorded;
    the UI polls through the same lock; and the recording starts with meta and
    holds every input, with no token."""
    fc_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fc_listener.bind(("127.0.0.1", 0))
    fc_listener.listen(1)
    states_rx, health_rx, sender = _udp(), _udp(), _udp()
    clock = _Clock(T0)
    rec = tmp_path / "flight.jsonl"
    config = CompanionConfig(
        fc_endpoint=f"tcp:127.0.0.1:{fc_listener.getsockname()[1]}",
        recording=rec,
        track_port=0,
        command_port=0,
        mission_state_port=states_rx.getsockname()[1],
        fc_link_health_port=health_rx.getsockname()[1],
        ui_port=0,
    )
    fc_conn: socket.socket | None = None
    try:
        with Companion(config, UI_TOKEN, clock=clock) as comp:
            comp.start()
            fc_conn, _ = fc_listener.accept()
            fc_conn.sendall(_fc_frames(T0))  # no SITL proof: fc_link stays receive-only
            fd = comp.fc_link.fileno()
            assert fd is not None and select.select([fd], [], [], 2.0)[0]
            comp.run_once()  # reads the FC, ticks at T0
            health = decode(PacketKind.FC_LINK_HEALTH, health_rx.recv(65536))
            assert isinstance(health, FcLinkHealthPacket)
            assert health.fc_link_up is True and health.gate_state.value == "locked"

            track = TrackPacket(
                t_cap=T0,
                track_id=7,
                state=TrackState.TENTATIVE,
                u=900.0,
                v_px=500.0,
                du=0.0,
                dv=0.0,
                w=20.0,
                h=20.0,
                hits=1,
                misses=0,
                age_frames=1,
            )
            sender.sendto(encode(track), comp.tracks.address)
            sender.sendto(b"not a track", comp.tracks.address)
            sends = [
                _body("ui-c1", "prime", params=PRIME_PARAMS),
                _body("ui-c2", "abort", token=WRONG_TOKEN),
                _body("ui-c3", "launch"),
            ]
            for body in sends:
                sender.sendto(body, comp.commands.address)
            clock.t = T0 + 10
            comp.run_once()
            acks = {}
            for _ in sends:
                ack = decode(PacketKind.ACK, sender.recv(65536))
                assert isinstance(ack, AckPacket)
                acks[ack.cmd_id] = ack.result
            assert acks == {
                "ui-c1": AckResult.ACCEPTED,
                "ui-c2": AckResult.REJECTED_AUTH,
                "ui-c3": AckResult.REJECTED_MALFORMED,
            }
            published = decode(PacketKind.MISSION_STATE, states_rx.recv(65536))
            assert isinstance(published, MissionStatePacket)
            assert published.mission_state is MissionState.PRIMED
            assert comp.tracks.rejected == 1

            host, port = comp.server.address
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/state")
            view = json.loads(conn.getresponse().read())["view"]
            conn.close()
            assert view["mission_state"] == "PRIMED"
            assert view["battery_pct"] == BATTERY_PCT
            clock.t = T0 + 50
            comp.run_once()
    finally:
        if fc_conn is not None:
            fc_conn.close()
        for s in (fc_listener, states_rx, health_rx, sender):
            s.close()

    text = rec.read_text(encoding="ascii")
    records = list(read_records(text.splitlines()))
    assert records[0].stream is Stream.META
    by = {s: [r for r in records if r.stream is s] for s in Stream}
    assert [r.t_rx for r in by[Stream.TICK]] == [T0, T0 + 50]
    assert [r.packet for r in by[Stream.TRACK]] == [track]
    assert [r.auth_ok for r in by[Stream.COMMAND]] == [True, False]
    assert [r.t_rx for r in by[Stream.GROUND_HB]] == [T0 + 10]
    assert len(by[Stream.FC_LINK_HEALTH]) == 1
    assert by[Stream.MAVLINK] and all(r.direction == "rx" for r in by[Stream.MAVLINK])
    assert UI_TOKEN not in text and WRONG_TOKEN not in text
