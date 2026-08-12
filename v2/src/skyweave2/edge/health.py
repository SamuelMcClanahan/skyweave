"""Listening to the 1 Hz health plane.

The node design (section 10) asks every node for a 1 Hz health packet carrying
fps, drop count, thermals "if available", sync error and orientation. The
daemon sends one. Nothing on the host was listening: ``SocketIngestAdapter``
decodes a health datagram purely to validate it and throws the result away, and
its return contract belongs to the W-series, so this is a separate listener
rather than a change to that one.

Two things about the health plane shape everything here:

1. **Health rides the measurement port.** ``main.c`` opens ONE UDP socket to
   ``--jetson``/``--measurement-port`` and sends both observation packets and
   health packets down it. A listener bound to some other port hears silence,
   so this one binds the measurement port and demultiplexes on the framing
   header's payload type. Observation datagrams are counted, not decoded — the
   fixture path already gates their contents.

2. **One corrupt packet may not kill ingest.** ``decode_health`` raises on an
   unspecified clock domain, and a listener that dies on it would turn a single
   malformed datagram into a dead monitor. Every rejection is caught, labelled
   and counted; nothing is dropped without a name.

What this module measures and what it does NOT: it records the ARRIVAL times of
health packets and can report their cadence, but a cadence measured on a
loopback socket against a replay that finishes in two seconds is a statement
about this host. E7 — 1 Hz on the node, with real drop counters — needs the
board and stays board-gated. The listener is what makes that measurement
possible without writing new code in the middle of a bench session.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from skyweave2.transport.codec import Health, decode_health
from skyweave2.transport.udp import UdpReceiver
from skyweave2.transport.wire import PayloadType, WireError, unframe


@dataclass(frozen=True)
class HealthReading:
    """One health packet and when it landed.

    ``received_monotonic_ns`` is the LISTENER's clock, deliberately: the
    packet's own ``ts_ns`` is the node's monotonic clock in the node's domain,
    and comparing two clocks to derive a cadence would measure their offset.
    Cadence is an arrival-side question.
    """

    received_monotonic_ns: int
    health: Health


@dataclass
class ListenerStats:
    datagrams: int = 0
    health_packets: int = 0
    observation_packets: int = 0
    other_packets: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())

    def as_dict(self) -> dict:
        return {
            "datagrams": self.datagrams,
            "health_packets": self.health_packets,
            "observation_packets": self.observation_packets,
            "other_packets": self.other_packets,
            "rejected": dict(sorted(self.rejected.items())),
            "rejected_total": self.rejected_total,
        }


@dataclass(frozen=True)
class Cadence:
    """Inter-arrival statistics for one node's health packets."""

    packets: int
    span_s: float
    mean_period_s: float | None
    p95_period_s: float | None
    min_period_s: float | None
    max_period_s: float | None

    def as_dict(self) -> dict:
        return {
            "packets": self.packets,
            "span_s": self.span_s,
            "mean_period_s": self.mean_period_s,
            "p95_period_s": self.p95_period_s,
            "min_period_s": self.min_period_s,
            "max_period_s": self.max_period_s,
        }


def cadence(readings: list[HealthReading]) -> Cadence:
    """Inter-arrival stats. p95 is NEAREST-RANK, matching ``tolerance.py``.

    The repo already carries two p95 conventions — ``eval/scorecard`` uses an
    interpolated percentile, ``tolerance.Divergence`` uses nearest-rank — so
    this one says which it is rather than leaving a bound ambiguous. Nearest
    rank means the number reported is an interval that actually occurred.
    """
    if len(readings) < 2:
        return Cadence(
            packets=len(readings), span_s=0.0, mean_period_s=None,
            p95_period_s=None, min_period_s=None, max_period_s=None,
        )
    stamps = sorted(reading.received_monotonic_ns for reading in readings)
    # strict=False on purpose: the second sequence is the first shifted by one,
    # so it is shorter by one by construction — that is what makes these pairs
    # consecutive rather than a length bug.
    periods = [(b - a) / 1e9 for a, b in zip(stamps, stamps[1:], strict=False)]
    ordered = sorted(periods)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return Cadence(
        packets=len(readings),
        span_s=(stamps[-1] - stamps[0]) / 1e9,
        mean_period_s=sum(periods) / len(periods),
        p95_period_s=ordered[index],
        min_period_s=ordered[0],
        max_period_s=ordered[-1],
    )


