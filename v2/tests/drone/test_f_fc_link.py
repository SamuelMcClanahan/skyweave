"""F series, fast tier: fc_link v1 and vehicle state (DRONE_CONTRACTS_D0.md §6, [M2a], [G1]).

No mocks of our own code. The FC side is a test-owned local TCP peer that
speaks real MAVLink2 (pymavlink-encoded frames as system 1, component 1) and
captures every byte fc_link writes; assertions parse those bytes with
pymavlink. Time is an injected step clock ([F11]).
"""

from __future__ import annotations

import io
import math
import select
import socket
from collections.abc import Iterator
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone import packets as P
from skyweave2.drone.fc_link import (
    TONES,
    TYPE_MASK_VELOCITY_YAW_RATE,
    EndpointRefused,
    FcLink,
    check_endpoint,
    clamp_velocity,
)
from skyweave2.drone.recording import Recorder, Stream, read_records
from skyweave2.drone.types import FcRequest, FcRequestKind, LandedState, VelocityCommand
from skyweave2.drone.vehicle_state import (
    LinkConfig,
    VehicleState,
    parse_frames,
    rc_approve_cmd_id,
)

# ArduCopter custom_mode numbers (ArduPilot docs; fixture facts, not our table).
GUIDED, LOITER, RTL, LAND = 4, 5, 6, 9

FC = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)
GCS = mavlink2.MAVLink(None, srcSystem=255, srcComponent=190)
COMPANION_ECHO = mavlink2.MAVLink(None, srcSystem=1, srcComponent=191)
OTHER_VEHICLE = mavlink2.MAVLink(None, srcSystem=2, srcComponent=1)

ARM_DISARM = mavlink2.MAV_CMD_COMPONENT_ARM_DISARM  # 400
TAKEOFF = mavlink2.MAV_CMD_NAV_TAKEOFF  # 22
SET_MODE = mavlink2.MAV_CMD_DO_SET_MODE  # 176
SET_INTERVAL = mavlink2.MAV_CMD_SET_MESSAGE_INTERVAL  # 511


