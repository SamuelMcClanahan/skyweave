"""Camera model and pixel conventions (D0, frozen).

Conventions:
- World frame: local ENU, meters. x east, y north, z up.
- Camera frame: OpenCV. +X right, +Y down, +Z along the optical axis.
- ``T_world_cam`` maps points FROM the camera frame TO the world frame:
  ``p_world = R @ p_cam + t``. The camera center in world coordinates is ``t``.
- Pixels: continuous coordinates; (0.0, 0.0) is the CENTER of the top-left
  pixel. u right, v down. An image of width W spans u in [-0.5, W - 0.5].
- Distortion: Brown-Conrady (k1, k2, p1, p2, k3), applied to normalized
  image coordinates. Stored pixel coordinates are RAW (distorted) sensor
  pixels unless a field explicitly says otherwise.

Resolution scaling law (D0):
  a detector may run at a reduced resolution, but reported centroids are in
  full-resolution coordinates. With per-axis scale s = full/proc:
      u_full = (u_proc + 0.5) * s - 0.5
"""

from __future__ import annotations

import numpy as np

from skyweave2.contracts.base import FrozenContractModel

_EPS_DEPTH = 1e-9


def proc_to_full(coord_proc: float, scale: float) -> float:
    """Map a pixel-center coordinate from the processing grid to the full grid."""
    return (coord_proc + 0.5) * scale - 0.5


def full_to_proc(coord_full: float, scale: float) -> float:
    """Inverse of :func:`proc_to_full`."""
    return (coord_full + 0.5) / scale - 0.5


class CameraModel(FrozenContractModel):
    """Estimator-side calibrated camera. Serialized separately from any truth model.

    Reserved wire field numbers: 1 camera_id, 2 width, 3 height, 4 k,
    5 dist, 6 t_world_cam, 7 calibration_rev.
    """

    camera_id: int
    width: int
    height: int
    k: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    dist: tuple[float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0)
    t_world_cam: tuple[
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
    ]
    calibration_rev: str

    # -- derived arrays -------------------------------------------------
    def k_matrix(self) -> np.ndarray:
        return np.asarray(self.k, dtype=np.float64)

    def rotation_world_cam(self) -> np.ndarray:
        return np.asarray(self.t_world_cam, dtype=np.float64)[:3, :3]

    def position_world(self) -> np.ndarray:
        return np.asarray(self.t_world_cam, dtype=np.float64)[:3, 3]

    # -- distortion in normalized coordinates ---------------------------
    def distort_normalized(self, x: float, y: float) -> tuple[float, float]:
        k1, k2, p1, p2, k3 = self.dist
        r2 = x * x + y * y
        radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        x_d = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        y_d = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        return x_d, y_d

    def undistort_normalized(
        self, x_d: float, y_d: float, iterations: int = 20
    ) -> tuple[float, float]:
        """Fixed-point inversion of :meth:`distort_normalized`."""
        x, y = x_d, y_d
        for _ in range(iterations):
            k1, k2, p1, p2, k3 = self.dist
            r2 = x * x + y * y
            radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
            dx = 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
            dy = p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
            x = (x_d - dx) / radial
            y = (y_d - dy) / radial
        return x, y

    # -- projection ------------------------------------------------------
    def project(self, p_world: np.ndarray) -> tuple[float, float] | None:
        """World point -> raw pixel (u, v), or None if behind the camera."""
        p = np.asarray(p_world, dtype=np.float64)
        r = self.rotation_world_cam()
        p_cam = r.T @ (p - self.position_world())
        if p_cam[2] <= _EPS_DEPTH:
            return None
        x, y = p_cam[0] / p_cam[2], p_cam[1] / p_cam[2]
        x_d, y_d = self.distort_normalized(x, y)
        k = self.k_matrix()
        u = k[0, 0] * x_d + k[0, 2]
        v = k[1, 1] * y_d + k[1, 2]
        return float(u), float(v)

    def unproject(self, u: float, v: float) -> tuple[np.ndarray, np.ndarray]:
        """Raw pixel -> (origin_world, unit_direction_world)."""
        k = self.k_matrix()
        x_d = (u - k[0, 2]) / k[0, 0]
        y_d = (v - k[1, 2]) / k[1, 1]
        x, y = self.undistort_normalized(x_d, y_d)
        d_cam = np.array([x, y, 1.0], dtype=np.float64)
        d_world = self.rotation_world_cam() @ d_cam
        d_world /= np.linalg.norm(d_world)
        return self.position_world(), d_world
