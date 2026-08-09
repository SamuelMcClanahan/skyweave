"""Deterministic initializer: weighted least-squares closest point over bearing lines.

Working plan §7.4: for camera center c_i, unit bearing d_i, weight w_i:

    P_i = I - d_i d_i^T
    A   = sum(w_i P_i)
    b   = sum(w_i P_i c_i)
    A x0 = b

x0 minimizes the weighted total squared perpendicular distance to the rays.
Conditioning and cheirality are checked here; the initializer never returns a
confident point from weak geometry — the caller maps ``ok=False`` to
``ResultStatus.WEAK_GEOMETRY``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from skyweave2.geometry.bearings import Bearing, triangulation_angle_deg
from skyweave2.geometry.config import GeometryConfig


@dataclass(frozen=True)
class InitializerResult:
    position: np.ndarray  # (3,) world meters; best effort even when ok=False
    ok: bool
    condition: float  # conditioning of the weighted closest-point matrix A
    triangulation_angle_deg: float
    cheirality_violations: list[int] = field(default_factory=list)  # camera_ids behind-ray


def _closest_point(bearings: list[Bearing], eps_singular: float) -> tuple[np.ndarray, float]:
    a = np.zeros((3, 3), dtype=np.float64)
    b = np.zeros(3, dtype=np.float64)
    for brg in bearings:
        p = np.eye(3) - np.outer(brg.direction, brg.direction)
        a += brg.weight * p
        b += brg.weight * (p @ brg.origin)
    eigvals = np.linalg.eigvalsh(a)
    if eigvals[0] <= eps_singular * max(eigvals[-1], 1.0):
        # Singular (parallel rays): least-squares fallback, flagged by condition.
        x0 = np.linalg.lstsq(a, b, rcond=None)[0]
        return x0, float(np.inf)
    x0 = np.linalg.solve(a, b)
    return x0, float(eigvals[-1] / eigvals[0])


def initialize(bearings: list[Bearing], config: GeometryConfig) -> InitializerResult:
    """Closest-point solve with conditioning, angle, and cheirality checks."""
    if len(bearings) < config.min_cameras:
        raise ValueError(f"need at least {config.min_cameras} bearings, got {len(bearings)}")

    x0, condition = _closest_point(bearings, config.eps_singular)
    angle = triangulation_angle_deg(bearings)
    violations = [
        brg.camera_id for brg in bearings if float((x0 - brg.origin) @ brg.direction) <= 0.0
    ]
    ok = (
        np.all(np.isfinite(x0))
        and condition <= config.max_condition
        and angle >= config.min_triangulation_angle_deg
        and not violations
    )
    return InitializerResult(
        position=x0,
        ok=bool(ok),
        condition=condition,
        triangulation_angle_deg=angle,
        cheirality_violations=violations,
    )
