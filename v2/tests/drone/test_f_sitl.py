"""F series, slow tier: fc_link v1 against the pinned ArduCopter 4.7.0 SITL.

Gate (brief work item 3): a scripted SITL run (arm, takeoff, velocity, land)
passes; injected attitude staleness trips the degraded path; sends are
provably blocked while the gate is locked.

fc_link talks to SITL SERIAL1 (the companion port, as wired on the FC); the
test plays the pilot on SERIAL0 (``sitl.PilotLink``): it moves the mode switch
into GUIDED, as the human does before LAUNCH (T03). The injected clock is
monotonic wall time because SITL runs at speedup 1 (``conftest.wall_clock``).
All waits are bounded process-control timeouts.
"""

from __future__ import annotations

import io
import select
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.fc_link import FcLink
from skyweave2.drone.packets import GateState
from skyweave2.drone.recording import Recorder, Stream, read_records
from skyweave2.drone.sitl import SitlInstance
from skyweave2.drone.types import FcRequest, FcRequestKind, VelocityCommand
from skyweave2.drone.vehicle_state import LinkConfig, parse_frames

pytestmark = [pytest.mark.slow, pytest.mark.sitl]

PROOF_TIMEOUT_S = 20.0
EKF_GPS_TIMEOUT_S = 90.0


def _frames(buf: io.StringIO, direction: str) -> list[Any]:
    out = []
    for rec in read_records(buf.getvalue().splitlines()):
        if rec.stream is Stream.MAVLINK and rec.direction == direction:
            out.extend(parse_frames(rec.raw or b""))
    return out


def _connect(sitl: SitlInstance, clock: Callable[[], int], *, enabled: bool, endpoint: str = ""):
    buf = io.StringIO()
    link = FcLink(
        endpoint or sitl.companion_endpoint,
        clock,
        LinkConfig(setpoints_enabled=enabled),
        recorder=Recorder(buf),
    )
    link.connect(timeout_s=5.0)
    assert sitl.pilot is not None
    assert sitl.pilot.wait_for(lambda: link.sitl_proven, PROOF_TIMEOUT_S, every=link.poll), (
        "no SIMSTATE on SERIAL1 receive-only"
    )
    return link, buf


def _wait_ekf_gps(sitl: SitlInstance, link: FcLink) -> None:
    """Until the EKF uses GPS, ArduCopter refuses to arm in GUIDED (sitl.py)."""
    pilot = sitl.pilot
    assert pilot is not None
    ok = pilot.wait_for(
        lambda: any("is using GPS" in s for s in pilot.statustext), EKF_GPS_TIMEOUT_S, link.poll
    )
    assert ok, f"EKF never used GPS; statustext: {pilot.statustext[-10:]}"


def _pilot_selects_guided(sitl: SitlInstance, link: FcLink, clock: Callable[[], int]) -> None:
    """The pilot's mode switch into GUIDED (T03), once the vehicle is ready.

    Selected earlier (during SITL boot) GUIDED was accepted and then replaced by
    STABILIZE about 0.2 s later (observed, see sitl.py); arming then would arm
    in STABILIZE and refuse the takeoff. So the switch moves after the EKF uses
    GPS, as a pilot would, and GUIDED must hold for a second.
    """
    pilot = sitl.pilot
    assert pilot is not None
    pilot.set_mode("GUIDED")
    assert pilot.wait_for(lambda: link.state.mode(clock()) == "GUIDED", 10, link.poll)
    held_from = time.monotonic()
    assert pilot.wait_for(
        lambda: link.state.mode(clock()) != "GUIDED" or time.monotonic() - held_from > 1.0,
        3,
        link.poll,
    )
    assert link.state.mode(clock()) == "GUIDED"


