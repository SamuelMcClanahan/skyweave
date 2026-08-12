"""D8.1 A3: finding D8-F10 closed, gated.

`decode_health` raises two unrelated exception types — `ProtocolViolation`
(a `WireError`) for a clock domain the contract will not map, and protobuf's
own `DecodeError` for a corrupt body, which is not a `WireError` at all.
`SocketIngestAdapter.poll` caught only the first, so a single malformed health
datagram propagated out of the ingest loop on the fusion host — in the loop
that receives from three nodes, on a port health SHARES with measurements
(finding D8-F9). The project's rule is that one corrupt packet may not kill
ingest and every drop is labelled; this is that rule, asserted.

The same mistake in `edge/health.py` was caught by
`test_a_corrupt_datagram_is_counted_and_does_not_kill_the_listener`; this is
that test's claim, made against the transport adapter that finding names.

Both failure modes are asserted SEPARATELY. A fix that lumped them under one
reason would still satisfy "never raises" while losing the label that tells an
operator which side of the wire is wrong.

The quiet path widened; the loud one did not move. `raise_on_reject=True` is
the default in `loopback.socket_replay_inprocess`, so the second test pins
that A3 did not disarm it for the health plane.
"""

from __future__ import annotations

import pytest
from google.protobuf.message import DecodeError

from skyweave2.contracts import ClockDomain
from skyweave2.transport.adapter import SocketIngestAdapter
from skyweave2.transport.codec import Health, encode_health
from skyweave2.transport.proto import skyweave_pb2 as pb
from skyweave2.transport.udp import UdpReceiver, UdpSender
from skyweave2.transport.wire import PayloadType, frame

CORRUPT_BODY = b"\xff\xff\xff"


def _valid_health() -> bytes:
    return encode_health(
        Health(
            camera_id=0,
            session_uuid="00000000-0000-0000-0000-000000000000",
            ts_ns=1,
            clock_domain=ClockDomain.NODE_MONO,
            fps=30.0,
            drops=0,
            time_sync_error_ms=0.0,
        )
    )


def _unspecified_clock_domain_health() -> bytes:
    """The `WireError` half, built from the protobuf directly.

    `ClockDomain` has no UNSPECIFIED member, so this datagram cannot be
    expressed through `Health` at all — which is exactly why a peer can send
    it and this side has to reject it. Omitting the field is proto3's default,
    `CLOCK_DOMAIN_UNSPECIFIED`.
    """
    message = pb.HealthPacket(
        camera_id=0,
        session_uuid="s",
        ts_ns=1,
        fps=30.0,
        drops=0,
        time_sync_error_ms=0.0,
    )
    return frame(PayloadType.HEALTH, message.SerializeToString(deterministic=True))


def test_a_corrupt_health_datagram_is_labeled_and_does_not_kill_ingest():
    """Finding D8-F10: one bad health packet may not stop the fusion host."""
    with UdpReceiver("127.0.0.1", 0) as receiver:
        with UdpSender("127.0.0.1", receiver.port) as sender:
            sender.send(frame(PayloadType.HEALTH, CORRUPT_BODY))
            sender.send(_unspecified_clock_domain_health())
            sender.send(_valid_health())
        adapter = SocketIngestAdapter(receiver)
        for _ in range(3):
            assert adapter.poll(2.0) == []

    # Labelled, not lumped: a corrupt protobuf and a contract violation are
    # different bugs, on different sides of the wire.
    assert adapter.stats.rejected == {
        "health_decode:DecodeError": 1,
        "health_decode:ProtocolViolation": 1,
    }
    # The good packet still lands, and health never touches the measurement
    # counters — a rejected health datagram is not a lost observation.
    assert adapter.stats.health_packets == 1
    assert adapter.stats.datagrams == 0
    assert adapter.stats.observations == 0


def test_the_loud_path_still_raises_on_a_corrupt_health_datagram():
    """A3 widened the quiet path; it did not delete the loud one.

    `socket_replay_inprocess` builds its adapter with `raise_on_reject=True`,
    so a fix that swallowed the exception outright — instead of routing it
    through `_reject` — would silently turn every W4/W5/W7/W8 replay deaf on
    the health plane.
    """
    with UdpReceiver("127.0.0.1", 0) as receiver:
        with UdpSender("127.0.0.1", receiver.port) as sender:
            sender.send(frame(PayloadType.HEALTH, CORRUPT_BODY))
        adapter = SocketIngestAdapter(receiver, raise_on_reject=True)
        with pytest.raises(DecodeError):
            adapter.poll(2.0)

    assert adapter.stats.rejected == {"health_decode:DecodeError": 1}
