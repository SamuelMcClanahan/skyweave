"""``SWOB``: observations as bytes, so C and Python can be handed the SAME input.

Test E2 asks whether the daemon's nanopb encoder and the host codec produce
identical datagrams "for identical observation inputs". That word does the
work: if the two sides read the fixture through different parsers they are
not identical inputs, and a byte difference could be a JSON reader's
rounding rather than an encoder's disagreement.

So the fixture is a fixed-width binary record with no parsing choices in it:
network byte order, explicit lengths, a magic on every record, and the
D0-declared ``float`` fields stored as binary32 exactly as the wire will
carry them. The C reader is ``firmware/rv1106/src/sw_obsfixture.c`` and is
deliberately the dumbest possible loop.

A human-readable JSONL twin is written beside every fixture, and test E2
checks that the two describe the same observations. The binary is what the
encoders eat; the JSONL is what a reviewer reads.

**The fixture is wire-shaped, so it narrows where the wire narrows.**
``time_sync_error_ms``, ``exposure_us``, ``gain_db``, ``line_readout_us`` and
``confidence`` are D0-declared ``float`` and are stored here as binary32, so
``decode_fixture(encode_fixture(x)) == x`` is FALSE for a value binary32
cannot hold — it equals ``codec.wire_normalize(x)`` instead. That is D7-F2
reproduced deliberately: a fixture that carried doubles would hand the two
encoders inputs the wire cannot represent, and the byte comparison would be
measuring the fixture's precision rather than the encoders' agreement.
"""

from __future__ import annotations

import struct
from typing import BinaryIO

from skyweave2.contracts import ClockDomain, FrameEnvelope, Observation2D

FIXTURE_MAGIC = b"SWOB"
EVENT_MAGIC = b"SWOE"
OBSERVATION_MAGIC = b"SWOO"
FIXTURE_END_MAGIC = b"SWOZ"
FIXTURE_VERSION = 1

_HEADER = ">4sBBH"  # magic, version, reserved, event_count
#   camera_id, frame_seq, capture_ts_ns, clock_domain,
#   time_sync_error_ms, exposure_us, gain_db,
#   full_w, full_h, proc_w, proc_h, line_readout_us,
#   len(uuid), len(cal), len(det), obs_count
_EVENT = ">4sIQqBfffIIIIfBBBB"
#   obs_id, u, v, cov_uu, cov_uv, cov_vv, bbox_x, bbox_y, bbox_w, bbox_h,
#   area_px, persistence_count, confidence,
#   has_local_blob, local_blob_id, has_evidence_ref, len(evidence_ref)
_OBSERVATION = ">4sIdddddiiIIIIfBIBB"
_END = ">4sI"

_DOMAIN_TO_WIRE = {
    ClockDomain.SYNTHETIC: 1,
    ClockDomain.NODE_MONO: 2,
    ClockDomain.NODE_PTP: 3,
    ClockDomain.JETSON_RX: 4,
}
_WIRE_TO_DOMAIN = {value: key for key, value in _DOMAIN_TO_WIRE.items()}

CaptureEvent = tuple[FrameEnvelope, list[Observation2D]]


class ObsFixtureError(Exception):
    """A malformed observation fixture. Never recovered."""


def group_by_capture_event(observations: list[Observation2D]) -> list[CaptureEvent]:
    """Split a flat observation stream into (envelope, observations) events.

    Grouping is by envelope EQUALITY, in arrival order, and consecutive runs
    only: the wire rule is one capture event per datagram, and an event that
    reappeared after another camera's would be a second datagram, not a
    merge. Merging non-adjacent runs here would let the fixture pack a packet
    the live path never builds.
    """
    events: list[CaptureEvent] = []
    for obs in observations:
        if events and events[-1][0] == obs.envelope:
            events[-1][1].append(obs)
        else:
            events.append((obs.envelope, [obs]))
    return events