def test_f_sitl_scripted_run_arm_takeoff_velocity_land(sitl: SitlInstance, wall_clock) -> None:
    """[F1]-[F6], [F9], [F10], [M2a]: with the gate enabled, fc_link alone arms and takes off
    to 10 m in GUIDED, holds 2.0 m/s north (re-sent at 10 Hz, >= 2 Hz) until the FC reports
    >= 1.9 m/s, requests LAND while GUIDED, and the vehicle lands and disarms."""
    pilot = sitl.pilot
    assert pilot is not None
    link, buf = _connect(sitl, wall_clock, enabled=True)
    assert link.blocked_writes == 0  # nothing was even attempted before the proof
    _wait_ekf_gps(sitl, link)
    _pilot_selects_guided(sitl, link, wall_clock)

    def snap():
        return link.state.snapshot(wall_clock())

    armed = False
    for _ in range(10):  # a refused arm (EKF settling) is retried
        assert link.request(FcRequest(kind=FcRequestKind.ARM_AND_TAKEOFF, value=10.0))
        armed = pilot.wait_for(lambda: snap().armed is True, 3, link.poll)
        if armed:
            break
    assert armed, f"never armed; statustext: {pilot.statustext[-10:]}"
    assert pilot.wait_for(lambda: (snap().rel_alt_m or 0.0) >= 9.5, 45, link.poll), snap()
    assert snap().airborne and not link.state.attitude_degraded(wall_clock())

    last = [-(10**9)]
    resend_t: list[int] = []

    def send_north() -> None:
        link.poll()
        now = wall_clock()
        if now - last[0] >= 100:  # 10 Hz
            assert link.send_velocity(VelocityCommand(vn=2.0, ve=0.0, vd=0.0, yaw_rate=0.0))
            last[0] = now
            resend_t.append(now)

    def fast_enough() -> bool:
        v = link.state.velocity_ned(wall_clock())
        return v is not None and v[0] >= 1.9

    assert pilot.wait_for(fast_enough, 30, send_north), link.state.velocity_ned(wall_clock())
    assert (
        len(resend_t) >= 2
        and max(b - a for a, b in zip(resend_t, resend_t[1:], strict=False)) <= 500
    )
    assert link.last_setpoint_t == resend_t[-1]

    assert link.request(FcRequest(kind=FcRequestKind.MODE_LAND))
    assert pilot.wait_for(lambda: snap().mode == "LAND", 5, link.poll)
    assert pilot.wait_for(lambda: snap().armed is False, 90, link.poll), snap()
    assert snap().on_ground
    assert link.exit_blocked_count == 0 and link.blocked_count == 0

    acks = {(f.command, f.result) for f in _frames(buf, "rx") if f.get_type() == "COMMAND_ACK"}
    accepted = mavlink2.MAV_RESULT_ACCEPTED
    for cmd in (
        mavlink2.MAV_CMD_SET_MESSAGE_INTERVAL,
        mavlink2.MAV_CMD_COMPONENT_ARM_DISARM,
        mavlink2.MAV_CMD_NAV_TAKEOFF,
        mavlink2.MAV_CMD_DO_SET_MODE,
    ):
        assert (cmd, accepted) in acks, cmd
    sent = [f for f in _frames(buf, "tx") if f.get_type() == "SET_POSITION_TARGET_LOCAL_NED"]
    assert sent and {f.type_mask for f in sent} == {1479}  # [F6], [F10]


def test_f_sitl_locked_gate_never_arms(sitl: SitlInstance, wall_clock) -> None:
    """[F5]: against SITL in GUIDED with the EKF ready, a locked gate writes no setpoint and no
    arm or takeoff, counts every attempt, and the vehicle never arms; the same SITL then arms
    for an enabled link (the block was the gate, not the vehicle)."""
    pilot = sitl.pilot
    assert pilot is not None
    link, buf = _connect(sitl, wall_clock, enabled=False)
    assert link.gate_state is GateState.LOCKED
    _wait_ekf_gps(sitl, link)
    _pilot_selects_guided(sitl, link, wall_clock)

    attempts = 0
    deadline = time.monotonic() + 4.0
    seen_armed = False
    while time.monotonic() < deadline:
        link.poll()
        pilot.poll()
        assert not link.request(FcRequest(kind=FcRequestKind.ARM_AND_TAKEOFF, value=10.0))
        assert not link.send_velocity(VelocityCommand(vn=2.0, ve=0.0, vd=-1.0, yaw_rate=0.0))
        attempts += 2
        seen_armed |= pilot.armed is True or link.state.snapshot(wall_clock()).armed is True
        time.sleep(0.1)
    assert not seen_armed
    assert link.blocked_count == attempts and link.last_setpoint_t is None
    tx = _frames(buf, "tx")
    assert [f for f in tx if f.get_type() == "SET_POSITION_TARGET_LOCAL_NED"] == []
    assert {f.command for f in tx if f.get_type() == "COMMAND_LONG"} == {
        mavlink2.MAV_CMD_SET_MESSAGE_INTERVAL
    }  # only the [F2] stream requests ever went out
    assert link.state.snapshot(wall_clock()).armed is False
    link.close()

    control, _ = _connect(sitl, wall_clock, enabled=True)
    armed = False
    for _ in range(10):
        assert control.request(FcRequest(kind=FcRequestKind.ARM_AND_TAKEOFF, value=3.0))
        armed = pilot.wait_for(lambda: pilot.armed is True, 3, control.poll)
        if armed:
            break
    assert armed, f"positive control never armed; statustext: {pilot.statustext[-10:]}"
    control.close()


