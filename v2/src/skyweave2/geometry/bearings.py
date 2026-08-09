"""Bearing construction: Observation2D + CameraModel -> world ray + angular covariance.

A bearing is the back-projection of one 2D centroid: origin at the camera
center, unit direction in world ENU, plus a 2x2 angular covariance expressed
in a tangent basis perpendicular to the direction. The angular covariance is
the pixel centroid covariance mapped through the unprojection Jacobian
(finite differences, honest about distortion).

The initializer weight is the inverse mean angular variance, so cameras with
tighter centroids and longer focal lengths pull harder on the closest-point
solve, matching working plan §7.4.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave2.contracts import CameraModel, Observation2D
from skyweave2.geometry.config import GeometryConfig


@dataclass(frozen=True)
class Bearing:
    """One camera's ray toward the target, with angular uncertainty."""

    camera_id: int
    origin: np.ndarray  # (3,) world meters, the camera center
    direction: np.ndarray  # (3,) world, unit norm
    tangent_basis: np.ndarray  # (2, 3) rows e1, e2 orthonormal, perpendicular to direction
    cov_angular: np.ndarray  # (2, 2) rad^2 in the tangent basis
    weight: float  # 1 / mean angular variance, for the weighted closest-point solve


def _tangent_basis(direction: np.ndarray, eps: float) -> np.ndarray:
    """Two orthonormal vectors perpendicular to ``direction``."""
    ref = np.array([0.0, 0.0, 1.0])
    if abs(float(direction @ ref)) > 1.0 - 1e-6:
        ref = np.array([1.0, 0.0, 0.0])
    e1 = np.cross(direction, ref)
    n1 = float(np.linalg.norm(e1))
    if n1 < eps:
        raise ValueError("degenerate direction vector")
    e1 /= n1
    e2 = np.cross(direction, e1)
    e2 /= float(np.linalg.norm(e2))
    return np.stack([e1, e2])


def bearing_from_pixel(
    camera: CameraModel,
    u: float,
    v: float,
    cov_px: np.ndarray,
    config: GeometryConfig,
) -> Bearing:
    """Build a bearing from a raw full-resolution pixel and its 2x2 covariance."""
    origin, direction = camera.unproject(u, v)
    basis = _tangent_basis(direction, config.eps_direction_norm)

    # Jacobian of the tangent-plane direction components wrt (u, v), by
    # forward differences through the full unprojection (incl. distortion).
    h = config.fd_step_px
    jac = np.empty((2, 2), dtype=np.float64)
    for col, (du, dv) in enumerate(((h, 0.0), (0.0, h))):
        _, d_pert = camera.unproject(u + du, v + dv)
        jac[:, col] = basis @ (d_pert - direction) / h

    cov_angular = jac @ np.asarray(cov_px, dtype=np.float64) @ jac.T
    mean_var = float(np.trace(cov_angular)) / 2.0
    weight = 1.0 / max(mean_var, config.eps_singular)
    return Bearing(
        camera_id=camera.camera_id,
        origin=origin,
        direction=direction,
        tangent_basis=basis,
        cov_angular=cov_angular,
        weight=weight,
    )


def bearing_from_observation(
    observation: Observation2D,
    camera: CameraModel,
    config: GeometryConfig,
) -> Bearing:
    """Contract-level entry: Observation2D carries full-res raw pixels (D0 §2)."""
    if observation.envelope.camera_id != camera.camera_id:
        raise ValueError(
            f"observation camera_id {observation.envelope.camera_id} "
            f"!= camera model camera_id {camera.camera_id}"
        )
    if observation.envelope.calibration_rev != camera.calibration_rev:
        raise ValueError(
            f"calibration_rev mismatch: observation {observation.envelope.calibration_rev!r} "
            f"vs camera {camera.calibration_rev!r}"
        )
    cov_px = np.array(
        [
            [observation.cov_uu, observation.cov_uv],
            [observation.cov_uv, observation.cov_vv],
        ],
        dtype=np.float64,
    )
    return bearing_from_pixel(camera, observation.u, observation.v, cov_px, config)


def triangulation_angle_deg(bearings: list[Bearing]) -> float:
    """Maximum pairwise angle between bearing directions (D0 field 9)."""
    best = 0.0
    for i in range(len(bearings)):
        for j in range(i + 1, len(bearings)):
            c = float(np.clip(bearings[i].direction @ bearings[j].direction, -1.0, 1.0))
            best = max(best, float(np.degrees(np.arccos(c))))
    return best
