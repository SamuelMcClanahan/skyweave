"""N2-N7: bookkeeping attribution, time honesty, restart, dropout,
overconfidence detection, and determinism under fault replay."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from skyweave2.contracts import TrackState
from skyweave2.faults import injectors as inj
from skyweave2.faults.bookkeeping import LAYERS, build_layer_table
from skyweave2.faults.honesty import (
    HonestyVerdict,
    OverconfidenceEvent,
    decisions_fingerprint,
    evaluate_honesty,
    mahalanobis_sigma,
)
from skyweave2.fusion.aligner import obs_key
from skyweave2.fusion.engine import run_stream
from tests.faults.conftest import FPS, clean_stream, truth_lookup


def _keys(stream):
    return {obs_key(o) for o in stream}


def test_n2_planted_false_blobs_are_attributed_and_never_published(cameras, config):
    """N2: a planted false blob must terminate at a real defense layer, and
    the layer table must attribute it there — not to a layer the pipeline
    never ran."""
    stream = clean_stream(cameras, frames=range(0, 60))
    faulted, ledger = inj.inject_false_blobs(stream, 2.0, seed=7, cameras=cameras)
    assert ledger.planted, "fixture planted nothing"
    run = run_stream(faulted, cameras, config, session_uuid="n2")
    table = build_layer_table("false_blobs", "2.0", ledger, run, _keys(faulted))

    # Every planted item got a terminating layer that exists.
    for key in ledger.planted:
        layer = table.terminating_layer[key]
        assert layer in LAYERS, f"planted item attributed to unknown layer {layer}"
    # Finding D6-F3 pinned: at the manifest's HIGHEST clutter magnitude
    # (2.0 false blobs per camera-frame) a small fraction of spurious
    # 3-camera coincidences do survive to publication. Before the arrival-
    # order fix this assertion read `== 0` and passed vacuously, because
    # every plant was killed as "late" and never reached these layers.
    assert table.false_accept_rate < 0.10, (
        f"false-accept rate moved: {table.false_accept_rate:.3f}"
    )
    assert table.false_accepts > 0, (
        "no false accepts at the highest clutter rate — plants may again be "
        "failing to reach the association layers"
    )
    # The overwhelming majority are still rejected, and by real layers.
    rejected_layers = {layer for layer, tally in table.per_layer.items()
                       if tally.rejected_false > 0}
    assert rejected_layers - {7}, "no layer rejected any clutter"
    # True observations mostly survive.
    assert table.false_reject_rate < 0.5


def test_n2_attribution_is_grounded_in_the_engine_audit(cameras, config):
    """A planted item's attributed layer must correspond to an actual
    rejection record naming it (bookkeeping cannot invent a layer)."""
    stream = clean_stream(cameras, frames=range(0, 40))
    faulted, ledger = inj.inject_false_blobs(stream, 1.0, seed=11, cameras=cameras)
    run = run_stream(faulted, cameras, config, session_uuid="n2b")
    table = build_layer_table("false_blobs", "1.0", ledger, run, _keys(faulted))

    recorded = set()
    for batch in run.batches:
        for rejection in batch.rejections:
            recorded.update(rejection.obs_ids)
    for entry in run.excluded:
        recorded.add(obs_key(entry.obs))
    for record in run.records:
        recorded.update(record.obs_ids)

    for key, layer in table.terminating_layer.items():
        if key in recorded:
            continue
        # Un-recorded items may only be attributed to the "never associated"
        # layer — anything else would be a fabricated attribution.
        assert layer == 3, f"{key}: layer {layer} claimed with no audit record"


def test_n2_deleted_true_detections_count_as_false_rejects(cameras, config):
    """Deleting true detections must show up as lost true items, not be
    silently absorbed."""
    stream = clean_stream(cameras, frames=range(0, 60))
    faulted, ledger = inj.inject_deleted_detections(stream, 20.0, seed=7)
    run = run_stream(faulted, cameras, config, session_uuid="n2c")
    table = build_layer_table("deleted", "20", ledger, run, _keys(faulted))
    assert table.total_planted == 0  # deletion plants nothing
    assert table.total_true == len(faulted)
    # The surviving stream still tracks (the engine tolerates 20% loss).
    assert run.published


def test_n3_honest_and_dishonest_time_sync_differ_and_are_labeled(cameras, config):
    """N3: the same physical clock error declared honestly vs dishonestly
    produces different, correctly labeled envelope claims — and the run is
    scored under both."""
    stream = clean_stream(cameras, frames=range(0, 90))
    honest, _ = inj.inject_clock_error(stream, 10.0, 0.0, 0.0, seed=7, honest=True,
                                       dishonest_claim_ms=0.5, camera_ids=(0,))
    dishonest, _ = inj.inject_clock_error(stream, 10.0, 0.0, 0.0, seed=7, honest=False,
                                          dishonest_claim_ms=0.5, camera_ids=(0,))
    honest_claims = {o.envelope.time_sync_error_ms for o in honest
                     if o.envelope.camera_id == 0}
    dishonest_claims = {o.envelope.time_sync_error_ms for o in dishonest
                        if o.envelope.camera_id == 0}
    assert honest_claims == {10.0}
    assert dishonest_claims == {0.5}
    # Physically identical streams: only the DECLARATION differs.
    assert [o.envelope.capture_ts_ns for o in honest] == \
           [o.envelope.capture_ts_ns for o in dishonest]
    run_h = run_stream(honest, cameras, config, session_uuid="n3h")
    run_d = run_stream(dishonest, cameras, config, session_uuid="n3d")
    # Both must survive without crashing and produce labeled outcomes.
    for run in (run_h, run_d):
        verdict = evaluate_honesty(run, truth_lookup(), sigma_threshold=5.0)
        assert verdict.no_nan and verdict.labeled_exits


def test_n4_node_restart_no_corruption_track_survives_with_audit(cameras, config):
    """N4: a mid-clip reboot (new UUID + seq reset) must not corrupt
    alignment; the track survives or re-births, and the audit shows it."""
    stream = clean_stream(cameras, frames=range(0, 90))
    faulted, ledger = inj.inject_node_restart(stream, 1.5, FPS, camera_id=0)
    run = run_stream(faulted, cameras, config, session_uuid="n4")
    verdict = evaluate_honesty(run, truth_lookup(), sigma_threshold=5.0)
    assert verdict.no_nan
    assert run.published, "no track survived the reboot"
    # Post-restart observations were consumed by SOME accepted update.
    restart_frame = int(ledger.notes["restart_frame"])
    consumed = {oid for r in run.records if r.accepted for oid in r.obs_ids}
    post = [o for o in faulted if o.envelope.camera_id == 0
            and o.envelope.session_uuid != stream[0].envelope.session_uuid]
    assert post, "fixture produced no post-restart observations"
    assert any(obs_key(o) in consumed for o in post), (
        "post-reboot observations never entered an audited update"
    )
    # The reboot did not silently corrupt alignment: no sequence anomalies
    # beyond the declared session change.
    assert all(e.reason in ("late", "stale_or_duplicate") for e in run.excluded)
    _ = restart_frame


def _dropout_sigmas(cameras, config, camera_id):
    stream = clean_stream(cameras, frames=range(0, 120))
    faulted, ledger = inj.inject_camera_dropout(stream, camera_id=camera_id,
                                                from_s=2.0, fps=FPS)
    run = run_stream(faulted, cameras, config, session_uuid=f"n5-{camera_id}")
    cutoff = int(ledger.notes["dropout_from_frame"])
    before, after = [], []
    for batch_index, track in run.published:
        sigma = float(np.sqrt(np.trace(
            np.asarray(track.covariance).reshape(6, 6)[0:3, 0:3]) / 3.0))
        (before if batch_index < cutoff else after).append(sigma)
    return run, cutoff, before, after


def test_n5_end_camera_dropout_degrades_covariance_honestly(cameras, config):
    """N5: losing an END camera halves the 20 m baseline, so covariance must
    GROW, and route-B (2-camera) confirmation must still work."""
    run, cutoff, before, after = _dropout_sigmas(cameras, config, camera_id=2)
    assert before and after, "need samples on both sides of the dropout"
    assert np.mean(after) > np.mean(before), (
        f"covariance did not grow: {np.mean(before):.3f} -> {np.mean(after):.3f}"
    )
    post_states = [t for i, t in run.published if i >= cutoff]
    assert post_states, "tracking stopped entirely on two cameras"
    assert any(t.status in (TrackState.CONFIRMED, TrackState.REACQUIRED)
               for t in post_states)


def test_n5_center_camera_dropout_is_nearly_free_geometry(cameras, config):
    """Finding D6-F2 pinned: dropping the CENTER camera keeps the full 20 m
    baseline, so covariance does NOT grow — losing a camera is not
    equivalent to losing geometry, and a naive 'fewer cameras => bigger
    covariance' expectation is wrong. Tracking continues either way."""
    run, cutoff, before, after = _dropout_sigmas(cameras, config, camera_id=1)
    assert before and after
    # Measured: the center camera contributes redundancy, not baseline.
    assert np.mean(after) < 1.5 * np.mean(before)
    post_states = [t for i, t in run.published if i >= cutoff]
    assert any(t.status in (TrackState.CONFIRMED, TrackState.REACQUIRED)
               for t in post_states)