class AttitudeDropProxy:
    """A test-owned TCP fault proxy between SITL SERIAL1 and fc_link.

    Forwards both directions frame by frame; while a drop window is open it
    discards ATTITUDE frames from SITL and counts them.
    """

    def __init__(self, upstream_port: int) -> None:
        self.upstream_port = upstream_port
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.endpoint = f"tcp:127.0.0.1:{self.listener.getsockname()[1]}"
        self.dropped = 0
        self._drop_until = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def drop_attitude_for(self, seconds: float) -> None:
        self._drop_until = time.monotonic() + seconds

    def _run(self) -> None:
        client, _ = self.listener.accept()
        upstream = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=5)
        parser = mavlink2.MAVLink(None)
        parser.robust_parsing = True
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([client, upstream], [], [], 0.05)
                if client in ready:
                    data = client.recv(65536)
                    if not data:
                        return
                    upstream.sendall(data)
                if upstream in ready:
                    data = upstream.recv(65536)
                    if not data:
                        return
                    out = bytearray()
                    for msg in parser.parse_buffer(data) or []:
                        if msg.get_type() == "BAD_DATA":
                            continue
                        if msg.get_type() == "ATTITUDE" and time.monotonic() < self._drop_until:
                            self.dropped += 1
                            continue
                        out += msg.get_msgbuf()
                    client.sendall(bytes(out))
        finally:
            client.close()
            upstream.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.listener.close()


def test_f_sitl_attitude_staleness_trips_degraded_and_recovers(
    sitl: SitlInstance, wall_clock
) -> None:
    """[F3], [F4], [G1a]: a 300 ms ATTITUDE blackout injected between SITL and fc_link makes
    attitude degraded (age past attitude_bound_ms, also in the health packet) while the link
    stays up; attitude recovers when frames resume."""
    pilot = sitl.pilot
    assert pilot is not None
    port = int(sitl.companion_endpoint.rsplit(":", 1)[1])
    proxy = AttitudeDropProxy(port)
    try:
        link, _ = _connect(sitl, wall_clock, enabled=False, endpoint=proxy.endpoint)
        bound = link.config.attitude_bound_ms

        def degraded() -> bool:
            return link.state.attitude_degraded(wall_clock())

        assert pilot.wait_for(lambda: not degraded(), 10, link.poll, period_s=0.005)
        proxy.drop_attitude_for(0.3)
        assert pilot.wait_for(degraded, 2, link.poll, period_s=0.005)
        health = link.health_packet()
        assert health.attitude_age_ms is not None and health.attitude_age_ms > bound
        assert health.fc_link_up  # other telemetry keeps flowing: only attitude is stale
        assert link.state.snapshot(wall_clock()).attitude_degraded
        assert link.state.attitude_at(wall_clock()) is None  # [G1]: no sample within the bound
        assert pilot.wait_for(lambda: not degraded(), 2, link.poll, period_s=0.005)
        assert proxy.dropped >= 5  # about 15 at 50 Hz
        assert link.health_packet().attitude_age_ms <= bound
        link.close()
    finally:
        proxy.close()
