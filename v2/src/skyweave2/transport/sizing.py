"""Worst-case datagram arithmetic for the 1200 B ceiling (W2).

"Verified against the worst case" has to mean the worst case the SCHEMA
allows, not the worst case the current gate clip happens to produce. So the
worst-case packet built here saturates every declared bound at once:

- every string at its nanopb ``max_size`` (36-char UUID, 64-char revisions,
  64-char evidence ref);
- every varint at its widest encoding (max uint32 / max uint64, and a
  NEGATIVE ``capture_ts_ns``, which costs a full 10-byte varint because proto3
  sign-extends ``int64``);
- ``bbox`` origins negative so the zigzag varints are wide;
- every optional field present.

A real gate-clip datagram is a fraction of this. That is the point: the
ceiling must hold for what the schema permits, and the margin is reported.
"""

from __future__ import annotations

from dataclasses import dataclass

from skyweave2.contracts import ClockDomain, FrameEnvelope, Observation2D
from skyweave2.transport.codec import build_observation_packet
from skyweave2.transport.wire import (
    DATAGRAM_CEILING_BYTES,
    HEADER_LEN,
    WIRE_LIMITS,
    WireLimits,
)

_MAX_U32 = 2**32 - 1
_MAX_U64 = 2**64 - 1
# Widest int64 varint: proto3 encodes a negative int64 as 10 bytes.
_WIDEST_I64 = -(2**62)


@dataclass(frozen=True)
class SizeBudget:
    """Where the worst-case bytes go."""

    header_bytes: int
    envelope_bytes: int
    per_observation_bytes: int
    observation_count: int
    total_bytes: int
    ceiling_bytes: int

    @property
    def headroom_bytes(self) -> int:
        return self.ceiling_bytes - self.total_bytes

    @property
    def fits(self) -> bool:
        return self.total_bytes <= self.ceiling_bytes


def worst_case_envelope(limits: WireLimits = WIRE_LIMITS) -> FrameEnvelope:
    """The largest FrameEnvelope the declared bounds permit."""
    return FrameEnvelope(
        camera_id=_MAX_U32,
        session_uuid="U" * limits.text_len(limits.session_uuid_max_size),
        frame_seq=_MAX_U64,
        capture_ts_ns=_WIDEST_I64,
        clock_domain=ClockDomain.JETSON_RX,
        # Values chosen so no float field lands on proto3's implicit zero
        # default (an omitted field would understate the worst case).
        time_sync_error_ms=1234.5678,
        exposure_us=65535.0,
        gain_db=48.0,
        full_width=_MAX_U32,
        full_height=_MAX_U32,
        proc_width=_MAX_U32,
        proc_height=_MAX_U32,
        calibration_rev="C" * limits.text_len(limits.calibration_rev_max_size),
        detector_rev="D" * limits.text_len(limits.detector_rev_max_size),
        line_readout_us=31.25,
    )


def worst_case_observation(
    envelope: FrameEnvelope, obs_id: int, limits: WireLimits = WIRE_LIMITS
) -> Observation2D:
    """The largest Observation2D the declared bounds permit."""
    return Observation2D(
        envelope=envelope,
        obs_id=obs_id,
        u=-1.2345678901234567e300,
        v=-9.8765432109876543e300,
        cov_uu=1.7976931348623157e308,
        cov_uv=-1.0e-300,
        cov_vv=1.7976931348623157e308,
        bbox_x=-(2**31),
        bbox_y=-(2**31),
        bbox_w=_MAX_U32,
        bbox_h=_MAX_U32,
        area_px=_MAX_U32,
        persistence_count=_MAX_U32,
        confidence=1.0,
        local_blob_id=_MAX_U32,
        evidence_ref="E" * limits.text_len(limits.evidence_ref_max_size),
    )


def worst_case_datagram_size(
    observation_count: int, limits: WireLimits = WIRE_LIMITS
) -> int:
    """Framed size of the worst packet holding ``observation_count`` items."""
    envelope = worst_case_envelope(limits)
    observations = [
        worst_case_observation(envelope, obs_id, limits)
        for obs_id in range(observation_count)
    ]
    packet = build_observation_packet(envelope, observations)
    return HEADER_LEN + len(packet.SerializeToString(deterministic=True))


def worst_case_budget(
    observation_count: int | None = None,
    limits: WireLimits = WIRE_LIMITS,
    ceiling: int = DATAGRAM_CEILING_BYTES,
) -> SizeBudget:
    """Worst-case budget for the declared observation bound (or a given count)."""
    count = (
        limits.observations_max_count if observation_count is None else observation_count
    )
    empty = worst_case_datagram_size(0, limits)
    total = worst_case_datagram_size(count, limits)
    one = worst_case_datagram_size(1, limits)
    return SizeBudget(
        header_bytes=HEADER_LEN,
        envelope_bytes=empty - HEADER_LEN,
        per_observation_bytes=one - empty,
        observation_count=count,
        total_bytes=total,
        ceiling_bytes=ceiling,
    )


def max_observations_under_ceiling(
    limits: WireLimits = WIRE_LIMITS,
    ceiling: int = DATAGRAM_CEILING_BYTES,
    search_limit: int = 256,
) -> int:
    """Largest observation count whose WORST case still fits the ceiling.

    Measured by encoding, not by arithmetic on estimated field widths: the
    estimate is what would drift silently as the schema changes.
    """
    best = 0
    for count in range(0, search_limit + 1):
        if worst_case_datagram_size(count, limits) <= ceiling:
            best = count
        else:
            break
    return best
