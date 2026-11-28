"""F series, fast tier: [F1] part (c), the real FC bench mode (DRONE_CONTRACTS_D0.md §6;
decision B-D1, §11.4).

No hardware and no mocks of our own code. The serial device is a
pseudo-terminal pair (``os.openpty``): fc_link opens the slave by its path, as
it opens a UART, and the test holds the master. The test plays the FC with
real MAVLink2 frames (pymavlink-encoded, system 1, component 1) and reads
every byte fc_link writes. Time is an injected step clock ([F11]).

The loopback half of [F1] (c), with its SITL-mode control, is in
``test_f_fc_link.py`` (it owns the TCP peer).
"""

from __future__ import annotations

import io
import os
import select
import socket
import tty
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone import packets as P
from skyweave2.drone.companiond import (
    UI_ENV_VAR,
    Companion,
    CompanionConfig,
    config_from_args,
    main,
)
from skyweave2.drone.core import CompanionCore, CoreConfig, replay
from skyweave2.drone.fc_link import EndpointRefused, FcLink, check_endpoint
from skyweave2.drone.recording import Recorder, Stream, read_records
from skyweave2.drone.types import FcRequest, FcRequestKind, VelocityCommand
from skyweave2.drone.vehicle_state import LinkConfig, parse_frames

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "r_touch_trial.jsonl"

GUIDED, LOITER = 4, 5  # ArduCopter custom_mode numbers (fixture facts)
BAUD = 115200  # a pty ignores the rate; the bench UART runs 921600
T0 = 10_000  # board ms
UI_TOKEN = "ui-test-token"  # not a credential; must never reach a recording or a log

FC = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)

ARM_DISARM = mavlink2.MAV_CMD_COMPONENT_ARM_DISARM  # 400
TAKEOFF = mavlink2.MAV_CMD_NAV_TAKEOFF  # 22
SET_MODE = mavlink2.MAV_CMD_DO_SET_MODE  # 176
SET_INTERVAL = mavlink2.MAV_CMD_SET_MESSAGE_INTERVAL  # 511


class StepClock:
    """Injected board clock ([F11]); tests move it by hand."""

    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def heartbeat(mode: int, armed: bool = False) -> Any:
    base = mavlink2.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    if armed:
        base |= mavlink2.MAV_MODE_FLAG_SAFETY_ARMED
    return FC.heartbeat_encode(
        mavlink2.MAV_TYPE_QUADROTOR,
        mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
        base,
        mode,
        mavlink2.MAV_STATE_ACTIVE,
    )


def attitude(yaw: float = 0.0, boot: int = 0) -> Any:
    return FC.attitude_encode(boot, 0.0, 0.0, yaw, 0.0, 0.0, 0.0)


def rc(ch8: int, chancount: int = 16) -> Any:
    chans = [1500] * 18
    chans[7] = ch8
    return FC.rc_channels_encode(0, chancount, *chans, 255)


def commands(frames: list[Any]) -> list[int]:
    return [f.command for f in frames if f.get_type() == "COMMAND_LONG"]


def types(frames: list[Any]) -> list[str]:
    return [f.get_type() for f in frames]


def pump(link: FcLink, want: int) -> None:
    """Poll ``link`` until it has read ``want`` frames in all."""
    for _ in range(200):
        fd = link.fileno()
        assert fd is not None
        select.select([fd], [], [], 0.05)
        link.poll()
        if link.rx_frames >= want:
            return
    raise AssertionError("fc_link did not read the FC's frames")


class PtyFc:
    """The FC end of a serial line: the master of a pseudo-terminal pair.

    ``device`` is the slave's path (``/dev/pts/N``), which fc_link opens like a
    UART. Closing both ends here is the cable being pulled: the path goes away
    and a reader of the slave gets a hangup.
    """

    def __init__(self) -> None:
        self.master, self._slave = os.openpty()
        tty.setraw(self._slave)  # a raw line from the start: no echo, no CR/NL mapping
        os.set_blocking(self.master, False)
        self.device = os.ttyname(self._slave)
        self.endpoint = f"serial:{self.device}:{BAUD}"
        self._open = True

    def write(self, *msgs: Any) -> list[bytes]:
        raws = [bytes(m.pack(FC)) for m in msgs]
        os.write(self.master, b"".join(raws))
        return raws

    def send(self, link: FcLink, *msgs: Any) -> list[bytes]:
        """Send frames as the FC and pump them into ``link``."""
        want = link.rx_frames + len(msgs)
        raws = self.write(*msgs)
        pump(link, want)
        return raws

    def drain(self) -> bytes:
        """Everything fc_link wrote since the last drain (waits briefly for stragglers)."""
        got = bytearray()
        while select.select([self.master], [], [], 0.05)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:  # the slave side closed
                break
            if not chunk:
                break
            got += chunk
        return bytes(got)

    def unplug(self) -> None:
        if self._open:
            os.close(self.master)
            os.close(self._slave)
            self._open = False


