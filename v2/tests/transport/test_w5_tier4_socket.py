"""W5: Tier IV faults re-run over real sockets reproduce D6's policies.

Two mechanisms, both exercised:

1. **Stream-level, transmitted for real.** The D6 injector produces the
   faulted stream and its arrival ORDER; the socket path then transmits
   exactly that — duplicates become duplicate datagrams, reordered items are
   genuinely sent out of order, dropped items are simply never sent. Because
   both paths see the same faulted items, any policy difference is
   attributable to the wire, the codec or the adapter, which is what this
   phase is measuring.

2. **Datagram-level, injected at the send path.** ``netfaults`` drops,
   duplicates, delays and reorders the datagrams themselves. Each axis has a
   can-fail assertion proving the fault was really applied, because an
   injector that reports a fault it did not make would make every verdict
   below meaningless (the D6 process finding, repeated here on purpose).

Policy comparison means the aligner's LABELED exits (late,
stale_or_duplicate, sequence_anomaly), the engine's rejection reasons, the
tracker's accept/reject records, and the published states — i.e. the
decisions fingerprint, not a summary of it.
"""

from __future__ import annotations

from skyweave2.faults import injectors as inj
from skyweave2.faults.bookkeeping import build_layer_table
from skyweave2.faults.honesty import decisions_fingerprint
from skyweave2.fusion.aligner import obs_key
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream
from skyweave2.transport.loopback import socket_replay_inprocess
from skyweave2.transport.netfaults import NetworkFaultPolicy
from tests.transport.conftest import as_wire, dump_observations

FPS = 30.0
SEED = 7


def policies(observations, cameras, label: str) -> dict:
    """Everything a policy comparison must cover, in one comparable object."""
    run = run_stream(observations, cameras, FusionConfig(), session_uuid=label)
    excluded: dict[str, int] = {}
    for entry in run.excluded:
        excluded[entry.reason] = excluded.get(entry.reason, 0) + 1
    rejections: dict[str, int] = {}
    for batch in run.batches:
        for rejection in batch.rejections:
            rejections[rejection.reason] = rejections.get(rejection.reason, 0) + 1
    return {
        "excluded": excluded,
        "rejections": rejections,
        "published": len(run.published),
        "statuses": run.statuses,
        "nis_rejects": sum(1 for r in run.records if not r.accepted),
        "fingerprint": decisions_fingerprint(run),
        "run": run,
    }


def tier4_axes(stream, cameras):
    """One representative magnitude per Tier IV axis, from the D6 injectors.

    The full manifest sweep runs in the report; the suite covers every axis
    at a magnitude large enough to actually exercise a policy.
    """
    frames = [o.envelope.frame_seq for o in stream]
    half_s = (max(frames) / FPS) / 2.0
    return [
        ("clock_offset_ms[honest]", *inj.inject_clock_error(
            stream, 10.0, 0.0, 0.0, seed=SEED, honest=True,
            dishonest_claim_ms=0.5, camera_ids=(0,))),
        ("clock_offset_ms[dishonest]", *inj.inject_clock_error(
            stream, 10.0, 0.0, 0.0, seed=SEED, honest=False,
            dishonest_claim_ms=0.5, camera_ids=(0,))),
        ("drift_ppm", *inj.inject_clock_error(
            stream, 0.0, 100.0, 0.0, seed=SEED, honest=True,
            dishonest_claim_ms=0.5, camera_ids=(0,))),
        ("jitter_ms_sigma", *inj.inject_clock_error(
            stream, 0.0, 0.0, 1.0, seed=SEED, honest=True,
            dishonest_claim_ms=0.5, camera_ids=(0,))),
        ("observation_loss_pct[random]", *inj.inject_observation_loss(
            stream, 20.0, "random", SEED)),
        ("observation_loss_pct[burst]", *inj.inject_observation_loss(
            stream, 20.0, "burst", SEED)),
        ("reorder", *inj.inject_reorder(stream, SEED)),
        ("duplication", *inj.inject_duplication(stream, SEED)),
        ("lateness", *inj.inject_lateness(stream, SEED)),
        ("node_restart", *inj.inject_node_restart(stream, 2.0, FPS, camera_id=0)),
        ("camera_dropout", *inj.inject_camera_dropout(stream, 0, half_s, FPS)),
    ]


