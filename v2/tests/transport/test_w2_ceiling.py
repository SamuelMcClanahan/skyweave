"""W2: the 1200 B ceiling, verified against the worst case the SCHEMA allows.

A too-big datagram must FAIL LOUDLY and never truncate. Three distinct
truncation routes exist and each is tested separately, because each one fails
silently if it is not closed:

1. the encoder emitting a shortened packet (it raises instead, and emits
   nothing at all);
2. the kernel shortening an oversized datagram because the receive buffer was
   sized at the ceiling — demonstrated to be real on this platform, then shown
   to be closed by the receiver's oversized buffer;
3. a peer that ignores the declared observation count but stays under the byte
   ceiling, which a byte-only check would wave through and the D8 nanopb
   daemon could not decode.
"""

from __future__ import annotations

import socket

import pytest

from skyweave2.transport import codec, sizing
from skyweave2.transport.udp import UdpReceiver, UdpSender
from skyweave2.transport.wire import (
    DATAGRAM_CEILING_BYTES,
    HEADER_LEN,
    RECV_BUFFER_BYTES,
    WIRE_LIMITS,
    DatagramTooLarge,
    PayloadType,
    ProtocolViolation,
    TooManyObservations,
    WireFormatError,
    declared_limits_from_options,
    frame,
    unframe,
)
from tests.transport.test_w1_schema import canonical_envelope, canonical_observation

# ---------------------------------------------------------------------------
# The declared bounds are ONE declaration
# ---------------------------------------------------------------------------


def test_nanopb_options_and_python_limits_agree():
    """The C daemon and the host must allocate against the same numbers."""
    assert declared_limits_from_options() == WIRE_LIMITS


def test_options_parser_can_fail(tmp_path):
    bad = tmp_path / "bad.options"
    bad.write_text("skyweave.v2.FrameEnvelope.session_uuid max_size:99\n")
    with pytest.raises(ValueError, match="missing bounds"):
        declared_limits_from_options(bad)


# ---------------------------------------------------------------------------
# Worst case vs the ceiling
# ---------------------------------------------------------------------------


def test_worst_case_at_the_declared_bound_fits_the_ceiling():
    budget = sizing.worst_case_budget()
    assert budget.observation_count == WIRE_LIMITS.observations_max_count
    assert budget.fits, (
        f"worst case is {budget.total_bytes} B against a "
        f"{budget.ceiling_bytes} B ceiling"
    )
    assert budget.total_bytes <= DATAGRAM_CEILING_BYTES


def test_the_declared_bound_is_exactly_the_derived_maximum():
    """Not a comfortable round number: one more observation must NOT fit.

    Without this, the bound could silently drift below the true maximum and
    the ceiling would look verified while wasting most of the budget.
    """
    derived = sizing.max_observations_under_ceiling()
    assert WIRE_LIMITS.observations_max_count == derived
    assert sizing.worst_case_datagram_size(derived) <= DATAGRAM_CEILING_BYTES
    assert sizing.worst_case_datagram_size(derived + 1) > DATAGRAM_CEILING_BYTES


def test_the_worst_case_really_is_worse_than_a_realistic_packet():
    """Can-fail guard: a worst case that is not worse proves nothing."""
    envelope = canonical_envelope()
    realistic = codec.encode_observation_packet(
        envelope, [canonical_observation(envelope)]
    )
    assert sizing.worst_case_datagram_size(1) > len(realistic) * 2


def test_size_budget_components_add_up():
    budget = sizing.worst_case_budget()
    assert budget.header_bytes == HEADER_LEN
    assert budget.envelope_bytes > 0 and budget.per_observation_bytes > 0
    predicted = (
        budget.header_bytes
        + budget.envelope_bytes
        + budget.per_observation_bytes * budget.observation_count
    )
    # Repeated-field tag/length overhead makes this near-exact, not exact;
    # a large gap would mean the budget is not describing the real encoding.
    assert abs(predicted - budget.total_bytes) <= 4 * budget.observation_count