def test_n6_overconfidence_detector_can_fail(cameras, config):
    """N6: the detector must FIRE on a state whose error exceeds its own
    covariance, and stay silent on an honest one. Without this fixture a
    detector that never fires would look like a clean sweep."""
    # Direct unit check of the metric.
    honest_sigma = mahalanobis_sigma(np.array([0.1, 0.0, 0.0]), np.eye(3))
    assert honest_sigma == pytest.approx(0.1)
    liar_sigma = mahalanobis_sigma(np.array([50.0, 0.0, 0.0]), np.eye(3) * 0.01)
    assert liar_sigma == pytest.approx(500.0)

    # And end-to-end: a synthetic run whose published state is far from truth
    # with a tiny covariance must produce an event.
    stream = clean_stream(cameras, frames=range(0, 40))
    run = run_stream(stream, cameras, config, session_uuid="n6")
    assert run.published

    class LyingRun:
        def __init__(self, base):
            self.batches = base.batches
            self.records = base.records
            self.statuses = base.statuses
            self.excluded = base.excluded
            self.published = [
                (i, t.model_copy(update={
                    "state": (t.state[0] + 100.0,) + tuple(t.state[1:]),
                    "covariance": tuple(
                        (0.0001 if k % 7 == 0 else 0.0) for k in range(36)
                    ),
                }))
                for i, t in base.published
            ]

    class HonestRun:
        """Published states sitting exactly on truth with an honest 0.5 m
        covariance: the detector must stay SILENT here."""

        def __init__(self, base):
            self.batches = base.batches
            self.records = base.records
            self.statuses = base.statuses
            self.excluded = base.excluded
            self.published = []
            for i, t in base.published:
                truth = truth_lookup()(i)
                if truth is None:
                    continue
                cov = [0.0] * 36
                for k in range(3):
                    cov[k * 6 + k] = 0.25  # 0.5 m sigma per axis
                self.published.append((i, t.model_copy(update={
                    "state": tuple(float(v) for v in truth) + tuple(t.state[3:6]),
                    "covariance": tuple(cov),
                })))

    honest_verdict = evaluate_honesty(HonestRun(run), truth_lookup(),
                                      sigma_threshold=5.0)
    assert honest_verdict.overconfidence_count == 0, "detector fires on honest data"
    lying_verdict = evaluate_honesty(LyingRun(run), truth_lookup(),
                                     sigma_threshold=5.0, truth_match_m=500.0)
    assert lying_verdict.overconfidence_count > 0, "detector cannot fire"
    assert not lying_verdict.passes(tolerance=0)
    event = lying_verdict.overconfidence_events[0]
    assert isinstance(event, OverconfidenceEvent) and event.sigma > 5.0


