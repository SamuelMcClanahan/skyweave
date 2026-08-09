"""D7 wire freeze: protobuf schema, UDP/TCP transport, and replay.

The measurement path carries protobuf observations and never video. The vendor
RTSP stream stays an optional human debug stream; no H.264 ever rides the
measurement path (D0 section 10, standing rule).

Everything here is an ADAPTER around the frozen D5/D6 engine. No module in
this package imports a threshold, edits a contract model, or makes a policy
decision that the aligner, association stage or tracker already owns.
"""

from skyweave2.transport.adapter import IngestStats, Receipt, SocketIngestAdapter
from skyweave2.transport.codec import (
    Evidence,
    Health,
    decode_observation_packet,
    encode_observation_packet,
    wire_normalize,
)
from skyweave2.transport.replay import (
    CaptureEvent,
    ObservationReplaySource,
    PacingMode,
    capture_events,
)
from skyweave2.transport.wire import (
    DATAGRAM_CEILING_BYTES,
    WIRE_LIMITS,
    WIRE_VERSION,
    DatagramTooLarge,
    PayloadType,
    ProtocolViolation,
    TooManyObservations,
    WireError,
    WireFormatError,
    frame,
    unframe,
)

__all__ = [
    "CaptureEvent",
    "DATAGRAM_CEILING_BYTES",
    "DatagramTooLarge",
    "Evidence",
    "Health",
    "IngestStats",
    "ObservationReplaySource",
    "PacingMode",
    "PayloadType",
    "ProtocolViolation",
    "Receipt",
    "SocketIngestAdapter",
    "TooManyObservations",
    "WIRE_LIMITS",
    "WIRE_VERSION",
    "WireError",
    "WireFormatError",
    "capture_events",
    "decode_observation_packet",
    "encode_observation_packet",
    "frame",
    "unframe",
    "wire_normalize",
]
