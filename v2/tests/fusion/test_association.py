"""F2 (clean association) and F3 (ghost rejection — fixture fails first)."""

from __future__ import annotations

import numpy as np

from skyweave2.fusion.association import TrackSeed, associate
from skyweave2.fusion.engine import run_stream
from skyweave2.geometry import GeometryConfig
from tests.fusion.conftest import make_observation, observe_point, target_position


def test_f2_single_target_three_cameras(cameras, config):
    truth = target_position(225)
    observations = observe_point(cameras, truth, frame_seq=225)
    result = associate(observations, cameras, [], config)
    assert isinstance(result.groups, list)  # multi-group interface always
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.camera_ids == (0, 1, 2)
    assert group.source == "direct"
    assert result.unassigned == []


def test_f2_single_target_two_cameras(cameras, config):
    truth = target_position(225)
    observations = observe_point(cameras, truth, frame_seq=225, camera_ids=(0, 2))
    result = associate(observations, cameras, [], config)
    assert len(result.groups) == 1
    assert result.groups[0].camera_ids == (0, 2)


def test_f2_track_seeded_group_claims_first(cameras, config):
    truth = target_position(225)
    observations = observe_point(cameras, truth, frame_seq=225)
    seed = TrackSeed(track_id=7, position=truth + np.array([0.5, 0.5, 0.0]))
    result = associate(observations, cameras, [seed], config)
    assert len(result.groups) == 1
    assert result.groups[0].source == "track"
    assert result.groups[0].seed_track_id == 7


def _ghost_fixture(cameras):
    """Two decoys whose cam0/cam2 rays genuinely cross in 3D.

    Construction: place a phantom 3D point q away from the true target,
    then give cam0 a decoy exactly on its ray to q and cam2 a decoy exactly
    on its ray to q. The cross-pairing (cam0-decoy x cam2-decoy) then
    triangulates q with tiny pair residual — the plausible-but-wrong
    pairing of F3. Camera 1 sees nothing near q.
    """
    q = np.array([-60.0, 200.0, 45.0])  # phantom, far from the true target
    obs = []
    for camera_id in (0, 2):
        proj = cameras[camera_id].project(q)
        assert proj is not None
        obs.append(make_observation(cameras[camera_id], proj[0], proj[1],
                                    frame_seq=225, obs_id=5))
    return q, obs


def test_f3_ghost_fixture_actually_passes_the_pair_gate(cameras, config):
    """The dangerous half of F3: prove the fixture WOULD fool a pair-only
    associator. If this stops passing, the ghost test below tests nothing."""
    _, ghost_obs = _ghost_fixture(cameras)
    result = associate(ghost_obs, cameras, [], config)
    assert len(result.groups) == 1  # the wrong pairing forms a 2-cam group
    assert result.groups[0].camera_ids == (0, 2)


def test_f3_ghost_gets_no_consensus_and_true_group_wins(cameras, config):
    truth = target_position(225)
    true_obs = observe_point(cameras, truth, frame_seq=225)
    _, ghost_obs = _ghost_fixture(cameras)
    result = associate(true_obs + ghost_obs, cameras, [], config)
    # The true 3-camera group exists and owns the true observations.
    three_cam = [g for g in result.groups if len(g.camera_ids) == 3]
    assert len(three_cam) == 1
    # The ghost pairing never acquires third-camera consensus.
    for group in result.groups:
        if group.camera_ids != (0, 1, 2):
            assert len(group.camera_ids) == 2
            assert all(o.obs_id == 5 for o in group.observations)


def test_f3_ghost_never_confirms_over_a_stream(cameras, config):
    """Full-chain F3 with a fixture that is PROVABLY dangerous: for five
    frames the decoys in cams 0 and 2 are projections of a single moving
    phantom 3D point — their rays genuinely cross, the pair gate passes
    (asserted below), and 2-camera ghost groups DO form. The crossing is
    transient, so route B's consecutive-batch requirement must starve the
    ghost; the earlier drifting-image-decoy version of this fixture never
    even passed the pair gate and tested nothing (review finding)."""
    rng = np.random.default_rng(35)
    # STRICTLY shorter than the route-B streak: a phantom persisting for
    # confirm_two_cam_batches consecutive batches is, by the frozen design,
    # a legitimately confirmable object — transient means below that.
    phantom_frames = range(210, 210 + config.tracker.confirm_two_cam_batches - 1)
    stream = []
    ghost_obs_by_frame = {}
    for frame in range(200, 230):
        truth = target_position(frame)
        stream += observe_point(cameras, truth, frame_seq=frame,
                                noise_px=0.15, rng=rng)
        if frame in phantom_frames:
            phantom = np.array([-70.0 + 2.0 * (frame - 210), 190.0, 50.0])
            ghost_obs = []
            for camera_id in (0, 2):
                proj = cameras[camera_id].project(phantom)
                ghost_obs.append(make_observation(
                    cameras[camera_id], proj[0], proj[1], frame_seq=frame, obs_id=9,
                ))
            ghost_obs_by_frame[frame] = ghost_obs
            stream += ghost_obs

    # Fixture-fails-first proof: the phantom pairing forms a candidate
    # group when presented alone (it would fool a pair-only associator).
    probe = associate(ghost_obs_by_frame[210], cameras, [], config)
    assert len(probe.groups) == 1, "ghost fixture is not dangerous — test is vacuous"

    run = run_stream(stream, cameras, config, session_uuid="f3-run",
                     geometry_config=GeometryConfig())
    confirmed_positions = [t.state[0:3] for _, t in run.published]
    assert confirmed_positions, "true target should confirm"
    for pos in confirmed_positions:
        # Every published state sits on the true trajectory (y ~ 244 m,
        # z ~ 20 m), never at the phantom (y=190, z=50).
        assert abs(pos[1] - 243.84) < 15.0 and abs(pos[2] - 20.0) < 10.0, (
            f"ghost track published at {pos}"
        )