class StepClock:
    """Injected board clock ([F11]); tests move it by hand."""

    def __init__(self, t: int = 10_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


# -- FC-side message builders (real encodings) ------------------------------


def heartbeat(mode: int, armed: bool = False, mav: Any = FC) -> Any:
    base = mavlink2.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    if armed:
        base |= mavlink2.MAV_MODE_FLAG_SAFETY_ARMED
    return mav.heartbeat_encode(
        mavlink2.MAV_TYPE_QUADROTOR,
        mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
        base,
        mode,
        mavlink2.MAV_STATE_ACTIVE,
    )


def attitude(roll: float = 0.0, pitch: float = 0.0, yaw: float = 0.0, boot: int = 0) -> Any:
    return FC.attitude_encode(boot, roll, pitch, yaw, 0.0, 0.0, 0.0)


def simstate(mav: Any = FC) -> Any:
    return mav.simstate_encode(0.0, 0.0, 0.0, 0.0, 0.0, -9.8, 0.0, 0.0, 0.0, 370000000, -1220000000)


def sim_state() -> Any:
    return FC.sim_state_encode(
        1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -9.8, 0.0, 0.0, 0.0,
        37.0, -122.0, 10.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    )  # fmt: skip


def rc(ch8: int, chancount: int = 16) -> Any:
    chans = [1500] * 18
    chans[7] = ch8
    return FC.rc_channels_encode(0, chancount, *chans, 255)


def sys_status(battery_remaining: int) -> Any:
    return FC.sys_status_encode(0, 0, 0, 0, 12000, 100, battery_remaining, 0, 0, 0, 0, 0, 0)


def ingest(vs: VehicleState, t: int, *msgs: Any, mav: Any = FC) -> None:
    """Encode each message as ``mav`` and feed the parsed frame to ``vs`` at ``t``."""
    for msg in msgs:
        for parsed in parse_frames(bytes(msg.pack(mav))):
            vs.ingest(parsed, t)


# -- the test-owned MAVLink peer ---------------------------------------------


class MavPeer:
    """Plays the FC on a local TCP socket and captures every byte fc_link writes."""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(2)
        self.endpoint = f"tcp:127.0.0.1:{self.listener.getsockname()[1]}"
        self.conn: socket.socket | None = None
        self.captured = bytearray()
        self.sent_frames: list[bytes] = []

    def accept(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.conn, _ = self.listener.accept()

    def send(self, link: FcLink, *msgs: Any, mav: Any = FC) -> None:
        """Send frames as ``mav`` and pump them into ``link``."""
        assert self.conn is not None
        raws = [bytes(m.pack(mav)) for m in msgs]
        self.sent_frames.extend(raws)
        self.conn.sendall(b"".join(raws))
        want = link.rx_frames + len(raws)
        for _ in range(200):
            fd = link.fileno()
            assert fd is not None
            select.select([fd], [], [], 0.05)
            link.poll()
            if link.rx_frames >= want:
                return
        raise AssertionError("fc_link did not read the peer's frames")

    def drain(self) -> bytes:
        """Everything fc_link wrote since the last drain (waits briefly for stragglers)."""
        assert self.conn is not None
        got = bytearray()
        while select.select([self.conn], [], [], 0.05)[0]:
            chunk = self.conn.recv(65536)
            if not chunk:
                break
            got += chunk
        self.captured += got
        return bytes(got)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.listener.close()


@pytest.fixture()
def peer() -> Iterator[MavPeer]:
    p = MavPeer()
    try:
        yield p
    finally:
        p.close()


def connect(peer: MavPeer, clock: StepClock, *, enabled: bool, recorder: Any = None) -> FcLink:
    link = FcLink(peer.endpoint, clock, LinkConfig(setpoints_enabled=enabled), recorder=recorder)
    link.connect(timeout_s=2.0)
    peer.accept()
    return link


def prove(peer: MavPeer, link: FcLink) -> list[Any]:
    """Send the SITL proof; return the [F2] frames fc_link wrote in answer."""
    peer.send(link, simstate())
    assert link.sitl_proven
    return parse_frames(peer.drain())


def write_everything(link: FcLink) -> list[bool]:
    """Every write path fc_link has."""
    return [
        link.send_velocity(VelocityCommand(vn=1.0, ve=0.0, vd=0.0, yaw_rate=0.0)),
        link.request(FcRequest(kind=FcRequestKind.ARM_AND_TAKEOFF, value=10.0)),
        link.request(FcRequest(kind=FcRequestKind.MODE_RTL)),
        link.request(FcRequest(kind=FcRequestKind.MODE_LAND)),
        link.request(FcRequest(kind=FcRequestKind.TONE, value="ENGAGED")),
    ]


def commands(frames: list[Any]) -> list[int]:
    return [f.command for f in frames if f.get_type() == "COMMAND_LONG"]


def types(frames: list[Any]) -> list[str]:
    return [f.get_type() for f in frames]


# ---------------------------------------------------------------------------
# [F1] (a): loopback only
# ---------------------------------------------------------------------------

REFUSED = [
    "/dev/ttyACM0",
    "/dev/ttyS1:921600",
    "/dev/serial/by-id/usb-ArduPilot_MatekH743-if00",
    "serial:/dev/ttyAMA0:921600",
    "COM3",
    "tcp:192.168.1.10:5762",
    "tcp:0.0.0.0:5762",
    "udpout:10.0.0.2:14550",
    "udp:192.168.4.1:14550",
    "tcp:localhost:5762",
    "tcp:127.0.0.2:5762",
    "tcp:127.0.0.1.example.com:5762",
    "tcpin:127.0.0.1:5762",
    "tcp:127.0.0.1:0",
    "tcp:127.0.0.1:65536",
    "tcp:127.0.0.1",
    "",
]


@pytest.mark.parametrize("endpoint", REFUSED)
def test_f1_non_loopback_and_serial_endpoints_refused_at_construction(endpoint: str) -> None:
    """[F1] (a): a serial device or a non-loopback host is refused before any socket exists."""
    with pytest.raises(EndpointRefused):
        check_endpoint(endpoint)
    with pytest.raises(EndpointRefused):
        FcLink(endpoint, StepClock(), LinkConfig(setpoints_enabled=True))


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp:127.0.0.1:5762",
        "udp:127.0.0.1:14550",
        "udpin:127.0.0.1:14551",
        "udpout:127.0.0.1:14552",
    ],
)
def test_f1_loopback_endpoints_accepted(endpoint: str) -> None:
    """[F1] (a): ``tcp:127.0.0.1:<port>`` and ``udp...:127.0.0.1:<port>`` are accepted."""
    check_endpoint(endpoint)
    link = FcLink(endpoint, StepClock(), LinkConfig())
    assert not link.connected and not link.sitl_proven


