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
from skyweave2.drone.core import CompanionCore, CoreConfig, CoreOutput, replay
from skyweave2.drone.fc_link import EndpointRefused
from skyweave2.drone.ground_ui import (
    TOKEN_HEADER,
    TOKEN_STORAGE_KEY,
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
    Box,
    CommandName,
    CommandPacket,
    DetectionPacket,
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
from skyweave2.drone.tracker import id_base_from_start
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


def _token_header(token: str | None) -> dict[str, str]:
    """[U6]: the header a state poll carries its token in; none without one."""
    return {} if token is None else {TOKEN_HEADER: token}


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

    def fc_tick(self, t: int, frames: bytes | None = None) -> None:
        """What the companion loop does at ``t``: FC frames in (default: the FC
        on the ground in LOITER), then a tick."""
        self.clock.t = t
        with self.service.lock:
            self.core.on_mavlink_rx(_fc_frames(t) if frames is None else frames, t)
            self.outputs.append(self.core.on_tick(t))

    def track(self, t: int, track_id: int) -> None:
        """One confirmed track packet [P2] into the core at ``t`` (this bench's
        core runs no tracker, so it takes track packets)."""
        self.clock.t = t
        pkt = TrackPacket(
            t_cap=t - 20,
            track_id=track_id,
            state=TrackState.CONFIRMED,
            u=960.0,
            v_px=600.0,
            du=0.0,
            dv=0.0,
            w=40.0,
            h=40.0,
            hits=3,
            misses=0,
            age_frames=3,
        )
        with self.service.lock:
            self.outputs.append(self.core.on_track(pkt, t))

    def http(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        host, port = self.server.address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            send = {"Content-Type": "application/json"} if body is not None else {}
            send.update(headers or {})
            conn.request(method, path, body=body, headers=send)
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

    def poll(self, t: int, token: str | None = UI_TOKEN) -> dict[str, Any]:
        """One ``GET /state`` as the page sends it once the operator has typed
        the token ([U6]: the token in the ``X-Skyweave-Token`` header).
        ``token=None`` is a poll with no token header (a page with no token
        yet, or another device on the access point)."""
        self.clock.t = t
        status, data = self.http("GET", "/state", headers=_token_header(token))
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
    no form (nothing can submit the token in a URL), no storage that outlives
    the tab, and no cookie ([U3] since E1-D15 allows the tab's sessionStorage;
    ``test_u3_page_keeps_the_token_for_a_reload_in_session_storage_only``
    covers it). The server answers no other endpoint."""
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
    for api in ("localStorage", "indexedDB", "document.cookie", "WebSocket", "XMLHttpRequest"):
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


def test_u6_each_token_bearing_state_poll_is_one_ground_hb_record(bench: _Bench) -> None:
    """[U6], [R2], [U1], E1-D15: every GET /state that carries the right token
    in its X-Skyweave-Token header is one ground heartbeat, recorded as a
    ground_hb record at the poll's stamp, including polls that arrive in
    parallel (the request threads are serialized into the core by its lock)."""
    for k in range(3):
        assert bench.poll(T0 + 100 * k)["heartbeat"] is True
    bench.clock.t = T0 + 500
    header = _token_header(UI_TOKEN)
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = [
            s for s, _ in pool.map(lambda _: bench.http("GET", "/state", headers=header), range(8))
        ]
    assert statuses == [200] * 8
    hbs = [r.t_rx for r in bench.records(Stream.GROUND_HB)]
    assert hbs == [T0, T0 + 100, T0 + 200] + [T0 + 500] * 8
    assert (bench.service.polls, bench.service.polls_unauthenticated) == (11, 0)


def _view_but_time_and_ground_link(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state["view"].items() if k not in ("t", "ground_link")}


@pytest.mark.parametrize(
    "token",
    [None, "", WRONG_TOKEN, UI_TOKEN[:-1], UI_TOKEN + "x", UI_TOKEN.upper(), "ui-töken"],
    ids=["no_header", "empty", "wrong", "one_short", "one_long", "other_case", "not_ascii"],
)
def test_u6_e1_f17_poll_without_the_right_token_is_not_a_heartbeat(
    bench: _Bench, token: str | None
) -> None:
    """[U6], [R2], [P5c], E1-D15 (closes finding E1-F17): GET /state answers a
    poll with no token, or a wrong one, with the view, but that poll is not a
    ground heartbeat: no ground_hb record, the ground link's newest heartbeat
    stays the prime's, and the poll is counted. Near misses of the token (one
    char short, one long, another case) are wrong tokens. Discrimination: the
    same poll with the right token is one heartbeat at its stamp, and it gets
    the same view."""
    bench.fc_tick(T0)
    assert bench.ack(T0 + 50, _body("ui-p1", "prime", params=PRIME_PARAMS)).result is (
        AckResult.ACCEPTED
    )
    without = bench.poll(T0 + 100, token=token)
    assert without["heartbeat"] is False
    assert bench.records(Stream.GROUND_HB) == []
    assert without["view"]["t"] == T0 + 100
    assert without["view"]["mission_state"] == "PRIMED"
    assert without["view"]["ground_link"] == {"last_hb_t": T0 + 50, "up": True}
    assert without["html"] == render_status(view_from_recording(bench.lines(), until_t=T0 + 100))
    assert (bench.service.polls, bench.service.polls_unauthenticated) == (1, 1)

    with_token = bench.poll(T0 + 200)
    assert with_token["heartbeat"] is True
    assert [r.t_rx for r in bench.records(Stream.GROUND_HB)] == [T0 + 200]
    assert with_token["view"]["ground_link"] == {"last_hb_t": T0 + 200, "up": True}
    assert _view_but_time_and_ground_link(with_token) == _view_but_time_and_ground_link(without)
    assert (bench.service.polls, bench.service.polls_unauthenticated) == (2, 1)


def test_u6_token_in_the_url_is_not_a_heartbeat_and_the_header_name_is_case_free(
    bench: _Bench,
) -> None:
    """[U6], [U3], E1-D15: the token never travels in a URL, so the server
    takes it from the header only: a poll with the right token in its query
    string and no header is answered with the view and is not a heartbeat.
    The header's name is an HTTP field name (any letter case)."""
    bench.clock.t = T0 + 100
    status, data = bench.http("GET", f"/state?token={UI_TOKEN}")
    assert status == 200 and json.loads(data)["heartbeat"] is False
    assert json.loads(data)["view"]["t"] == T0 + 100
    assert bench.records(Stream.GROUND_HB) == []
    bench.clock.t = T0 + 200
    status, data = bench.http("GET", "/state", headers={TOKEN_HEADER.lower(): UI_TOKEN})
    assert status == 200 and json.loads(data)["heartbeat"] is True
    assert [r.t_rx for r in bench.records(Stream.GROUND_HB)] == [T0 + 200]
    assert (bench.service.polls, bench.service.polls_unauthenticated) == (2, 1)


TRACK = 4242  # the candidate's track_id (fixture fact)


def _to_search(bench: _Bench) -> int:
    """Prime, the switch into GUIDED, takeoff: the mission in SEARCH. Returns
    the stamp of the last tick. The prime (an authenticated ground command) at
    T0 + 100 is the newest ground heartbeat."""
    ground = {"armed": False, "landed": LandedState.ON_GROUND, "alt_m": 0.0}
    flying = {"armed": True, "landed": LandedState.IN_AIR, "alt_m": 10.0}
    bench.fc_tick(T0)
    assert bench.ack(T0 + 100, _body("ui-p1", "prime", params=PRIME_PARAMS)).result is (
        AckResult.ACCEPTED
    )
    bench.fc_tick(T0 + 150)  # LOITER seen after the prime (T03 edge, E1-D4)
    bench.fc_tick(T0 + 200, _fc_flight(T0 + 200, mode=GUIDED_MODE, **ground))  # T03
    bench.fc_tick(T0 + 250, _fc_flight(T0 + 250, mode=GUIDED_MODE, **flying))  # T04
    assert bench.core.mission.state is MissionState.SEARCH
    return T0 + 250


@pytest.mark.parametrize(
    ("token", "fires"),
    [(None, True), (WRONG_TOKEN, True), (UI_TOKEN, False)],
    ids=["no_token", "wrong_token", "right_token"],
)
def test_u6_e1_f17_only_token_bearing_polls_hold_off_the_ground_link_failsafe(
    bench: _Bench, token: str | None, fires: bool
) -> None:
    """[U6], T18, E1-D15 (closes finding E1-F17): in flight, a device that
    polls GET /state every 250 ms without the right token does not keep the
    ground link alive: T18 fires (failsafe:ground_link, RETURN) once
    ground_link_timeout_ms has passed since the last real heartbeat (the
    prime). The same polls with the token are heartbeats, and the mission
    stays in SEARCH."""
    _to_search(bench)
    last_hb = T0 + 100
    timeout = bench.core.config.mission.ground_link_timeout_ms
    flying = {"armed": True, "landed": LandedState.IN_AIR, "alt_m": 10.0}
    t = T0 + 500
    while t <= last_hb + timeout + 500:
        assert bench.poll(t - 10, token=token)["heartbeat"] is (not fires)
        bench.fc_tick(t, _fc_flight(t, mode=GUIDED_MODE, **flying))
        t += 250
    events = [
        e.name
        for r in bench.records(Stream.MISSION_STATE)
        if isinstance(r.packet, MissionStatePacket)
        for e in r.packet.events
    ]
    assert ("failsafe:ground_link" in events) is fires
    assert ("T18" in bench.tids()) is fires
    assert bench.core.mission.state is (MissionState.RETURN if fires else MissionState.SEARCH)
    assert bench.service.polls_unauthenticated == (bench.service.polls if fires else 0)


def test_u6_p5c_poll_tokens_never_recorded_or_logged(
    bench: _Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """[U6], [P5c], [R2], E1-D15: the token a state poll carries (right or
    wrong) never reaches the recording (meta.config included), the /state
    answer, or the process log, although the poll that was not a heartbeat is
    logged."""
    caplog.set_level(logging.DEBUG, logger="skyweave2")
    bench.fc_tick(T0)
    answers = [
        bench.poll(T0 + 100),
        bench.poll(T0 + 200, token=WRONG_TOKEN),
        bench.poll(T0 + 300, token=None),
    ]
    assert [a["heartbeat"] for a in answers] == [True, False, False]
    assert caplog.text.count("not a ground heartbeat") == 2
    meta = bench.records(Stream.META)[0]
    texts = [
        bench.sink.getvalue(),
        canonical_json(dict(meta.config or {})).decode("ascii"),
        caplog.text,
        *(json.dumps(a) for a in answers),
    ]
    assert all(UI_TOKEN not in text and WRONG_TOKEN not in text for text in texts)


# -- [U3], [U6], [U7] the page's script (E1-D12, E1-D15) --------------------------


def test_u3_page_keeps_the_token_for_a_reload_in_session_storage_only(bench: _Bench) -> None:
    """[U3], [U6], E1-D15: the page keeps the token in memory and in the tab's
    sessionStorage, and restores it on load before its first poll, so a reload
    of the same tab keeps the heartbeat. It writes the token nowhere that
    outlives the tab (no localStorage, no indexedDB, no cookie) and never into
    a URL: it changes no location or history, and its only requests go to the
    bare paths /state and /command. The server never renders the token into
    the page."""
    page = bench.page(T0)
    script = _Doc(page).script
    assert f'var TOKEN_KEY = "{TOKEN_STORAGE_KEY}";' in script
    assert "tokenEl.value = kept" in script.split("function restoreToken()", 1)[1].split("}\n")[0]
    assert "window.sessionStorage.getItem(TOKEN_KEY)" in script
    assert "window.sessionStorage.setItem(TOKEN_KEY, tokenEl.value)" in script
    assert 'tokenEl.addEventListener("input", keepToken)' in script
    assert script.count("sessionStorage.setItem(") == 1  # the token is the only thing stored
    assert script.rindex("restoreToken();") < script.rindex("poll();")
    for api in ("localStorage", "indexedDB", "document.cookie", "location", "history"):
        assert api not in page
    assert UI_TOKEN not in page


def test_u6_page_poll_sends_the_token_in_the_header_never_in_the_url(bench: _Bench) -> None:
    """[U6], [U3], E1-D15: the page's poll puts the typed token in the
    X-Skyweave-Token request header (only a value that fits the token
    pattern), and fetches the bare /state path: the token is in no URL."""
    script = _Doc(bench.page(T0)).script
    poll = script.split("function poll() {", 1)[1].split("\n  }\n", 1)[0]
    assert f'if (TOKEN_RE.test(tok)) opts.headers = {{"{TOKEN_HEADER}": tok}};' in poll
    assert 'fetch("/state", opts)' in poll
    assert poll.index("opts.headers") < poll.index('fetch("/state", opts)')
    assert "var TOKEN_RE = /^[!-~]{1,256}$/;" in script  # [P5] the token pattern


def test_u7_page_approve_sends_the_displayed_candidate_or_nothing(bench: _Bench) -> None:
    """[U7], E1-D12: the page's approve reads the candidate the status block
    displays (the ``candidate`` row's data-value), and sends it as
    ``params.track_id``. With no candidate displayed it tells the operator and
    returns before anything is sent. The row it reads holds null while no
    candidate is displayed."""
    doc = _Doc(bench.page(T0))
    script = doc.script
    shown = script.split("function shownCandidate() {", 1)[1].split("\n  }\n", 1)[0]
    assert 'document.getElementById("candidate")' in shown
    assert 'JSON.parse(el.getAttribute("data-value"))' in shown
    send = script.split("function send(command) {", 1)[1].split("\n  }\n", 1)[0]
    approve = send.split('if (command === "approve_engage") {', 1)[1]
    assert approve.index("var cand = shownCandidate();") < approve.index("if (cand === null) {")
    none_shown = approve.split("if (cand === null) {", 1)[1].split("}", 1)[0]
    assert "not sent: no candidate is displayed" in none_shown
    assert none_shown.rstrip().endswith("return;")
    assert "attempt(" not in none_shown and "fetch(" not in none_shown
    assert "params = {track_id: cand};" in approve
    assert "if (params !== null) pkt.params = params;" in send
    assert send.index("if (cand === null) {") < send.index("attempt(")
    assert doc.value("candidate") is None


def test_u7_m8_approve_naming_the_displayed_candidate_through_the_page_path(
    bench: _Bench,
) -> None:
    """[U7], [M8], [M5], [P5] approve params, [R2], [R3], E1-D12: in ACQUIRING
    the page's candidate row (the one its approve reads) holds the mission's
    candidate id. Posted the way the page posts it, an approve naming another
    id is acked rejected_state, one with a string id rejected_params, and the
    one naming the displayed candidate is accepted (T07) and engages that
    track. The command records keep ``params.track_id`` (never the token), and
    the recording replays exactly."""
    t = _to_search(bench)
    bench.track(t + 50, TRACK)  # T05
    t5 = t + 50
    settle = bench.core.config.mission.approve_settle_ms
    shown = _Doc(bench.page(t5 + 100)).value("candidate")
    assert shown == TRACK == bench.core.mission.candidate_id
    assert _Doc(bench.poll(t5 + 200)["html"]).value("candidate") == shown

    at = t5 + settle
    sent = [{"track_id": shown + 1}, {"track_id": str(shown)}, {"track_id": shown}]
    results = [
        bench.ack(at + 10 * i, _body(f"ui-a{i}", "approve_engage", params=params)).result
        for i, params in enumerate(sent)
    ]
    assert results == [
        AckResult.REJECTED_STATE,
        AckResult.REJECTED_PARAMS,
        AckResult.ACCEPTED,
    ]
    assert bench.tids()[-1] == "T07"
    assert bench.core.mission.engaged_track_id == shown
    approves = [
        r.packet
        for r in bench.records(Stream.COMMAND)
        if isinstance(r.packet, CommandPacket) and r.packet.command is CommandName.APPROVE_ENGAGE
    ]
    assert [dict(p.params or {}) for p in approves] == sent
    assert UI_TOKEN not in bench.sink.getvalue()
    assert replay(bench.lines()).mismatch() is None


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
    """[C10], [C8], [P5], [P5b], [F4], [U6], [R2], E2-D5: one companion process.
    FC frames arrive on its link; a detection and commands arrive on their UDP
    ports; the tracker inside the process publishes the frame's track packet
    on the track port; each command's ack goes back to its sender (accepted
    prime, rejected_auth, rejected_malformed); the mission_state and
    fc_link_health packets reach their listeners; an undecodable detection
    datagram is counted, not recorded; the UI polls through the same lock; and
    the recording starts with meta and holds every input and the published
    track, with no token."""
    fc_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fc_listener.bind(("127.0.0.1", 0))
    fc_listener.listen(1)
    states_rx, health_rx, tracks_rx, sender = _udp(), _udp(), _udp(), _udp()
    clock = _Clock(T0)
    rec = tmp_path / "flight.jsonl"
    config = CompanionConfig(
        fc_endpoint=f"tcp:127.0.0.1:{fc_listener.getsockname()[1]}",
        recording=rec,
        detection_port=0,
        command_port=0,
        track_port=tracks_rx.getsockname()[1],
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

            det = DetectionPacket(
                t_cap=T0, frame_seq=0, boxes=(Box(x=890.0, y=490.0, w=20.0, h=20.0, conf=0.9),)
            )
            sender.sendto(encode(det), comp.detections.address)
            sender.sendto(b"not a detection", comp.detections.address)
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
            track = decode(PacketKind.TRACK, tracks_rx.recv(65536))
            assert isinstance(track, TrackPacket) and track.t_cap == T0
            assert comp.detections.rejected == 1

            host, port = comp.server.address
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/state", headers=_token_header(UI_TOKEN))  # [U6] a heartbeat
            view = json.loads(conn.getresponse().read())["view"]
            conn.close()
            assert view["mission_state"] == "PRIMED"
            assert view["battery_pct"] == BATTERY_PCT
            clock.t = T0 + 50
            comp.run_once()
    finally:
        if fc_conn is not None:
            fc_conn.close()
        for s in (fc_listener, states_rx, health_rx, tracks_rx, sender):
            s.close()

    text = rec.read_text(encoding="ascii")
    records = list(read_records(text.splitlines()))
    assert records[0].stream is Stream.META
    by = {s: [r for r in records if r.stream is s] for s in Stream}
    assert [r.t_rx for r in by[Stream.TICK]] == [T0, T0 + 50]
    assert [r.packet for r in by[Stream.DETECTION]] == [det]
    assert [r.packet for r in by[Stream.TRACK]] == [track]
    assert [r.auth_ok for r in by[Stream.COMMAND]] == [True, False]
    assert [r.t_rx for r in by[Stream.GROUND_HB]] == [T0 + 10]
    assert len(by[Stream.FC_LINK_HEALTH]) == 1
    assert by[Stream.MAVLINK] and all(r.direction == "rx" for r in by[Stream.MAVLINK])
    assert UI_TOKEN not in text and WRONG_TOKEN not in text


GUIDED_MODE = 4  # ArduCopter custom_mode (fixture fact)
FLIGHT_FRAMES = 6  # frames per _fc_flight bundle


def _fc_flight(boot_ms: int, *, mode: int, armed: bool, landed: LandedState, alt_m: float) -> bytes:
    """One [M2a] telemetry bundle: mode, arming, landed state, altitude, battery, attitude."""
    mav = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)
    base = mavlink2.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    if armed:
        base |= mavlink2.MAV_MODE_FLAG_SAFETY_ARMED
    alt_mm = round(alt_m * 1000)
    msgs = [
        mav.heartbeat_encode(
            mavlink2.MAV_TYPE_QUADROTOR,
            mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
            base,
            mode,
            mavlink2.MAV_STATE_ACTIVE,
        ),
        mav.extended_sys_state_encode(0, int(landed)),
        mav.global_position_int_encode(boot_ms, 0, 0, alt_mm, alt_mm, 0, 0, 0, 0),
        mav.local_position_ned_encode(boot_ms, 0.0, 0.0, -alt_m, 0.0, 0.0, 0.0),
        mav.sys_status_encode(0, 0, 0, 0, 12000, 100, BATTERY_PCT, 0, 0, 0, 0, 0, 0),
        mav.attitude_encode(boot_ms, 0.0, 0.0, 0.3, 0.0, 0.0, 0.0),
    ]
    assert len(msgs) == FLIGHT_FRAMES
    return b"".join(bytes(m.pack(mav)) for m in msgs)


def _drain(s: socket.socket, kind: PacketKind) -> list[Any]:
    """Every datagram already queued on a listener, decoded."""
    out = []
    while select.select([s], [], [], 0.0)[0]:
        out.append(decode(kind, s.recv(65536)))
    return out


def test_c10_e2d5_companion_runs_the_tracker_publishes_tracks_and_replays(
    tmp_path: Path,
) -> None:
    """E2-D5, [C10], [P2], [K2], [K4], T01 T03 T04 T05, [R2], [R3]: the
    companion process binds the detection port and runs the tracker in
    process. The FC takes it through prime, the switch into GUIDED, and
    takeoff to SEARCH; three detection frames of one box then come in on the
    detection port. The tracker's packets reach the track port, one per frame,
    and the third (confirmed) one makes the candidate (T05). The recording
    holds the detections and the published tracks, its meta.config carries
    the tracker with the id_base of the process start, and replay re-runs the
    tracker from the detection records and matches every track record."""
    defaults = CompanionConfig(fc_endpoint="tcp:127.0.0.1:5762", recording=tmp_path / "unused")
    assert (defaults.detection_port, defaults.track_port) == (14601, 14602)  # contract §9

    fc_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fc_listener.bind(("127.0.0.1", 0))
    fc_listener.listen(1)
    states_rx, health_rx, tracks_rx, sender = _udp(), _udp(), _udp(), _udp()
    clock = _Clock(T0)
    rec = tmp_path / "flight.jsonl"
    config = CompanionConfig(
        fc_endpoint=f"tcp:127.0.0.1:{fc_listener.getsockname()[1]}",
        recording=rec,
        detection_port=0,
        command_port=0,
        track_port=tracks_rx.getsockname()[1],
        mission_state_port=states_rx.getsockname()[1],
        fc_link_health_port=health_rx.getsockname()[1],
        ui_port=0,
    )
    dets = [
        DetectionPacket(
            t_cap=T0 + 180 + 50 * i,
            frame_seq=i,
            boxes=(Box(x=880.0 + i, y=485.0, w=40.0, h=30.0, conf=0.9),),
        )
        for i in range(3)
    ]
    fc_conn: socket.socket | None = None
    try:
        with Companion(config, UI_TOKEN, clock=clock) as comp:
            comp.start()
            fc_conn, _ = fc_listener.accept()

            def fc(**vehicle: Any) -> None:
                """The FC's bundle, read in full before the next tick falls due."""
                want = comp.fc_link.rx_frames + FLIGHT_FRAMES
                assert fc_conn is not None
                fc_conn.sendall(_fc_flight(clock.t, **vehicle))
                for _ in range(200):
                    comp.run_once(0.01)
                    if comp.fc_link.rx_frames >= want:
                        return
                raise AssertionError("the companion did not read the FC's frames")

            def tick(t: int, *datagrams: tuple[bytes, Any]) -> None:
                """Inputs at ``t``, then the companion's step (its tick is due)."""
                clock.t = t
                for data, addr in datagrams:
                    sender.sendto(data, addr)
                comp.run_once()

            ground = {"armed": False, "landed": LandedState.ON_GROUND, "alt_m": 0.0}
            flying = {"armed": True, "landed": LandedState.IN_AIR, "alt_m": 10.0}
            fc(mode=LOITER, **ground)  # the first step also ticks at T0 (unprimed)
            prime = _body("ui-p1", "prime", params=PRIME_PARAMS)
            tick(T0 + 50, (prime, comp.commands.address))  # T01; the tick sees LOITER
            ack = decode(PacketKind.ACK, sender.recv(65536))
            assert isinstance(ack, AckPacket) and ack.result is AckResult.ACCEPTED
            fc(mode=GUIDED_MODE, **ground)  # the pilot moves the switch into GUIDED
            tick(T0 + 100)  # T03
            fc(mode=GUIDED_MODE, **flying)
            tick(T0 + 150)  # T04
            for det in dets:
                fc(mode=GUIDED_MODE, **flying)
                tick(det.t_cap + 20, (encode(det), comp.detections.address))
            tids = [row[1] for row in comp.core.mission.transition_log]
            tracks = _drain(tracks_rx, PacketKind.TRACK)
            states = _drain(states_rx, PacketKind.MISSION_STATE)
    finally:
        if fc_conn is not None:
            fc_conn.close()
        for s in (fc_listener, states_rx, health_rx, tracks_rx, sender):
            s.close()

    assert tids == ["T01", "T03", "T04", "T05"]
    assert [p.state for p in tracks] == [TrackState.TENTATIVE] * 2 + [TrackState.CONFIRMED]
    assert [p.t_cap for p in tracks] == [d.t_cap for d in dets]  # one packet per frame
    (track_id,) = {p.track_id for p in tracks}
    assert states[-1].mission_state is MissionState.ACQUIRING
    assert f"candidate:{track_id}" in [e.name for s in states for e in s.events]

    records = list(read_records(rec.read_text(encoding="ascii").splitlines()))
    assert records[0].config is not None
    tracker_cfg = records[0].config["tracker"]  # [R2]: the tracker the process ran
    assert tracker_cfg["id_base"] == id_base_from_start(T0)  # [K4]: the process start
    assert tracker_cfg["coast_cap"] == records[0].config["coast_cap"]  # [P2a]
    assert [r.packet for r in records if r.stream is Stream.DETECTION] == dets
    assert [r.packet for r in records if r.stream is Stream.TRACK] == tracks

    result = replay(rec)  # [R3]: the tracker re-runs from the detection records
    assert result.mismatch() is None
    assert [p for _, p in result.recorded_tracks] == tracks
