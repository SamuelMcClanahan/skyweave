from __future__ import annotations

import math

import numpy as np

from skyweave.rayweave.grid import VoxelGrid


def ray_aabb_intersection(
    origin: np.ndarray,
    direction: np.ndarray,
    box_min: np.ndarray,
    box_max: np.ndarray,
) -> tuple[float, float] | None:
    t_enter = -math.inf
    t_exit = math.inf
    for axis in range(3):
        d = float(direction[axis])
        if abs(d) < 1e-12:
            if origin[axis] < box_min[axis] or origin[axis] > box_max[axis]:
                return None
            continue
        t1 = (box_min[axis] - origin[axis]) / d
        t2 = (box_max[axis] - origin[axis]) / d
        near = min(t1, t2)
        far = max(t1, t2)
        t_enter = max(t_enter, near)
        t_exit = min(t_exit, far)
        if t_enter > t_exit:
            return None
    return max(t_enter, 0.0), t_exit


def trace_ray_indices(origin: np.ndarray, direction: np.ndarray, grid: VoxelGrid) -> list[tuple[int, int, int]]:
    hit = ray_aabb_intersection(origin, direction, grid.bounds_min, grid.bounds_max)
    if hit is None:
        return []
    t_enter, t_exit = hit
    if t_exit < 0:
        return []

    start = origin + direction * (t_enter + 1e-9)
    current = grid.point_to_index(start)
    if current is None:
        return []
    ix, iy, iz = current

    step = [1 if direction[axis] >= 0 else -1 for axis in range(3)]
    dims = grid.dims
    bounds_min = grid.bounds_min
    voxel = grid.voxel_size

    t_max: list[float] = []
    t_delta: list[float] = []
    for axis, idx in enumerate((ix, iy, iz)):
        if abs(float(direction[axis])) < 1e-12:
            t_max.append(math.inf)
            t_delta.append(math.inf)
            continue
        boundary_idx = idx + (1 if step[axis] > 0 else 0)
        boundary = bounds_min[axis] + boundary_idx * voxel
        t_max.append(float((boundary - origin[axis]) / direction[axis]))
        t_delta.append(float(voxel / abs(direction[axis])))

    visited: list[tuple[int, int, int]] = []
    while 0 <= ix < dims[0] and 0 <= iy < dims[1] and 0 <= iz < dims[2]:
        visited.append((ix, iy, iz))
        axis = int(np.argmin(t_max))
        if t_max[axis] > t_exit:
            break
        if axis == 0:
            ix += step[0]
        elif axis == 1:
            iy += step[1]
        else:
            iz += step[2]
        t_max[axis] += t_delta[axis]
    return visited