# ---------------------------------------------------------------------------
# Route 1: the encoder never truncates
# ---------------------------------------------------------------------------


def test_oversized_body_raises_and_emits_nothing():
    body = b"\x00" * (DATAGRAM_CEILING_BYTES + 1)
    with pytest.raises(DatagramTooLarge) as excinfo:
        frame(PayloadType.OBSERVATION, body)
    assert excinfo.value.size == len(body) + HEADER_LEN
    assert excinfo.value.ceiling == DATAGRAM_CEILING_BYTES
    assert "never" in str(excinfo.value)


def test_exactly_at_the_ceiling_is_accepted():
    """Boundary: the ceiling is inclusive, so off-by-one cannot hide here."""
    body = b"\x00" * (DATAGRAM_CEILING_BYTES - HEADER_LEN)
    datagram = frame(PayloadType.OBSERVATION, body)
    assert len(datagram) == DATAGRAM_CEILING_BYTES
    with pytest.raises(DatagramTooLarge):
        frame(PayloadType.OBSERVATION, body + b"\x00")


def test_an_over_ceiling_capture_event_fails_loudly():
    """A long evidence_ref alone can push a legal event over the ceiling."""
    envelope = canonical_envelope(
        session_uuid="U" * 36, calibration_rev="C" * 64, detector_rev="D" * 64
    )
    observations = [
        canonical_observation(
            envelope, obs_id=i, evidence_ref="E" * 64, bbox_x=-(2**31), bbox_y=-(2**31),
            bbox_w=2**32 - 1, bbox_h=2**32 - 1, area_px=2**32 - 1,
            persistence_count=2**32 - 1, local_blob_id=2**32 - 1,
        )
        for i in range(WIRE_LIMITS.observations_max_count)
    ]
    # At the declared bound this must still fit...
    datagram = codec.encode_observation_packet(envelope, observations)
    assert len(datagram) <= DATAGRAM_CEILING_BYTES
    # ...and one more must be refused, by count, before bytes are even weighed.
    observations.append(canonical_observation(envelope, obs_id=99))
    with pytest.raises(TooManyObservations):
        codec.encode_observation_packet(envelope, observations)


# ---------------------------------------------------------------------------
# Route 2: the kernel never truncates for us
# ---------------------------------------------------------------------------


def test_a_ceiling_sized_receive_buffer_really_would_truncate():
    """The trap, demonstrated on this platform rather than assumed.

    If this ever stops truncating, the guard below becomes untestable and the
    reason for the oversized buffer should be re-derived, not deleted.
    """
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        oversized = b"\xa5" * (DATAGRAM_CEILING_BYTES + 200)
        sender.sendto(oversized, receiver.getsockname())
        receiver.settimeout(2.0)
        got = receiver.recv(DATAGRAM_CEILING_BYTES)
        assert len(got) == DATAGRAM_CEILING_BYTES
        assert len(got) < len(oversized)  # silently short: no error, no flag
    finally:
        sender.close()
        receiver.close()


def test_the_receiver_sees_the_whole_oversized_datagram_and_rejects_it():
    assert RECV_BUFFER_BYTES > DATAGRAM_CEILING_BYTES
    with UdpReceiver("127.0.0.1", 0) as receiver:
        with UdpSender("127.0.0.1", receiver.port) as sender:
            oversized = (
                bytes(frame(PayloadType.OBSERVATION, b"\x00" * 8)[:HEADER_LEN])
                + b"\x00" * (DATAGRAM_CEILING_BYTES + 100)
            )
            sender.send(oversized)
            received = receiver.recv(2.0)
        assert received is not None
        assert len(received) == len(oversized), "the datagram arrived shortened"
        with pytest.raises(WireFormatError, match="over the .* ceiling"):
            unframe(received)