def _encode_event(envelope: FrameEnvelope, observations: list[Observation2D]) -> bytes:
    if len(observations) > 255:
        raise ObsFixtureError(
            f"capture event carries {len(observations)} observations; the "
            "fixture declares the count in one byte and the wire bound is far "
            "below 255 anyway"
        )
    uuid_bytes = envelope.session_uuid.encode("utf-8")
    cal_bytes = envelope.calibration_rev.encode("utf-8")
    det_bytes = envelope.detector_rev.encode("utf-8")
    for name, raw in (
        ("session_uuid", uuid_bytes),
        ("calibration_rev", cal_bytes),
        ("detector_rev", det_bytes),
    ):
        if len(raw) > 255:
            raise ObsFixtureError(f"{name} is {len(raw)} UTF-8 bytes, over 255")
    out = [
        struct.pack(
            _EVENT,
            EVENT_MAGIC,
            envelope.camera_id,
            envelope.frame_seq,
            envelope.capture_ts_ns,
            _DOMAIN_TO_WIRE[envelope.clock_domain],
            envelope.time_sync_error_ms,
            envelope.exposure_us,
            envelope.gain_db,
            envelope.full_width,
            envelope.full_height,
            envelope.proc_width,
            envelope.proc_height,
            envelope.line_readout_us,
            len(uuid_bytes),
            len(cal_bytes),
            len(det_bytes),
            len(observations),
        ),
        uuid_bytes,
        cal_bytes,
        det_bytes,
    ]
    for obs in observations:
        evidence = b"" if obs.evidence_ref is None else obs.evidence_ref.encode("utf-8")
        if len(evidence) > 255:
            raise ObsFixtureError(f"evidence_ref is {len(evidence)} UTF-8 bytes, over 255")
        out.append(
            struct.pack(
                _OBSERVATION,
                OBSERVATION_MAGIC,
                obs.obs_id,
                obs.u,
                obs.v,
                obs.cov_uu,
                obs.cov_uv,
                obs.cov_vv,
                obs.bbox_x,
                obs.bbox_y,
                obs.bbox_w,
                obs.bbox_h,
                obs.area_px,
                obs.persistence_count,
                obs.confidence,
                0 if obs.local_blob_id is None else 1,
                0 if obs.local_blob_id is None else obs.local_blob_id,
                0 if obs.evidence_ref is None else 1,
                len(evidence),
            )
        )
        out.append(evidence)
    return b"".join(out)


def encode_fixture(events: list[CaptureEvent]) -> bytes:
    """Serialize capture events into the ``SWOB`` fixture format."""
    if len(events) > 0xFFFF:
        raise ObsFixtureError(f"{len(events)} events; the header counts in 16 bits")
    chunks = [struct.pack(_HEADER, FIXTURE_MAGIC, FIXTURE_VERSION, 0, len(events))]
    for envelope, observations in events:
        chunks.append(_encode_event(envelope, observations))
    chunks.append(struct.pack(_END, FIXTURE_END_MAGIC, len(events)))
    return b"".join(chunks)


def _read_exactly(stream: BinaryIO, count: int, what: str) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise ObsFixtureError(
            f"observation fixture truncated reading {what}: wanted {count} B, "
            f"got {len(data)} B"
        )
    return data


