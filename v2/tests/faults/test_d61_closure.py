"""D6.1: systematic channel, redefined overconfidence gate, layer 2, floor.

Every gate here has a can-fail fixture in BOTH directions, per the spec.
"""

from __future__ import annotations

import numpy as np

from skyweave2.contracts import SystematicSource
from skyweave2.faults import injectors as inj
from skyweave2.faults.bookkeeping import build_layer_table
from skyweave2.faults.honesty import evaluate_honesty, sigma_with_bound
from skyweave2.fusion.aligner import obs_key
from skyweave2.fusion.association import associate
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream
from skyweave2.fusion.systematic import calibration_bound, estimate_systematic
from tests.faults.conftest import clean_stream, truth_lookup
from tests.fusion.conftest import make_observation, observe_point, target_position


def _declared(rotation=0.0, position=0.0, width=0.0, speed=0.0) -> FusionConfig:
    config = FusionConfig()
    return config.model_copy(update={"systematic": config.systematic.model_copy(
        update={"calibration_rotation_sigma_deg": rotation,
                "calibration_position_sigma_m": position,
                "target_width_m": width, "estimated_speed_mps": speed})})


# ---- D6-F1: the systematic channel is populated ------------------------


def test_declared_zero_gives_zero_bound_and_no_sources(cameras):
    stream = clean_stream(cameras, frames=range(0, 30))
    run = run_stream(stream, cameras, _declared(), session_uuid="d61-zero")
    results = [r for b in run.batches for _, r in b.results]
    assert results
    for result in results:
        assert result.systematic_bound == (0.0, 0.0, 0.0)
        assert result.systematic_sources == []


def test_declared_rotation_produces_a_bound_that_scales_with_range(cameras):
    """A rotation sigma is angular: its world bound must grow with range."""
    near = np.array([0.0, 100.0, 20.0])
    far = np.array([0.0, 400.0, 20.0])
    cams = list(cameras.values())
    bound_near = calibration_bound(near, cams, 1.0, 0.0)
    bound_far = calibration_bound(far, cams, 1.0, 0.0)
    ratio = float(np.max(bound_far)) / float(np.max(bound_near))
    # Propagated through the SOLVE, an angular error grows as range^2 /
    # baseline (D1's depth-error law), not linearly: 4x range gives ~16x,
    # not ~4x. A bound computed as a bare per-ray displacement would land
    # near 4 and fail here.
    assert 12.0 < ratio < 24.0, f"bound did not scale as range^2: {ratio:.2f}"
    assert float(np.max(bound_far)) > np.radians(1.0) * 400.0 * 2.0, (
        "bound is missing the triangulation amplification"
    )
    # A position sigma behaves differently per axis, and the solve shows it:
    # cross-range is ~1:1 with the declared displacement, while DEPTH
    # amplifies with range because moving a camera changes the effective
    # baseline. Asserting "position sigma is range-independent" (the naive
    # per-ray expectation) fails here — correctly.
    pos_near = calibration_bound(near, cams, 0.0, 0.5)
    pos_far = calibration_bound(far, cams, 0.0, 0.5)
    assert abs(pos_near[0] - pos_far[0]) < 0.05, "cross-range should not scale"
    depth_ratio = pos_far[1] / pos_near[1]
    assert 3.0 < depth_ratio < 5.5, f"depth did not scale with range: {depth_ratio:.2f}"


def test_sources_are_labeled_per_contributing_term(cameras):
    stream = observe_point(cameras, target_position(45), frame_seq=45)
    cams = [cameras[o.envelope.camera_id] for o in stream]
    position = target_position(45)

    _, only_calib = estimate_systematic(position, stream, cams, 0.1, 0.0, 0.0, 0.0)
    assert only_calib == [SystematicSource.CALIBRATION]

    _, only_target = estimate_systematic(position, stream, cams, 0.0, 0.0, 0.75, 0.0)
    assert only_target == [SystematicSource.TARGET_REFERENCE]

    _, only_clock = estimate_systematic(position, stream, cams, 0.0, 0.0, 0.0, 30.0)
    assert only_clock == [SystematicSource.CLOCK]

    bound, all_three = estimate_systematic(position, stream, cams, 0.1, 0.05, 0.75, 30.0)
    assert set(all_three) == {SystematicSource.CALIBRATION,
                              SystematicSource.TARGET_REFERENCE,
                              SystematicSource.CLOCK}
    assert min(bound) > 0.0


def test_published_tracks_carry_the_bound(cameras):
    stream = clean_stream(cameras, frames=range(0, 40))
    run = run_stream(stream, cameras, _declared(rotation=0.1, width=0.75, speed=30.0),
                     session_uuid="d61-carry")
    assert run.published
    assert run.systematic, "no systematic bound reached the published states"
    for (_batch, _tid), (bound, sources) in run.systematic.items():
        assert max(bound) > 0.0
        assert SystematicSource.CALIBRATION in sources


# ---- D6-F1 gate: honest vs dishonest declaration -----------------------