def test_the_adapter_labels_an_oversized_datagram_instead_of_parsing_it():
    from skyweave2.transport.adapter import SocketIngestAdapter

    with UdpReceiver("127.0.0.1", 0) as receiver:
        with UdpSender("127.0.0.1", receiver.port) as sender:
            header = frame(PayloadType.OBSERVATION, b"")[:HEADER_LEN]
            sender.send(header + b"\x00" * (DATAGRAM_CEILING_BYTES + 100))
        adapter = SocketIngestAdapter(receiver)
        assert adapter.poll(2.0) == []
    assert adapter.stats.rejected_total == 1
    assert any("framing" in reason for reason in adapter.stats.rejected)
    assert adapter.stats.observations == 0


# ---------------------------------------------------------------------------
# Route 3: the count bound is independent of the byte ceiling
# ---------------------------------------------------------------------------


def test_count_bound_bites_even_when_the_bytes_would_fit():
    """Short revisions leave room for far more than the declared count."""
    envelope = canonical_envelope(
        session_uuid="00000000-0000-0000-0000-000000000000",
        calibration_rev="c",
        detector_rev="d",
    )
    over = [
        canonical_observation(envelope, obs_id=i, evidence_ref=None)
        for i in range(WIRE_LIMITS.observations_max_count + 1)
    ]
    packet = codec.build_observation_packet(envelope, over)
    fitting_bytes = HEADER_LEN + len(packet.SerializeToString(deterministic=True))
    assert fitting_bytes <= DATAGRAM_CEILING_BYTES, (
        "this fixture must be under the byte ceiling for the count check to "
        "be the thing under test"
    )
    with pytest.raises(TooManyObservations) as excinfo:
        codec.encode_observation_packet(envelope, over)
    assert excinfo.value.count == WIRE_LIMITS.observations_max_count + 1
    assert excinfo.value.bound == WIRE_LIMITS.observations_max_count


def test_declared_string_bounds_are_enforced_at_encode_time():
    """A bound only one side enforces is not a bound.

    The host has no natural string limit, so without this check it could emit a
    200-character revision: under the byte ceiling, decodable here, and
    undecodable by the D8 nanopb daemon that allocates from ``max_size``.
    """
    limits = WIRE_LIMITS
    for field, overrides in (
        ("session_uuid", {"session_uuid": "U" * limits.session_uuid_max_size}),
        ("calibration_rev",
         {"calibration_rev": "C" * limits.calibration_rev_max_size}),
        ("detector_rev", {"detector_rev": "D" * limits.detector_rev_max_size}),
    ):
        envelope = canonical_envelope(**overrides)
        with pytest.raises(ProtocolViolation, match=field):
            codec.encode_observation_packet(
                envelope, [canonical_observation(envelope, evidence_ref=None)]
            )
        # Exactly at the bound must still be accepted: an off-by-one here
        # would quietly shrink every declared field by one character.
        at_bound = canonical_envelope(
            **{key: value[:-1] for key, value in overrides.items()}
        )
        codec.encode_observation_packet(
            at_bound, [canonical_observation(at_bound, evidence_ref=None)]
        )

    envelope = canonical_envelope()
    with pytest.raises(ProtocolViolation, match="evidence_ref"):
        codec.encode_observation_packet(
            envelope,
            [canonical_observation(
                envelope, evidence_ref="E" * limits.evidence_ref_max_size
            )],
        )


def test_string_bounds_are_counted_in_utf8_bytes_not_characters():
    """nanopb sizes buffers in bytes; a short multi-byte string still overflows."""
    limits = WIRE_LIMITS
    # 22 four-byte characters = 88 UTF-8 bytes, over a 64-byte bound, but only
    # 22 characters — a character-based check would wave this through.
    multibyte = "\U0001f6f0" * 22
    assert len(multibyte) < limits.text_len(limits.calibration_rev_max_size)
    assert len(multibyte.encode("utf-8")) > limits.text_len(
        limits.calibration_rev_max_size
    )
    envelope = canonical_envelope(calibration_rev=multibyte)
    with pytest.raises(ProtocolViolation, match="UTF-8 bytes"):
        codec.encode_observation_packet(
            envelope, [canonical_observation(envelope, evidence_ref=None)]
        )


