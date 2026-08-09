"""Time and session semantics (D0, frozen).

Four clock domains (see enums.ClockDomain). A timestamp crosses domains only
through an explicit ClockMapping; the original value is always kept.

Session rule: every node boot creates a new ``session_uuid``. ``frame_seq``
is monotonic within one session only. A reboot is detected by a UUID change,
never by heuristics on sequence numbers.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from skyweave2.contracts.base import ContractModel
from skyweave2.contracts.frames import FrameEnvelope


class ClockMapping(ContractModel):
    """Affine mapping from a source domain into a reference domain."""

    offset_ns: int = 0
    drift_ppm: float = 0.0
    uncertainty_ms: float = Field(ge=0.0, default=0.0)

    def map_ns(self, ts_ns: int) -> int:
        return int(ts_ns + self.offset_ns + ts_ns * self.drift_ppm * 1e-6)


class SessionEvent(str, Enum):
    IN_ORDER = "in_order"
    NEW_SESSION = "new_session"
    STALE_OR_DUPLICATE = "stale_or_duplicate"
    SEQUENCE_ANOMALY = "sequence_anomaly"


class SessionRegistry:
    """Per-camera session and sequence bookkeeping (host side).

    Not a wire type. Classifies each envelope so a node reboot (new UUID,
    sequence reset) is accepted cleanly while a same-session sequence
    regression is flagged instead of silently corrupting alignment.
    """

    def __init__(self) -> None:
        self._latest: dict[int, tuple[str, int]] = {}

    def classify(self, envelope: FrameEnvelope) -> SessionEvent:
        key = envelope.camera_id
        previous = self._latest.get(key)
        if previous is None or previous[0] != envelope.session_uuid:
            self._latest[key] = (envelope.session_uuid, envelope.frame_seq)
            return SessionEvent.NEW_SESSION if previous is not None else SessionEvent.IN_ORDER
        _, last_seq = previous
        if envelope.frame_seq <= last_seq:
            return SessionEvent.STALE_OR_DUPLICATE
        self._latest[key] = (envelope.session_uuid, envelope.frame_seq)
        return SessionEvent.IN_ORDER