def drop_counter_regressions(readings: list[HealthReading]) -> list[str]:
    """Every place a node's drop total went DOWN, named with both values.

    ``HealthPacket.drops`` is documented as monotonic within a session: it is a
    total since boot, so a decrease means a restart the session uuid did not
    admit, a counter that wrapped, or a packet from a different node arriving
    under the same identity. All three are findings; none of them is a number
    to average away.
    """
    per_node: dict[tuple[int, str], int] = {}
    out = []
    for reading in sorted(readings, key=lambda item: item.received_monotonic_ns):
        key = (reading.health.camera_id, reading.health.session_uuid)
        previous = per_node.get(key)
        if previous is not None and reading.health.drops < previous:
            out.append(
                f"camera {key[0]} session {key[1]}: drops went {previous} -> "
                f"{reading.health.drops}"
            )
        per_node[key] = reading.health.drops
    return out


class HealthListener:
    """A UDP listener for the measurement plane that keeps the health half.

    Bind it on the port the daemon is told to send to (``--measurement-port``),
    poll it while the daemon runs, and read the health packets back out. It
    owns its socket and closes it; it is a context manager for the same reason
    the transport's own sockets are.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        rcvbuf_bytes: int = 4 * 1024 * 1024,
        monotonic_ns=time.monotonic_ns,
    ) -> None:
        self._receiver = UdpReceiver(host=host, port=port, rcvbuf_bytes=rcvbuf_bytes)
        self._monotonic_ns = monotonic_ns
        self.stats = ListenerStats()
        self.readings: list[HealthReading] = []

    @property
    def port(self) -> int:
        return self._receiver.port

    def __enter__(self) -> HealthListener:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._receiver.close()

    def poll(self, timeout_s: float | None = 0.0) -> list[HealthReading]:
        """Drain whatever has arrived, returning only the NEW health readings."""
        fresh: list[HealthReading] = []
        while True:
            datagram = self._receiver.recv(timeout_s)
            if datagram is None:
                return fresh
            # Only the FIRST recv waits: after that the socket either has more
            # queued or it does not, and blocking again would turn a drain into
            # a sleep.
            timeout_s = 0.0
            self.stats.datagrams += 1
            try:
                payload_type, body = unframe(datagram)
            except WireError as exc:
                self.stats.reject(f"unframe: {type(exc).__name__}")
                continue
            if payload_type is PayloadType.OBSERVATION:
                self.stats.observation_packets += 1
                continue
            if payload_type is not PayloadType.HEALTH:
                self.stats.other_packets += 1
                continue
            try:
                health = decode_health(body)
            except Exception as exc:  # noqa: BLE001 - decode/validation, labelled
                # Broad on purpose, and the breadth is a finding rather than a
                # style choice: `decode_health` raises `ProtocolViolation` (a
                # WireError) for a bad clock domain, but protobuf's own
                # `DecodeError` for a corrupt BODY, and that one is not a
                # WireError at all. Catching only WireError here let a single
                # malformed datagram kill the listener — which the corrupt-
                # datagram test caught, and which finding D8-F10 recorded as
                # also true of `SocketIngestAdapter.poll`. D8.1 A3 closed that
                # one; the two branches now have the same shape on purpose.
                self.stats.reject(f"decode_health: {type(exc).__name__}")
                continue
            self.stats.health_packets += 1
            reading = HealthReading(
                received_monotonic_ns=self._monotonic_ns(), health=health
            )
            self.readings.append(reading)
            fresh.append(reading)

    def collect(self, duration_s: float, poll_timeout_s: float = 0.25) -> list[HealthReading]:
        """Listen for a wall-clock interval and return what arrived in it."""
        deadline = time.monotonic() + duration_s
        collected: list[HealthReading] = []
        while time.monotonic() < deadline:
            collected += self.poll(poll_timeout_s)
        return collected

    def cadence(self) -> Cadence:
        return cadence(self.readings)

    def summary(self) -> dict:
        """Everything this listener knows, for a run artifact."""
        return {
            "stats": self.stats.as_dict(),
            "cadence": self.cadence().as_dict(),
            "drop_counter_regressions": drop_counter_regressions(self.readings),
            "last_drops": self.readings[-1].health.drops if self.readings else None,
        }


__all__ = [
    "Cadence",
    "HealthListener",
    "HealthReading",
    "ListenerStats",
    "cadence",
    "drop_counter_regressions",
]
