import numpy as np
import pytest

from skyweave.config import SimCheckConfig
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.geom import point_distance
from skyweave.fusion.triangulator import triangulate_detections
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.sim.generator import SyntheticPacketGenerator
from skyweave.sim.scene import SCENE_CHOICES, build_scene

ROOM_SCENE_CHOICES = tuple(scene for scene in SCENE_CHOICES if scene != "pixel_plane_crossing")


def _first_aligned_result(voxel_size: float):
    config = SimCheckConfig()
    config.rayweave.grid.voxel_size_m = voxel_size
    scale = 0.10 / voxel_size
    config.rayweave.grid.dims = tuple(int(round(v * scale)) for v in (48, 48, 32))
    scene = build_scene(config.simulation)
    frame = SyntheticPacketGenerator(scene, config.simulation).frames()[20]
    aligned = TimeAligner(config.fusion.align_window_ns, 2).align_frame(frame.motion_packets, frame.detection_packets)
    assert aligned is not None
    grid = VoxelGrid.from_config(config.rayweave.grid)
    scored = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer).score(aligned)
    peaks, measurements = PeakExtractor(grid, config.rayweave.peaks).extract(scored)
    return config, scene, frame, aligned, peaks, measurements


def test_room_perimeter_camera_counts_have_valid_inward_poses() -> None:
    for count in (3, 5, 7, 9, 11, 13, 15):
        config = SimCheckConfig()
        config.simulation.camera_count = count
        config.simulation.camera_layout = "room_perimeter"
        scene = build_scene(config.simulation)
        assert sorted(scene.cameras) == list(range(count))
        target = np.asarray(config.simulation.camera_target_m, dtype=np.float64)
        for camera in scene.cameras.values():
            assert camera.width == config.simulation.image_width
            assert camera.height == config.simulation.image_height
            assert camera.K.shape == (3, 3)
            assert camera.D.shape == (5,)
            assert camera.T_world_cam.shape == (4, 4)
            assert np.isfinite(camera.T_world_cam).all()
            inward = target - camera.position
            forward = camera.T_world_cam[:3, 2]
            assert float(np.dot(inward, forward)) > 0.0


def test_dispersed_perimeter_camera_layout_has_valid_inward_poses() -> None:
    config = SimCheckConfig()
    config.simulation.camera_count = 7
    config.simulation.camera_layout = "dispersed_perimeter"
    config.simulation.room_size_m = (42.0, 24.0, 14.0)
    config.simulation.camera_height_m = 1.6
    config.simulation.camera_target_m = (0.0, 0.0, 8.8)
    config.simulation.camera_margin_m = 1.5
    scene = build_scene(config.simulation)

    assert sorted(scene.cameras) == list(range(7))
    target = np.asarray(config.simulation.camera_target_m, dtype=np.float64)
    positions = np.asarray([camera.position for camera in scene.cameras.values()])
    assert np.ptp(positions[:, 0]) > 30.0
    assert np.ptp(positions[:, 1]) > 15.0
    for camera in scene.cameras.values():
        inward = target - camera.position
        forward = camera.T_world_cam[:3, 2]
        assert np.isfinite(camera.T_world_cam).all()
        assert float(np.dot(inward, forward)) > 0.0


