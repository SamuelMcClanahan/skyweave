"""W6: wall-clock mode reports REAL packet age and holds its declared jitter.

Two halves, because timing tests that only measure a real clock cannot
distinguish "the scheduler is correct" from "the machine was quiet":

- with an INJECTED clock the scheduling arithmetic is checked exactly (no
  sleeping, no tolerance, no flake);
- on real loopback the achieved jitter and packet age are measured and scored
  against the budgets declared in ``TransportConfig``.

Packet age is receive time minus the SCHEDULED capture instant, so the
pacer's own error is inside the number rather than excluded from it. D0's
rule that receive time lives beside capture time and never overwrites it is
asserted directly.
"""

from __future__ import annotations

import numpy as np

from skyweave2.contracts import ClockDomain
from skyweave2.transport.config import TRANSPORT_CONFIG
from skyweave2.transport.loopback import run_loopback, split_by_camera
from skyweave2.transport.replay import ObservationReplaySource, PacingMode
from tests.transport.test_w4_parity import synthetic_truth_object


class FakeClock:
    """A monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now_ns = 1_000_000_000

    def monotonic_ns(self) -> int:
        return self.now_ns

    def sleep(self, seconds: float) -> None:
        self.now_ns += int(round(seconds * 1e9))


def test_deterministic_mode_never_sleeps_and_carries_synthetic_times(stream):
    clock = FakeClock()
    source = ObservationReplaySource(
        stream, mode=PacingMode.DETERMINISTIC,
        monotonic_ns=clock.monotonic_ns, sleep=clock.sleep,
    )
    sent = []
    stats = source.replay(sent.append)
    assert clock.now_ns == 1_000_000_000, "deterministic mode slept"
    assert stats.schedule_error_ns == []
    assert stats.due_monotonic_ns == {}, (
        "deterministic mode must not claim a wall-clock due time it never had"
    )
    assert stats.events == len(source.events)
    # Synthetic capture times are carried verbatim.
    assert [e.envelope.capture_ts_ns for e in sent] == [
        e.envelope.capture_ts_ns for e in source.events
    ]


def test_wall_clock_schedule_is_exact_under_an_injected_clock(stream):
    clock = FakeClock()
    source = ObservationReplaySource(
        stream, mode=PacingMode.WALL_CLOCK, speed=1.0,
        monotonic_ns=clock.monotonic_ns, sleep=clock.sleep,
    )
    start = clock.now_ns
    stats = source.replay(lambda event: None, start_epoch_ns=start)
    assert stats.schedule_error_ns and max(
        abs(e) for e in stats.schedule_error_ns
    ) == 0, "a perfect clock produced a non-zero scheduling error"
    first = source.events[0].envelope.capture_ts_ns
    for event in source.events:
        expected = start + (event.envelope.capture_ts_ns - first)
        assert stats.due_monotonic_ns[event.identity] == expected


def test_speed_compresses_the_schedule_proportionally(stream):
    """Pure arithmetic on an injected clock: a literal speed, no tolerance."""
    speed = 10.0
    clock = FakeClock()
    source = ObservationReplaySource(
        stream, mode=PacingMode.WALL_CLOCK, speed=speed,
        monotonic_ns=clock.monotonic_ns, sleep=clock.sleep,
    )
    last = source.events[-1]
    first = source.events[0].envelope.capture_ts_ns
    span_ns = last.envelope.capture_ts_ns - first
    assert source.scheduled_offset_ns(last) == int(span_ns / speed)


def test_an_event_before_the_first_is_due_immediately_not_in_the_past(stream):
    """A reordered or rebooted stream must not schedule negative offsets."""
    clock = FakeClock()
    reordered = [stream[-1], *stream[:-1]]
    source = ObservationReplaySource(
        reordered, mode=PacingMode.WALL_CLOCK,
        monotonic_ns=clock.monotonic_ns, sleep=clock.sleep,
    )
    assert all(source.scheduled_offset_ns(e) >= 0 for e in source.events)


def test_shared_epoch_is_honoured_before_the_first_event(stream):
    clock = FakeClock()
    source = ObservationReplaySource(
        stream[:3], mode=PacingMode.WALL_CLOCK,
        monotonic_ns=clock.monotonic_ns, sleep=clock.sleep,
    )
    future = clock.now_ns + 5_000_000_000
    source.replay(lambda event: None, start_epoch_ns=future)
    assert clock.now_ns >= future, "the replay did not wait for the shared epoch"


def test_loopback_packet_age_is_measured_and_within_the_declared_budget(
    cameras, stream, tmp_path
):
    result = run_loopback(
        split_by_camera(stream), cameras, synthetic_truth_object(), "w6",
        tmp_path / "w6", mode=PacingMode.WALL_CLOCK, speed=TRANSPORT_CONFIG.rig_replay_speed,
        spawn_margin_s=1.0,
    )
    assert result.engine["rejected"] == {}
    assert result.engine["observations"] == len(stream)

    ages_ns = result.packet_age_ns()
    transit_ns = result.transit_ns()
    schedule_ns = result.schedule_error_ns()
    assert ages_ns, "wall-clock mode produced no packet-age measurements"
    assert len(ages_ns) == len(transit_ns)
    assert all(np.isfinite(a) for a in ages_ns)
    # Age minus transit is EXACTLY the pacer's scheduling error, per receipt.
    # A median inequality was too weak: dropping the due-time bookkeeping and
    # joining against the SEND time instead would still satisfy it, while
    # silently removing the pacer's own error from the age it helped cause.
    # Integer arithmetic, no tolerance; lines above already pin the run clean
    # (nothing lost, nothing duplicated) so the multisets have to match.
    lag_ns = [age - transit for age, transit in zip(ages_ns, transit_ns, strict=True)]
    assert sorted(lag_ns) == sorted(schedule_ns), (
        "packet age is joined against the send time, not the scheduled capture "
        "instant: the pacer's own scheduling error dropped out of the age"
    )

    age_p95_ms = float(np.percentile(ages_ns, 95)) / 1e6
    age_max_ms = max(ages_ns) / 1e6
    jitter_p95_ms = float(np.percentile(np.abs(schedule_ns), 95)) / 1e6
    assert jitter_p95_ms <= TRANSPORT_CONFIG.loopback_schedule_jitter_p95_ms, (
        f"schedule jitter p95 {jitter_p95_ms:.2f} ms over the declared budget"
    )
    assert age_p95_ms <= TRANSPORT_CONFIG.loopback_packet_age_p95_ms, (
        f"packet age p95 {age_p95_ms:.2f} ms over the declared budget"
    )
    # A PATHOLOGY bound, not a performance one. Gating the tail tightly on a
    # non-real-time OS measures the host scheduler, not the wire: a single
    # preemption of tens of milliseconds is ordinary under load. This catches a
    # stalled sender or a wildly out-of-band release; the p95 gates above are
    # what bound performance.
    assert age_max_ms <= TRANSPORT_CONFIG.loopback_packet_age_max_ms, (
        f"packet age max {age_max_ms:.2f} ms — far beyond a scheduler hiccup, "
        "something on the send or receive path stalled"
    )


def test_receive_time_lives_beside_capture_time_and_never_overwrites_it(
    cameras, stream, tmp_path
):
    """D0 section 3, asserted on the real receive path."""
    result = run_loopback(
        split_by_camera(stream[:30]), cameras, synthetic_truth_object(), "w6-receipts",
        tmp_path / "w6r", mode=PacingMode.WALL_CLOCK,
        speed=TRANSPORT_CONFIG.rig_replay_speed, spawn_margin_s=1.0,
    )
    capture_by_identity = {
        (o.envelope.camera_id, o.envelope.session_uuid, o.envelope.frame_seq):
            o.envelope.capture_ts_ns
        for o in stream[:30]
    }
    assert result.engine["receipts"]
    for receipt in result.engine["receipts"]:
        identity = (
            receipt["camera_id"], receipt["session_uuid"], receipt["frame_seq"]
        )
        assert receipt["capture_ts_ns"] == capture_by_identity[identity], (
            "capture time was rewritten on receive"
        )
        assert receipt["rx_clock_domain"] == ClockDomain.JETSON_RX.value
        assert receipt["rx_monotonic_ns"] > 0
    # And the delivered contract models still carry the SOURCE clock domain.
    assert all(
        o.envelope.clock_domain == ClockDomain.SYNTHETIC for o in result.delivered
    )
