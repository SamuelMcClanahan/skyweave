"""pydantic contract models <-> protobuf wire messages (D7 freeze).

The codec is an ADAPTER and nothing else. It never rounds a measurement into
range, never fills a missing field with a plausible default, and never drops
an observation to make a packet fit. Every disagreement between what arrived
and what the frozen contract allows exits as an exception.

Three asymmetries are handled explicitly, because each is a place where a
codec can quietly lie:

1. **Envelope-once.** D0 field 1 of ``Observation2D`` is "FrameEnvelope OR
   REFERENCE TO ONE". Inside an ``ObservationPacket`` the envelope rides once
   at packet level and per-observation field 1 must be UNSET. If a peer sets
   it, that is a protocol violation and is raised, not silently overridden —
   an overridden envelope would rewrite capture time, which D0 forbids.

2. **float32 narrowing.** D0's type column declares ``time_sync_error_ms``,
   ``exposure_us``, ``gain_db``, ``line_readout_us`` and ``confidence`` as
   ``float``, i.e. binary32, while pydantic holds Python doubles. Values not
   exactly representable in binary32 CHANGE on the wire. This codec follows
   the frozen declaration rather than widening the field, and exposes
   :func:`wire_normalize` so the effect can be measured and reported instead
   of hidden. ``u``, ``v``, the covariance terms and every timestamp are
   ``double``/integer and cross the wire exactly.

3. **Absent vs zero.** ``local_blob_id`` and ``evidence_ref`` are proto3
   ``optional`` so "no local blob" and "blob 0" stay distinguishable, and so
   dropping evidence is representable as absence rather than as an empty
   string (T7/W3).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from skyweave2.contracts import ClockDomain, FrameEnvelope, Observation2D
from skyweave2.transport.proto import skyweave_pb2 as pb
from skyweave2.transport.wire import (
    WIRE_LIMITS,
    PayloadType,
    ProtocolViolation,
    TooManyObservations,
    WireLimits,
    frame,
)

# --------------------------------------------------------------------------
# Enum mapping. The pydantic side is the frozen wire-stable STRING (D0
# enums.py); the protobuf side is the D7 numeric encoding of the same set.
# --------------------------------------------------------------------------

_CLOCK_TO_PB = {
    ClockDomain.SYNTHETIC: pb.CLOCK_DOMAIN_SYNTHETIC,
    ClockDomain.NODE_MONO: pb.CLOCK_DOMAIN_NODE_MONO,
    ClockDomain.NODE_PTP: pb.CLOCK_DOMAIN_NODE_PTP,
    ClockDomain.JETSON_RX: pb.CLOCK_DOMAIN_JETSON_RX,
}
_PB_TO_CLOCK = {value: key for key, value in _CLOCK_TO_PB.items()}


def _clock_to_pb(domain: ClockDomain) -> int:
    try:
        return _CLOCK_TO_PB[domain]
    except KeyError as exc:  # pragma: no cover - only if enums.py grows a value
        raise ProtocolViolation(
            f"clock domain {domain!r} has no D7 wire number; add it to the "
            ".proto and record the decision before sending it"
        ) from exc


def _clock_from_pb(value: int) -> ClockDomain:
    if value == pb.CLOCK_DOMAIN_UNSPECIFIED:
        raise ProtocolViolation(
            "clock_domain is UNSPECIFIED — a timestamp with no domain is "
            "unusable (D0 section 3); rejected rather than defaulted"
        )
    try:
        return _PB_TO_CLOCK[value]
    except KeyError as exc:
        raise ProtocolViolation(
            f"unknown clock domain number {value} from a newer peer; the "
            "timestamp cannot be mapped, so the packet is rejected"
        ) from exc


def to_float32(value: float) -> float:
    """Round a Python double through binary32, as a proto ``float`` field does."""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


_FLT_MAX = 3.4028234663852886e38


def _check_float32(name: str, value: float) -> None:
    """A D0-declared ``float`` field cannot carry a value binary32 has no room for.

    Narrowing a double to its nearest binary32 neighbour is the documented,
    bounded cost of D0's type column. SATURATING to infinity is not: a finite
    measurement silently becoming ``inf`` is total loss wearing the costume of
    a rounding error. protobuf saturates, so the check happens here.

    Infinities and NaN pass through: they are values the sender chose to send,
    and turning them into an encode failure would hide them rather than carry
    them honestly to a receiver that must decide what to do.
    """
    if value != value or value in (float("inf"), float("-inf")):
        return
    if abs(value) > _FLT_MAX:
        raise ProtocolViolation(
            f"{name} is {value!r}, outside the range of the binary32 `float` "
            f"field D0 declares (|v| <= {_FLT_MAX!r}); it would cross the wire "
            "as infinity — a total loss, not a narrowing — so it is rejected "
            "rather than saturated"
        )


def _check_text(name: str, value: str, max_size: int) -> None:
    """Enforce a declared nanopb ``max_size`` at encode time.

    The bound is declared once in ``proto/skyweave.options`` and the D8 C
    daemon allocates from it. Python has no such limit, so without this check
    a host could happily emit a 200-character ``calibration_rev``: under the
    byte ceiling, decodable by this build, and undecodable by the board that
    has to receive it. Same argument as the observation-count bound — a
    declared bound that only one side enforces is not a bound.

    The comparison is on UTF-8 BYTES, not characters: nanopb sizes buffers in
    bytes, so a short string of multi-byte characters can still overflow.
    ``max_size`` includes the NUL terminator, hence the ``- 1``.
    """
    encoded = len(value.encode("utf-8"))
    if encoded > max_size - 1:
        raise ProtocolViolation(
            f"{name} is {encoded} UTF-8 bytes, over the declared nanopb bound "
            f"of {max_size - 1} (max_size {max_size} in proto/skyweave.options); "
            "the D8 nanopb daemon allocates from that bound and could not "
            "decode this datagram"
        )


# --------------------------------------------------------------------------
# Encode
# --------------------------------------------------------------------------


def _fill_envelope(
    target: pb.FrameEnvelope, envelope: FrameEnvelope, limits: WireLimits = WIRE_LIMITS
) -> None:
    _check_text("session_uuid", envelope.session_uuid, limits.session_uuid_max_size)
    _check_text(
        "calibration_rev", envelope.calibration_rev, limits.calibration_rev_max_size
    )
    _check_text("detector_rev", envelope.detector_rev, limits.detector_rev_max_size)
    _check_float32("time_sync_error_ms", envelope.time_sync_error_ms)
    _check_float32("exposure_us", envelope.exposure_us)
    _check_float32("gain_db", envelope.gain_db)
    _check_float32("line_readout_us", envelope.line_readout_us)
    target.camera_id = envelope.camera_id
    target.session_uuid = envelope.session_uuid
    target.frame_seq = envelope.frame_seq
    target.capture_ts_ns = envelope.capture_ts_ns
    target.clock_domain = _clock_to_pb(envelope.clock_domain)
    target.time_sync_error_ms = envelope.time_sync_error_ms
    target.exposure_us = envelope.exposure_us
    target.gain_db = envelope.gain_db
    target.full_width = envelope.full_width
    target.full_height = envelope.full_height
    target.proc_width = envelope.proc_width
    target.proc_height = envelope.proc_height
    target.calibration_rev = envelope.calibration_rev
    target.detector_rev = envelope.detector_rev
    target.line_readout_us = envelope.line_readout_us


def _fill_observation(
    target: pb.Observation2D, obs: Observation2D, limits: WireLimits = WIRE_LIMITS
) -> None:
    """Fill everything EXCEPT field 1 (envelope), which rides at packet level."""
    if obs.evidence_ref is not None:
        _check_text("evidence_ref", obs.evidence_ref, limits.evidence_ref_max_size)
    _check_float32("confidence", obs.confidence)
    target.obs_id = obs.obs_id
    target.u = obs.u
    target.v = obs.v
    target.cov_uu = obs.cov_uu
    target.cov_uv = obs.cov_uv
    target.cov_vv = obs.cov_vv
    target.bbox.x = obs.bbox_x
    target.bbox.y = obs.bbox_y
    target.bbox.w = obs.bbox_w
    target.bbox.h = obs.bbox_h
    target.area_px = obs.area_px
    target.persistence_count = obs.persistence_count
    target.confidence = obs.confidence
    if obs.local_blob_id is not None:
        target.local_blob_id = obs.local_blob_id
    if obs.evidence_ref is not None:
        target.evidence_ref = obs.evidence_ref


def build_observation_packet(
    envelope: FrameEnvelope,
    observations: list[Observation2D],
    limits: WireLimits = WIRE_LIMITS,
) -> pb.ObservationPacket:
    """One capture event as a protobuf message (no framing, no size check).

    Every observation must belong to the packet's capture event: a mismatched
    envelope would be silently erased by the envelope-once encoding, so it is
    rejected here instead.
    """
    packet = pb.ObservationPacket()
    _fill_envelope(packet.envelope, envelope, limits)
    for obs in observations:
        if obs.envelope != envelope:
            raise ProtocolViolation(
                "observation "
                f"{obs.envelope.camera_id}:{obs.envelope.frame_seq}:{obs.obs_id} "
                "does not belong to this packet's capture event; the "
                "envelope-once encoding would erase the difference"
            )
        _fill_observation(packet.observations.add(), obs, limits)
    return packet


def encode_observation_packet(
    envelope: FrameEnvelope,
    observations: list[Observation2D],
    limits: WireLimits = WIRE_LIMITS,
) -> bytes:
    """A framed, bound-checked measurement datagram for one capture event.

    Two independent bounds, both loud, neither inferred from the other:
    ``TooManyObservations`` when the event exceeds the declared count (what
    nanopb allocates from) and ``DatagramTooLarge`` when the encoded bytes
    exceed the ceiling (what the network carries). There is no truncating
    path and no splitting path — an event that cannot be sent whole is not
    sent at all, and the caller learns exactly how far over it was.
    """
    detail = (
        f"camera {envelope.camera_id} frame {envelope.frame_seq}, "
        f"{len(observations)} observations"
    )
    if len(observations) > limits.observations_max_count:
        raise TooManyObservations(
            len(observations), limits.observations_max_count, detail
        )
    packet = build_observation_packet(envelope, observations, limits)
    body = packet.SerializeToString(deterministic=True)
    return frame(PayloadType.OBSERVATION, body, detail=detail)


# --------------------------------------------------------------------------
# Decode
# --------------------------------------------------------------------------


def envelope_from_pb(message: pb.FrameEnvelope) -> FrameEnvelope:
    """Protobuf envelope -> frozen contract model (pydantic validates)."""
    return FrameEnvelope(
        camera_id=message.camera_id,
        session_uuid=message.session_uuid,
        frame_seq=message.frame_seq,
        capture_ts_ns=message.capture_ts_ns,
        clock_domain=_clock_from_pb(message.clock_domain),
        time_sync_error_ms=message.time_sync_error_ms,
        exposure_us=message.exposure_us,
        gain_db=message.gain_db,
        full_width=message.full_width,
        full_height=message.full_height,
        proc_width=message.proc_width,
        proc_height=message.proc_height,
        calibration_rev=message.calibration_rev,
        detector_rev=message.detector_rev,
        line_readout_us=message.line_readout_us,
    )


def observation_from_pb(
    message: pb.Observation2D, envelope: FrameEnvelope
) -> Observation2D:
    if not message.HasField("bbox"):
        raise ProtocolViolation(
            f"observation {message.obs_id} carries no bbox (D0 field 8); a "
            "measurement without its bounding box is incomplete, not empty"
        )
    return Observation2D(
        envelope=envelope,
        obs_id=message.obs_id,
        u=message.u,
        v=message.v,
        cov_uu=message.cov_uu,
        cov_uv=message.cov_uv,
        cov_vv=message.cov_vv,
        bbox_x=message.bbox.x,
        bbox_y=message.bbox.y,
        bbox_w=message.bbox.w,
        bbox_h=message.bbox.h,
        area_px=message.area_px,
        persistence_count=message.persistence_count,
        confidence=message.confidence,
        local_blob_id=(
            message.local_blob_id if message.HasField("local_blob_id") else None
        ),
        evidence_ref=(
            message.evidence_ref if message.HasField("evidence_ref") else None
        ),
    )


def decode_observation_packet(body: bytes) -> tuple[FrameEnvelope, list[Observation2D]]:
    """Protobuf body -> (envelope, observations), fully validated.

    ``ParseFromString`` keeps unknown field numbers in the unknown-field set
    and ignores them, which is the forward-compatibility half of T5: a newer
    peer's extra fields must not break this parser. The backward half — this
    parser's fields missing from an older peer's message — surfaces as
    pydantic validation errors on fields the contract requires.
    """
    packet = pb.ObservationPacket()
    packet.ParseFromString(body)
    if not packet.HasField("envelope"):
        raise ProtocolViolation(
            "ObservationPacket carries no envelope; a measurement with no "
            "capture-event identity cannot be aligned (D0 section 4)"
        )
    envelope = envelope_from_pb(packet.envelope)
    observations: list[Observation2D] = []
    for message in packet.observations:
        if message.HasField("envelope"):
            raise ProtocolViolation(
                f"observation {message.obs_id} carries its own envelope inside "
                "an ObservationPacket; the packet-level envelope is "
                "authoritative and capture time is never overwritten (D0 "
                "section 3) — rejected rather than reconciled"
            )
        observations.append(observation_from_pb(message, envelope))
    return envelope, observations


def wire_normalize(obs: Observation2D) -> Observation2D:
    """The observation as the wire can represent it, without any I/O.

    Only the D0-declared ``float`` fields change. Used to MEASURE the
    narrowing in the report and in tests — never on the ingest path, where the
    real decode is the authority.
    """
    envelope = obs.envelope.model_copy(
        update={
            "time_sync_error_ms": to_float32(obs.envelope.time_sync_error_ms),
            "exposure_us": to_float32(obs.envelope.exposure_us),
            "gain_db": to_float32(obs.envelope.gain_db),
            "line_readout_us": to_float32(obs.envelope.line_readout_us),
        }
    )
    return obs.model_copy(
        update={
            "envelope": envelope,
            "confidence": to_float32(obs.confidence),
        }
    )


# --------------------------------------------------------------------------
# Health plane
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    """Low-rate node telemetry (brief: fps, drops, time_sync_error_ms, IMU).

    Not a D0 contract type — it carries no measurement and never reaches the
    estimator. The IMU quaternion is xyzw per D0 section 1, and is mount
    telemetry only: "derived, never authoritative".
    """

    camera_id: int
    session_uuid: str
    ts_ns: int
    clock_domain: ClockDomain
    fps: float
    drops: int
    time_sync_error_ms: float
    imu_quaternion: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)


def encode_health(health: Health, limits: WireLimits = WIRE_LIMITS) -> bytes:
    _check_text("session_uuid", health.session_uuid, limits.session_uuid_max_size)
    message = pb.HealthPacket(
        camera_id=health.camera_id,
        session_uuid=health.session_uuid,
        ts_ns=health.ts_ns,
        clock_domain=_clock_to_pb(health.clock_domain),
        fps=health.fps,
        drops=health.drops,
        time_sync_error_ms=health.time_sync_error_ms,
    )
    x, y, z, w = health.imu_quaternion
    message.imu_quaternion.x = x
    message.imu_quaternion.y = y
    message.imu_quaternion.z = z
    message.imu_quaternion.w = w
    return frame(PayloadType.HEALTH, message.SerializeToString(deterministic=True))


def decode_health(body: bytes) -> Health:
    message = pb.HealthPacket()
    message.ParseFromString(body)
    quaternion = message.imu_quaternion
    return Health(
        camera_id=message.camera_id,
        session_uuid=message.session_uuid,
        ts_ns=message.ts_ns,
        clock_domain=_clock_from_pb(message.clock_domain),
        fps=message.fps,
        drops=message.drops,
        time_sync_error_ms=message.time_sync_error_ms,
        imu_quaternion=(quaternion.x, quaternion.y, quaternion.z, quaternion.w),
    )


# --------------------------------------------------------------------------
# Evidence plane
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """An optional, droppable patch/mask referenced by ``evidence_ref``.

    ``expiry_ts_ns`` is the evidence-drop rule made explicit: evidence that
    arrives after its expiry is discarded. It never re-opens a solved batch
    and never gates a measurement.
    """

    evidence_ref: str
    camera_id: int
    session_uuid: str
    frame_seq: int
    capture_ts_ns: int
    expiry_ts_ns: int
    kind: int
    x0: int
    y0: int
    width: int
    height: int
    payload: bytes

    def expired_at(self, now_ns: int) -> bool:
        return now_ns > self.expiry_ts_ns


def encode_evidence(evidence: Evidence, limits: WireLimits = WIRE_LIMITS) -> bytes:
    _check_text("evidence_ref", evidence.evidence_ref, limits.evidence_ref_max_size)
    _check_text("session_uuid", evidence.session_uuid, limits.session_uuid_max_size)
    if len(evidence.payload) > limits.evidence_payload_max_size:
        # The declared bound sits BELOW the evidence channel ceiling, so the
        # ceiling alone would not catch this: there is a window of payload
        # sizes that frames cleanly here and overruns a nanopb bytes[8192].
        raise ProtocolViolation(
            f"evidence payload is {len(evidence.payload)} B, over the declared "
            f"nanopb bound of {limits.evidence_payload_max_size}; the D8 daemon "
            "allocates from that bound and could not decode this datagram"
        )
    message = pb.EvidencePacket(
        evidence_ref=evidence.evidence_ref,
        camera_id=evidence.camera_id,
        session_uuid=evidence.session_uuid,
        frame_seq=evidence.frame_seq,
        capture_ts_ns=evidence.capture_ts_ns,
        expiry_ts_ns=evidence.expiry_ts_ns,
        kind=evidence.kind,
        x0=evidence.x0,
        y0=evidence.y0,
        width=evidence.width,
        height=evidence.height,
        payload=evidence.payload,
    )
    return frame(PayloadType.EVIDENCE, message.SerializeToString(deterministic=True))


def decode_evidence(body: bytes) -> Evidence:
    message = pb.EvidencePacket()
    message.ParseFromString(body)
    return Evidence(
        evidence_ref=message.evidence_ref,
        camera_id=message.camera_id,
        session_uuid=message.session_uuid,
        frame_seq=message.frame_seq,
        capture_ts_ns=message.capture_ts_ns,
        expiry_ts_ns=message.expiry_ts_ns,
        kind=message.kind,
        x0=message.x0,
        y0=message.y0,
        width=message.width,
        height=message.height,
        payload=message.payload,
    )


# --------------------------------------------------------------------------
# Control plane
# --------------------------------------------------------------------------

_CONTROL_PAYLOAD_FIELD = {
    pb.CONTROL_TYPE_SET_CONFIG: "config",
    pb.CONTROL_TYPE_CALIBRATION_REVISION: "calibration",
    pb.CONTROL_TYPE_ACK: "ack",
}


def encode_control(
    message: pb.ControlMessage, limits: WireLimits = WIRE_LIMITS
) -> bytes:
    """Frame a control message (the TCP length prefix is added by the channel)."""
    _check_control(message)
    _check_control_bounds(message, limits)
    return frame(PayloadType.CONTROL, message.SerializeToString(deterministic=True))


def _check_control_bounds(message: pb.ControlMessage, limits: WireLimits) -> None:
    """The control plane obeys the same declared bounds as everything else.

    Control and health are the planes where a silent drop costs most: they are
    how a node reports that something is wrong. A NACK the board cannot decode
    is worse than no NACK at all.
    """
    _check_text("session_uuid", message.session_uuid, limits.session_uuid_max_size)
    if message.HasField("calibration"):
        _check_text(
            "calibration_rev",
            message.calibration.calibration_rev,
            limits.calibration_rev_max_size,
        )
        _check_text(
            "detector_rev",
            message.calibration.detector_rev,
            limits.detector_rev_max_size,
        )
    if message.HasField("ack"):
        _check_text("ack.detail", message.ack.detail, limits.ack_detail_max_size)


def decode_control(body: bytes) -> pb.ControlMessage:
    message = pb.ControlMessage()
    message.ParseFromString(body)
    _check_control(message)
    return message


def _check_control(message: pb.ControlMessage) -> None:
    """The declared type and the carried payload must agree.

    A discriminated wrapper is only as good as this check: without it a
    SET_CONFIG carrying no config would read as "configure with all zeros".
    """
    if message.type == pb.CONTROL_TYPE_UNSPECIFIED:
        raise ProtocolViolation("control message has no type")
    expected = _CONTROL_PAYLOAD_FIELD.get(message.type)
    for control_type, field_name in _CONTROL_PAYLOAD_FIELD.items():
        present = message.HasField(field_name)
        if control_type == message.type and not present:
            raise ProtocolViolation(
                f"control type {pb.ControlType.Name(message.type)} carries no "
                f"{field_name} payload"
            )
        if control_type != message.type and present:
            raise ProtocolViolation(
                f"control type {pb.ControlType.Name(message.type)} carries an "
                f"unexpected {field_name} payload"
            )
    if expected is None and message.type not in (
        pb.CONTROL_TYPE_START,
        pb.CONTROL_TYPE_STOP,
    ):  # pragma: no cover - unreachable while ControlType has 5 values
        raise ProtocolViolation(
            f"control type {message.type} has no declared payload rule"
        )


__all__ = [
    "Evidence",
    "Health",
    "WIRE_LIMITS",
    "build_observation_packet",
    "decode_control",
    "decode_evidence",
    "decode_health",
    "decode_observation_packet",
    "encode_control",
    "encode_evidence",
    "encode_health",
    "encode_observation_packet",
    "envelope_from_pb",
    "observation_from_pb",
    "to_float32",
    "wire_normalize",
]