def test_every_tier4_axis_matches_between_file_and_socket(cameras, stream):
    """The core W5 claim, axis by axis."""
    divergences = []
    for name, faulted, ledger in tier4_axes(stream, cameras):
        result = socket_replay_inprocess(faulted)
        assert result.encode_failures == [], f"{name}: unsendable capture event"
        assert result.adapter.stats.rejected == {}, f"{name}: {result.adapter.stats.rejected}"
        assert len(result.delivered) == len(faulted), (
            f"{name}: socket delivered {len(result.delivered)} of {len(faulted)}"
        )
        # The wire delivered exactly the faulted stream, duplicates and all.
        assert dump_observations(result.delivered) == dump_observations(
            as_wire(faulted)
        ), f"{name}: the wire changed the stream"

        file_policy = policies(faulted, cameras, f"w5-{name}")
        socket_policy = policies(result.delivered, cameras, f"w5-{name}")
        for key in ("excluded", "rejections", "published", "statuses", "nis_rejects"):
            if file_policy[key] != socket_policy[key]:
                divergences.append((name, key, file_policy[key], socket_policy[key]))
        if file_policy["fingerprint"] != socket_policy["fingerprint"]:
            divergences.append((name, "fingerprint", "file", "socket"))

        # The layer table — D6's core deliverable — must also agree.
        keys = {obs_key(o) for o in faulted}
        file_table = build_layer_table(name, "socket", ledger, file_policy["run"], keys)
        socket_keys = {obs_key(o) for o in result.delivered}
        socket_table = build_layer_table(
            name, "socket", ledger, socket_policy["run"], socket_keys
        )
        if file_table.false_accept_rate != socket_table.false_accept_rate:
            divergences.append((name, "false_accept_rate",
                                file_table.false_accept_rate,
                                socket_table.false_accept_rate))
        if file_table.false_reject_rate != socket_table.false_reject_rate:
            divergences.append((name, "false_reject_rate",
                                file_table.false_reject_rate,
                                socket_table.false_reject_rate))
    assert divergences == [], f"D6-policy divergences over sockets: {divergences}"


def test_each_tier4_axis_actually_changed_something(cameras, stream):
    """Can-fail guard: an axis that perturbs nothing proves nothing.

    Without this, a broken injector would make every parity assertion above
    pass trivially — the exact failure mode D6 recorded as a process finding.
    """
    baseline = policies(stream, cameras, "w5-baseline")
    inert = []
    for name, faulted, _ledger in tier4_axes(stream, cameras):
        changed = (
            dump_observations(faulted) != dump_observations(stream)
            or len(faulted) != len(stream)
        )
        if not changed:
            inert.append(name)
    assert inert == [], f"axes that injected nothing: {inert}"
    # And at least the destructive axes must move a policy, not just the bytes.
    destructive = {
        name: faulted for name, faulted, _ in tier4_axes(stream, cameras)
        if name in ("observation_loss_pct[random]", "camera_dropout", "lateness")
    }
    for name, faulted in destructive.items():
        after = policies(faulted, cameras, f"w5-{name}")
        assert after["fingerprint"] != baseline["fingerprint"], (
            f"{name} changed no decision at all"
        )


# ---------------------------------------------------------------------------
# Datagram-level injection at the real send path
# ---------------------------------------------------------------------------


def test_datagram_loss_is_real_and_is_labeled_by_the_aligner(cameras, stream):
    policy = NetworkFaultPolicy(seed=SEED, loss_pct=20.0)
    result = socket_replay_inprocess(stream, fault_policy=policy)
    ledger = result.fault_ledger
    assert ledger.dropped, "the loss emulator dropped nothing"
    assert ledger.sent == ledger.offered - len(ledger.dropped)
    assert len(result.delivered) < len(stream)
    # Loss is invisible to the aligner by construction (a datagram that never
    # arrived cannot be labeled); it shows up as fewer supported cameras.
    after = policies(result.delivered, cameras, "w5-netloss")
    assert after["excluded"] == {}
    assert after["published"] > 0


