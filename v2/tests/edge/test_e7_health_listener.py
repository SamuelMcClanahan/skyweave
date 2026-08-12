"""E7's listener, exercised against the HOST-built daemon.

E7 itself is "health packets at 1 Hz with real drop counters, on the node", and
it is board-gated. What is board-free — and what the D8.1-prep amendment asks
for — is the thing that would RECEIVE them. This file gates that: the listener
demultiplexes the measurement port, decodes health, counts what it discards and
reports cadence and drop-counter monotonicity.

The cadence measured here is this host's loopback and says nothing about a
node. What it proves is that the arithmetic works on real arrivals rather than
on a list somebody typed, which is the difference between tooling and a plan.

The health period is driven far above 1 Hz on purpose (E5 set the precedent):
a replay that finishes in two seconds would otherwise produce one packet, and
one packet has no cadence at all.
"""

from __future__ import annotations

import socket
import subprocess

from skyweave2.contracts import ClockDomain
from skyweave2.edge import benchmark, daemon, health, metrics
from skyweave2.transport.codec import Health
from skyweave2.transport.wire import PayloadType, frame


def _reading(ns: int, drops: int, camera_id: int = 0) -> health.HealthReading:
    return health.HealthReading(
        received_monotonic_ns=ns,
        health=Health(
            camera_id=camera_id, session_uuid="s", ts_ns=ns,
            clock_domain=ClockDomain.NODE_MONO, fps=30.0, drops=drops,
            time_sync_error_ms=0.0,
        ),
    )


# ---------------------------------------------------------------------------
# Against the real daemon
# ---------------------------------------------------------------------------


def test_the_listener_hears_the_daemons_health_packets(edge_build_dir, tmp_path):
    """Real packets, off the same port the measurements use.

    The run also carries observation datagrams, so this is the demultiplexing
    path and not a channel with only one kind of thing on it.
    """
    plan = benchmark.BenchmarkPlan(frames=60, warmup_frames=10)
    run = benchmark.run_soak(
        tmp_path / "soak", 288, 162, duration_s=2.0, plan=plan, target_fps=30.0,
        build_dir=edge_build_dir, health_period_ms=200,
    ).run
    assert run.returncode == 0, run.stderr_tail
    stats = run.health["stats"]
    assert stats["health_packets"] >= 5, stats
    assert stats["observation_packets"] > 0, "no measurements to demultiplex from"
    assert stats["rejected_total"] == 0, stats["rejected"]
    # The daemon was asked for a packet every 200 ms and the arrivals should
    # look like it. Loose bounds: this is a loopback socket on a laptop, and
    # the CADENCE claim that matters is E7's, on the node.
    cadence = run.health["cadence"]
    assert cadence["packets"] >= 5
    assert 0.1 <= cadence["mean_period_s"] <= 0.5, cadence


def test_the_health_drop_counter_carries_real_drops(edge_build_dir, tmp_path):
    """A cluttered scene drops components at the cap; health must say so.

    Twelve movers against a cap of seven, so the cap bites on most frames. The
    health packet's `drops` is a TOTAL over four causes (finding D8-F2), and
    the daemon's own `--stats` publishes the same total — if these two ever
    disagree, one of them is not counting what it claims to.
    """
    plan = benchmark.BenchmarkPlan(frames=60, warmup_frames=10, movers=12)
    run = benchmark.run_soak(
        tmp_path / "clutter", 384, 216, duration_s=2.0, plan=plan, target_fps=30.0,
        build_dir=edge_build_dir, health_period_ms=200,
    ).run
    assert run.returncode == 0, run.stderr_tail
    assert run.stats["components_dropped_over_cap"] > 0, "the cap never bit"
    last = run.health["last_drops"]
    assert last is not None and last > 0
    # The final health packet is sent after the loop, so it carries the run's
    # whole total — the same number `--stats` writes.
    assert last == run.stats["health_drops_total"]
    assert run.health["drop_counter_regressions"] == []