# ---------------------------------------------------------------------------
# [F1] (b): receive-only until the SITL proof
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("proof", ["SIMSTATE", "SIM_STATE"])
def test_f1b_receive_only_until_sitl_proof(peer: MavPeer, proof: str) -> None:
    """[F1] (b), [F2]: before the FC sends SIMSTATE/SIM_STATE every write path writes zero
    bytes (counted); a foreign SIMSTATE proves nothing; after the proof fc_link requests its
    streams and writes flow; the proof resets on reconnect."""
    clock = StepClock()
    link = connect(peer, clock, enabled=True)
    peer.send(link, heartbeat(GUIDED), attitude(), rc(1100))
    for other in (GCS, OTHER_VEHICLE, COMPANION_ECHO):  # not the FC autopilot: no proof
        peer.send(link, simstate(other), mav=other)
    assert link.state.snapshot(clock()).mode == "GUIDED"  # the link is up and receiving

    results = write_everything(link)
    assert results == [False] * 5
    assert link.blocked_writes == 5  # setpoint, arm (takeoff never tried), RTL, LAND, tone
    assert peer.drain() == b""  # zero bytes; SITL needs no companion HEARTBEAT (sitl.py)
    assert not link.sitl_proven

    peer.send(link, sim_state() if proof == "SIM_STATE" else simstate())
    assert link.sitl_proven
    requested = parse_frames(peer.drain())
    assert commands(requested) == [SET_INTERVAL] * 7
    intervals = {int(f.param1): f.param2 for f in requested}
    assert intervals[mavlink2.MAVLINK_MSG_ID_ATTITUDE] == pytest.approx(20_000)  # 50 Hz [F2]
    assert set(intervals) == {
        mavlink2.MAVLINK_MSG_ID_ATTITUDE,
        mavlink2.MAVLINK_MSG_ID_HEARTBEAT,
        mavlink2.MAVLINK_MSG_ID_EXTENDED_SYS_STATE,
        mavlink2.MAVLINK_MSG_ID_GLOBAL_POSITION_INT,
        mavlink2.MAVLINK_MSG_ID_LOCAL_POSITION_NED,
        mavlink2.MAVLINK_MSG_ID_SYS_STATUS,
        mavlink2.MAVLINK_MSG_ID_RC_CHANNELS,
    }

    assert write_everything(link) == [True] * 5
    assert types(parse_frames(peer.drain())) == [
        "SET_POSITION_TARGET_LOCAL_NED",
        "COMMAND_LONG",  # arm
        "COMMAND_LONG",  # takeoff
        "COMMAND_LONG",  # RTL
        "COMMAND_LONG",  # LAND
        "PLAY_TUNE",
    ]

    link.connect(timeout_s=2.0)  # reconnect: the proof resets
    peer.accept()
    assert not link.sitl_proven
    before = link.blocked_writes
    peer.send(link, heartbeat(GUIDED))
    assert write_everything(link) == [False] * 5
    assert link.blocked_writes == before + 5
    assert peer.drain() == b""


def test_f1b_udpin_receive_only_until_proof() -> None:
    """[F1] (a)/(b) over UDP: a ``udpin`` link writes nothing before the proof, learns its peer
    from the FC's datagrams, and after the SITL proof answers that peer."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    link = FcLink(f"udpin:127.0.0.1:{port}", StepClock(), LinkConfig(setpoints_enabled=True))
    link.connect(timeout_s=1.0)
    fc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fc_sock.bind(("127.0.0.1", 0))
    try:
        stop = VelocityCommand(vn=0.0, ve=0.0, vd=0.0, yaw_rate=0.0)
        assert not link.send_velocity(stop) and link.blocked_writes == 1
        for msg in (heartbeat(GUIDED), simstate()):
            fc_sock.sendto(bytes(msg.pack(FC)), ("127.0.0.1", port))
        fd = link.fileno()
        assert fd is not None
        for _ in range(100):
            select.select([fd], [], [], 0.05)
            link.poll()
            if link.rx_frames >= 2:
                break
        assert link.sitl_proven
        assert link.send_velocity(stop)
        got = []
        while select.select([fc_sock], [], [], 0.05)[0]:
            got.extend(parse_frames(fc_sock.recv(65536)))
        assert types(got) == ["COMMAND_LONG"] * 7 + ["SET_POSITION_TARGET_LOCAL_NED"]
    finally:
        fc_sock.close()
        link.close()


def test_f1_udp_proof_not_peer_bound() -> None:
    """[F1] (b), finding F1-UDP-PROOF-NOT-PEER-BOUND: on ``udpin`` the proof binds to the write
    peer. Sender A (a real-FC bridge) speaks first; a SIMSTATE from sender B (a SITL on another
    source port, same sys1/comp1) is dropped before parsing, ingesting, or recording, and is
    counted. The link stays unproven and writes zero bytes to A and to B."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    buf = io.StringIO()
    cfg = LinkConfig(setpoints_enabled=True)
    link = FcLink(f"udpin:127.0.0.1:{port}", StepClock(), cfg, recorder=Recorder(buf))
    link.connect(timeout_s=1.0)
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        fd = link.fileno()
        assert fd is not None

        def pump_until(done: Any) -> None:
            for _ in range(100):
                select.select([fd], [], [], 0.05)
                link.poll()
                if done():
                    return
            raise AssertionError("fc_link did not read the datagram")

        hb = bytes(heartbeat(GUIDED).pack(FC))
        a.sendto(hb, ("127.0.0.1", port))
        pump_until(lambda: link.rx_frames >= 1)  # A is now the write peer
        b.sendto(bytes(simstate().pack(FC)), ("127.0.0.1", port))
        pump_until(lambda: link.rx_frames + link.foreign_datagrams >= 2)  # B read or dropped

        assert not link.sitl_proven
        assert (link.rx_frames, link.foreign_datagrams) == (1, 1)  # B's SIMSTATE never parsed
        assert not link.send_velocity(VelocityCommand(vn=1.0, ve=0.0, vd=0.0, yaw_rate=0.0))
        assert not link.request(FcRequest(kind=FcRequestKind.ARM_AND_TAKEOFF, value=10.0))
        assert link.blocked_writes == 2  # setpoint, arm (takeoff never tried)
        assert select.select([a, b], [], [], 0.05)[0] == []  # zero bytes to A and to B
        records = read_records(buf.getvalue().splitlines())
        mav = [(r.direction, r.raw) for r in records if r.stream is Stream.MAVLINK]
        assert mav == [("rx", hb)]  # only A's HEARTBEAT was recorded
    finally:
        a.close()
        b.close()
        link.close()