@pytest.fixture()
def fc() -> Iterator[PtyFc]:
    f = PtyFc()
    try:
        yield f
    finally:
        f.unplug()


def real_link(fc: PtyFc, clock: StepClock, recorder: Recorder | None = None) -> FcLink:
    link = FcLink(fc.endpoint, clock, LinkConfig(real_fc=True), recorder=recorder)
    link.connect(timeout_s=1.0)
    return link


# ---------------------------------------------------------------------------
# [F1] (c): the endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "serial:/dev/ttyS4:921600",
        "serial:/dev/ttyACM0:115200",
        "serial:/dev/serial/by-path/platform-xhci-hcd.1.auto-usb-0:1:1.0:57600",
    ],
)
def test_f1c_serial_endpoint_refused_without_real_fc_accepted_with_it(endpoint: str) -> None:
    """[F1] (a)/(c), B-D1: ``serial:<device>:<baud>`` is refused unless ``real_fc`` is
    explicitly True; with it the link is built, and nothing is opened at construction."""
    with pytest.raises(EndpointRefused):
        check_endpoint(endpoint)
    with pytest.raises(EndpointRefused):
        check_endpoint(endpoint, real_fc=1)  # type: ignore[arg-type]  # not an explicit True
    with pytest.raises(EndpointRefused):
        FcLink(endpoint, StepClock(), LinkConfig())
    check_endpoint(endpoint, real_fc=True)
    link = FcLink(endpoint, StepClock(), LinkConfig(real_fc=True))
    assert link.real_fc and not link.connected  # the device does not exist; none was opened


@pytest.mark.parametrize(
    "endpoint",
    [
        "/dev/ttyS4",
        "/dev/ttyS4:921600",
        "serial:ttyS4:921600",
        "serial:/dev/ttyS4",
        "serial:/dev/ttyS4:",
        "serial::921600",
        "serial:/dev/ttyS4:9600",
        "serial:/dev/ttyS4:921601",
        "tcp:192.168.1.10:5762",
        "udpout:10.0.0.2:14550",
        "tcp:localhost:5762",
        "COM3",
        "",
    ],
)
def test_f1c_real_fc_still_refuses_other_endpoints(endpoint: str) -> None:
    """[F1] (a)/(c), B-D1: ``real_fc`` adds the serial form and nothing else. A bare
    device path, a relative device, an unlisted baud rate, and a non-loopback host are
    still refused at construction."""
    with pytest.raises(EndpointRefused):
        check_endpoint(endpoint, real_fc=True)
    with pytest.raises(EndpointRefused):
        FcLink(endpoint, StepClock(), LinkConfig(real_fc=True))


# ---------------------------------------------------------------------------
# [F1] (c): the setpoint gate cannot be enabled
# ---------------------------------------------------------------------------


def test_f1c_real_fc_with_setpoints_enabled_is_refused_at_construction() -> None:
    """[F1] (c), [F5], B-D1: ``real_fc`` with setpoints enabled is refused: by the link
    configuration, by fc_link's own check on a configuration forced past it, and when a
    recording's ``meta.config`` claims the pair."""
    with pytest.raises(ValueError, match="real_fc with setpoints_enabled is refused"):
        LinkConfig(real_fc=True, setpoints_enabled=True)
    forced = LinkConfig(real_fc=True)
    object.__setattr__(forced, "setpoints_enabled", True)  # past LinkConfig's own check
    with pytest.raises(ValueError, match="real_fc with setpoints_enabled is refused"):
        FcLink("tcp:127.0.0.1:5762", StepClock(), forced)
    with pytest.raises(ValueError, match="real_fc with setpoints_enabled is refused"):
        LinkConfig.from_obj({**LinkConfig(real_fc=True).to_obj(), "setpoints_enabled": True})
    with pytest.raises(ValueError):
        LinkConfig(real_fc=1)  # type: ignore[arg-type]  # only a bool is a mode


