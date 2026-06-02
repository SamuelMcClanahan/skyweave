import numpy as np

from skyweave.messages import VoxelGridSpec
from skyweave.rayweave.dda import trace_ray_indices
from skyweave.rayweave.grid import VoxelGrid


def test_trace_ray_through_grid() -> None:
    grid = VoxelGrid(VoxelGridSpec(frame_id="world", origin=(0.0, 0.0, 0.0), dims=(4, 4, 4), voxel_size_m=1.0))
    indices = trace_ray_indices(np.array([-1.0, 1.5, 1.5]), np.array([1.0, 0.0, 0.0]), grid)
    assert indices == [(0, 1, 1), (1, 1, 1), (2, 1, 1), (3, 1, 1)]