# ---------------------------------------------------------------------------
# [F5]: the setpoint gate
# ---------------------------------------------------------------------------


def test_f5_gate_locked_by_default() -> None:
    """[F5]: locked unless an explicit configuration value enables it."""
    assert LinkConfig().setpoints_enabled is False
    assert FcLink("tcp:127.0.0.1:5762", StepClock(), LinkConfig()).gate_state is P.GateState.LOCKED


@pytest.mark.parametrize("enabled", [False, True])
def test_f5_locked_gate_provably_blocks_sends(peer: MavPeer, enabled: bool) -> None:
    """[F5]: with the gate locked, velocity sends and arm-and-takeoff write zero bytes and are
    counted; the same calls with the gate enabled put the frames on the wire."""
    clock = StepClock()
    link = connect(peer, clock, enabled=enabled)
    peer.send(link, heartbeat(GUIDED))
    prove(peer, link)
    n = 5
    for i in range(n):
        clock.t += 100
        link.send_velocity(VelocityCommand(vn=1.0 + i, ve=0.0, vd=0.0, yaw_rate=0.0))
    link.request(FcRequest(kind=FcRequestKind.ARM_AND_TAKEOFF, value=10.0))
    frames = parse_frames(peer.drain())
    setpoints = [f for f in frames if f.get_type() == "SET_POSITION_TARGET_LOCAL_NED"]
    arm_takeoff = [c for c in commands(frames) if c in (ARM_DISARM, TAKEOFF)]
    if enabled:
        assert len(setpoints) == n and arm_takeoff == [ARM_DISARM, TAKEOFF]
        assert link.blocked_count == 0
        assert link.last_setpoint_t == clock.t
        assert link.health_packet().gate_state is P.GateState.ENABLED
    else:
        assert setpoints == [] and arm_takeoff == []
        assert frames == []
        assert link.blocked_count == n + 1
        assert link.last_setpoint_t is None
        assert link.health_packet().gate_state is P.GateState.LOCKED


# ---------------------------------------------------------------------------
# [F6]: setpoint form and hard limits
# ---------------------------------------------------------------------------


