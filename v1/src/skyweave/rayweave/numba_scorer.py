from __future__ import annotations

import numpy as np

try:
    from numba import njit, prange
except ImportError as exc:  # pragma: no cover - exercised only when backend is selected without numba
    raise ImportError("The numba scorer backend requires installing the 'numba' package.") from exc


@njit(cache=True)
def score_rays_numba(
    score_by_camera: np.ndarray,
    touched_indices: np.ndarray,
    touched_stamps: np.ndarray,
    frame_stamp: int,
    ray_camera_slots: np.ndarray,
    ray_u: np.ndarray,
    ray_v: np.ndarray,
    ray_weight: np.ndarray,
    camera_positions: np.ndarray,
    camera_rotations: np.ndarray,
    camera_intrinsics: np.ndarray,
    grid_origin: np.ndarray,
    dims: np.ndarray,
    voxel_size: float,
) -> int:
    bounds_max0 = grid_origin[0] + dims[0] * voxel_size
    bounds_max1 = grid_origin[1] + dims[1] * voxel_size
    bounds_max2 = grid_origin[2] + dims[2] * voxel_size
    touched_count = 0

    for ray_idx in range(ray_u.shape[0]):
        slot = ray_camera_slots[ray_idx]
        fx = camera_intrinsics[slot, 0]
        fy = camera_intrinsics[slot, 1]
        cx = camera_intrinsics[slot, 2]
        cy = camera_intrinsics[slot, 3]

        x = (ray_u[ray_idx] - cx) / fx
        y = (ray_v[ray_idx] - cy) / fy
        inv_norm = 1.0 / (x * x + y * y + 1.0) ** 0.5
        dir_cam0 = x * inv_norm
        dir_cam1 = y * inv_norm
        dir_cam2 = inv_norm

        direction0 = (
            camera_rotations[slot, 0, 0] * dir_cam0
            + camera_rotations[slot, 0, 1] * dir_cam1
            + camera_rotations[slot, 0, 2] * dir_cam2
        )
        direction1 = (
            camera_rotations[slot, 1, 0] * dir_cam0
            + camera_rotations[slot, 1, 1] * dir_cam1
            + camera_rotations[slot, 1, 2] * dir_cam2
        )
        direction2 = (
            camera_rotations[slot, 2, 0] * dir_cam0
            + camera_rotations[slot, 2, 1] * dir_cam1
            + camera_rotations[slot, 2, 2] * dir_cam2
        )
        world_norm = (direction0 * direction0 + direction1 * direction1 + direction2 * direction2) ** 0.5
        if world_norm < 1e-12:
            continue
        direction0 /= world_norm
        direction1 /= world_norm
        direction2 /= world_norm

        origin0 = camera_positions[slot, 0]
        origin1 = camera_positions[slot, 1]
        origin2 = camera_positions[slot, 2]

        t_enter = -1.0e300
        t_exit = 1.0e300

        hit, t_enter, t_exit = _update_intersection_axis(origin0, direction0, grid_origin[0], bounds_max0, t_enter, t_exit)
        if not hit:
            continue
        hit, t_enter, t_exit = _update_intersection_axis(origin1, direction1, grid_origin[1], bounds_max1, t_enter, t_exit)
        if not hit:
            continue
        hit, t_enter, t_exit = _update_intersection_axis(origin2, direction2, grid_origin[2], bounds_max2, t_enter, t_exit)
        if not hit or t_exit < 0.0:
            continue
        if t_enter < 0.0:
            t_enter = 0.0

        start0 = origin0 + direction0 * (t_enter + 1e-9)
        start1 = origin1 + direction1 * (t_enter + 1e-9)
        start2 = origin2 + direction2 * (t_enter + 1e-9)
        ix = int((start0 - grid_origin[0]) / voxel_size)
        iy = int((start1 - grid_origin[1]) / voxel_size)
        iz = int((start2 - grid_origin[2]) / voxel_size)
        if ix < 0 or ix >= dims[0] or iy < 0 or iy >= dims[1] or iz < 0 or iz >= dims[2]:
            continue

        step0 = 1 if direction0 >= 0.0 else -1
        step1 = 1 if direction1 >= 0.0 else -1
        step2 = 1 if direction2 >= 0.0 else -1

        t_max0, t_delta0 = _axis_step(origin0, direction0, grid_origin[0], voxel_size, ix, step0)
        t_max1, t_delta1 = _axis_step(origin1, direction1, grid_origin[1], voxel_size, iy, step1)
        t_max2, t_delta2 = _axis_step(origin2, direction2, grid_origin[2], voxel_size, iz, step2)

        weight = ray_weight[ray_idx]
        while 0 <= ix < dims[0] and 0 <= iy < dims[1] and 0 <= iz < dims[2]:
            score_by_camera[slot, ix, iy, iz] += weight
            flat_index = (ix * dims[1] + iy) * dims[2] + iz
            if touched_stamps[flat_index] != frame_stamp:
                touched_stamps[flat_index] = frame_stamp
                touched_indices[touched_count] = flat_index
                touched_count += 1
            if t_max0 <= t_max1 and t_max0 <= t_max2:
                if t_max0 > t_exit:
                    break
                ix += step0
                t_max0 += t_delta0
            elif t_max1 <= t_max2:
                if t_max1 > t_exit:
                    break
                iy += step1
                t_max1 += t_delta1
            else:
                if t_max2 > t_exit:
                    break
                iz += step2
                t_max2 += t_delta2

    return touched_count