def test_evidence_channel_has_its_own_larger_ceiling():
    """Evidence may exceed the measurement ceiling; it is droppable."""
    from skyweave2.transport.wire import EVIDENCE_CEILING_BYTES, ceiling_for

    assert EVIDENCE_CEILING_BYTES > DATAGRAM_CEILING_BYTES
    assert ceiling_for(PayloadType.EVIDENCE) == EVIDENCE_CEILING_BYTES
    assert ceiling_for(PayloadType.OBSERVATION) == DATAGRAM_CEILING_BYTES
    assert ceiling_for(PayloadType.HEALTH) == DATAGRAM_CEILING_BYTES
    big = codec.Evidence(
        evidence_ref="patch", camera_id=0, session_uuid="s", frame_seq=1,
        capture_ts_ns=0, expiry_ts_ns=1, kind=1, x0=0, y0=0, width=64, height=64,
        payload=b"\x00" * 4096,
    )
    datagram = codec.encode_evidence(big)
    assert len(datagram) > DATAGRAM_CEILING_BYTES

    # Two bounds, and the DECLARED one binds first because it sits BELOW the
    # channel ceiling. Without the explicit payload check there is a window of
    # payload sizes that frames cleanly here and overruns a nanopb bytes[8192].
    assert WIRE_LIMITS.evidence_payload_max_size < EVIDENCE_CEILING_BYTES
    with pytest.raises(ProtocolViolation, match="evidence payload"):
        codec.encode_evidence(
            codec.Evidence(
                evidence_ref="patch", camera_id=0, session_uuid="s", frame_seq=1,
                capture_ts_ns=0, expiry_ts_ns=1, kind=1, x0=0, y0=0,
                width=64, height=64,
                payload=b"\x00" * (WIRE_LIMITS.evidence_payload_max_size + 1),
            )
        )
    # A payload inside that window used to frame cleanly; now it is refused.
    with pytest.raises(ProtocolViolation, match="evidence payload"):
        codec.encode_evidence(
            codec.Evidence(
                evidence_ref="patch", camera_id=0, session_uuid="s", frame_seq=1,
                capture_ts_ns=0, expiry_ts_ns=1, kind=1, x0=0, y0=0,
                width=64, height=64,
                payload=b"\x00" * (EVIDENCE_CEILING_BYTES - 100),
            )
        )
    # The channel ceiling is still a real backstop for everything else.
    with pytest.raises(DatagramTooLarge):
        frame(PayloadType.EVIDENCE, b"\x00" * EVIDENCE_CEILING_BYTES)


def test_every_plane_enforces_its_declared_bounds():
    """Health, evidence and control are bounded too, not just measurement.

    These are the planes where a silent drop costs most: they are how a node
    reports that something is wrong.
    """
    from skyweave2.contracts import ClockDomain
    from skyweave2.transport import control

    long_uuid = "u" * WIRE_LIMITS.session_uuid_max_size
    with pytest.raises(ProtocolViolation, match="session_uuid"):
        codec.encode_health(
            codec.Health(
                camera_id=0, session_uuid=long_uuid, ts_ns=0,
                clock_domain=ClockDomain.NODE_PTP, fps=30.0, drops=0,
                time_sync_error_ms=0.0,
            )
        )
    with pytest.raises(ProtocolViolation, match="session_uuid"):
        codec.encode_control(control.start_message(0, long_uuid))
    with pytest.raises(ProtocolViolation, match="calibration_rev"):
        codec.encode_control(
            control.calibration_message(
                0, "s", "C" * WIRE_LIMITS.calibration_rev_max_size, "d"
            )
        )
    # The host's own failure text is the concrete case: a ProtocolViolation
    # message is longer than Ack.detail's bound, so an unbounded NACK would be
    # a datagram the board cannot decode.
    with pytest.raises(ProtocolViolation, match="ack.detail"):
        control.ack_message(1, False, "x" * WIRE_LIMITS.ack_detail_max_size)


