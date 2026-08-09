"""Socket ingest adapter: datagrams in, Observation2D out. Nothing else.

The iron rule of this phase is that the fusion engine and every threshold are
frozen, so this adapter deliberately does NOT:

- reorder, buffer, or de-duplicate anything (the ``EventAligner`` owns session,
  duplicate, reorder and lateness policy, and it must see exactly what the
  wire delivered);
- repair, clamp, or default a decoded field (``codec`` raises instead);
- let evidence gate a measurement (evidence is read on its own socket and can
  be dropped entirely without touching the observation stream).

What it does own is receipt bookkeeping. D0 section 3 requires the Jetson to
store receive time BESIDE capture time and never overwrite it, so every
delivered datagram produces a :class:`Receipt` that lives outside the frozen
envelope.

A datagram that cannot be decoded is counted under a LABELED reason and
dropped — one corrupt packet may not kill ingest — but the count is part of
the adapter's public state and the rig and report both assert on it. Set
``raise_on_reject`` to turn any rejection into an immediate failure; the tests
use it to prove the loud path exists.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from skyweave2.contracts import ClockDomain, Observation2D
from skyweave2.transport.codec import (
    Evidence,
    decode_evidence,
    decode_health,
    decode_observation_packet,
)
from skyweave2.transport.udp import UdpReceiver
from skyweave2.transport.wire import PayloadType, WireError, unframe


@dataclass(frozen=True)
class Receipt:
    """Receive-side record for one delivered capture event.

    Lives beside the frozen ``FrameEnvelope``; nothing here is ever written
    back into it. ``rx_clock_domain`` is always JETSON_RX (D0 section 3).
    """

    arrival_index: int
    camera_id: int
    session_uuid: str
    frame_seq: int
    capture_ts_ns: int
    observation_count: int
    datagram_bytes: int
    rx_monotonic_ns: int
    rx_clock_domain: ClockDomain = ClockDomain.JETSON_RX

    @property
    def identity(self) -> tuple[int, str, int]:
        return (self.camera_id, self.session_uuid, self.frame_seq)


@dataclass
class IngestStats:
    """Labeled ingest bookkeeping. Every drop has a reason."""

    datagrams: int = 0
    observations: int = 0
    bytes_total: int = 0
    sizes: list[int] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    health_packets: int = 0
    evidence_received: int = 0
    evidence_expired: int = 0

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())


class SocketIngestAdapter:
    """Feeds the UNCHANGED fusion engine from real sockets."""

    def __init__(
        self,
        receiver: UdpReceiver,
        evidence_receiver: UdpReceiver | None = None,
        raise_on_reject: bool = False,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._receiver = receiver
        self._evidence_receiver = evidence_receiver
        self._raise_on_reject = raise_on_reject
        self._monotonic_ns = monotonic_ns
        self.stats = IngestStats()
        # Set by drain() when its hard deadline fires: a run that ended on the
        # backstop is NOT a run that ended cleanly, and callers must be able
        # to tell the difference without reading a counter dictionary.
        self.deadline_exceeded = False
        self.receipts: list[Receipt] = []
        self.observations: list[Observation2D] = []
        self.evidence: list[Evidence] = []
        # The delivered-event stream: what the wire actually handed the engine,
        # in arrival order. This is the object W7 compares across processes.
        self.delivered_events: list[tuple[int, str, int, tuple[int, ...]]] = []

    # -- measurement plane -------------------------------------------------

    def _handle_measurement(self, body: bytes, size: int) -> list[Observation2D]:
        envelope, observations = decode_observation_packet(body)
        receipt = Receipt(
            arrival_index=len(self.receipts),
            camera_id=envelope.camera_id,
            session_uuid=envelope.session_uuid,
            frame_seq=envelope.frame_seq,
            capture_ts_ns=envelope.capture_ts_ns,
            observation_count=len(observations),
            datagram_bytes=size,
            rx_monotonic_ns=self._monotonic_ns(),
        )
        self.receipts.append(receipt)
        self.delivered_events.append(
            (
                envelope.camera_id,
                envelope.session_uuid,
                envelope.frame_seq,
                tuple(o.obs_id for o in observations),
            )
        )
        self.stats.datagrams += 1
        self.stats.observations += len(observations)
        self.stats.bytes_total += size
        self.stats.sizes.append(size)
        self.observations.extend(observations)
        return observations

    def poll(self, timeout_s: float | None) -> list[Observation2D]:
        """Read at most one measurement datagram; return its observations.

        Empty list means "nothing arrived, or what arrived was rejected" —
        the two are distinguished by :attr:`stats`, never conflated.
        """
        datagram = self._receiver.recv(timeout_s)
        if datagram is None:
            return []
        try:
            payload_type, body = unframe(datagram)
        except WireError as exc:
            return self._reject(f"framing:{type(exc).__name__}", exc)
        if payload_type is PayloadType.HEALTH:
            try:
                decode_health(body)
            except WireError as exc:
                return self._reject("health_decode", exc)
            self.stats.health_packets += 1
            return []
        if payload_type is not PayloadType.OBSERVATION:
            return self._reject(f"unexpected_payload:{payload_type.name}", None)
        try:
            return self._handle_measurement(body, len(datagram))
        except Exception as exc:  # noqa: BLE001 - decode/validation, both labeled
            return self._reject(f"decode:{type(exc).__name__}", exc)

    def _reject(self, reason: str, exc: Exception | None) -> list[Observation2D]:
        self.stats.reject(reason)
        if self._raise_on_reject:
            if exc is not None:
                raise exc
            raise WireError(f"rejected datagram: {reason}")
        return []

    # -- evidence plane ----------------------------------------------------

    def poll_evidence(self, timeout_s: float | None, now_ns: int | None = None) -> None:
        """Read at most one evidence datagram.

        Evidence never produces, blocks, or modifies an observation. Expired
        evidence is counted and discarded rather than allowed to re-open a
        solved batch (the evidence-drop rule).
        """
        if self._evidence_receiver is None:
            return
        datagram = self._evidence_receiver.recv(timeout_s)
        if datagram is None:
            return
        try:
            payload_type, body = unframe(datagram)
            if payload_type is not PayloadType.EVIDENCE:
                self.stats.reject(f"evidence_channel_payload:{payload_type.name}")
                return
            evidence = decode_evidence(body)
        except WireError as exc:
            self.stats.reject(f"evidence_framing:{type(exc).__name__}")
            if self._raise_on_reject:
                raise
            return
        self.stats.evidence_received += 1
        reference_ns = self._monotonic_ns() if now_ns is None else now_ns
        if evidence.expired_at(reference_ns):
            self.stats.evidence_expired += 1
            return
        self.evidence.append(evidence)

    # -- drain loop --------------------------------------------------------

    def drain(
        self,
        idle_timeout_s: float = 0.25,
        max_idle_polls: int = 4,
        stop_when: Callable[[], bool] | None = None,
        poll_evidence: bool = True,
        deadline_s: float = 120.0,
    ) -> list[Observation2D]:
        """Receive until the stream goes quiet, then return the arrival order.

        With ``stop_when`` (the rig sets it from control-plane STOP messages)
        the idle counter only starts once every sender has signalled STOP —
        a STOP can overtake the last datagrams it follows, so draining
        continues until the socket is genuinely quiet. Dropping those would
        make the socket path lose measurements the file path keeps.

        ``deadline_s`` is a hard backstop against a lost STOP. Blowing it is
        recorded as a labeled rejection AND on :attr:`deadline_exceeded`: a
        truncated run must never be indistinguishable from a complete one.
        Both are wall-clock and neither enters a scored artifact.
        """
        deadline_ns = self._monotonic_ns() + int(deadline_s * 1e9)
        idle = 0
        while True:
            if self._monotonic_ns() > deadline_ns:
                self.deadline_exceeded = True
                self.stats.reject("drain_deadline_exceeded")
                break
            # Idleness is "no datagram arrived", NOT "no observation arrived".
            # A valid capture event carrying zero observations is traffic; so
            # is a rejected datagram. Counting either as idle would let a live
            # stream time the loop out and look like a clean finish.
            before = self.stats.datagrams + self.stats.rejected_total
            self.poll(idle_timeout_s)
            if poll_evidence:
                self.poll_evidence(0.0)
            if self.stats.datagrams + self.stats.rejected_total > before:
                idle = 0
                continue
            if stop_when is None or stop_when():
                idle += 1
                if idle >= max_idle_polls:
                    break
            else:
                idle = 0
        return self.observations

    def delivered_fingerprint(self) -> str:
        """Canonical digest of the DELIVERED-EVENT STREAM (W7).

        Decisions are only reproducible given the same delivered events, so
        this is the object that has to match across processes before any
        claim about identical decisions means anything.
        """
        import json

        return json.dumps(
            [list(event[:3]) + [list(event[3])] for event in self.delivered_events],
            sort_keys=True,
        )