def test_gate_metric_absorbs_error_only_up_to_the_declared_bound():
    error = np.array([1.0, 0.0, 0.0])
    cov = np.eye(3) * 0.01  # 0.1 m sigma
    # No declaration: 10 sigma.
    assert sigma_with_bound(error, cov, None) == 10.0
    # Honest declaration covering the error: nothing left to charge.
    assert sigma_with_bound(error, cov, (1.0, 1.0, 1.0)) == 0.0
    # Partial declaration: only the excess is charged.
    assert sigma_with_bound(error, cov, (0.5, 0.5, 0.5)) == 5.0
    # A lie (near-zero declaration) leaves the full error charged.
    assert sigma_with_bound(error, cov, (1e-6, 1e-6, 1e-6)) > 9.99


def test_honest_declaration_clears_a_calibration_fault(cameras):
    """Injected rotation error, DECLARED honestly: zero overconfidence."""
    stream = clean_stream(cameras, frames=range(0, 90))
    perturbed, _ = inj.perturb_cameras(cameras, seed=7, rotation_err_deg=0.5)
    honest = _declared(rotation=0.5, width=0.75, speed=30.0)
    run = run_stream(stream, perturbed, honest, session_uuid="d61-honest")
    verdict = evaluate_honesty(run, truth_lookup(), sigma_threshold=5.0)
    assert verdict.published_confirmed > 0
    assert verdict.overconfidence_count == 0, (
        f"{verdict.overconfidence_count} events despite an honest declaration"
    )
    assert verdict.declared_bound_states > 0


def test_dishonest_declaration_is_caught(cameras):
    """The same injected fault, DECLARED as near-zero: events appear. This is
    the can-fail direction — if the gate stopped firing here it would be
    blessing a lie."""
    stream = clean_stream(cameras, frames=range(0, 90))
    perturbed, _ = inj.perturb_cameras(cameras, seed=7, rotation_err_deg=0.5)
    lying = _declared(rotation=1e-6, position=1e-6, width=0.0, speed=0.0)
    run = run_stream(stream, perturbed, lying, session_uuid="d61-lie")
    verdict = evaluate_honesty(run, truth_lookup(), sigma_threshold=5.0)
    assert verdict.overconfidence_count > 0, "the gate blessed a known lie"


# ---- D6-F2: variance floor from D4 Measured repeatability --------------


def test_variance_floor_is_evidence_derived_and_active():
    from skyweave2.geometry import GeometryConfig

    floor = GeometryConfig().residual_variance_floor
    # (0.128 px measured / 0.5 px declared)^2 = 0.0655; pinned to the
    # documented derivation, not to any gate-scene outcome.
    assert abs(floor - 0.066) < 0.005
    assert floor > 0.0, "floor disabled: D6-F2 regression"


def test_zero_fault_overconfidence_is_reduced_by_the_floor(cameras):
    """The zero-fault baseline must be cleaner than the pre-D6.1 measurement
    (1.3%). This can fail if the floor is removed."""
    stream = clean_stream(cameras, frames=range(0, 90))
    run = run_stream(stream, cameras, _declared(width=0.75, speed=30.0),
                     session_uuid="d61-floor")
    verdict = evaluate_honesty(run, truth_lookup(), sigma_threshold=5.0)
    assert verdict.published_confirmed > 0
    rate = verdict.overconfidence_count / verdict.published_confirmed
    assert rate == 0.0, f"zero-fault regime still overconfident: {rate:.3f}"


# ---- D6-F4: layer 2 appears in the layer table -------------------------


def test_epipolar_rejections_are_recorded_per_observation(cameras, config):
    """A genuinely skew pair must leave an 'epipolar' audit record."""
    truth = target_position(45)
    obs_a = observe_point(cameras, truth, frame_seq=45, camera_ids=(0,))[0]
    skew = truth + np.array([25.0, -60.0, 12.0])
    proj = cameras[2].project(skew)
    obs_b = make_observation(cameras[2], proj[0], proj[1], frame_seq=45, obs_id=1)
    result = associate([obs_a, obs_b], cameras, [], config)
    reasons = [r.reason for r in result.rejections]
    assert "epipolar" in reasons, "epipolar prefilter left no audit record"
    record = next(r for r in result.rejections if r.reason == "epipolar")
    assert set(record.obs_ids) == {obs_key(obs_a), obs_key(obs_b)}
    assert record.detail > config.association.epipolar_gate_px


def test_layer_two_appears_in_the_layer_table(cameras, config):
    """With clutter present, layer 2 must carry a non-zero tally — it read
    0/0 for the whole D6 campaign before this instrumentation."""
    stream = clean_stream(cameras, frames=range(0, 60))
    faulted, ledger = inj.inject_false_blobs(stream, 2.0, seed=7, cameras=cameras)
    run = run_stream(faulted, cameras, config, session_uuid="d61-layer2")
    table = build_layer_table("false_blobs", "2.0", ledger, run,
                              {obs_key(o) for o in faulted})
    tally = table.per_layer.get(2)
    assert tally is not None, "layer 2 still absent from the table"
    assert (tally.rejected_false + tally.rejected_true) > 0
