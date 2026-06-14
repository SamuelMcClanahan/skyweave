from __future__ import annotations

import numpy as np

from skyweave.config import GridConfig, PeakConfig
from skyweave.messages import SparseVoxel, WeavefieldVolume
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import ScoredWeavefield


def _scored(radius: int) -> tuple[VoxelGrid, ScoredWeavefield, PeakConfig]:
    grid = VoxelGrid.from_config(GridConfig(origin_m=(0.0, 0.0, 0.0), dims=(3, 3, 3), voxel_size_m=1.0))
    dense = np.zeros(grid.dims, dtype=np.float32)
    dense[1, 1, 1] = 1.0
    dense[2, 1, 1] = 0.9
    volume = WeavefieldVolume(
        ts_ns=1,
        grid=grid.spec,
        voxels=[
            SparseVoxel(ix=1, iy=1, iz=1, score=1.0),
            SparseVoxel(ix=2, iy=1, iz=1, score=0.9),
        ],
        peaks=[],
        decay_s=1.0,
        source_packet_ids=[],
    )
    scored = ScoredWeavefield(volume, dense, np.zeros(grid.dims, dtype=np.uint8), {})
    config = PeakConfig(threshold_percentile=100.0, max_peaks=1, soft_argmax_radius_voxels=radius, soft_argmax_beta=0.0)
    return grid, scored, config


def test_peak_soft_argmax_refines_with_local_neighbor_scores() -> None:
    grid, scored, hard_config = _scored(radius=0)
    hard_peak = PeakExtractor(grid, hard_config).extract(scored)[0][0]

    grid, scored, soft_config = _scored(radius=1)
    soft_peak = PeakExtractor(grid, soft_config).extract(scored)[0][0]

    assert hard_peak.position == (1.5, 1.5, 1.5)
    assert soft_peak.position[0] > hard_peak.position[0]
    assert soft_peak.position[0] < 2.5
    assert soft_peak.position[1:] == hard_peak.position[1:]