def test_n6_verdict_passes_only_within_tolerance():
    verdict = HonestyVerdict()
    assert verdict.passes(tolerance=0)
    verdict.overconfidence_events.append(
        OverconfidenceEvent(batch_index=1, track_id=0, sigma=9.0, error_m=3.0,
                            status="confirmed")
    )
    assert not verdict.passes(tolerance=0)
    assert verdict.passes(tolerance=1)
    verdict.no_nan = False
    assert not verdict.passes(tolerance=1)


def test_n7_determinism_under_fault_replay(cameras, config):
    """N7: the same faulted stream replayed twice yields byte-identical
    decisions — across a compound of every stream-fault injector."""
    stream = clean_stream(cameras, frames=range(0, 90))

    def build():
        s, _ = inj.inject_false_blobs(stream, 0.5, seed=7, cameras=cameras)
        s, _ = inj.inject_deleted_detections(s, 5.0, seed=7)
        s, _ = inj.inject_clock_error(s, 0.5, 10.0, 0.1, seed=7, honest=True,
                                      dishonest_claim_ms=0.5)
        s, _ = inj.inject_reorder(s, seed=7)
        s, _ = inj.inject_duplication(s, seed=7, pct=5.0)
        return s

    stream_a, stream_b = build(), build()
    assert [obs_key(o) for o in stream_a] == [obs_key(o) for o in stream_b]
    run_a = run_stream(stream_a, cameras, config, session_uuid="n7")
    run_b = run_stream(stream_b, cameras, config, session_uuid="n7")
    assert decisions_fingerprint(run_a) == decisions_fingerprint(run_b)
    assert run_a.published, "determinism check is vacuous with no output"


