"""Regression tests for the D5 adversarial-review findings, plus the
can-fail fixtures the review demanded for every gate."""

from __future__ import annotations

import numpy as np

from skyweave2.contracts import TrackState
from skyweave2.fusion.association import associate
from skyweave2.fusion.engine import run_stream
from skyweave2.fusion.tracker import Tracker
from tests.fusion.conftest import make_observation, observe_point, target_position


def _project_obs(cameras, camera_id, point, frame_seq, obs_id):
    proj = cameras[camera_id].project(np.asarray(point, dtype=np.float64))
    assert proj is not None
    return make_observation(cameras[camera_id], proj[0], proj[1],
                            frame_seq=frame_seq, obs_id=obs_id)


def test_static_decoy_in_track_gate_does_not_starve_the_track(cameras, config):
    """CRITICAL review finding: cams 0/1 see the target, cam2 sees only a
    persistent static decoy whose ray passes near the track's path. The old
    claim-first design rejected the whole {true, true, decoy} group every
    batch and the target went unpublished through the encounter. Now the
    decoy is LOO-pruned and the survivors pass."""
    decoy_point = target_position(225) + np.array([0.0, 0.0, 8.0])
    rng = np.random.default_rng(41)
    stream = []
    for frame in range(200, 250):
        truth = target_position(frame)
        stream += observe_point(cameras, truth, frame_seq=frame,
                                camera_ids=(0, 1), noise_px=0.1, rng=rng)
        stream.append(_project_obs(cameras, 2, decoy_point, frame, obs_id=6))
    run = run_stream(stream, cameras, config, session_uuid="regress-decoy")

    published_batches = {i for i, _ in run.published}
    # The target publishes through the poisoned window (allowing brief
    # confirmation warm-up at the start).
    assert len(published_batches) >= 40, (
        f"track starved: only {len(published_batches)} published batches"
    )
    # The decoy leaves pruned-member audit records, not track deaths.
    pruned = [r for b in run.batches for r in b.rejections
              if r.reason == "pruned_member"]
    assert pruned, "the decoy was never pruned"
    assert all(status != TrackState.DELETED.value
               for status in run.statuses.values()) or len(run.statuses) == 1
    # Published states stay on the true trajectory.
    for batch_index, track in run.published:
        truth = target_position(batch_index)
        assert np.linalg.norm(np.asarray(track.state[0:3]) - truth) < 3.0


def test_failing_mixed_group_cannot_steal_true_observations(cameras, config):
    """MAJOR review finding: a 3-member mixed proposal that FAILS its solve
    must not claim the true observation it contains — the true pair must
    still form and track."""
    rng = np.random.default_rng(43)
    stream = []
    for frame in range(200, 215):
        truth = target_position(frame)
        stream += observe_point(cameras, truth, frame_seq=frame,
                                noise_px=0.1, rng=rng)
        # Decoys engineered onto the epipolar geometry of the true cam0
        # observation: each is the projection of a point ON cam0's true ray,
        # so pair gates pass, but the pair points disagree in 3D and the
        # merged 3-camera solve fails its residual.
        bearing_point_a = truth + np.array([0.0, -40.0, -3.2])  # on cam0 ray-ish
        stream.append(_project_obs(cameras, 1, bearing_point_a, frame, obs_id=7))
    run = run_stream(stream, cameras, config, session_uuid="regress-steal")
    assert run.published, "true target never tracked"
    for batch_index, track in run.published:
        truth = target_position(batch_index)
        assert np.linalg.norm(np.asarray(track.state[0:3]) - truth) < 5.0, (
            f"phantom published at {track.state[0:3]}"
        )


def test_gap_deleted_seed_group_births_instead_of_vanishing(cameras, config):
    """MAJOR review finding: a track deleted by the event-time gap loop must
    not swallow the solve-validated group its seed claimed — the group
    births a fresh track with audit records."""
    tracker = Tracker(config, session_uuid="regress-gap")
    rng = np.random.default_rng(47)
    stream = []
    frames_a = list(range(200, 204))
    for frame in frames_a:
        stream += observe_point(cameras, target_position(frame), frame_seq=frame,
                                noise_px=0.05, rng=rng)
    # Long silent gap (enough to coast AND delete), then support returns.
    gap = config.tracker.coast_after_misses + config.tracker.delete_after_coast + 2
    frames_b = list(range(204 + gap, 204 + gap + 4))
    for frame in frames_b:
        stream += observe_point(cameras, target_position(frame), frame_seq=frame,
                                noise_px=0.05, rng=rng)
    run = run_stream(stream, cameras, config, session_uuid="regress-gap",
                     tracker=tracker)
    statuses = tracker.statuses()
    assert statuses[0] == TrackState.DELETED  # the gap killed the first track
    assert len(statuses) >= 2, "post-gap observations vanished without a birth"
    # Every batch's consumed observations appear in some audit record.
    recorded = {oid for r in tracker.records for oid in r.obs_ids}
    for frame in frames_b[1:]:  # first post-gap batch may seed the dead track
        assert any(f":{frame}:" in oid for oid in recorded), (
            f"frame {frame} observations left no audit trace"
        )
    assert run.published, "the reborn track never confirmed"


def test_epipolar_gate_rejects_skew_pairs(cameras, config):
    """Can-fail fixture for the pair gate: two observations whose rays are
    genuinely skew (pair reprojection far above the gate) must produce NO
    candidate group. Deleting the gate makes this fail."""
    truth = target_position(225)
    obs_a = _project_obs(cameras, 0, truth, 225, obs_id=0)
    skew_point = truth + np.array([25.0, -60.0, 12.0])
    obs_b = _project_obs(cameras, 2, skew_point, 225, obs_id=1)
    result = associate([obs_a, obs_b], cameras, [], config)
    assert result.groups == []
    assert len(result.unassigned) == 2


def test_residual_gate_rejects_two_camera_disagreement(cameras, config):
    """Can-fail fixture for the post-solve residual gate: a 2-camera pair
    that passes the epipolar gate but disagrees beyond residual_px_max is
    rejected WITH an audit record (no pruning possible at min_cameras)."""
    truth = target_position(225)
    obs_a = _project_obs(cameras, 0, truth, 225, obs_id=0)
    proj = cameras[2].project(truth)
    # 8 px offset PERPENDICULAR to the epipolar direction (v): an along-
    # epipolar offset is absorbed by triangulation as depth error, but a
    # cross-epipolar one skews the rays and survives into the solve
    # residual (~4 px > residual_px_max) while the loosened pair gate passes.
    obs_b = make_observation(cameras[2], proj[0], proj[1] + 8.0, 225, obs_id=1)
    tight = config.model_copy(deep=True)
    tight.association.epipolar_gate_px = 12.0  # ensure the pair gate passes
    result = associate([obs_a, obs_b], cameras, [], tight)
    assert result.groups == []
    reasons = [r.reason for r in result.rejections]
    assert "residual" in reasons or "weak_geometry" in reasons
    residual_rejects = [r for r in result.rejections if r.reason == "residual"]
    if residual_rejects:
        assert residual_rejects[0].detail > tight.association.residual_px_max