def test_f1c_companiond_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[F1] (c), [F5], B-D1: ``--real-fc`` sets the mode; ``--real-fc`` with
    ``--enable-setpoints`` is refused by the argument handling, and so is a serial
    endpoint without ``--real-fc``. Both refusals say why, exit 2, and open nothing."""
    rec = tmp_path / "bench.jsonl"
    serial = ["--fc", "serial:/dev/ttyS4:921600", "--record", str(rec)]
    bench = config_from_args(["--real-fc", *serial])
    assert bench.core.link.real_fc is True and bench.core.gate is P.GateState.LOCKED
    sitl = config_from_args(["--fc", "tcp:127.0.0.1:5762", "--record", str(rec)])
    assert sitl.core.link.real_fc is False
    with pytest.raises(ValueError, match="real_fc with setpoints_enabled is refused"):
        config_from_args(["--real-fc", "--enable-setpoints", *serial])

    monkeypatch.setenv(UI_ENV_VAR, UI_TOKEN)
    with caplog.at_level("ERROR"):
        assert main(["--real-fc", "--enable-setpoints", *serial]) == 2
        assert "cannot be unlocked on a real FC" in caplog.text
        caplog.clear()
        assert main(serial) == 2  # a serial endpoint without --real-fc
        assert "needs the real_fc option" in caplog.text
    assert UI_TOKEN not in caplog.text
    assert not rec.exists()


# ---------------------------------------------------------------------------
# [F1] (c): what a real FC link writes, and what it never writes
# ---------------------------------------------------------------------------


def test_f1c_serial_link_asks_for_streams_and_never_writes_setpoint_arm_or_takeoff(
    fc: PtyFc,
) -> None:
    """[F1] (c), [F2], [F5], [F8], [F9], B-D1: on a serial device, with no SITL proof
    ever sent, fc_link writes the stream requests once the FC is heard, tones and exit
    requests. Velocity setpoints, arm and takeoff write zero bytes and are counted as
    blocked, as with the locked gate, even with the FC armed in GUIDED."""
    clock = StepClock()
    link = real_link(fc, clock)
    assert link.gate_state is P.GateState.LOCKED
    assert fc.drain() == b""  # nothing to ask before the FC is heard

    fc.send(link, heartbeat(GUIDED, armed=True))
    assert not link.sitl_proven  # no SIMSTATE / SIM_STATE came
    requested = parse_frames(fc.drain())
    assert commands(requested) == [SET_INTERVAL] * 7
    intervals = {int(f.param1): f.param2 for f in requested}
    assert intervals[mavlink2.MAVLINK_MSG_ID_ATTITUDE] == pytest.approx(20_000)  # 50 Hz [F2]
    assert mavlink2.MAVLINK_MSG_ID_RC_CHANNELS in intervals

    n = 3
    results = []
    for i in range(n):
        clock.t += 100
        results.append(
            link.send_velocity(VelocityCommand(vn=1.0 + i, ve=0.0, vd=0.0, yaw_rate=0.0))
        )
    results.append(link.request(FcRequest(kind=FcRequestKind.ARM_AND_TAKEOFF, value=10.0)))
    assert results == [False] * (n + 1)
    assert fc.drain() == b""  # zero bytes on the wire
    assert link.blocked_count == n + 1
    assert link.last_setpoint_t is None
    assert link.health_packet().gate_state is P.GateState.LOCKED

    assert link.request(FcRequest(kind=FcRequestKind.MODE_RTL))
    assert link.request(FcRequest(kind=FcRequestKind.MODE_LAND))
    assert link.request(FcRequest(kind=FcRequestKind.TONE, value="ENGAGED"))
    allowed = parse_frames(fc.drain())
    assert types(allowed) == ["COMMAND_LONG", "COMMAND_LONG", "PLAY_TUNE"]
    assert commands(allowed) == [SET_MODE, SET_MODE]
    assert {(f.get_srcSystem(), f.get_srcComponent()) for f in requested + allowed} == {(1, 191)}
    assert link.blocked_writes == 0 and link.blocked_count == n + 1


def test_f1c_streams_are_requested_again_when_the_fc_is_heard_after_a_link_loss(
    fc: PtyFc,
) -> None:
    """[F1] (c), [F2], B-D1: a UART does not close when the FC reboots, and a rebooted
    FC has forgotten the stream requests. They are written again when the FC is heard
    after more than ``link_bound_ms`` of silence, and not while the link stays up."""
    clock = StepClock()
    link = real_link(fc, clock)
    bound = link.config.link_bound_ms
    fc.send(link, heartbeat(LOITER))
    assert commands(parse_frames(fc.drain())) == [SET_INTERVAL] * 7

    clock.t += bound  # at the bound: the link was up all along
    fc.send(link, heartbeat(LOITER), attitude())
    assert fc.drain() == b""

    clock.t += bound + 1  # FC silent past the bound (power cycle)
    assert not link.health_packet().fc_link_up
    fc.send(link, heartbeat(LOITER))
    assert link.health_packet().fc_link_up
    assert commands(parse_frames(fc.drain())) == [SET_INTERVAL] * 7


# ---------------------------------------------------------------------------
# [F1] (c): frames from a serial device take the same path as SITL frames
# ---------------------------------------------------------------------------


def test_f1c_serial_frames_reach_the_same_parsing_path_as_sitl_frames(fc: PtyFc) -> None:
    """[F1] (c), [F2], [F3], [F7], [F10], [P4], B-D1: HEARTBEAT, ATTITUDE and RC_CHANNELS
    arriving on the serial device set link up, the mode, the attitude age (by the injected
    clock) and the radio approve detector, and are recorded as raw bytes. A frame the
    line delivers in two pieces is one frame."""
    clock = StepClock()
    buf = io.StringIO()
    link = real_link(fc, clock, Recorder(buf))
    before = link.health_packet()
    assert not before.fc_link_up and before.attitude_age_ms is None and not before.rc_seen

    raws = fc.send(link, heartbeat(LOITER), attitude(yaw=0.3, boot=5000), rc(1100))
    clock.t += 40
    health = link.health_packet()
    assert health.fc_link_up and health.rc_seen
    assert health.attitude_age_ms == 40  # [F3]
    snap = link.state.snapshot(clock())
    assert snap.mode == "LOITER" and snap.armed is False
    newest = link.state.newest_attitude()
    assert newest is not None
    assert (newest.t_ms, newest.time_boot_ms) == (T0, 5000)  # [F2]: receive time, FC time
    assert newest.yaw == pytest.approx(0.3)

    # [F7]: the switch goes high, in a frame split across two reads.
    high = bytes(rc(1900).pack(FC))
    os.write(fc.master, high[:9])
    fd = link.fileno()
    assert fd is not None and select.select([fd], [], [], 2.0)[0]
    assert link.poll() == 0 and link.rx_frames == 3
    os.write(fc.master, high[9:])
    pump(link, 4)
    assert link.state.take_approvals() == [clock.t]
    assert link.rx_bad == 0

    records = list(read_records(buf.getvalue().splitlines()))
    rx = [r.raw for r in records if r.stream is Stream.MAVLINK and r.direction == "rx"]
    assert rx == [*raws, high]  # [F10]: the FC's bytes, frame by frame


# ---------------------------------------------------------------------------
# [F1] (c): a serial device that is missing or goes away
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("noticed_by", ["read", "write"])
def test_f1c_unplugged_serial_device_closes_the_link_without_raising(
    fc: PtyFc, noticed_by: str
) -> None:
    """[F1] (c), B-D1, E1-D11: when the serial device goes away, the read path and the
    write path close the link and return; neither raises. A new connect to the missing
    device is an ``OSError`` for the caller's retry."""
    clock = StepClock()
    link = real_link(fc, clock)
    fc.send(link, heartbeat(GUIDED))
    fc.drain()

    fc.unplug()
    tone = FcRequest(kind=FcRequestKind.TONE, value="LOST")
    if noticed_by == "write":
        assert link.request(tone) is False
        assert not link.connected and link.blocked_writes == 1
    assert link.poll() == 0
    assert not link.connected and link.fileno() is None
    before = link.blocked_writes
    assert link.request(tone) is False and link.blocked_writes == before + 1
    assert link.maybe_health() is not None  # the health beat still runs; no heartbeat write
    assert link.blocked_writes == before + 1
    clock.t += link.config.link_bound_ms + 1
    assert not link.health_packet().fc_link_up
    with pytest.raises(OSError):
        link.connect(timeout_s=1.0)
    assert not link.connected


