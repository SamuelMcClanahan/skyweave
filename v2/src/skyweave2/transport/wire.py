"""Datagram framing, declared bounds, and the size ceiling (D7 freeze).

Framing is a fixed 4-byte header in front of every protobuf body:

    magic "SW" (2 B) | wire_version (1 B) | payload_type (1 B) | protobuf body

Rationale for a raw header rather than an outer protobuf wrapper: a C daemon
switching on two bytes needs no decoder to reject foreign traffic, nanopb
allocates nothing for it, and an unknown schema version is rejected BEFORE any
parsing. The same header prefixes the body of a TCP control frame, behind its
length prefix.

The 1200-byte ceiling is PROVISIONAL (D0 section 10, "D7 opening") and is a
hard limit in both directions:

- encode side: a body that would exceed it raises :class:`DatagramTooLarge`.
  There is no truncating path and no fragmenting path — measurement data never
  splits, so an event that does not fit is a loud failure, by design.
- receive side: the socket is read with a buffer far LARGER than the ceiling
  and oversized datagrams are rejected explicitly. Sizing the receive buffer
  at exactly the ceiling would make the kernel truncate an oversized datagram
  and hand up a body that parses as a short-but-valid packet — silent data
  loss dressed as success. See :data:`RECV_BUFFER_BYTES`.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

MAGIC = b"SW"
WIRE_VERSION = 1
HEADER_FORMAT = ">2sBB"
HEADER_LEN = struct.calcsize(HEADER_FORMAT)

# Provisional ceiling for the measurement plane (D0 section 10). Includes the
# 4-byte header: it is a ceiling on the DATAGRAM, which is what the network
# carries, not on the protobuf body alone.
DATAGRAM_CEILING_BYTES = 1200

# Evidence rides its own droppable channel and is explicitly allowed to exceed
# the measurement ceiling (and therefore to IP-fragment): losing it may never
# lose a measurement, so its size is not a real-time constraint.
EVIDENCE_CEILING_BYTES = 8 * 1024 + 256

# Deliberately much larger than any ceiling: the receiver must be able to SEE
# an over-ceiling datagram in order to reject it. A buffer equal to the
# ceiling would let the kernel silently discard the tail.
RECV_BUFFER_BYTES = 65535

# TCP control frames are length-prefixed; this bounds a single frame so a
# hostile or corrupt length cannot make the host allocate without limit.
CONTROL_FRAME_MAX_BYTES = 64 * 1024
CONTROL_LENGTH_PREFIX_FORMAT = ">I"
CONTROL_LENGTH_PREFIX_LEN = struct.calcsize(CONTROL_LENGTH_PREFIX_FORMAT)


class PayloadType(IntEnum):
    """What the protobuf body behind the header is."""

    OBSERVATION = 1
    HEALTH = 2
    EVIDENCE = 3
    CONTROL = 4


class WireError(Exception):
    """Base class for every loud failure on the wire path."""


class DatagramTooLarge(WireError):
    """An encoded datagram exceeded its channel ceiling.

    Raised instead of truncating or fragmenting. ``size`` is the size the
    datagram WOULD have had, so the caller can report the overshoot honestly.
    """

    def __init__(self, size: int, ceiling: int, detail: str = "") -> None:
        self.size = size
        self.ceiling = ceiling
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"datagram is {size} B, ceiling is {ceiling} B — "
            f"over by {size - ceiling} B{suffix}; measurement data never "
            "splits and is never truncated"
        )


class WireFormatError(WireError):
    """A datagram's framing is unusable: bad magic, version, type, or length."""


class ProtocolViolation(WireError):
    """Framing was fine but the decoded content breaks a D0/D7 rule."""


class TooManyObservations(WireError):
    """A capture event carried more observations than the declared bound.

    Separate from :class:`DatagramTooLarge` on purpose. A packet can sit well
    under 1200 B and still exceed ``observations max_count`` when the
    revision strings are short — and the D8 nanopb daemon allocates from the
    COUNT, so it would fail to decode a datagram this host was happy to send.
    Both bounds are enforced; neither is inferred from the other.
    """

    def __init__(self, count: int, bound: int, detail: str = "") -> None:
        self.count = count
        self.bound = bound
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"capture event carries {count} observations, declared bound is "
            f"{bound}{suffix}; measurement data never splits across datagrams, "
            "so this event cannot be sent under the current freeze"
        )


@dataclass(frozen=True)
class WireLimits:
    """Declared field bounds — the Python mirror of ``proto/skyweave.options``.

    nanopb allocates from the ``.options`` file and this module's ceiling
    arithmetic allocates from here. Test W2 parses both and fails if they
    ever disagree, so the D8 C daemon and the host can never be sized against
    different numbers.

    String sizes are nanopb ``max_size``: buffer bytes INCLUDING the NUL
    terminator, so a 36-character UUID is declared as 37.
    """

    session_uuid_max_size: int = 37
    calibration_rev_max_size: int = 65
    detector_rev_max_size: int = 65
    evidence_ref_max_size: int = 65
    evidence_payload_max_size: int = 8192
    ack_detail_max_size: int = 129
    # DERIVED from the ceiling, not chosen: see sizing.max_observations_under_ceiling.
    observations_max_count: int = 5

    def text_len(self, max_size: int) -> int:
        """Characters that fit in a nanopb buffer of ``max_size`` bytes."""
        return max_size - 1


WIRE_LIMITS = WireLimits()

_OPTIONS_PATH = Path(__file__).resolve().parents[3] / "proto" / "skyweave.options"