@njit(cache=True, parallel=True)
def combine_scores_numba(
    score_by_camera: np.ndarray,
    combined: np.ndarray,
    support_counts: np.ndarray,
    min_supporting_cameras: int,
) -> None:
    n_cameras = score_by_camera.shape[0]
    nx = score_by_camera.shape[1]
    ny = score_by_camera.shape[2]
    nz = score_by_camera.shape[3]
    for ix in prange(nx):
        for iy in range(ny):
            for iz in range(nz):
                score = 0.0
                support = 0
                for camera_idx in range(n_cameras):
                    camera_score = score_by_camera[camera_idx, ix, iy, iz]
                    if camera_score > 0.0:
                        support += 1
                        score += camera_score
                support_counts[ix, iy, iz] = support
                combined[ix, iy, iz] = score if support >= min_supporting_cameras else 0.0


@njit(cache=True)
def combine_touched_scores_numba(
    score_by_camera: np.ndarray,
    combined: np.ndarray,
    support_counts: np.ndarray,
    touched_indices: np.ndarray,
    min_supporting_cameras: int,
) -> None:
    n_cameras = score_by_camera.shape[0]
    ny = score_by_camera.shape[2]
    nz = score_by_camera.shape[3]
    yz = ny * nz
    for idx in range(touched_indices.shape[0]):
        flat = touched_indices[idx]
        ix = flat // yz
        rem = flat - ix * yz
        iy = rem // nz
        iz = rem - iy * nz
        score = 0.0
        support = 0
        for camera_idx in range(n_cameras):
            camera_score = score_by_camera[camera_idx, ix, iy, iz]
            if camera_score > 0.0:
                support += 1
                score += camera_score
        support_counts[ix, iy, iz] = support
        combined[ix, iy, iz] = score if support >= min_supporting_cameras else 0.0


@njit(cache=True)
def _update_intersection_axis(
    origin: float,
    direction: float,
    box_min: float,
    box_max: float,
    t_enter: float,
    t_exit: float,
) -> tuple[bool, float, float]:
    if abs(direction) < 1e-12:
        return box_min <= origin <= box_max, t_enter, t_exit

    t1 = (box_min - origin) / direction
    t2 = (box_max - origin) / direction
    near = t1 if t1 < t2 else t2
    far = t2 if t1 < t2 else t1
    if near > t_enter:
        t_enter = near
    if far < t_exit:
        t_exit = far
    return t_enter <= t_exit, t_enter, t_exit


@njit(cache=True)
def _axis_step(
    origin: float,
    direction: float,
    grid_origin: float,
    voxel_size: float,
    index: int,
    step: int,
) -> tuple[float, float]:
    if abs(direction) < 1e-12:
        return 1.0e300, 1.0e300
    boundary_index = index + 1 if step > 0 else index
    boundary = grid_origin + boundary_index * voxel_size
    t_max = (boundary - origin) / direction
    t_delta = voxel_size / abs(direction)
    return t_max, t_delta