def _udp() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s


def test_f1c_companion_loop_survives_a_missing_and_an_unplugged_serial_device(
    tmp_path: Path,
) -> None:
    """[F1] (c), [C10], [R2], [R3], B-D1, E1-D11: the companion starts with the serial
    device missing, connects when it appears, reports the link down after the device
    is unplugged, and recovers (stream requests written again) when it returns; the loop
    never raises. The recording's ``meta.config`` says ``real_fc`` ran with the gate
    locked, holds no token, and replays."""
    dev = tmp_path / "ttyFC"
    rec = tmp_path / "bench.jsonl"
    clock = StepClock()
    sinks = [_udp(), _udp(), _udp()]
    config = CompanionConfig(
        fc_endpoint=f"serial:{dev}:{BAUD}",
        recording=rec,
        core=CoreConfig(link=LinkConfig(real_fc=True)),
        detection_port=0,
        command_port=0,
        track_port=sinks[0].getsockname()[1],
        mission_state_port=sinks[1].getsockname()[1],
        fc_link_health_port=sinks[2].getsockname()[1],
        ui_port=0,
    )
    plugged: list[PtyFc] = []

    def plug() -> PtyFc:
        f = PtyFc()
        plugged.append(f)
        os.symlink(f.device, dev)
        return f

    try:
        with Companion(config, UI_TOKEN, clock=clock) as comp:
            link = comp.fc_link

            def fc_comes_up(f: PtyFc) -> list[Any]:
                """The retry opens the device; the FC's frames bring the link up.
                Returns what the companion wrote."""
                clock.t += config.fc_retry_ms
                comp.run_once()
                assert link.connected
                want = link.rx_frames + 2
                f.write(heartbeat(LOITER), attitude())
                for _ in range(200):
                    comp.run_once(0.01)
                    if link.rx_frames >= want:
                        break
                else:
                    raise AssertionError("the companion did not read the FC's frames")
                assert link.health_packet().fc_link_up
                return parse_frames(f.drain())

            comp.start()  # the device is missing: logged and retried, not raised
            comp.run_once()
            assert not link.connected and not link.health_packet().fc_link_up

            first = plug()
            wrote = fc_comes_up(first)
            assert commands(wrote) == [SET_INTERVAL] * 7
            assert "HEARTBEAT" in types(wrote)  # the companion's, on the health beat

            first.unplug()
            dev.unlink()
            comp.run_once(0.01)  # reads the hangup
            assert not link.connected
            clock.t += config.core.link.link_bound_ms + 1
            comp.run_once()  # the retry finds no device
            assert not link.connected and not link.health_packet().fc_link_up

            wrote = fc_comes_up(plug())
            assert commands(wrote) == [SET_INTERVAL] * 7
            assert comp.input_errors == 0
    finally:
        for f in plugged:
            f.unplug()
        for s in sinks:
            s.close()

    text = rec.read_text(encoding="ascii")
    records = list(read_records(text.splitlines()))
    meta = records[0].config
    assert meta is not None
    assert meta["link"]["real_fc"] is True and meta["gate"] == "locked"  # [R2]: the mode
    assert UI_TOKEN not in text
    sent = [
        f
        for r in records
        if r.stream is Stream.MAVLINK and r.direction == "tx" and r.raw is not None
        for f in parse_frames(r.raw)
    ]
    assert set(types(sent)) == {"COMMAND_LONG", "HEARTBEAT"}  # [F10]; never a setpoint
    assert set(commands(sent)) == {SET_INTERVAL}  # never arm or takeoff
    assert replay(rec).mismatch() is None  # [R3]