def test_f6_every_setpoint_is_local_ned_mask_1479_and_limited(peer: MavPeer) -> None:
    """[F6]: every setpoint frame is LOCAL_NED with type_mask 1479; the horizontal vector is
    scaled to v_xy_hard keeping its direction; vd and yaw rate are clamped."""
    clock = StepClock()
    link = connect(peer, clock, enabled=True)
    prove(peer, link)
    cmds = [
        VelocityCommand(vn=2.0, ve=0.0, vd=0.0, yaw_rate=0.0),
        VelocityCommand(vn=6.0, ve=8.0, vd=3.0, yaw_rate=3.0),  # |h| = 10 -> 5
        VelocityCommand(vn=-3.0, ve=-4.0, vd=-2.5, yaw_rate=-3.0),  # |h| = 5: unchanged
        VelocityCommand(vn=0.0, ve=-9.0, vd=1.0, yaw_rate=0.5),
    ]
    for c in cmds:
        assert link.send_velocity(c)
    sent = [
        f for f in parse_frames(peer.drain()) if f.get_type() == "SET_POSITION_TARGET_LOCAL_NED"
    ]
    assert len(sent) == len(cmds)
    assert {f.type_mask for f in sent} == {1479}  # the contract's value, not our constant
    assert TYPE_MASK_VELOCITY_YAW_RATE == 0x05C7
    assert {f.coordinate_frame for f in sent} == {mavlink2.MAV_FRAME_LOCAL_NED}
    got = [(f.vx, f.vy, f.vz, f.yaw_rate) for f in sent]
    want = [
        (2.0, 0.0, 0.0, 0.0),
        (3.0, 4.0, 2.0, math.pi / 2),
        (-3.0, -4.0, -2.0, -math.pi / 2),
        (0.0, -5.0, 1.0, 0.5),
    ]
    for g, w in zip(got, want, strict=True):
        assert g == pytest.approx(w, abs=1e-6)  # float32 on the wire


def test_f6_horizontal_limit_keeps_direction_and_discriminates() -> None:
    """[F6]: the limit is a norm scale (direction kept), not a per-axis clamp; at the limit
    nothing changes, just above it the vector is scaled."""
    cfg = LinkConfig()
    at = clamp_velocity(VelocityCommand(vn=3.0, ve=4.0, vd=0.0, yaw_rate=0.0), cfg)
    assert (at.vn, at.ve) == (3.0, 4.0)
    over = clamp_velocity(VelocityCommand(vn=3.0006, ve=4.0008, vd=0.0, yaw_rate=0.0), cfg)
    assert math.hypot(over.vn, over.ve) == pytest.approx(cfg.v_xy_hard)
    assert over.ve / over.vn == pytest.approx(4.0 / 3.0)
    diag = clamp_velocity(VelocityCommand(vn=10.0, ve=1.0, vd=0.0, yaw_rate=0.0), cfg)
    assert diag.ve / diag.vn == pytest.approx(0.1)  # a per-axis clamp would give (5, 1)
    with pytest.raises(ValueError):
        clamp_velocity(VelocityCommand(vn=math.nan, ve=0.0, vd=0.0, yaw_rate=0.0), cfg)


# ---------------------------------------------------------------------------
# [M2a] vehicle predicates, [F3] staleness, [G1] attitude sampling
# ---------------------------------------------------------------------------


def test_m2a_vehicle_predicates_from_mavlink_frames() -> None:
    """[M2a]: snapshot fields from real frames; unknown before the first frames; only the FC
    autopilot counts; battery -1 is unknown; everything but the link is unknown while it is
    down, and a mode seen before a link loss does not come back with the link ([F9])."""
    vs = VehicleState(LinkConfig())
    s0 = vs.snapshot(0)
    assert (s0.fc_link_up, s0.mode, s0.armed) == (False, None, None)
    assert s0.landed_state is LandedState.UNDEFINED and not s0.on_ground and not s0.airborne
    assert (s0.rel_alt_m, s0.home_dist_m, s0.battery_pct, s0.attitude_age_ms) == (
        None,
        None,
        None,
        None,
    )
    assert s0.attitude_degraded and not s0.rc_seen

    t = 5_000
    ingest(
        vs,
        t,
        heartbeat(GUIDED, armed=True),
        FC.extended_sys_state_encode(0, mavlink2.MAV_LANDED_STATE_IN_AIR),
        FC.global_position_int_encode(0, 0, 0, 0, 12_345, 0, 0, 0, 0),
        FC.local_position_ned_encode(0, 3.0, 4.0, -12.0, 1.5, 0.0, 0.0),
        sys_status(-1),
        rc(1500),
        attitude(),
    )
    s = vs.snapshot(t)
    assert (s.fc_link_up, s.mode, s.armed) == (True, "GUIDED", True)
    assert s.airborne and not s.on_ground
    assert s.rel_alt_m == pytest.approx(12.345)
    assert s.home_dist_m == pytest.approx(5.0)
    assert s.battery_pct is None  # -1: unknown
    assert s.rc_seen and not s.attitude_degraded
    assert vs.velocity_ned(t) == pytest.approx((1.5, 0.0, 0.0))

    ingest(vs, t, sys_status(57), FC.extended_sys_state_encode(0, 1))
    ingest(vs, t, heartbeat(LAND, mav=COMPANION_ECHO), mav=COMPANION_ECHO)
    ingest(vs, t, heartbeat(RTL, mav=GCS), mav=GCS)
    s = vs.snapshot(t)
    assert s.battery_pct == 57.0 and s.on_ground
    assert s.mode == "GUIDED" and vs.foreign_frames == 2  # other components never set the mode

    bound = LinkConfig().link_bound_ms
    assert vs.snapshot(t + bound).fc_link_up  # "within" the bound: up
    down = vs.snapshot(t + bound + 1)
    assert not down.fc_link_up
    assert (down.mode, down.armed, down.rel_alt_m, down.battery_pct, down.rc_seen) == (
        None,
        None,
        None,
        None,
        False,
    )
    assert down.landed_state is LandedState.UNDEFINED

    t2 = t + 3_000
    ingest(vs, t2, attitude())  # the link returns without a HEARTBEAT yet
    s2 = vs.snapshot(t2)
    assert s2.fc_link_up and s2.mode is None and s2.armed is None and s2.rel_alt_m is None
    ingest(vs, t2 + 10, heartbeat(LOITER))
    s3 = vs.snapshot(t2 + 10)
    assert s3.mode == "LOITER" and s3.armed is False