def decode_fixture(stream: BinaryIO) -> list[CaptureEvent]:
    """Read a ``SWOB`` fixture back into frozen-contract models.

    Round-tripping through this in the tests is what makes the fixture a
    shared input rather than a Python-shaped one: if the format could not
    carry a field, the round trip fails here before any C ever sees it.
    """
    magic, version, reserved, declared = struct.unpack(
        _HEADER, _read_exactly(stream, struct.calcsize(_HEADER), "the fixture header")
    )
    if magic != FIXTURE_MAGIC:
        raise ObsFixtureError(f"bad fixture magic {magic!r}")
    if version != FIXTURE_VERSION:
        raise ObsFixtureError(f"fixture version {version}, this build speaks "
                              f"{FIXTURE_VERSION}")
    if reserved != 0:
        raise ObsFixtureError(f"reserved byte is {reserved}, must be 0")
    events: list[CaptureEvent] = []
    for _ in range(declared):
        events.append(_decode_event(stream))
    tail_magic, tail_count = struct.unpack(
        _END, _read_exactly(stream, struct.calcsize(_END), "the fixture trailer")
    )
    if tail_magic != FIXTURE_END_MAGIC:
        raise ObsFixtureError(f"bad fixture trailer magic {tail_magic!r}")
    if tail_count != declared:
        raise ObsFixtureError(
            f"fixture header declares {declared} events, trailer declares {tail_count}"
        )
    return events


def _decode_event(stream: BinaryIO) -> CaptureEvent:
    fixed = _read_exactly(stream, struct.calcsize(_EVENT), "an event header")
    (
        magic, camera_id, frame_seq, capture_ts_ns, domain, time_sync_error_ms,
        exposure_us, gain_db, full_width, full_height, proc_width, proc_height,
        line_readout_us, uuid_len, cal_len, det_len, obs_count,
    ) = struct.unpack(_EVENT, fixed)
    if magic != EVENT_MAGIC:
        raise ObsFixtureError(f"bad event magic {magic!r}")
    try:
        clock_domain = _WIRE_TO_DOMAIN[domain]
    except KeyError as exc:
        raise ObsFixtureError(f"unknown clock domain number {domain}") from exc
    envelope = FrameEnvelope(
        camera_id=camera_id,
        session_uuid=_read_exactly(stream, uuid_len, "session_uuid").decode("utf-8"),
        frame_seq=frame_seq,
        capture_ts_ns=capture_ts_ns,
        clock_domain=clock_domain,
        time_sync_error_ms=time_sync_error_ms,
        exposure_us=exposure_us,
        gain_db=gain_db,
        full_width=full_width,
        full_height=full_height,
        proc_width=proc_width,
        proc_height=proc_height,
        calibration_rev=_read_exactly(stream, cal_len, "calibration_rev").decode("utf-8"),
        detector_rev=_read_exactly(stream, det_len, "detector_rev").decode("utf-8"),
        line_readout_us=line_readout_us,
    )
    observations = [_decode_observation(stream, envelope) for _ in range(obs_count)]
    return envelope, observations


def _decode_observation(stream: BinaryIO, envelope: FrameEnvelope) -> Observation2D:
    fixed = _read_exactly(stream, struct.calcsize(_OBSERVATION), "an observation")
    (
        magic, obs_id, u, v, cov_uu, cov_uv, cov_vv, bbox_x, bbox_y, bbox_w,
        bbox_h, area_px, persistence_count, confidence, has_blob, blob_id,
        has_evidence, evidence_len,
    ) = struct.unpack(_OBSERVATION, fixed)
    if magic != OBSERVATION_MAGIC:
        raise ObsFixtureError(f"bad observation magic {magic!r}")
    evidence = _read_exactly(stream, evidence_len, "evidence_ref").decode("utf-8")
    return Observation2D(
        envelope=envelope,
        obs_id=obs_id,
        u=u,
        v=v,
        cov_uu=cov_uu,
        cov_uv=cov_uv,
        cov_vv=cov_vv,
        bbox_x=bbox_x,
        bbox_y=bbox_y,
        bbox_w=bbox_w,
        bbox_h=bbox_h,
        area_px=area_px,
        persistence_count=persistence_count,
        confidence=confidence,
        local_blob_id=blob_id if has_blob else None,
        evidence_ref=evidence if has_evidence else None,
    )


__all__ = [
    "CaptureEvent",
    "FIXTURE_MAGIC",
    "FIXTURE_VERSION",
    "ObsFixtureError",
    "decode_fixture",
    "encode_fixture",
    "group_by_capture_event",
]
