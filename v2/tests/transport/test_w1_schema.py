"""W1: proto round-trip + pydantic parity + golden bytes.

The load-bearing test here is not the round-trip — encoding and decoding with
the same library proves very little. It is:

- every field number in the ``.proto`` is read back out of the FROZEN CONTRACT
  DOCSTRINGS and compared, so a contract edit that forgets the wire fails;
- the ``.proto`` TEXT is parsed independently and compared against the
  compiled descriptor, so a forgotten regeneration fails;
- exact datagram BYTES are pinned, so any change to the encoding fails even
  when round-trip still works.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from skyweave2.contracts import ClockDomain, FrameEnvelope, Observation2D
from skyweave2.contracts import frames as frames_module
from skyweave2.contracts import observation as observation_module
from skyweave2.transport import codec
from skyweave2.transport.proto import skyweave_pb2 as pb
from skyweave2.transport.wire import (
    HEADER_LEN,
    MAGIC,
    WIRE_VERSION,
    PayloadType,
    ProtocolViolation,
    unframe,
)

V2 = Path(__file__).resolve().parents[2]
PROTO_PATH = V2 / "proto" / "skyweave.proto"
GOLDEN_DIR = Path(__file__).resolve().parent / "wire_golden"

# Regeneration is deliberately awkward and env-gated, mirroring the repo's
# golden policy: pinned bytes change only with a recorded reason.
REGENERATE = os.environ.get("SKYWEAVE_REGENERATE_WIRE_GOLDEN") == "1"


# ---------------------------------------------------------------------------
# Field numbers: .proto vs the frozen contract docstrings
# ---------------------------------------------------------------------------

_RESERVATION = re.compile(r"(\d+)\s+([A-Za-z_][\w/]*)")

# The docstring writes the D0 bbox reservation as the four pydantic field
# names it covers; the wire carries them in one bounded sub-message.
_DOCSTRING_ALIASES = {"bbox_x/y/w/h": "bbox"}


def _reserved_numbers(docstring: str) -> dict[str, int]:
    """Parse 'Reserved wire field numbers: 1 a, 2 b, ...' from a docstring."""
    marker = "Reserved wire field numbers"
    start = docstring.index(marker)
    end = docstring.index(".", docstring.index(":", start))
    body = docstring[start:end]
    out: dict[str, int] = {}
    for number, name in _RESERVATION.findall(body.split(":", 1)[1]):
        out[_DOCSTRING_ALIASES.get(name, name)] = int(number)
    return out


def _descriptor_numbers(descriptor) -> dict[str, int]:
    return {field.name: field.number for field in descriptor.fields}


def test_frame_envelope_matches_the_d0_reservation():
    reserved = _reserved_numbers(frames_module.__doc__)
    assert _descriptor_numbers(pb.FrameEnvelope.DESCRIPTOR) == reserved
    # The docstring must actually have described all 15 fields, or the
    # comparison above could pass vacuously against a truncated parse.
    assert len(reserved) == 15


def test_observation_matches_the_d0_reservation():
    reserved = _reserved_numbers(observation_module.__doc__)
    assert _descriptor_numbers(pb.Observation2D.DESCRIPTOR) == reserved
    assert len(reserved) == 13
    # Field 1 exists on the wire type (D0 reserves it) even though a packet
    # never sets it; losing the declaration would free number 1 for reuse.
    assert pb.Observation2D.DESCRIPTOR.fields_by_name["envelope"].number == 1


def test_pydantic_and_proto_field_names_agree():
    """Same names on both sides — a rename on either side is a wire change."""
    pydantic_names = set(FrameEnvelope.model_fields)
    assert set(_descriptor_numbers(pb.FrameEnvelope.DESCRIPTOR)) == pydantic_names

    proto_names = set(_descriptor_numbers(pb.Observation2D.DESCRIPTOR))
    expected = (set(Observation2D.model_fields) - {"bbox_x", "bbox_y", "bbox_w",
                                                   "bbox_h"}) | {"bbox"}
    assert proto_names == expected


# ---------------------------------------------------------------------------
# .proto text vs the compiled descriptor (catches a missed regeneration)
# ---------------------------------------------------------------------------

_MESSAGE = re.compile(r"^message\s+(\w+)\s*\{", re.MULTILINE)
_FIELD = re.compile(
    r"^\s*(?:(optional|repeated)\s+)?([\w.]+)\s+(\w+)\s*=\s*(\d+)\s*;", re.MULTILINE
)


def _parse_proto_text(text: str) -> dict[str, dict[str, tuple[int, str, str]]]:
    """Independent parse: {message: {field: (number, type, label)}}."""
    # Strip comments so a commented-out field never counts.
    text = re.sub(r"//[^\n]*", "", text)
    out: dict[str, dict[str, tuple[int, str, str]]] = {}
    matches = list(_MESSAGE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        body = body[: body.index("}")] if "}" in body else body
        fields = {
            name: (int(number), type_name, label or "singular")
            for label, type_name, name, number in _FIELD.findall(body)
        }
        out[match.group(1)] = fields
    return out


def test_proto_text_matches_the_compiled_descriptor():
    parsed = _parse_proto_text(PROTO_PATH.read_text(encoding="utf-8"))
    file_descriptor = pb.DESCRIPTOR
    compiled = set(file_descriptor.message_types_by_name)
    assert set(parsed) == compiled, (
        "proto/skyweave.proto and the checked-in descriptor disagree on which "
        "messages exist — regenerate with "
        "`uv run python -m skyweave2.transport.generate`"
    )
    for message_name, fields in parsed.items():
        descriptor = file_descriptor.message_types_by_name[message_name]
        for field_name, (number, _type, label) in fields.items():
            field = descriptor.fields_by_name[field_name]
            assert field.number == number, f"{message_name}.{field_name} number"
            if label == "repeated":
                assert field.is_repeated
            elif label == "optional":
                assert field.has_presence


def test_the_proto_parser_can_actually_fail():
    """A can-fail fixture for the parser itself."""
    parsed = _parse_proto_text("message Foo {\n  uint32 bar = 7;\n}\n")
    assert parsed == {"Foo": {"bar": (7, "uint32", "singular")}}
    commented = _parse_proto_text("message Foo {\n  // uint32 bar = 7;\n}\n")
    assert commented == {"Foo": {}}


def test_schema_has_no_maps_and_no_recursion():
    """nanopb constraints, asserted rather than asserted-in-a-comment."""
    for descriptor in pb.DESCRIPTOR.message_types_by_name.values():
        for field in descriptor.fields:
            assert not (
                field.message_type is not None
                and field.message_type.GetOptions().map_entry
            ), f"{descriptor.name}.{field.name} is a map"

    def reaches(descriptor, seen: frozenset) -> None:
        for field in descriptor.fields:
            if field.message_type is None:
                continue
            name = field.message_type.full_name
            assert name not in seen, f"recursive type via {descriptor.name}.{field.name}"
            reaches(field.message_type, seen | {name})

    for descriptor in pb.DESCRIPTOR.message_types_by_name.values():
        reaches(descriptor, frozenset({descriptor.full_name}))


# ---------------------------------------------------------------------------
# Round-trip and pydantic parity
# ---------------------------------------------------------------------------


def canonical_envelope(**overrides) -> FrameEnvelope:
    """A fixed envelope. Every float value is exactly binary32-representable
    so this fixture isolates encoding from the separately-tested narrowing."""
    values = dict(
        camera_id=2,
        session_uuid="8f14e45f-ceea-467a-9d3f-1b2c3d4e5f60",
        frame_seq=1234,
        capture_ts_ns=41_133_333_333,
        clock_domain=ClockDomain.SYNTHETIC,
        time_sync_error_ms=0.5,
        exposure_us=3000.0,
        gain_db=6.0,
        full_width=2304,
        full_height=1296,
        proc_width=1536,
        proc_height=864,
        calibration_rev="d7-fixed-cal-r1",
        detector_rev="d7-mog2-1536",
        line_readout_us=0.0,
    )
    values.update(overrides)
    return FrameEnvelope(**values)


def canonical_observation(envelope: FrameEnvelope, **overrides) -> Observation2D:
    values = dict(
        envelope=envelope,
        obs_id=1,
        u=1152.3125,
        v=647.75,
        cov_uu=0.25,
        cov_uv=0.0,
        cov_vv=0.25,
        bbox_x=1149,
        bbox_y=644,
        bbox_w=6,
        bbox_h=6,
        area_px=12,
        persistence_count=2,
        confidence=1.0,
        local_blob_id=3,
        evidence_ref="patch-0001",
    )
    values.update(overrides)
    return Observation2D(**values)


def test_round_trip_preserves_every_field():
    envelope = canonical_envelope()
    observations = [
        canonical_observation(envelope),
        canonical_observation(
            envelope, obs_id=2, local_blob_id=None, evidence_ref=None, cov_uv=-0.05
        ),
    ]
    datagram = codec.encode_observation_packet(envelope, observations)
    payload_type, body = unframe(datagram)
    assert payload_type is PayloadType.OBSERVATION
    decoded_envelope, decoded = codec.decode_observation_packet(body)
    assert decoded_envelope == envelope
    assert decoded == observations


def test_absent_is_not_zero():
    """local_blob_id 0 and evidence_ref '' must survive as themselves."""
    envelope = canonical_envelope()
    zero = canonical_observation(envelope, local_blob_id=0, evidence_ref="")
    absent = canonical_observation(envelope, local_blob_id=None, evidence_ref=None)
    _, decoded_zero = codec.decode_observation_packet(
        unframe(codec.encode_observation_packet(envelope, [zero]))[1]
    )
    _, decoded_absent = codec.decode_observation_packet(
        unframe(codec.encode_observation_packet(envelope, [absent]))[1]
    )
    assert decoded_zero[0].local_blob_id == 0
    assert decoded_zero[0].evidence_ref == ""
    assert decoded_absent[0].local_blob_id is None
    assert decoded_absent[0].evidence_ref is None


def test_encoding_is_byte_stable():
    envelope = canonical_envelope()
    observations = [canonical_observation(envelope)]
    first = codec.encode_observation_packet(envelope, observations)
    second = codec.encode_observation_packet(envelope, observations)
    assert first == second


def test_line_readout_us_crosses_the_wire():
    """Field 15 is 0.0 in every other fixture, and 0.0 is proto3's implicit
    default — so without a non-zero case the encoder line is unpinned and
    deleting it entirely leaves the suite green. Rolling shutter (D0 s.3) is
    exactly when this field stops being zero."""
    envelope = canonical_envelope(line_readout_us=31.25)  # binary32-exact
    body = unframe(
        codec.encode_observation_packet(envelope, [canonical_observation(envelope)])
    )[1]
    decoded_envelope, _ = codec.decode_observation_packet(body)
    assert decoded_envelope.line_readout_us == 31.25


def test_geometry_fields_cross_the_wire_exactly():
    """u/v/covariance are double on the wire; no narrowing is acceptable."""
    envelope = canonical_envelope()
    obs = canonical_observation(
        envelope,
        u=1152.1234567890123,
        v=647.9876543210987,
        cov_uu=0.1234567890123456,
        cov_uv=-0.0123456789012345,
        cov_vv=0.9876543210987654,
    )
    _, decoded = codec.decode_observation_packet(
        unframe(codec.encode_observation_packet(envelope, [obs]))[1]
    )
    for field in ("u", "v", "cov_uu", "cov_uv", "cov_vv"):
        assert getattr(decoded[0], field) == getattr(obs, field), field
    assert decoded[0].envelope.capture_ts_ns == envelope.capture_ts_ns


def test_float32_narrowing_is_real_and_confined_to_the_declared_float_fields():
    """The D0 type column says `float` for these; the consequence is measured,
    not hidden. If this test starts failing, the .proto widened a field."""
    envelope = canonical_envelope(time_sync_error_ms=0.1, gain_db=1.1)
    obs = canonical_observation(envelope, confidence=0.3)
    _, decoded = codec.decode_observation_packet(
        unframe(codec.encode_observation_packet(envelope, [obs]))[1]
    )
    assert decoded[0].envelope.time_sync_error_ms != 0.1
    assert decoded[0].envelope.time_sync_error_ms == codec.to_float32(0.1)
    assert decoded[0].confidence == codec.to_float32(0.3)
    # wire_normalize models exactly what the wire did, so it can be trusted
    # to quantify the effect in the report.
    assert decoded[0] == codec.wire_normalize(obs)


# ---------------------------------------------------------------------------
# Unknown-field tolerance, both directions (D0 T5)
# ---------------------------------------------------------------------------


def _unknown_field_bytes(number: int, value: int) -> bytes:
    """A varint field the current schema does not define."""
    out = bytearray()
    tag = number << 3  # wire type 0 (varint)
    while tag > 0x7F:
        out.append((tag & 0x7F) | 0x80)
        tag >>= 7
    out.append(tag)
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def test_newer_peer_extra_fields_are_tolerated():
    """Forward compatibility: a field this build has never heard of."""
    envelope = canonical_envelope()
    observations = [canonical_observation(envelope)]
    packet = codec.build_observation_packet(envelope, observations)
    packet.observations[0].MergeFromString(_unknown_field_bytes(900, 42))
    packet.envelope.MergeFromString(_unknown_field_bytes(901, 7))
    body = packet.SerializeToString(deterministic=True)
    decoded_envelope, decoded = codec.decode_observation_packet(body)
    assert decoded_envelope == envelope
    assert decoded == observations


def test_the_unknown_field_really_was_in_the_bytes():
    """Can-fail guard: prove the previous test was not a no-op."""
    envelope = canonical_envelope()
    packet = codec.build_observation_packet(envelope, [canonical_observation(envelope)])
    clean = packet.SerializeToString(deterministic=True)
    packet.observations[0].MergeFromString(_unknown_field_bytes(900, 42))
    dirty = packet.SerializeToString(deterministic=True)
    assert len(dirty) > len(clean)


def test_older_peer_missing_optional_fields_are_tolerated():
    """Backward compatibility: fields an older node never sends."""
    envelope = canonical_envelope()
    packet = codec.build_observation_packet(
        envelope,
        [canonical_observation(envelope, local_blob_id=None, evidence_ref=None)],
    )
    packet.envelope.ClearField("line_readout_us")
    packet.envelope.ClearField("gain_db")
    _, decoded = codec.decode_observation_packet(
        packet.SerializeToString(deterministic=True)
    )
    assert decoded[0].local_blob_id is None
    assert decoded[0].evidence_ref is None
    assert decoded[0].envelope.line_readout_us == 0.0


def test_missing_required_measurement_fields_fail_loudly():
    """A field the contract requires must NOT be defaulted into existence."""
    envelope = canonical_envelope()
    packet = codec.build_observation_packet(envelope, [canonical_observation(envelope)])
    packet.observations[0].ClearField("cov_uu")  # D0: never zero
    with pytest.raises(Exception) as excinfo:
        codec.decode_observation_packet(packet.SerializeToString(deterministic=True))
    assert "cov_uu" in str(excinfo.value) or "positive" in str(excinfo.value)


def test_unknown_clock_domain_is_rejected_not_defaulted():
    envelope = canonical_envelope()
    packet = codec.build_observation_packet(envelope, [canonical_observation(envelope)])
    packet.envelope.clock_domain = 99
    with pytest.raises(ProtocolViolation, match="unknown clock domain"):
        codec.decode_observation_packet(packet.SerializeToString(deterministic=True))
    packet.envelope.clock_domain = pb.CLOCK_DOMAIN_UNSPECIFIED
    with pytest.raises(ProtocolViolation, match="UNSPECIFIED"):
        codec.decode_observation_packet(packet.SerializeToString(deterministic=True))


def test_per_observation_envelope_inside_a_packet_is_rejected():
    """The envelope-once rule: never silently reconciled."""
    envelope = canonical_envelope()
    packet = codec.build_observation_packet(envelope, [canonical_observation(envelope)])
    packet.observations[0].envelope.camera_id = 9
    with pytest.raises(ProtocolViolation, match="own envelope"):
        codec.decode_observation_packet(packet.SerializeToString(deterministic=True))


def test_observation_from_a_different_capture_event_is_rejected():
    envelope = canonical_envelope()
    other = canonical_envelope(frame_seq=1235)
    with pytest.raises(ProtocolViolation, match="does not belong"):
        codec.encode_observation_packet(envelope, [canonical_observation(other)])


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def test_header_shape_is_pinned():
    envelope = canonical_envelope()
    datagram = codec.encode_observation_packet(envelope, [canonical_observation(envelope)])
    assert datagram[:2] == MAGIC
    assert datagram[2] == WIRE_VERSION
    assert datagram[3] == int(PayloadType.OBSERVATION)
    assert HEADER_LEN == 4


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: b"XX" + d[2:], "bad magic"),
        (lambda d: d[:2] + bytes([WIRE_VERSION + 1]) + d[3:], "unsupported wire version"),
        (lambda d: d[:3] + bytes([250]) + d[4:], "unknown payload type"),
        (lambda d: d[:3], "shorter than"),
    ],
)
def test_bad_framing_is_rejected(mutate, match):
    envelope = canonical_envelope()
    datagram = codec.encode_observation_packet(envelope, [canonical_observation(envelope)])
    with pytest.raises(Exception, match=match):
        unframe(mutate(datagram))


# ---------------------------------------------------------------------------
# Golden bytes
# ---------------------------------------------------------------------------


def _golden(name: str, produce) -> None:
    """Compare (or regenerate) one pinned datagram."""
    path = GOLDEN_DIR / f"{name}.hex"
    actual = produce().hex()
    if REGENERATE:  # pragma: no cover - explicit, env-gated maintenance path
        # Write and then still ASSERT: a regeneration run must exercise the
        # same comparison, so a test containing several goldens regenerates
        # all of them rather than stopping at the first.
        path.write_text(actual + "\n", encoding="utf-8")
    assert path.exists(), (
        f"missing golden {path.name}; regenerate with "
        "SKYWEAVE_REGENERATE_WIRE_GOLDEN=1 and record the reason"
    )
    assert actual == path.read_text(encoding="utf-8").strip(), (
        f"{path.name}: the wire encoding changed. This is a wire-freeze break "
        "unless a decision says otherwise."
    )


def test_golden_observation_packet_single():
    envelope = canonical_envelope()
    _golden(
        "observation_single",
        lambda: codec.encode_observation_packet(envelope, [canonical_observation(envelope)]),
    )


def test_golden_observation_packet_multi_with_absent_optionals():
    envelope = canonical_envelope()
    observations = [
        canonical_observation(envelope, obs_id=0),
        canonical_observation(
            envelope, obs_id=1, local_blob_id=None, evidence_ref=None,
            u=-0.5, v=-0.5, bbox_x=-4, bbox_y=-4,
        ),
        canonical_observation(envelope, obs_id=2, persistence_count=1, confidence=0.5),
    ]
    _golden(
        "observation_multi",
        lambda: codec.encode_observation_packet(envelope, observations),
    )


def test_golden_health_packet():
    health = codec.Health(
        camera_id=1,
        session_uuid="8f14e45f-ceea-467a-9d3f-1b2c3d4e5f60",
        ts_ns=41_133_333_333,
        clock_domain=ClockDomain.NODE_PTP,
        fps=29.5,
        drops=17,
        time_sync_error_ms=0.25,
        imu_quaternion=(0.0, 0.0, 0.25, 0.96875),
    )
    _golden("health", lambda: codec.encode_health(health))


def test_golden_control_messages():
    from skyweave2.transport import control

    session = "8f14e45f-ceea-467a-9d3f-1b2c3d4e5f60"
    _golden(
        "control_start",
        lambda: codec.encode_control(control.start_message(1, session, control_seq=4)),
    )
    _golden(
        "control_calibration",
        lambda: codec.encode_control(
            control.calibration_message(1, session, "cal-r1", "det-r1", control_seq=5)
        ),
    )
    _golden(
        "control_config",
        lambda: codec.encode_control(
            control.config_message(1, session, 1536, 864, 30.0, 5, False, control_seq=6)
        ),
    )


def test_golden_evidence_packet():
    evidence = codec.Evidence(
        evidence_ref="patch-0001",
        camera_id=1,
        session_uuid="8f14e45f-ceea-467a-9d3f-1b2c3d4e5f60",
        frame_seq=1234,
        capture_ts_ns=41_133_333_333,
        expiry_ts_ns=41_633_333_333,
        kind=int(pb.EVIDENCE_KIND_PATCH_Y8),
        x0=1149,
        y0=644,
        width=8,
        height=8,
        payload=bytes(range(64)),
    )
    _golden("evidence", lambda: codec.encode_evidence(evidence))


def test_golden_files_decode_back_to_the_same_models():
    """Decode direction of the pin: stored bytes must still parse."""
    envelope = canonical_envelope()
    expected = [canonical_observation(envelope)]
    stored = bytes.fromhex(
        (GOLDEN_DIR / "observation_single.hex").read_text(encoding="utf-8").strip()
    )
    _, body = unframe(stored)
    decoded_envelope, decoded = codec.decode_observation_packet(body)
    assert decoded_envelope == envelope
    assert decoded == expected


def test_control_type_and_payload_must_agree():
    from skyweave2.transport import control

    session = "8f14e45f-ceea-467a-9d3f-1b2c3d4e5f60"
    message = control.calibration_message(1, session, "cal", "det")
    message.ClearField("calibration")
    with pytest.raises(ProtocolViolation, match="carries no calibration"):
        codec.encode_control(message)
    mismatched = control.start_message(1, session)
    mismatched.config.proc_width = 1536
    with pytest.raises(ProtocolViolation, match="unexpected config"):
        codec.encode_control(mismatched)
