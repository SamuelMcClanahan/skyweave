"""W3: the evidence channel can be dropped entirely and geometry is unchanged.

D0's T7 says losing an optional mask/crop must never lose a measurement. On
the wire that promise has a sharper form: evidence rides a SEPARATE socket, so
losing all of it, flooding it, or receiving it after expiry must leave the
measurement stream and every fusion decision bit-for-bit identical.
"""

from __future__ import annotations

import pytest

from skyweave2.contracts import Observation2D
from skyweave2.faults.honesty import decisions_fingerprint
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream
from skyweave2.transport import codec
from skyweave2.transport.adapter import SocketIngestAdapter
from skyweave2.transport.udp import UdpReceiver, UdpSender
from skyweave2.transport.wire import PayloadType, WireFormatError, frame
from tests.transport.conftest import as_wire, dump_observations


def _with_evidence(observations: list[Observation2D]) -> list[Observation2D]:
    """Attach an evidence reference to every measurement."""
    return [
        obs.model_copy(
            update={
                "evidence_ref": (
                    f"patch-{obs.envelope.camera_id}-{obs.envelope.frame_seq}"
                    f"-{obs.obs_id}"
                )
            }
        )
        for obs in observations
    ]


def _evidence_for(obs: Observation2D, expiry_ts_ns: int) -> codec.Evidence:
    return codec.Evidence(
        evidence_ref=obs.evidence_ref or "",
        camera_id=obs.envelope.camera_id,
        session_uuid=obs.envelope.session_uuid,
        frame_seq=obs.envelope.frame_seq,
        capture_ts_ns=obs.envelope.capture_ts_ns,
        expiry_ts_ns=expiry_ts_ns,
        kind=1,
        x0=obs.bbox_x,
        y0=obs.bbox_y,
        width=obs.bbox_w,
        height=obs.bbox_h,
        payload=b"\x7f" * 256,
    )


def _run_socket(
    observations: list[Observation2D],
    evidence: list[codec.Evidence],
    now_ns: int,
) -> tuple[list[Observation2D], SocketIngestAdapter]:
    """Both planes over real sockets; measurements drained to completion."""
    with UdpReceiver("127.0.0.1", 0) as measurement_rx, UdpReceiver(
        "127.0.0.1", 0
    ) as evidence_rx:
        with UdpSender("127.0.0.1", measurement_rx.port) as measurement_tx, UdpSender(
            "127.0.0.1", evidence_rx.port
        ) as evidence_tx:
            from skyweave2.transport.replay import capture_events

            for event in capture_events(observations):
                measurement_tx.send(
                    codec.encode_observation_packet(
                        event.envelope, list(event.observations)
                    )
                )
            for item in evidence:
                evidence_tx.send(codec.encode_evidence(item))
        adapter = SocketIngestAdapter(
            measurement_rx,
            evidence_receiver=evidence_rx,
            raise_on_reject=True,
            monotonic_ns=lambda: now_ns,
        )
        delivered = adapter.drain(idle_timeout_s=0.2, max_idle_polls=3)
        # Drain whatever evidence is still queued after the measurements are
        # exhausted, so "evidence arrived" is a fact, not a hope. Stop on
        # consecutive no-progress polls rather than a fixed budget: a
        # per-item timeout turns an empty queue into seconds of dead wait.
        idle = 0
        while idle < 3:
            before = adapter.stats.evidence_received + adapter.stats.rejected_total
            adapter.poll_evidence(0.05, now_ns=now_ns)
            after = adapter.stats.evidence_received + adapter.stats.rejected_total
            idle = 0 if after > before else idle + 1
    return delivered, adapter


def test_dropping_every_evidence_packet_changes_no_measurement(cameras, stream):
    observations = _with_evidence(stream)
    evidence = [_evidence_for(o, expiry_ts_ns=10**18) for o in observations]

    with_evidence, adapter_with = _run_socket(observations, evidence, now_ns=0)
    without_evidence, adapter_without = _run_socket(observations, [], now_ns=0)

    assert adapter_with.stats.evidence_received == len(evidence)
    assert adapter_without.stats.evidence_received == 0
    # Socket vs socket: RAW equality, no normalisation anywhere near it.
    assert dump_observations(with_evidence) == dump_observations(without_evidence)
    # Socket vs original: the D0-declared float fields narrow on the wire.
    assert with_evidence == as_wire(observations)