def test_f3_attitude_staleness_bound_discriminates() -> None:
    """[F3]: degraded when the newest ATTITUDE is older than attitude_bound_ms; age equal to the
    bound is fresh, bound + 1 is degraded; no sample is degraded."""
    cfg = LinkConfig()
    vs = VehicleState(cfg)
    assert vs.attitude_degraded(0) and vs.attitude_age_ms(0) is None
    ingest(vs, 7_000, attitude())
    fresh = vs.snapshot(7_000 + cfg.attitude_bound_ms)
    stale = vs.snapshot(7_000 + cfg.attitude_bound_ms + 1)
    assert (fresh.attitude_age_ms, fresh.attitude_degraded) == (cfg.attitude_bound_ms, False)
    assert (stale.attitude_age_ms, stale.attitude_degraded) == (cfg.attitude_bound_ms + 1, True)


def test_g1_attitude_interpolation_yaw_wrap_and_hold() -> None:
    """[G1]: linear roll/pitch and unwrapped yaw between samples (across +-pi), the nearest
    sample held outside the span, None past attitude_bound_ms from the sample used."""
    vs = VehicleState(LinkConfig())
    ya, yb = math.pi - 0.1, -math.pi + 0.1  # 0.2 rad clockwise across the wrap
    ingest(vs, 1_000, attitude(roll=0.0, pitch=0.1, yaw=ya, boot=500))
    ingest(vs, 1_020, attitude(roll=0.2, pitch=0.3, yaw=yb, boot=520))

    mid = vs.attitude_at(1_010)
    assert mid is not None and mid.t_ms == 1_010 and mid.time_boot_ms == 510
    assert (mid.roll, mid.pitch) == pytest.approx((0.1, 0.2))
    assert abs(mid.yaw) == pytest.approx(math.pi, abs=1e-6)  # not the naive average, 0
    q = vs.attitude_at(1_005)
    assert q is not None and q.yaw == pytest.approx(math.pi - 0.05, abs=1e-6)

    held_after = vs.attitude_at(1_120)
    assert held_after is not None and held_after.t_ms == 1_020
    assert held_after.yaw == pytest.approx(yb, abs=1e-6)
    assert vs.attitude_at(1_121) is None  # nothing is extrapolated or held past the bound
    held_before = vs.attitude_at(900)
    assert held_before is not None and held_before.t_ms == 1_000
    assert vs.attitude_at(899) is None

    ingest(vs, 1_320, attitude())  # a 300 ms gap inside the span
    assert vs.attitude_at(1_120) is not None  # 100 ms from the sample used
    assert vs.attitude_at(1_170) is None  # 150 ms from both

    for k in range(1, 151):  # 3 s at 50 Hz: at least 1 s is kept ([F2])
        ingest(vs, 1_320 + 20 * k, attitude(yaw=0.001 * k))
    newest = 1_320 + 20 * 150
    assert vs.attitude_at(newest - 1_000) is not None


# ---------------------------------------------------------------------------
# [F7]: radio approve detector
# ---------------------------------------------------------------------------


def _rc_seq(vs: VehicleState, seq: list[tuple[int, int] | tuple[int, int, int]]) -> list[int]:
    for item in seq:
        t, value, *count = item
        ingest(vs, t, rc(value, chancount=count[0] if count else 16))
    return vs.take_approvals()


def test_f7_first_sample_high_is_not_an_approve() -> None:
    """[F7]: the detector starts disarmed; a switch already high never approves."""
    vs = VehicleState(LinkConfig())
    assert _rc_seq(vs, [(1_000, 1900), (1_100, 1900), (1_200, 1900)]) == []


