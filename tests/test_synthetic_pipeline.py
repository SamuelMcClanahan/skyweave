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
from skyweave.sim.scene import build_scene


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