def test_dropping_every_evidence_packet_changes_no_decision(cameras, stream):
    observations = _with_evidence(stream)
    evidence = [_evidence_for(o, expiry_ts_ns=10**18) for o in observations]
    config = FusionConfig()

    with_evidence, _ = _run_socket(observations, evidence, now_ns=0)
    without_evidence, _ = _run_socket(observations, [], now_ns=0)

    first = decisions_fingerprint(
        run_stream(with_evidence, cameras, config, session_uuid="w3")
    )
    second = decisions_fingerprint(
        run_stream(without_evidence, cameras, config, session_uuid="w3")
    )
    assert first == second


def test_expired_evidence_is_discarded_with_a_label(cameras, stream):
    observations = _with_evidence(stream[:9])
    expired = [_evidence_for(o, expiry_ts_ns=100) for o in observations]
    delivered, adapter = _run_socket(observations, expired, now_ns=10_000)
    assert adapter.stats.evidence_received == len(expired)
    assert adapter.stats.evidence_expired == len(expired)
    assert adapter.evidence == []
    assert delivered == as_wire(observations)


def test_evidence_larger_than_the_measurement_ceiling_does_not_disturb_geometry(
    cameras, stream
):
    """Evidence is allowed to be big; measurements must not notice."""
    observations = _with_evidence(stream[:9])
    fat = [
        codec.Evidence(
            evidence_ref=o.evidence_ref or "",
            camera_id=o.envelope.camera_id,
            session_uuid=o.envelope.session_uuid,
            frame_seq=o.envelope.frame_seq,
            capture_ts_ns=o.envelope.capture_ts_ns,
            expiry_ts_ns=10**18,
            kind=1,
            x0=0,
            y0=0,
            width=64,
            height=64,
            payload=b"\x11" * 4096,
        )
        for o in observations
    ]
    delivered, adapter = _run_socket(observations, fat, now_ns=0)
    assert delivered == as_wire(observations)
    assert adapter.stats.evidence_received == len(fat)
    assert adapter.stats.rejected == {}


def test_a_corrupt_evidence_datagram_never_reaches_the_measurement_stream(
    cameras, stream
):
    observations = stream[:9]
    with UdpReceiver("127.0.0.1", 0) as measurement_rx, UdpReceiver(
        "127.0.0.1", 0
    ) as evidence_rx:
        with UdpSender("127.0.0.1", measurement_rx.port) as measurement_tx, UdpSender(
            "127.0.0.1", evidence_rx.port
        ) as evidence_tx:
            from skyweave2.transport.replay import capture_events

            for event in capture_events(observations):
                measurement_tx.send(
                    codec.encode_observation_packet(
                        event.envelope, list(event.observations)
                    )
                )
            evidence_tx.send(b"not-a-skyweave-datagram")
        adapter = SocketIngestAdapter(measurement_rx, evidence_receiver=evidence_rx)
        delivered = adapter.drain(idle_timeout_s=0.2, max_idle_polls=3)
        adapter.poll_evidence(0.2)
    assert delivered == as_wire(observations)
    assert adapter.stats.observations == len(observations)
    assert any("evidence_framing" in reason for reason in adapter.stats.rejected)


def test_a_measurement_on_the_evidence_socket_is_rejected(cameras, stream):
    """Channel confusion must be caught, not decoded by luck."""
    with UdpReceiver("127.0.0.1", 0) as evidence_rx:
        with UdpSender("127.0.0.1", evidence_rx.port) as tx:
            event_observations = stream[:1]
            tx.send(
                codec.encode_observation_packet(
                    event_observations[0].envelope, event_observations
                )
            )
        adapter = SocketIngestAdapter(
            UdpReceiver("127.0.0.1", 0), evidence_receiver=evidence_rx
        )
        adapter.poll_evidence(1.0)
    assert adapter.stats.rejected == {"evidence_channel_payload:OBSERVATION": 1}
    assert adapter.evidence == []


def test_evidence_socket_rejects_an_over_ceiling_datagram():
    from skyweave2.transport.wire import EVIDENCE_CEILING_BYTES, unframe

    header = frame(PayloadType.EVIDENCE, b"")[:4]
    with pytest.raises(WireFormatError, match="over the .* ceiling"):
        unframe(header + b"\x00" * (EVIDENCE_CEILING_BYTES + 1))