def test_f7_low_to_high_fires_once_and_threshold_discriminates() -> None:
    """[F7]: low then high emits one approve at the high sample's receive time and disarms;
    holding high adds nothing; at approve_pwm_high fires, one below does not."""
    vs = VehicleState(LinkConfig())
    got = _rc_seq(vs, [(1_000, 1100), (1_100, 1900), (1_200, 1900), (1_300, 1100), (1_400, 1700)])
    assert got == [1_100, 1_400]
    assert rc_approve_cmd_id(got[0]) == "rc:approve:1100"
    assert vs.take_approvals() == []
    assert _rc_seq(vs, [(1_500, 1100), (1_600, 1699), (1_700, 1699)]) == []


def _keep_link_up(vs: VehicleState, t0: int, t1: int) -> None:
    for t in range(t0, t1, 100):  # the FC link stays up while the RC goes quiet
        ingest(vs, t, attitude())


def test_f7_dropout_recovery_high_is_not_an_approve() -> None:
    """[F7]: rc_seen false (no RC_CHANNELS with chancount > 0 for longer than the bound, or a
    chancount 0 sample) disarms the detector even while the FC link stays up, so recovering
    with the switch high is not an approve; a gap of exactly the bound is still seen."""
    vs = VehicleState(LinkConfig())
    assert _rc_seq(vs, [(1_000, 1100)]) == []
    _keep_link_up(vs, 1_050, 2_001)
    assert vs.snapshot(2_001).fc_link_up and not vs.snapshot(2_001).rc_seen
    assert _rc_seq(vs, [(2_001, 1900)]) == []  # 1001 ms without RC
    assert _rc_seq(vs, [(3_000, 1100), (3_100, 1100, 0), (3_200, 1900)]) == []  # chancount 0
    assert _rc_seq(vs, [(4_000, 1100)]) == []
    _keep_link_up(vs, 4_050, 5_000)
    assert _rc_seq(vs, [(5_000, 1900)]) == [5_000]  # 1000 ms: still seen


def test_f7_invalid_samples_disarm() -> None:
    """[F7]: a value outside [valid_min, valid_max] or a chancount below the approve channel
    is invalid and disarms; the bounds themselves are valid."""
    vs = VehicleState(LinkConfig())
    assert _rc_seq(vs, [(1_000, 1100), (1_100, 2201), (1_200, 1900)]) == []
    assert _rc_seq(vs, [(2_000, 1100), (2_100, 799), (2_200, 1900)]) == []
    assert _rc_seq(vs, [(3_000, 1100), (3_100, 1100, 7), (3_200, 1900)]) == []
    assert _rc_seq(vs, [(4_000, 800), (4_100, 2200)]) == [4_100]


# ---------------------------------------------------------------------------
# [F9] exits, [F8] tones
# ---------------------------------------------------------------------------


def test_f9_exit_requests_only_while_link_up_and_guided(peer: MavPeer) -> None:
    """[F9]: RTL/LAND bytes only while the link is up and the newest HEARTBEAT is GUIDED;
    otherwise zero bytes and the attempt is counted, including after a link loss."""
    clock = StepClock()
    link = connect(peer, clock, enabled=False)  # exits are not behind the setpoint gate
    peer.send(link, heartbeat(LOITER))
    prove(peer, link)
    rtl = FcRequest(kind=FcRequestKind.MODE_RTL)
    land = FcRequest(kind=FcRequestKind.MODE_LAND)

    assert not link.request(rtl)  # LOITER: the pilot's mode is never overridden
    peer.send(link, heartbeat(GUIDED))
    assert link.request(rtl) and link.request(land)
    sent = [f for f in parse_frames(peer.drain()) if f.get_type() == "COMMAND_LONG"]
    assert [(f.command, int(f.param2)) for f in sent] == [(SET_MODE, RTL), (SET_MODE, LAND)]

    clock.t += LinkConfig().link_bound_ms + 1  # link down
    assert not link.request(rtl)
    peer.send(link, attitude())  # link back, newest HEARTBEAT predates the loss
    assert not link.request(land)
    assert peer.drain() == b""
    assert link.exit_blocked_count == 3


