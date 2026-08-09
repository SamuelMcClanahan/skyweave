"""Bearing construction: direction, tangent basis, angular covariance mapping."""

from __future__ import annotations

import numpy as np

from skyweave2.geometry import bearing_from_observation, bearing_from_pixel
from tests.geometry.conftest import exact_observations, look_at_camera


def test_bearing_direction_points_at_target(config):
    target = (10.0, 200.0, 30.0)
    cam = look_at_camera(0, (0.0, 0.0, 0.0), target)
    proj = cam.project(np.asarray(target))
    bearing = bearing_from_pixel(cam, proj[0], proj[1], np.eye(2) * 0.25, config)

    expected = np.asarray(target) / np.linalg.norm(np.asarray(target))
    assert np.allclose(bearing.direction, expected, atol=1e-9)
    assert np.allclose(bearing.origin, [0.0, 0.0, 0.0], atol=1e-12)
    # Tangent basis is orthonormal and perpendicular to the direction.
    assert np.allclose(bearing.tangent_basis @ bearing.direction, 0.0, atol=1e-9)
    assert np.allclose(bearing.tangent_basis @ bearing.tangent_basis.T, np.eye(2), atol=1e-9)


def test_angular_covariance_scales_like_sigma_over_f(config):
    """1 px of centroid sigma at focal length f is ~1/f radians of bearing sigma."""
    f = 2000.0
    sigma = 1.0
    cam = look_at_camera(0, (0.0, 0.0, 0.0), (0.0, 100.0, 0.0), f=f)
    proj = cam.project(np.array([0.0, 100.0, 0.0]))
    bearing = bearing_from_pixel(cam, proj[0], proj[1], np.eye(2) * sigma**2, config)

    mean_var = float(np.trace(bearing.cov_angular)) / 2.0
    expected = (sigma / f) ** 2
    assert 0.8 * expected < mean_var < 1.2 * expected
    assert np.isclose(bearing.weight, 1.0 / mean_var)


def test_bearing_from_observation_checks_identity(config):
    target = (0.0, 150.0, 10.0)
    cam_a = look_at_camera(0, (0.0, 0.0, 0.0), target)
    cam_b = look_at_camera(1, (20.0, 0.0, 0.0), target)
    obs = exact_observations([cam_a], target)[0]

    bearing = bearing_from_observation(obs, cam_a, config)
    assert bearing.camera_id == 0

    try:
        bearing_from_observation(obs, cam_b, config)
    except ValueError as err:
        assert "camera_id" in str(err)
    else:  # pragma: no cover
        raise AssertionError("mismatched camera_id must be rejected")