def test_n7_different_fault_seed_changes_decisions(cameras, config):
    """The determinism check must be able to fail: a different fault seed
    must produce different decisions."""
    stream = clean_stream(cameras, frames=range(0, 90))
    a, _ = inj.inject_false_blobs(stream, 1.0, seed=7, cameras=cameras)
    b, _ = inj.inject_false_blobs(stream, 1.0, seed=99, cameras=cameras)
    run_a = run_stream(a, cameras, config, session_uuid="n7")
    run_b = run_stream(b, cameras, config, session_uuid="n7")
    assert decisions_fingerprint(run_a) != decisions_fingerprint(run_b)


def test_clean_data_overconfidence_rate_is_pinned(cameras, config):
    """Finding D6-F1 pinned: even with NO fault injected, the D1
    residual-variance-scaled measurement covariance occasionally collapses
    on a batch whose residuals happen to be small (3 cameras => 3 dof), the
    EKF over-trusts that measurement, and the published state exceeds 5
    sigma of its own covariance.

    Zero-tolerance means this is a NAMED FINDING, not a threshold to move.
    This test pins the measured clean-data rate so a regression (or a
    future fix) is visible; it does not bless the behavior.
    """
    stream = clean_stream(cameras, frames=range(0, 60))
    run = run_stream(stream, cameras, config, session_uuid="pin")
    verdict = evaluate_honesty(run, truth_lookup(), sigma_threshold=5.0)
    assert verdict.published_confirmed > 0
    rate = verdict.overconfidence_count / verdict.published_confirmed
    # Measured 2026-08-08: 1 event in 40 confirmed publications.
    assert 0.0 <= rate <= 0.10, f"clean-data overconfidence rate moved: {rate:.3f}"
    if verdict.overconfidence_events:
        worst = max(verdict.overconfidence_events, key=lambda e: e.sigma)
        # The mechanism: a collapsed covariance, not a wild state error.
        assert worst.error_m < 1.0, "unexpectedly large error, different mechanism"


def test_n7_injector_seeding_is_stable_across_processes(cameras):
    """N7 (cross-process): the in-process replay check cannot see
    PYTHONHASHSEED drift — string tags hashed with Python's salted hash()
    made the campaign differ between RUNS (917 vs 1427 events) while
    looking deterministic within one. This pins the stable digest.
    """
    import subprocess
    import sys

    script = (
        "import json;"
        "from skyweave2.faults.injectors import _stable_tag;"
        "print(json.dumps([_stable_tag(t) for t in "
        "('false','split','merge','jitter','loss','burst','dup','late','calib','reorder')]))"
    )
    outputs = []
    staged_source = str(Path(__file__).resolve().parents[2] / "src")
    for salt in ("0", "12345"):
        env = {
            "PYTHONHASHSEED": salt,
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": staged_source,
        }
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, env=env, check=True)
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1], (
        "seed tags differ across PYTHONHASHSEED values: campaign is not "
        "reproducible across runs"
    )
    # And the digest is not accidentally constant.
    values = json.loads(outputs[0])
    assert len(set(values)) == len(values)