def test_datagram_duplication_is_real_and_is_quarantined(cameras, stream):
    policy = NetworkFaultPolicy(seed=SEED, duplication_pct=25.0)
    result = socket_replay_inprocess(stream, fault_policy=policy)
    ledger = result.fault_ledger
    assert ledger.duplicated, "the duplication emulator duplicated nothing"
    assert len(result.delivered) == len(stream) + sum(
        1 for _ in ledger.duplicated
    ) * 1, "a duplicated datagram did not deliver a duplicated observation"
    after = policies(result.delivered, cameras, "w5-netdup")
    assert after["excluded"].get("stale_or_duplicate", 0) == len(ledger.duplicated), (
        "verbatim retransmissions must be quarantined as duplicates, per D6"
    )


def test_datagram_delay_is_real_and_is_labeled_late(cameras, stream):
    policy = NetworkFaultPolicy(
        seed=SEED, lateness_pct=20.0, lateness_positions=200
    )
    result = socket_replay_inprocess(stream, fault_policy=policy)
    ledger = result.fault_ledger
    assert ledger.delayed, "the lateness emulator delayed nothing"
    assert len(result.delivered) == len(stream), "delayed is not lost"
    after = policies(result.delivered, cameras, "w5-netlate")
    assert after["excluded"].get("late", 0) > 0, (
        "held-back datagrams arrived without tripping the lateness policy"
    )


def test_datagram_reorder_is_real_and_does_not_lose_a_measurement(cameras, stream):
    policy = NetworkFaultPolicy(seed=SEED, reorder_window=12)
    result = socket_replay_inprocess(stream, fault_policy=policy)
    assert result.fault_ledger.reordered > 0, "nothing was actually reordered"
    assert len(result.delivered) == len(stream)
    delivered_order = [
        (o.envelope.camera_id, o.envelope.frame_seq, o.obs_id)
        for o in result.delivered
    ]
    original_order = [
        (o.envelope.camera_id, o.envelope.frame_seq, o.obs_id) for o in stream
    ]
    assert delivered_order != original_order, "arrival order was unchanged"
    assert sorted(delivered_order) == sorted(original_order), "reorder lost an item"


def test_camera_dropout_at_the_socket_stops_that_camera(cameras, stream):
    cutoff = 60
    policy = NetworkFaultPolicy(seed=SEED, dropout={0: cutoff})
    result = socket_replay_inprocess(stream, fault_policy=policy)
    assert result.fault_ledger.dropped_out, "the dropout emulator dropped nothing"
    late_cam0 = [
        o for o in result.delivered
        if o.envelope.camera_id == 0 and o.envelope.frame_seq >= cutoff
    ]
    assert late_cam0 == []
    surviving = [o for o in result.delivered if o.envelope.camera_id != 0]
    assert surviving, "the other cameras stopped too"
    after = policies(result.delivered, cameras, "w5-dropout")
    assert after["published"] > 0, "two-camera operation stopped publishing entirely"


def test_the_network_emulator_is_off_by_default(cameras, stream):
    """A no-op policy must be a genuine no-op."""
    result = socket_replay_inprocess(stream, fault_policy=NetworkFaultPolicy(seed=SEED))
    assert result.fault_ledger.dropped == []
    assert result.fault_ledger.duplicated == []
    assert result.fault_ledger.delayed == []
    assert result.fault_ledger.reordered == 0
    assert dump_observations(result.delivered) == dump_observations(as_wire(stream))


def test_datagram_faults_are_reproducible_across_runs(cameras, stream):
    """Same seed, same decisions — no salted hashing, no wall-clock."""
    first = socket_replay_inprocess(
        stream, fault_policy=NetworkFaultPolicy(seed=SEED, loss_pct=15.0,
                                                duplication_pct=10.0)
    )
    second = socket_replay_inprocess(
        stream, fault_policy=NetworkFaultPolicy(seed=SEED, loss_pct=15.0,
                                                duplication_pct=10.0)
    )
    assert first.fault_ledger.dropped == second.fault_ledger.dropped
    assert first.fault_ledger.duplicated == second.fault_ledger.duplicated
    assert dump_observations(first.delivered) == dump_observations(second.delivered)