def test_f8_tones_known_and_unknown(peer: MavPeer) -> None:
    """[F8]: PLAY_TUNE with the tone table's tune; an unknown name sends nothing."""
    clock = StepClock()
    link = connect(peer, clock, enabled=False)
    prove(peer, link)
    assert not link.request(FcRequest(kind=FcRequestKind.TONE, value="SEARCH"))
    assert not link.request(FcRequest(kind=FcRequestKind.TONE, value="no-such-tone"))
    assert peer.drain() == b"" and link.unknown_tones == 2
    assert link.request(FcRequest(kind=FcRequestKind.TONE, value="ABORT"))
    (tune,) = parse_frames(peer.drain())
    assert tune.get_type() == "PLAY_TUNE" and tune.tune == TONES["ABORT"]
    names = ("ACQUIRING", "ENGAGED", "TOUCH", "LOST", "RETURN", "ABORT")  # contract §9
    assert set(TONES) == set(names) and len(set(TONES.values())) == len(names)
    assert all(len(v) <= 30 for v in TONES.values())  # PLAY_TUNE.tune is char[30]


# ---------------------------------------------------------------------------
# [F10] MAVLink log, [P4] health packet
# ---------------------------------------------------------------------------


def test_f10_recorder_gets_every_rx_and_tx_frame_raw(peer: MavPeer) -> None:
    """[F10], [R2]: every frame received and sent reaches the recorder as its exact bytes,
    stamped with the injected clock."""
    buf = io.StringIO()
    clock = StepClock(t=20_000)
    link = connect(peer, clock, enabled=True, recorder=Recorder(buf))
    peer.send(link, heartbeat(GUIDED), attitude())
    clock.t = 20_050
    prove(peer, link)
    clock.t = 20_100
    link.send_velocity(VelocityCommand(vn=1.0, ve=0.5, vd=0.0, yaw_rate=0.1))
    peer.drain()
    records = list(read_records(buf.getvalue().splitlines()))
    rx = [r for r in records if r.stream is Stream.MAVLINK and r.direction == "rx"]
    tx = [r for r in records if r.stream is Stream.MAVLINK and r.direction == "tx"]
    assert [r.raw for r in rx] == peer.sent_frames
    assert b"".join(r.raw for r in tx) == bytes(peer.captured)
    assert [r.t_rx for r in rx] == [20_000, 20_000, 20_050]
    assert {r.t_rx for r in tx[:-1]} == {20_050} and tx[-1].t_rx == 20_100
    assert types(parse_frames(tx[-1].raw)) == ["SET_POSITION_TARGET_LOCAL_NED"]


def test_p4_health_packet_fields(peer: MavPeer) -> None:
    """[P4], [F4]: health fields from the injected clock; it round-trips the frozen wire;
    maybe_health publishes once per health_period_ms and records what it publishes."""
    buf = io.StringIO()
    clock = StepClock(t=30_000)
    link = connect(peer, clock, enabled=True, recorder=Recorder(buf))
    h0 = link.health_packet()
    assert (h0.t, h0.attitude_age_ms, h0.fc_link_up, h0.rc_seen, h0.last_setpoint_t) == (
        30_000,
        None,
        False,
        False,
        None,
    )
    assert h0.gate_state is P.GateState.ENABLED

    peer.send(link, attitude(), rc(1500))
    prove(peer, link)
    clock.t = 30_040
    link.send_velocity(VelocityCommand(vn=0.0, ve=0.0, vd=0.0, yaw_rate=0.0))
    clock.t = 30_070
    h = link.health_packet()
    assert (h.t, h.attitude_age_ms, h.fc_link_up, h.rc_seen, h.last_setpoint_t) == (
        30_070,
        70,
        True,
        True,
        30_040,
    )
    assert P.decode(P.PacketKind.FC_LINK_HEALTH, P.encode(h)) == h

    first = link.maybe_health()
    assert first is not None and first.t == 30_070
    clock.t += LinkConfig().health_period_ms - 1
    assert link.maybe_health() is None
    clock.t += 1
    second = link.maybe_health()
    assert second is not None and second.t == 30_070 + LinkConfig().health_period_ms
    recorded = [r.packet for r in read_records(buf.getvalue().splitlines()) if r.packet]
    assert recorded == [first, second]


def test_link_config_meta_round_trip() -> None:
    """[R2]: the link constants travel in meta.config; from_obj is strict (no coercion)."""
    cfg = LinkConfig(setpoints_enabled=True, attitude_bound_ms=120)
    obj = cfg.to_obj()
    assert LinkConfig.from_obj(obj) == cfg
    with pytest.raises(ValueError):
        LinkConfig.from_obj({**obj, "attitude_bound_ms": True})
    with pytest.raises(ValueError):
        LinkConfig.from_obj({**obj, "setpoints_enabled": 1})
    with pytest.raises(ValueError):
        LinkConfig.from_obj({k: v for k, v in obj.items() if k != "v_xy_hard"})
