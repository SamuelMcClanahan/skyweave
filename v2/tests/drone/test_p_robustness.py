"""Robustness regressions from the E1 bug hunt (TESTING_DOCTRINE rule 5).

Each test is named after its finding. The common thread: one datagram the
wire accepts, or a burst of them, must never end the companion process
([C7], [C9], [C10]); a process exit in flight leaves the mission unprimed
(E1-F6).
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path

import pytest

from skyweave2.drone import packets as P
from skyweave2.drone.ground_ui import RecordTap, ViewBuilder
from skyweave2.drone.mission import MAX_PENDING_EVENTS, Mission, MissionConfig
from skyweave2.drone.types import LandedState, VehicleSnapshot
from skyweave2.drone.udp import LOOPBACK, UdpSender

GOLDEN = Path(__file__).resolve().parent / "golden"


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def _prime_with_params(params: str) -> bytes:
    return (
        '{"v":1,"cmd_id":"x1","token":"fixture-token","command":"prime","params":{"a":'
        + params
        + "}}"
    ).encode()


def test_bughunt1_c5_json_depth_cap_discriminates() -> None:
    """[C5], finding 1 (deep params): nesting up to MAX_JSON_DEPTH decodes,
    one level more is refused as PacketError before parsing, so nothing the
    decoder accepts can fail the recorder's re-parse of the same line."""
    # The packet object and params object are two levels; fill the rest.
    ok = _prime_with_params(_nested(P.MAX_JSON_DEPTH - 2))
    assert P.decode(P.PacketKind.COMMAND, ok).command is P.CommandName.PRIME
    too_deep = _prime_with_params(_nested(P.MAX_JSON_DEPTH - 1))
    with pytest.raises(P.PacketError, match="nested deeper"):
        P.decode(P.PacketKind.COMMAND, too_deep)
    with pytest.raises(P.PacketError):
        P.decode(P.PacketKind.COMMAND, _prime_with_params(_nested(984)))


def test_bughunt1_c5_brackets_inside_strings_do_not_count() -> None:
    """[C5]: the depth scan ignores brackets inside JSON strings (and escaped
    quotes), so text content never trips the cap."""
    raw = _prime_with_params(json.dumps("[{" * 200 + '\\"' + "}]" * 200))
    assert P.decode(P.PacketKind.COMMAND, raw).params["a"].startswith("[{")


@pytest.mark.parametrize(
    ("value", "ok"),
    [(P.JSON_SAFE_INT, True), (P.JSON_SAFE_INT + 1, False), (int("9" * 130), False)],
)
def test_bughunt4_c6_track_id_json_safe_range(value: int, ok: bool) -> None:
    """[C6], finding 4 (130-digit track_id): integers outside +-(2^53 - 1)
    are refused, so every derived event name (``commit:<id>:<t_cap>``) stays
    within the 128-char limit [P3]."""
    obj = json.loads((GOLDEN / "track.json").read_text())
    obj["track_id"] = value
    raw = json.dumps(obj).encode()
    if ok:
        pkt = P.decode(P.PacketKind.TRACK, raw)
        name = f"commit:{pkt.track_id}:{P.JSON_SAFE_INT}"
        assert len(name) <= P.MAX_EVENT_NAME_LEN
    else:
        with pytest.raises(P.PacketError, match="JSON-safe"):
            P.decode(P.PacketKind.TRACK, raw)


def _ground(t: int) -> VehicleSnapshot:
    return VehicleSnapshot(
        t_ms=t,
        fc_link_up=True,
        mode="STABILIZE",
        armed=False,
        landed_state=LandedState.ON_GROUND,
        rel_alt_m=0.0,
        home_dist_m=0.0,
        battery_pct=95.0,
        attitude_age_ms=10,
        attitude_degraded=False,
        rc_seen=True,
    )


def test_bughunt2_c9_event_flood_forces_a_publish() -> None:
    """[C9], [P3], finding 2 (unauthenticated command flood): once
    MAX_PENDING_EVENTS events are pending, the very next input publishes them,
    so a mission state packet never grows past the datagram limit; every
    event still appears in exactly one packet."""
    m = Mission(MissionConfig())
    t = 1_000
    m.on_tick(_ground(t), t)
    prime = P.CommandPacket(
        cmd_id="p1", token="fixture-token", command=P.CommandName.PRIME,
        params=P.PrimeParams().to_obj(),
    )  # fmt: skip
    assert m.on_command(prime, True, t + 1).result is P.AckResult.ACCEPTED
    assert m.maybe_publish(t + 1) is not None  # the prime's own packet
    packets = []
    for i in range(3 * MAX_PENDING_EVENTS):
        bad = P.CommandPacket(cmd_id=f"f{i}", token="wrong-token", command=P.CommandName.ABORT)
        m.on_command(bad, False, t + 2)  # same stamp: no periodic tick in between
        pkt = m.maybe_publish(t + 2)
        if pkt is not None:
            packets.append(pkt)
    assert packets, "a flood within one publish period must still publish"
    assert all(len(p.events) <= MAX_PENDING_EVENTS for p in packets)
    assert all(len(P.encode(p)) <= P.MAX_DATAGRAM_BYTES for p in packets)
    names = [e.name for p in packets for e in p.events]
    assert len(names) == len([n for n in names if n == "cmd:abort:rejected_auth"])


def test_bughunt2_udp_sender_unencodable_packet_is_counted_not_raised() -> None:
    """[C9], finding 2: a packet the encoder refuses is dropped and counted by
    the publisher; the live loop is never ended by a publish."""
    sender = UdpSender(LOOPBACK, 9)  # discard port; nothing needs to listen
    try:
        nan_box = P.Box(x=math.nan, y=0.0, w=1.0, h=1.0, conf=0.5)
        assert sender.send(P.DetectionPacket(t_cap=1, frame_seq=1, boxes=(nan_box,))) is False
        assert sender.errors == 1
    finally:
        sender.close()


def test_bughunt1_record_tap_never_raises_into_the_recorder() -> None:
    """[U5], [R1], finding 1: a recorded line the UI view cannot take is
    counted and logged; the recording itself still receives every byte."""
    down = io.StringIO()
    tap = RecordTap(ViewBuilder(), down)
    text = "this is not a record\n"
    assert tap.write(text) == len(text)
    assert tap.view_errors == 1
    assert down.getvalue() == text


def test_bughunt3_g1a_nonfinite_geometry_is_a_degraded_packet() -> None:
    """[G1a], [G7], [G8], finding 3 (u = 1e200): a valid packet whose ray
    overflows is treated as attitude-degraded: zero command, no hold progress,
    never a NaN setpoint, never a false hold_complete."""
    from skyweave2.drone.camera import CameraModel
    from skyweave2.drone.guidance import GuidanceConfig, geometry_usable

    cam = CameraModel()
    good = P.TrackPacket(
        t_cap=1, track_id=7, state=P.TrackState.CONFIRMED, u=960.0, v_px=600.0,
        du=0.0, dv=0.0, w=40.0, h=40.0, hits=6, misses=0, age_frames=6,
    )  # fmt: skip
    assert geometry_usable(good, cam, 1.0, GuidanceConfig())
    bad = P.TrackPacket(**{**good.__dict__, "u": 1e200})
    assert not geometry_usable(bad, cam, 1.0, GuidanceConfig())
