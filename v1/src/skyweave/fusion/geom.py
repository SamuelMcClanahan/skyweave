from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CameraCalib:
    id: int
    K: np.ndarray
    D: np.ndarray
    width: int
    height: int
    T_world_cam: np.ndarray

    @property
    def position(self) -> np.ndarray:
        return self.T_world_cam[:3, 3]


def normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-12:
        raise ValueError("Cannot normalize a near-zero vector")
    return v / n


def look_at_pose(camera_position: np.ndarray, target: np.ndarray) -> np.ndarray:
    z_forward = normalize(target - camera_position)
    world_up = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    if abs(float(np.dot(z_forward, world_up))) > 0.98:
        world_up = np.array([0.0, 1.0, 0.0], dtype=np.float64)

    x_right = normalize(np.cross(z_forward, world_up))
    y_down = normalize(np.cross(z_forward, x_right))
    R = np.column_stack([x_right, y_down, z_forward])

    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = R
    T[:3, 3] = camera_position
    return T


def make_intrinsics(width: int, height: int, focal_length_px: float) -> np.ndarray:
    return np.array(
        [
            [focal_length_px, 0.0, width / 2.0],
            [0.0, focal_length_px, height / 2.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def world_to_camera(point_world: np.ndarray, camera: CameraCalib) -> np.ndarray:
    R = camera.T_world_cam[:3, :3]
    t = camera.T_world_cam[:3, 3]
    return R.T @ (point_world - t)


def project_point(point_world: np.ndarray, camera: CameraCalib) -> tuple[float, float] | None:
    point_cam = world_to_camera(point_world, camera)
    if point_cam[2] <= 1e-9:
        return None
    u = camera.K[0, 0] * point_cam[0] / point_cam[2] + camera.K[0, 2]
    v = camera.K[1, 1] * point_cam[1] / point_cam[2] + camera.K[1, 2]
    if not (0.0 <= u < camera.width and 0.0 <= v < camera.height):
        return None
    return float(u), float(v)


def ray_from_pixel(u: float, v: float, camera: CameraCalib) -> tuple[np.ndarray, np.ndarray]:
    x = (u - camera.K[0, 2]) / camera.K[0, 0]
    y = (v - camera.K[1, 2]) / camera.K[1, 1]
    direction_cam = normalize(np.array([x, y, 1.0], dtype=np.float64))
    direction_world = normalize(camera.T_world_cam[:3, :3] @ direction_cam)
    return camera.position.astype(np.float64), direction_world


def point_distance(a: tuple[float, float, float], b: np.ndarray) -> float:
    return float(np.linalg.norm(np.asarray(a, dtype=np.float64) - b))