# .options entry -> WireLimits attribute. EVERY entry in the file must appear
# here: declared_limits_from_options rejects an unmapped one, so a bound can
# never be added to the .options file and quietly enforced nowhere. Several
# entries share an attribute (the same semantic bound declared on more than
# one message); the loader checks they agree rather than letting the last one
# win.
_OPTIONS_TO_LIMIT = {
    ("skyweave.v2.FrameEnvelope.session_uuid", "max_size"): "session_uuid_max_size",
    ("skyweave.v2.FrameEnvelope.calibration_rev", "max_size"): "calibration_rev_max_size",
    ("skyweave.v2.FrameEnvelope.detector_rev", "max_size"): "detector_rev_max_size",
    ("skyweave.v2.Observation2D.evidence_ref", "max_size"): "evidence_ref_max_size",
    ("skyweave.v2.ObservationPacket.observations", "max_count"): "observations_max_count",
    ("skyweave.v2.EvidencePacket.evidence_ref", "max_size"): "evidence_ref_max_size",
    ("skyweave.v2.EvidencePacket.session_uuid", "max_size"): "session_uuid_max_size",
    ("skyweave.v2.EvidencePacket.payload", "max_size"): "evidence_payload_max_size",
    ("skyweave.v2.HealthPacket.session_uuid", "max_size"): "session_uuid_max_size",
    ("skyweave.v2.CalibrationRevision.calibration_rev", "max_size"):
        "calibration_rev_max_size",
    ("skyweave.v2.CalibrationRevision.detector_rev", "max_size"):
        "detector_rev_max_size",
    ("skyweave.v2.Ack.detail", "max_size"): "ack_detail_max_size",
    ("skyweave.v2.ControlMessage.session_uuid", "max_size"): "session_uuid_max_size",
}

_OPTIONS_LINE = re.compile(
    r"^\s*(?P<field>[\w.]+)\s+(?P<key>max_size|max_count):(?P<value>\d+)\s*$"
)


def parse_options_file(path: str | Path | None = None) -> dict[tuple[str, str], int]:
    """Parse ``proto/skyweave.options`` into {(field, key): value}."""
    path = Path(path) if path is not None else _OPTIONS_PATH
    out: dict[tuple[str, str], int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _OPTIONS_LINE.match(line)
        if match is None:
            raise ValueError(f"unparsable nanopb options line: {line!r}")
        out[(match["field"], match["key"])] = int(match["value"])
    return out


def declared_limits_from_options(path: str | Path | None = None) -> WireLimits:
    """Build a :class:`WireLimits` straight from the ``.options`` declaration.

    Total in both directions. A bound the map does not cover is an ERROR, not
    a silent omission: otherwise a line can be added to the ``.options`` file,
    allocated by the D8 nanopb daemon, and enforced by nothing on this side —
    which is precisely the asymmetry the file exists to prevent.
    """
    parsed = parse_options_file(path)
    missing = [key for key in _OPTIONS_TO_LIMIT if key not in parsed]
    if missing:
        raise ValueError(f"nanopb options file is missing bounds for: {missing}")
    unmapped = [key for key in parsed if key not in _OPTIONS_TO_LIMIT]
    if unmapped:
        raise ValueError(
            f"nanopb options file declares bounds nothing enforces: {unmapped}; "
            "add them to _OPTIONS_TO_LIMIT and check them on the encode path"
        )
    values: dict[str, int] = {}
    for key, attribute in _OPTIONS_TO_LIMIT.items():
        value = parsed[key]
        if attribute in values and values[attribute] != value:
            raise ValueError(
                f"nanopb options file declares conflicting values for "
                f"{attribute}: {values[attribute]} and {value}"
            )
        values[attribute] = value
    return WireLimits(**values)


def ceiling_for(payload_type: PayloadType) -> int:
    """The datagram ceiling that applies to one payload type."""
    if payload_type is PayloadType.EVIDENCE:
        return EVIDENCE_CEILING_BYTES
    return DATAGRAM_CEILING_BYTES


def frame(payload_type: PayloadType, body: bytes, *, detail: str = "") -> bytes:
    """Header + protobuf body, size-checked against the channel ceiling.

    Raises :class:`DatagramTooLarge` rather than emitting anything partial.
    """
    datagram = struct.pack(HEADER_FORMAT, MAGIC, WIRE_VERSION, int(payload_type)) + body
    ceiling = ceiling_for(payload_type)
    if len(datagram) > ceiling:
        raise DatagramTooLarge(len(datagram), ceiling, detail)
    return datagram


def unframe(datagram: bytes) -> tuple[PayloadType, bytes]:
    """Validate the header and split off the protobuf body.

    Every rejection is explicit. An over-ceiling datagram is rejected HERE
    rather than parsed: accepting it would mean the peer is not honoring the
    freeze, and the size a receiver saw is the only evidence of that.
    """
    if len(datagram) < HEADER_LEN:
        raise WireFormatError(
            f"datagram is {len(datagram)} B, shorter than the {HEADER_LEN} B header"
        )
    magic, version, raw_type = struct.unpack(HEADER_FORMAT, datagram[:HEADER_LEN])
    if magic != MAGIC:
        raise WireFormatError(f"bad magic {magic!r}, expected {MAGIC!r}")
    if version != WIRE_VERSION:
        raise WireFormatError(
            f"unsupported wire version {version}, this build speaks {WIRE_VERSION}"
        )
    try:
        payload_type = PayloadType(raw_type)
    except ValueError as exc:
        raise WireFormatError(f"unknown payload type {raw_type}") from exc
    ceiling = ceiling_for(payload_type)
    if len(datagram) > ceiling:
        raise WireFormatError(
            f"received {len(datagram)} B on the {payload_type.name} channel, "
            f"over the {ceiling} B ceiling — the sender is not honoring the "
            "D7 freeze; rejected rather than parsed"
        )
    return payload_type, datagram[HEADER_LEN:]
