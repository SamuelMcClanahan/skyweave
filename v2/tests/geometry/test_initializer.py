"""Initializer: closest-point recovery, degeneracy (G3), conditioning."""

from __future__ import annotations

import numpy as np

from skyweave2.contracts import ResultStatus
from skyweave2.geometry import bearing_from_pixel, initialize, localize
from tests.geometry.conftest import exact_observations, look_at_camera


def _bearings_for(cameras, point, config, sigma=0.5):
    out = []
    for cam in cameras:
        proj = cam.project(np.asarray(point))
        assert proj is not None
        out.append(bearing_from_pixel(cam, proj[0], proj[1], np.eye(2) * sigma**2, config))
    return out


def test_closest_point_recovers_exact_intersection(config):
    target = (5.0, 120.0, 15.0)
    cameras = [
        look_at_camera(0, (-20.0, 0.0, 0.0), target),
        look_at_camera(1, (20.0, 0.0, 2.0), target),
        look_at_camera(2, (0.0, 10.0, 25.0), target),
    ]
    result = initialize(_bearings_for(cameras, target, config), config)
    assert result.ok
    assert not result.cheirality_violations
    assert np.allclose(result.position, target, atol=1e-6)


def test_g3_collinear_cameras_near_parallel_rays_return_weak_geometry(config):
    """G3: collinear array staring at a far target must never yield a confident point."""
    target = (5.0, 5000.0, 0.0)
    cameras = [
        look_at_camera(i, (float(5 * i), 0.0, 0.0), target) for i in range(3)
    ]  # max pairwise angle ~0.11 deg, below the 0.5 deg gate
    result = initialize(_bearings_for(cameras, target, config), config)
    assert not result.ok
    assert result.triangulation_angle_deg < config.min_triangulation_angle_deg

    localization = localize(
        exact_observations(cameras, target), {c.camera_id: c for c in cameras}, config
    )
    assert localization.status == ResultStatus.WEAK_GEOMETRY


def test_g3_effectively_parallel_rays_return_weak_geometry(config):
    """Rays to a target ~1e9 m away are parallel to machine precision."""
    target = (0.0, 1e9, 0.0)
    cameras = [
        look_at_camera(0, (0.0, 0.0, 0.0), target),
        look_at_camera(1, (10.0, 0.0, 0.0), target),
    ]
    localization = localize(
        exact_observations(cameras, target), {c.camera_id: c for c in cameras}, config
    )
    assert localization.status == ResultStatus.WEAK_GEOMETRY