def test_health_and_measurement_arrive_on_the_same_socket(edge_build_dir, tmp_path):
    """Finding D8-F9, as a test rather than a sentence.

    The claim is not "a listener on some other port hears nothing" — that is
    true of any UDP port nobody sends to, and a test of it would pass under
    every possible implementation, including one with a separate health port.
    The claim with content is that ONE socket receives BOTH payload types,
    because the daemon has one sender and no health-port flag at all.

    Checked two ways: the counters from a real run, and the daemon's own
    ``--help``, which is the interface a future health port would have to
    appear in.
    """
    plan = benchmark.BenchmarkPlan(frames=30, warmup_frames=8)
    run = benchmark.run_soak(
        tmp_path / "shared", 288, 162, duration_s=1.0, plan=plan,
        target_fps=30.0, build_dir=edge_build_dir, health_period_ms=100,
    ).run
    assert run.returncode == 0, run.stderr_tail
    stats = run.health["stats"]
    # One socket, both kinds. If health ever moved to its own port, one of
    # these two counters would go to zero and this test would say so.
    assert stats["health_packets"] > 0
    assert stats["observation_packets"] > 0
    assert stats["datagrams"] == stats["health_packets"] + stats["observation_packets"]

    usage = subprocess.run(
        [str(daemon.daemon_path(edge_build_dir)), "--help"],
        capture_output=True, text=True, check=False,
    )
    text = usage.stdout + usage.stderr
    assert "--measurement-port" in text
    assert "--health-period-ms" in text
    assert "--health-port" not in text, (
        "the daemon grew a health port; the listener and finding D8-F9 both "
        "need revisiting"
    )


#: How many health periods a gap may span before this test calls it a gap.
#: DERIVED, not tuned, and the derivation is the whole point. A packet that is
#: merely late by a pace period lands at 1 + 33/200 = 1.17 periods; a packet the
#: daemon never sent puts its neighbours 2 + 33/200 = 2.17 periods apart. 2.0 is
#: the only line between those two, so this bound means exactly "no health send
#: was missed" — which is the "health gap" runbook C5 fails a soak on.
#:
#: It was 3.0, whose stated rationale ("a pace period's worth of lateness on
#: every packet would still have to be an order of magnitude worse to hide") is
#: backwards: 3.0 admits 0.6 s, and a wholly missed packet is 0.433 s, so the
#: gap the soak fails on passed underneath it.
RAM_PACE_CADENCE_SLACK = 2.0


def test_a_paced_ram_loop_still_sends_health_on_cadence(edge_build_dir, tmp_path):
    """Health keeps flowing while the daemon paces itself out of DDR.

    No host feeder is in the path: the daemon serves a preloaded clip and does
    its own sleeping, so a health packet that arrives late arrives late because
    of the daemon's own housekeeping order and nothing else. The bound is one
    missed send (see ``RAM_PACE_CADENCE_SLACK``), which is the failure runbook
    C5 names.

    NOT GATED HERE, and this test used to say it was: WHERE the pace sleep sits.
    The old docstring claimed "a sleep before the health check would push every
    health packet up to one pace period late and manufacture the gap the soak is
    watching for", and measurement says otherwise. Moving the whole pace block
    ahead of the health-cadence check leaves ``max_period_s`` at 0.233 s, exactly
    where the unmutated build sits, because both orderings quantise the health
    send onto the same 33.333 ms pace grid and differ only by a uniform
    one-period PHASE shift — which cannot open a gap. No value of
    ``RAM_PACE_CADENCE_SLACK`` could gate the placement: the tightest bound the
    unmutated build survives is the mutant's own number.

    What actually bounds health lateness under either ordering is
    ``sw_config_validate``'s refusal of a pace period longer than the health
    cadence (33 ms against 200 ms here), so the ordering is a phase choice, not
    a gap risk. Gating the placement needs an observable that responds to it —
    the frame index or the monotonic offset at each health send, recorded by the
    daemon — and that is firmware work, recorded as a finding rather than
    approximated with a bound on host timing noise. ``main.c``'s comment beside
    the sleep carries the same overclaim and needs the same correction.
    """
    plan = benchmark.BenchmarkPlan(
        frames=24, warmup_frames=6,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM, ram_clip_frames=6,
    )
    duration_s = 2.0
    health_period_s = 0.2
    result = benchmark.run_soak(
        tmp_path / "ram-soak", 288, 162, duration_s=duration_s, plan=plan,
        target_fps=30.0, build_dir=edge_build_dir,
        health_period_ms=int(health_period_s * 1000),
    )
    run = result.run
    assert run.returncode == 0, run.stderr_tail
    # The run really was paced by the DAEMON, out of DDR, with no host feeder
    # in the path — otherwise this is a test of the harness's own sleep.
    assert run.stats["source_mode"] == benchmark.SOURCE_MODE_INJECT_RAM
    assert run.stats["ram_loop_period_ns"] == int(round(1e9 / 30.0))
    assert run.stats["frames_in"] == result.frames_planned == 60

    expected = duration_s / health_period_s
    stats = run.health["stats"]
    assert stats["health_packets"] >= int(expected * 0.8), stats
    cadence = run.health["cadence"]
    assert cadence["packets"] >= int(expected * 0.8), cadence
    # Strictly under, because the bound IS the missed-send case: 2.0 periods is
    # not a number a healthy run should be allowed to touch.
    assert cadence["max_period_s"] < RAM_PACE_CADENCE_SLACK * health_period_s, cadence