# ---------------------------------------------------------------------------
# [R2]: meta.config says which mode ran
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("real_fc", [False, True])
def test_f1c_meta_config_says_which_mode_ran(real_fc: bool) -> None:
    """[F1] (c), [R2], B-D1: the recording's ``meta.config`` carries ``link.real_fc``,
    true or false, and a core rebuilt from it runs the same mode."""
    buf = io.StringIO()
    CompanionCore(CoreConfig(link=LinkConfig(real_fc=real_fc)), recorder=Recorder(buf))
    (meta,) = read_records(buf.getvalue().splitlines())
    assert meta.config is not None
    assert meta.config["link"]["real_fc"] is real_fc
    assert CoreConfig.from_obj(meta.config).link.real_fc is real_fc


def test_f1c_recording_from_before_b_d1_reads_as_sitl_mode() -> None:
    """[F1] (c), [R2], B-D1: a recording made before the option existed (the committed
    E1 fixture) has no ``real_fc`` key and reads as ``real_fc`` false; a ``real_fc``
    that is not a bool is refused."""
    old = next(iter(read_records(FIXTURE))).config
    assert old is not None and "real_fc" not in old["link"]
    assert CoreConfig.from_obj(old).link.real_fc is False
    with pytest.raises(ValueError):
        LinkConfig.from_obj({**LinkConfig().to_obj(), "real_fc": 1})