def test_supported_synthetic_scene_motions_are_finite_and_distinct() -> None:
    signatures = []
    for scene_name in ROOM_SCENE_CHOICES:
        config = SimCheckConfig()
        config.simulation.scene = scene_name
        scene = build_scene(config.simulation)
        assert len(scene.truth) == config.simulation.frames
        positions = np.asarray([sample.position for sample in scene.truth], dtype=np.float64)
        velocities = np.asarray([sample.velocity for sample in scene.truth], dtype=np.float64)
        assert np.isfinite(positions).all()
        assert np.isfinite(velocities).all()
        assert positions[:, 0].min() >= -1.6
        assert positions[:, 0].max() <= 1.6
        assert positions[:, 1].min() >= -0.7
        assert positions[:, 1].max() <= 1.5
        assert positions[:, 2].min() >= 0.6
        assert positions[:, 2].max() <= 1.8
        signature_points = positions[[0, len(positions) // 2, -1]]
        signatures.append(tuple(np.round(signature_points.reshape(-1), 3)))
    assert len(set(signatures)) == len(ROOM_SCENE_CHOICES)


def test_pixel_plane_scene_is_far_field_and_visible_from_dispersed_array() -> None:
    config = SimCheckConfig()
    config.simulation.scene = "pixel_plane_crossing"
    config.simulation.camera_count = 7
    config.simulation.camera_layout = "dispersed_perimeter"
    config.simulation.room_size_m = (42.0, 24.0, 14.0)
    config.simulation.camera_height_m = 1.6
    config.simulation.camera_target_m = (0.0, 0.0, 8.8)
    config.simulation.camera_margin_m = 1.5
    config.rayweave.grid.origin_m = (-12.0, -7.0, 6.0)
    config.rayweave.grid.dims = (48, 28, 16)
    config.rayweave.grid.voxel_size_m = 0.50
    scene = build_scene(config.simulation)

    positions = np.asarray([sample.position for sample in scene.truth], dtype=np.float64)
    assert np.ptp(positions[:, 0]) > 20.0
    assert positions[:, 2].min() > 8.0

    frame = SyntheticPacketGenerator(scene, config.simulation).frames()[len(scene.truth) // 2]
    assert len(frame.motion_packets) >= 3
    assert frame.not_visible_packets <= 1


def test_room_perimeter_generator_emits_all_visible_camera_packets() -> None:
    for count in (3, 7, 15):
        config = SimCheckConfig()
        config.simulation.camera_count = count
        config.simulation.camera_layout = "room_perimeter"
        config.simulation.pixel_noise_std_px = 0.0
        scene = build_scene(config.simulation)
        frame = SyntheticPacketGenerator(scene, config.simulation).frames()[20]
        assert len(frame.motion_packets) >= 2
        assert len(frame.motion_packets) == len(frame.detection_packets)
        assert {packet.camera_id for packet in frame.motion_packets}.issubset(set(range(count)))


def test_room_perimeter_end_to_end_scales_to_fifteen_cameras() -> None:
    for count in (3, 7, 15):
        config = SimCheckConfig()
        config.simulation.camera_count = count
        config.simulation.camera_layout = "room_perimeter"
        config.rayweave.grid.voxel_size_m = 0.10
        config.rayweave.grid.dims = (48, 48, 32)
        scene = build_scene(config.simulation)
        frame = SyntheticPacketGenerator(scene, config.simulation).frames()[20]
        aligned = TimeAligner(config.fusion.align_window_ns, 2).align_frame(frame.motion_packets, frame.detection_packets)
        assert aligned is not None
        grid = VoxelGrid.from_config(config.rayweave.grid)
        scored = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer).score(aligned)
        peaks, measurements = PeakExtractor(grid, config.rayweave.peaks).extract(scored)
        assert peaks
        assert measurements
        err = point_distance(measurements[0].position, frame.truth.position)
        assert err <= np.sqrt(3.0) * config.rayweave.grid.voxel_size_m


def test_synthetic_rayweave_peak_within_voxel_diagonal() -> None:
    config, _scene, frame, _aligned, peaks, measurements = _first_aligned_result(0.10)
    assert peaks
    assert measurements
    err = point_distance(measurements[0].position, frame.truth.position)
    assert err <= np.sqrt(3.0) * config.rayweave.grid.voxel_size_m


def test_triangulation_near_truth() -> None:
    config, scene, frame, aligned, _peaks, _measurements = _first_aligned_result(0.10)
    tri = triangulate_detections(aligned.ts_ns, aligned.detection_packets, scene.cameras, config.fusion.pixel_noise_px)
    assert tri is not None
    assert point_distance(tri.position, frame.truth.position) < 0.03


def test_voxel_size_sweep_smoke() -> None:
    for voxel_size in (0.10, 0.075, 0.05):
        _config, _scene, _frame, _aligned, peaks, measurements = _first_aligned_result(voxel_size)
        assert peaks
        assert measurements


def test_unknown_scorer_backend_rejected() -> None:
    config = SimCheckConfig()
    config.rayweave.scorer.backend = "missing_backend"
    scene = build_scene(config.simulation)
    grid = VoxelGrid.from_config(config.rayweave.grid)

    with pytest.raises(ValueError, match="Unsupported Rayweave scorer backend"):
        RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)


def test_unknown_scorer_evidence_mode_rejected() -> None:
    config = SimCheckConfig()
    config.rayweave.scorer.evidence_mode = "missing_mode"
    scene = build_scene(config.simulation)
    grid = VoxelGrid.from_config(config.rayweave.grid)

    with pytest.raises(ValueError, match="Unsupported Rayweave evidence mode"):
        RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)


def test_centroid_evidence_mode_scores_synthetic_blobs() -> None:
    config = SimCheckConfig()
    config.rayweave.scorer.evidence_mode = "centroids"
    scene = build_scene(config.simulation)
    frame = SyntheticPacketGenerator(scene, config.simulation).frames()[20]
    aligned = TimeAligner(config.fusion.align_window_ns, 2).align_frame(frame.motion_packets, frame.detection_packets)
    assert aligned is not None
    grid = VoxelGrid.from_config(config.rayweave.grid)

    scored = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer).score(aligned)

    assert np.count_nonzero(scored.dense_scores) > 0
    assert np.max(scored.support_counts) >= config.rayweave.scorer.min_supporting_cameras


def test_numba_scorer_matches_python_reference() -> None:
    pytest.importorskip("numba")
    config = SimCheckConfig()
    scene = build_scene(config.simulation)
    frame = SyntheticPacketGenerator(scene, config.simulation).frames()[20]
    aligned = TimeAligner(config.fusion.align_window_ns, 2).align_frame(frame.motion_packets, frame.detection_packets)
    assert aligned is not None
    grid = VoxelGrid.from_config(config.rayweave.grid)

    config.rayweave.scorer.backend = "python_numpy"
    python_scored = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer).score(aligned)

    config.rayweave.scorer.backend = "numba"
    numba_scored = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer).score(aligned)

    np.testing.assert_allclose(numba_scored.dense_scores, python_scored.dense_scores, rtol=1e-6, atol=1e-6)
    np.testing.assert_array_equal(numba_scored.support_counts, python_scored.support_counts)


def test_numba_centroid_scorer_matches_python_reference() -> None:
    pytest.importorskip("numba")
    config = SimCheckConfig()
    config.rayweave.scorer.evidence_mode = "centroids"
    scene = build_scene(config.simulation)
    frame = SyntheticPacketGenerator(scene, config.simulation).frames()[20]
    aligned = TimeAligner(config.fusion.align_window_ns, 2).align_frame(frame.motion_packets, frame.detection_packets)
    assert aligned is not None
    grid = VoxelGrid.from_config(config.rayweave.grid)

    config.rayweave.scorer.backend = "python_numpy"
    python_scored = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer).score(aligned)

    config.rayweave.scorer.backend = "numba"
    numba_scored = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer).score(aligned)

    np.testing.assert_allclose(numba_scored.dense_scores, python_scored.dense_scores, rtol=1e-6, atol=1e-6)
    np.testing.assert_array_equal(numba_scored.support_counts, python_scored.support_counts)