# ---------------------------------------------------------------------------
# The listener's own behaviour
# ---------------------------------------------------------------------------


def test_a_corrupt_datagram_is_counted_and_does_not_kill_the_listener():
    """One bad packet may not stop a monitor, and may not vanish either."""
    with health.HealthListener(host="127.0.0.1", port=0) as listener:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"not a skyweave datagram", ("127.0.0.1", listener.port))
        sender.sendto(frame(PayloadType.HEALTH, b"\xff\xff\xff"),
                      ("127.0.0.1", listener.port))
        sender.close()
        listener.poll(0.5)
        listener.poll(0.2)
        assert listener.stats.datagrams == 2
        assert listener.stats.health_packets == 0
        assert listener.stats.rejected_total == 2
        # Labelled, not lumped: one failed at the framing layer and one inside
        # the protobuf, and a monitor that could not tell them apart would send
        # somebody to read the wrong code.
        assert len(listener.stats.rejected) == 2


def test_cadence_is_nearest_rank_and_says_so():
    """Two p95 conventions already coexist in this repo; this one is declared.

    Nearest rank means the reported p95 is an interval that actually occurred,
    so the bound it feeds is a statement about a real gap between two packets.
    """
    readings = [_reading(index * 1_000_000_000, drops=0) for index in range(21)]
    # One late arrival, at the very end.
    readings.append(_reading(readings[-1].received_monotonic_ns + 3_000_000_000, 0))
    cadence = health.cadence(readings)
    assert cadence.packets == 22
    assert cadence.min_period_s == 1.0
    assert cadence.max_period_s == 3.0
    # 21 intervals: nearest rank at 95% is index ceil(0.95*21)-1 = 19, which is
    # a 1 s interval, not an interpolation between 1 and 3.
    assert cadence.p95_period_s == 1.0


def test_cadence_of_a_single_packet_is_not_a_number():
    cadence = health.cadence([_reading(0, drops=0)])
    assert cadence.packets == 1
    assert cadence.mean_period_s is None
    assert cadence.p95_period_s is None


def test_a_drop_counter_going_backwards_is_reported():
    """`drops` is monotonic within a session. A decrease is a finding.

    It means a restart the session uuid did not admit, a wrapped counter, or
    two nodes sharing an identity — all three worth a sentence in a report and
    none of them worth averaging away.
    """
    readings = [
        _reading(0, drops=5),
        _reading(1_000_000_000, drops=9),
        _reading(2_000_000_000, drops=2),
    ]
    regressions = health.drop_counter_regressions(readings)
    assert len(regressions) == 1
    assert "9 -> 2" in regressions[0]


def test_two_nodes_do_not_regress_against_each_other():
    """Per (camera, session), not globally: node 1 at 3 drops does not mean
    node 0 went backwards."""
    readings = [
        _reading(0, drops=9, camera_id=0),
        _reading(1_000_000_000, drops=3, camera_id=1),
        _reading(2_000_000_000, drops=10, camera_id=0),
    ]
    assert health.drop_counter_regressions(readings) == []


def test_the_collector_registry_names_every_axis_the_report_prints():
    """The report's collector table is generated from this registry.

    A collector added without a row here would be a measurement the report
    never mentions; a row here with no collector would be the reverse.
    """
    axes = {axis for axis, _, _ in metrics.COLLECTOR_REGISTRY}
    assert axes == {
        "sustained fps", "peak RSS", "A7 utilisation", "DDR bandwidth", "thermals",
    }
    board_only = [
        availability
        for axis, _, availability in metrics.COLLECTOR_REGISTRY
        if axis == "DDR bandwidth"
    ]
    assert board_only and "NOT-MEASURED" in board_only[0]