def test_the_options_guard_covers_every_declared_bound():
    """A bound in the .options file that nothing enforces must be an ERROR.

    Without this the file can grow a line the D8 daemon allocates from and
    this side checks nowhere — the exact asymmetry the file exists to prevent.
    """
    from skyweave2.transport.wire import _OPTIONS_PATH, parse_options_file

    declared = set(parse_options_file(_OPTIONS_PATH))
    from skyweave2.transport.wire import _OPTIONS_TO_LIMIT

    assert declared == set(_OPTIONS_TO_LIMIT), (
        "every nanopb bound must be mirrored in WireLimits and checked on the "
        "encode path"
    )


def test_options_parser_reads_the_file_not_the_defaults(tmp_path):
    """Positive propagation: a changed bound must reach WireLimits.

    Asserting only that a BAD file raises leaves 'return WireLimits()' — which
    ignores the file entirely — passing.
    """
    from skyweave2.transport.wire import _OPTIONS_PATH

    shifted = tmp_path / "shifted.options"
    shifted.write_text(
        _OPTIONS_PATH.read_text(encoding="utf-8").replace(
            "observations max_count:5", "observations max_count:3"
        ),
        encoding="utf-8",
    )
    assert declared_limits_from_options(shifted).observations_max_count == 3


def test_conflicting_duplicate_bounds_are_rejected(tmp_path):
    """The same semantic bound is declared on several messages; they must agree."""
    from skyweave2.transport.wire import _OPTIONS_PATH

    conflicting = tmp_path / "conflicting.options"
    conflicting.write_text(
        _OPTIONS_PATH.read_text(encoding="utf-8").replace(
            "skyweave.v2.HealthPacket.session_uuid     max_size:37",
            "skyweave.v2.HealthPacket.session_uuid     max_size:99",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="conflicting values"):
        declared_limits_from_options(conflicting)


def test_float32_overflow_is_rejected_not_saturated():
    """Narrowing is the declared cost; saturating to infinity is not.

    protobuf turns a double beyond FLT_MAX into +/-inf on assignment: a finite
    measurement silently becoming infinity is total loss dressed as rounding.
    """
    envelope = canonical_envelope()
    with pytest.raises(ProtocolViolation, match="gain_db"):
        codec.encode_observation_packet(
            canonical_envelope(gain_db=1e39),
            [canonical_observation(envelope, evidence_ref=None)],
        )
    # Infinity the SENDER chose is carried, not turned into an encode failure:
    # hiding it would be worse than delivering it to a receiver that must decide.
    infinite = canonical_envelope(gain_db=float("inf"))
    decoded, _ = codec.decode_observation_packet(
        unframe(codec.encode_observation_packet(
            infinite, [canonical_observation(infinite, evidence_ref=None)]
        ))[1]
    )
    assert decoded.gain_db == float("inf")


def test_control_frame_length_prefix_is_bounded_before_allocating():
    """A hostile length prefix must not make the host reserve memory."""
    import struct

    from skyweave2.transport.control import recv_control
    from skyweave2.transport.wire import (
        CONTROL_FRAME_MAX_BYTES,
        CONTROL_LENGTH_PREFIX_FORMAT,
    )

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=2.0)
    connection, _ = server.accept()
    try:
        client.sendall(
            struct.pack(CONTROL_LENGTH_PREFIX_FORMAT, CONTROL_FRAME_MAX_BYTES + 1)
        )
        connection.settimeout(2.0)
        with pytest.raises(WireFormatError, match="refusing to allocate"):
            recv_control(connection)
    finally:
        client.close()
        connection.close()
        server.close()
