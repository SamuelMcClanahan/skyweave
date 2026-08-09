"""W8: a session restart on the wire is handled per D0.

D0 section 3: every node boot generates a new ``session_uuid``, ``frame_seq``
is monotonic within one session only, and a reboot is detected by the UUID
change and NEVER by a heuristic on sequence numbers. Over a real socket the
restart arrives as a datagram whose envelope simply names a different
session, so the wire must carry it faithfully and the frozen
``SessionRegistry`` must classify it — the adapter must not "help".

The trap this closes: after a restart, ``frame_seq`` resets to 0 while the
stream continues. Any component that de-duplicated or ordered on sequence
number alone would read the reset as a flood of stale frames and quarantine
the whole post-reboot clip.
"""

from __future__ import annotations

from skyweave2.contracts.time import SessionEvent, SessionRegistry
from skyweave2.faults import injectors as inj
from skyweave2.faults.honesty import decisions_fingerprint
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream
from skyweave2.transport import codec, control
from skyweave2.transport.loopback import socket_replay_inprocess
from skyweave2.transport.replay import capture_events
from tests.transport.conftest import as_wire, dump_observations

FPS = 30.0
RESTART_AT_S = 2.0


def restarted(stream):
    return inj.inject_node_restart(stream, RESTART_AT_S, FPS, camera_id=0)


def test_the_restart_fixture_really_restarts(stream):
    faulted, ledger = restarted(stream)
    sessions = {
        o.envelope.session_uuid for o in faulted if o.envelope.camera_id == 0
    }
    assert len(sessions) == 2, "no new session UUID was injected"
    restart_frame = int(ledger.notes["restart_frame"])
    after = [
        o for o in faulted
        if o.envelope.camera_id == 0 and o.envelope.frame_seq < restart_frame
    ]
    assert after, "frame_seq did not reset"


def test_the_new_session_survives_the_wire_intact(stream):
    faulted, _ = restarted(stream)
    result = socket_replay_inprocess(faulted)
    assert result.encode_failures == []
    assert result.adapter.stats.rejected == {}
    assert len(result.delivered) == len(faulted)
    assert dump_observations(result.delivered) == dump_observations(as_wire(faulted))
    # Both sessions arrived, and each capture event kept its own identity.
    delivered_sessions = {
        o.envelope.session_uuid for o in result.delivered if o.envelope.camera_id == 0
    }
    assert len(delivered_sessions) == 2


def test_each_datagram_carries_exactly_one_session(stream):
    """The envelope-once encoding must never merge two sessions in a packet."""
    faulted, _ = restarted(stream)
    for event in capture_events(faulted):
        sessions = {o.envelope.session_uuid for o in event.observations}
        assert len(sessions) == 1
        datagram = codec.encode_observation_packet(
            event.envelope, list(event.observations)
        )
        from skyweave2.transport.wire import unframe

        envelope, observations = codec.decode_observation_packet(unframe(datagram)[1])
        assert envelope.session_uuid == event.envelope.session_uuid
        assert all(o.envelope.session_uuid == envelope.session_uuid for o in observations)


def test_the_registry_sees_a_new_session_not_a_sequence_anomaly(stream):
    """Detected by UUID change, never by a heuristic on frame_seq."""
    faulted, _ = restarted(stream)
    result = socket_replay_inprocess(faulted)
    registry = SessionRegistry()
    events = [registry.classify(o.envelope) for o in result.delivered]
    assert events.count(SessionEvent.NEW_SESSION) == 1
    assert SessionEvent.SEQUENCE_ANOMALY not in events


def test_the_post_reboot_stream_is_not_quarantined(cameras, stream):
    """The sequence reset must not be read as a flood of stale frames."""
    faulted, _ = restarted(stream)
    result = socket_replay_inprocess(faulted)
    run = run_stream(result.delivered, cameras, FusionConfig(), session_uuid="w8")
    excluded = {}
    for entry in run.excluded:
        excluded[entry.reason] = excluded.get(entry.reason, 0) + 1
    assert excluded.get("stale_or_duplicate", 0) == 0
    assert excluded.get("sequence_anomaly", 0) == 0
    assert run.published, "nothing was published across the restart"


def test_socket_and_file_paths_agree_across_the_restart(cameras, stream):
    faulted, _ = restarted(stream)
    result = socket_replay_inprocess(faulted)
    file_run = run_stream(faulted, cameras, FusionConfig(), session_uuid="w8-parity")
    socket_run = run_stream(
        result.delivered, cameras, FusionConfig(), session_uuid="w8-parity"
    )
    assert decisions_fingerprint(socket_run) == decisions_fingerprint(file_run)
    assert socket_run.statuses == file_run.statuses


def test_a_track_survives_or_re_births_with_an_audit_trail(cameras, stream):
    """D6's N4 obligation, re-checked over the wire."""
    faulted, ledger = restarted(stream)
    result = socket_replay_inprocess(faulted)
    run = run_stream(result.delivered, cameras, FusionConfig(), session_uuid="w8-audit")
    restart_frame = int(ledger.notes["restart_frame"])

    post = [
        (index, track) for index, track in run.published if index >= restart_frame
    ]
    assert post, "no state published after the restart"
    # Every update is traceable to the exact observations it consumed, and
    # those obs_ids name the session they came from (D0 obs_ids format).
    post_records = [r for r in run.records if r.ts_ns >= restart_frame * 33_333_333]
    assert post_records
    assert all(record.obs_ids for record in post_records)
    new_session = {
        o.envelope.session_uuid
        for o in result.delivered
        if o.envelope.camera_id == 0 and o.envelope.frame_seq < restart_frame
    }
    assert any(
        any(key.split(":")[1] in new_session for key in record.obs_ids)
        for record in post_records
    ), "post-restart updates never consumed a post-restart observation"


def test_the_control_plane_carries_the_restart_as_a_new_session(stream):
    """A rebooted node reconnects and announces its new session over TCP."""
    old_session = "00000000-0000-0000-0000-000000000000"
    new_session = "11111111-1111-1111-1111-111111111111"
    with control.ControlServer("127.0.0.1", 0) as server:
        client = control.connect("127.0.0.1", server.port)
        connection = server.accept(timeout_s=2.0)
        assert connection is not None
        try:
            control.send_control(client, control.start_message(0, old_session, 1))
            control.send_control(client, control.stop_message(0, old_session, 2))
            # Reboot: same camera, brand-new session identity.
            control.send_control(client, control.start_message(0, new_session, 1))
            first = control.recv_control(connection)
            second = control.recv_control(connection)
            third = control.recv_control(connection)
        finally:
            client.close()
            connection.close()
    assert first.session_uuid == old_session
    assert second.type == control.pb.CONTROL_TYPE_STOP
    assert third.session_uuid == new_session
    # control_seq restarting at 1 is a reboot, not a replay: the SESSION is
    # what distinguishes them, exactly as on the measurement plane.
    assert third.control_seq == first.control_seq
    assert third.session_uuid != first.session_uuid


def test_control_frames_survive_being_split_across_tcp_segments():
    """Length-prefixed framing must not depend on message boundaries.

    TCP is a byte stream: a control frame can arrive in pieces. Reading it as
    if one recv were one message is the classic bug, and it corrupts every
    later frame on the connection rather than just the split one.
    """
    session = "11111111-1111-1111-1111-111111111111"
    message = control.calibration_message(0, session, "cal-rev-1", "det-rev-1", 9)
    body = codec.encode_control(message)
    import struct

    from skyweave2.transport.wire import CONTROL_LENGTH_PREFIX_FORMAT

    payload = struct.pack(CONTROL_LENGTH_PREFIX_FORMAT, len(body)) + body

    with control.ControlServer("127.0.0.1", 0) as server:
        client = control.connect("127.0.0.1", server.port)
        connection = server.accept(timeout_s=2.0)
        assert connection is not None
        try:
            for index in range(len(payload)):
                client.sendall(payload[index : index + 1])
            connection.settimeout(5.0)
            received = control.recv_control(connection)
        finally:
            client.close()
            connection.close()
    assert received.session_uuid == session
    assert received.calibration.calibration_rev == "cal-rev-1"
    assert received.control_seq == 9
