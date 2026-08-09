"""Refinement: exact recovery, invariance, robustness, covariance semantics.

Covers G1, G2, G4, G5, G6, G7, G8, G9 from the D1 brief.
"""

from __future__ import annotations

import numpy as np
import pytest

from skyweave2.contracts import ResultStatus
from skyweave2.geometry import GeometryConfig, leave_one_out, localize, solve_point
from tests.geometry.conftest import exact_observations, look_at_camera

TARGET = (8.0, 150.0, 20.0)


def _ring_cameras(n, target=TARGET, radius=40.0):
    cams = []
    for i in range(n):
        angle = 2.0 * np.pi * i / n
        pos = (radius * np.cos(angle), radius * np.sin(angle) * 0.3, 5.0 * np.sin(angle))
        cams.append(look_at_camera(i, pos, target))
    return cams


def _solve_arrays(cameras, target, sigma=0.5, rng=None, bias_px=None):
    pixels = []
    for i, cam in enumerate(cameras):
        proj = cam.project(np.asarray(target, dtype=np.float64))
        assert proj is not None
        uv = np.asarray(proj)
        if rng is not None:
            uv = uv + rng.normal(0.0, sigma, size=2)
        if bias_px is not None and i == bias_px[0]:
            uv = uv + np.asarray(bias_px[1])
        pixels.append(uv)
    pixels = np.stack(pixels)
    covs = np.stack([np.eye(2) * sigma**2] * len(cameras))
    return pixels, covs


def test_g1_exact_recovery_3_to_6_cameras(config):
    """G1: exact pixels reproduce the target to < 1e-6 m."""
    for n in (3, 4, 5, 6):
        cameras = _ring_cameras(n)
        result = localize(
            exact_observations(cameras, TARGET), {c.camera_id: c for c in cameras}, config
        )
        assert result.status == ResultStatus.CONFIRMED
        error = np.linalg.norm(np.asarray(result.position) - np.asarray(TARGET))
        assert error < 1e-6, f"{n} cameras: error {error}"
        assert result.supporting_camera_ids == list(range(n))
        assert len(result.obs_ids) == n


def test_g2_camera_order_invariance(config):
    """G2: shuffled observation order gives an identical result."""
    cameras = _ring_cameras(5)
    observations = exact_observations(cameras, TARGET)
    models = {c.camera_id: c for c in cameras}

    forward = localize(observations, models, config)
    rng = np.random.default_rng(2)
    shuffled = list(observations)
    rng.shuffle(shuffled)
    backward = localize(shuffled, models, config)

    assert forward.position == backward.position
    assert forward.covariance == backward.covariance
    assert forward.obs_ids == backward.obs_ids


def test_g4_target_behind_camera_is_excluded_or_fails_honestly(config):
    """G4: a camera the target sits behind is dropped by cheirality."""
    target = (0.0, 243.84, 0.0)
    good = [
        look_at_camera(0, (-10.0, 0.0, 0.0), target),
        look_at_camera(1, (10.0, 0.0, 0.0), target),
    ]
    # Camera north of the target looking further north: target is behind it.
    behind = look_at_camera(2, (0.0, 400.0, 0.0), (0.0, 800.0, 0.0))
    assert behind.project(np.asarray(target)) is None
    cameras = good + [behind]

    observations = exact_observations(good, target)
    # Fabricated detection at the principal point of the behind-camera.
    observations += exact_observations([behind], (0.0, 800.0, 0.0))

    result = localize(observations, {c.camera_id: c for c in cameras}, config)
    if result.status == ResultStatus.CONFIRMED:
        assert 2 not in result.supporting_camera_ids
        error = np.linalg.norm(np.asarray(result.position) - np.asarray(target))
        assert error < 1e-6
    else:  # honest failure is the allowed alternative
        assert result.status == ResultStatus.WEAK_GEOMETRY


def test_g5_huber_bounds_single_outlier_influence(config):
    """G5: one camera off by 30 px among 4; robust error stays bounded.

    Documented bound: at this geometry (ring of 4 cameras ~40 m from a target
    at ~150 m, f = 2000 px), the Huber solve stays within 0.5 m of truth while
    the non-robust solve is demonstrably worse (at least twice the error).
    """
    cameras = _ring_cameras(4)
    pixels, covs = _solve_arrays(cameras, TARGET, bias_px=(1, (30.0, 0.0)))

    robust = solve_point(cameras, pixels, covs, config, robust=True)
    plain = solve_point(cameras, pixels, covs, config, robust=False)

    err_robust = np.linalg.norm(robust.position - np.asarray(TARGET))
    err_plain = np.linalg.norm(plain.position - np.asarray(TARGET))
    assert err_robust < 0.5, f"robust error {err_robust}"
    assert err_plain > 2.0 * err_robust, f"plain {err_plain} vs robust {err_robust}"
    # The outlier camera carries the smallest robust weight.
    assert int(np.argmin(robust.robust_weights)) == 1


def test_g6_leave_one_out_flags_the_perturbed_camera(config):
    """G6: the LOO diagnostic singles out the camera from the G5 scenario."""
    cameras = _ring_cameras(4)
    pixels, covs = _solve_arrays(cameras, TARGET, bias_px=(1, (30.0, 0.0)))

    entries = leave_one_out(cameras, pixels, covs, config)
    flagged = [e.camera_id for e in entries if e.flagged]
    assert flagged == [1]
    # The consensus signature: only the solve without the outlier is
    # internally consistent, and it rejects the held-out outlier hard.
    by_id = {e.camera_id: e for e in entries}
    assert by_id[1].reduced_rms_px < 1e-6
    assert by_id[1].residual_px > 25.0


def test_g7_covariance_anisotropy_at_5m_baseline(config):
    """G7: at 5 m baseline / 244 m range, depth sigma ~ (range/baseline) x cross sigma."""
    target = (0.0, 243.84, 0.0)
    cameras = [
        look_at_camera(0, (-2.5, 0.0, 0.0), target, f=1995.3),
        look_at_camera(1, (0.0, 0.0, 0.0), target, f=1995.3),
        look_at_camera(2, (2.5, 0.0, 0.0), target, f=1995.3),
    ]
    rng = np.random.default_rng(7)
    pixels, covs = _solve_arrays(cameras, target, sigma=0.5, rng=rng)
    outcome = solve_point(cameras, pixels, covs, config)
    assert not outcome.weak_geometry

    eigvals, eigvecs = np.linalg.eigh(outcome.covariance)
    depth_sigma = np.sqrt(eigvals[-1])
    cross_sigma = np.sqrt(np.mean(eigvals[:2]))
    ratio = depth_sigma / cross_sigma
    expected = 243.84 / 5.0  # ~49
    assert 0.3 * expected < ratio < 3.0 * expected, f"ratio {ratio}"
    # Largest eigenvector points along depth (the +y line of sight).
    assert abs(eigvecs[:, -1] @ np.array([0.0, 1.0, 0.0])) > 0.95


def test_g8_covariance_coverage_under_pure_centroid_noise(config):
    """G8: empirical scatter matches reported covariance within a tolerance band.

    With N cameras the residual-variance scale is estimated from 2N-3 dof, so
    the Mahalanobis statistic is 3*F(3, 2N-3) rather than chi-square. With 8
    cameras (13 dof) the expected 1/2/3-sigma coverage is ~0.65/0.93/0.985;
    the bands below bracket those values.
    """
    cameras = _ring_cameras(8, radius=50.0)
    target = np.asarray(TARGET)
    sigma = 1.0
    covs = np.stack([np.eye(2) * sigma**2] * len(cameras))
    truth_pixels = np.stack([np.asarray(c.project(target)) for c in cameras])

    rng = np.random.default_rng(88)
    d2 = []
    for _ in range(1200):
        pixels = truth_pixels + rng.normal(0.0, sigma, size=truth_pixels.shape)
        outcome = solve_point(cameras, pixels, covs, config)
        assert not outcome.weak_geometry
        err = outcome.position - target
        info = np.linalg.pinv(outcome.covariance, hermitian=True)
        d2.append(float(err @ info @ err))
    d2 = np.asarray(d2)

    cov1 = float(np.mean(d2 <= 3.5267))
    cov2 = float(np.mean(d2 <= 8.0249))
    cov3 = float(np.mean(d2 <= 14.1564))
    assert 0.58 <= cov1 <= 0.74, f"1-sigma coverage {cov1}"
    assert 0.88 <= cov2 <= 0.97, f"2-sigma coverage {cov2}"
    assert 0.95 <= cov3 <= 1.0, f"3-sigma coverage {cov3}"


def _angular_solve(cameras, pixels, config, x_init):
    """Minimal unweighted Gauss-Newton on tangent-plane angular residuals."""
    from skyweave2.geometry import bearing_from_pixel

    bearings = [
        bearing_from_pixel(cam, pixels[i, 0], pixels[i, 1], np.eye(2), config)
        for i, cam in enumerate(cameras)
    ]

    def residuals(x):
        out = []
        for brg in bearings:
            d = x - brg.origin
            d = d / np.linalg.norm(d)
            out.append(brg.tangent_basis @ (d - brg.direction))
        return np.concatenate(out)

    x = np.asarray(x_init, dtype=np.float64).copy()
    h = 1e-5
    for _ in range(40):
        r = residuals(x)
        jac = np.empty((len(r), 3))
        for axis in range(3):
            pert = x.copy()
            pert[axis] += h
            jac[:, axis] = (residuals(pert) - r) / h
        step, *_ = np.linalg.lstsq(jac, -r, rcond=None)
        x = x + step
        if np.linalg.norm(step) < 1e-12:
            break
    return x


def test_g9_angular_and_reprojection_residuals_agree_on_clean_cases(config):
    """G9: the pixel-residual solve matches an angular-residual solve."""
    cameras = _ring_cameras(4)
    target = np.asarray(TARGET)

    # Exact pixels: both must sit on the truth.
    pixels, covs = _solve_arrays(cameras, TARGET)
    pixel_solution = solve_point(cameras, pixels, covs, config).position
    angular_solution = _angular_solve(cameras, pixels, config, target + np.array([1.0, -2.0, 0.5]))
    assert np.linalg.norm(pixel_solution - target) < 1e-6
    assert np.linalg.norm(angular_solution - target) < 1e-6

    # Small noise: identical cameras and isotropic pixel noise make the two
    # residual spaces near-proportional, so the minimizers stay close.
    rng = np.random.default_rng(9)
    pixels, covs = _solve_arrays(cameras, TARGET, sigma=0.5, rng=rng)
    pixel_solution = solve_point(cameras, pixels, covs, config, robust=False).position
    angular_solution = _angular_solve(cameras, pixels, config, pixel_solution)
    assert np.linalg.norm(pixel_solution - angular_solution) < 0.05


def test_mixed_clock_domains_rejected(config):
    from skyweave2.contracts import ClockDomain

    cameras = _ring_cameras(3)
    observations = exact_observations(cameras, TARGET)
    changed_envelope = observations[0].envelope.model_copy(
        update={"clock_domain": ClockDomain.NODE_PTP}
    )
    observations[0] = observations[0].model_copy(update={"envelope": changed_envelope})
    with pytest.raises(ValueError, match="clock domains"):
        localize(observations, {c.camera_id: c for c in cameras}, config)


def test_config_thresholds_are_used_not_hardcoded():
    """Loosening the angle gate flips a weak result to confirmed: the gate lives in config."""
    target = (5.0, 5000.0, 0.0)
    cameras = [look_at_camera(i, (float(5 * i), 0.0, 0.0), target) for i in range(3)]
    observations = exact_observations(cameras, target)
    models = {c.camera_id: c for c in cameras}

    strict = localize(observations, models, GeometryConfig())
    loose = localize(
        observations, models, GeometryConfig(min_triangulation_angle_deg=0.01)
    )
    assert strict.status == ResultStatus.WEAK_GEOMETRY
    assert loose.status == ResultStatus.CONFIRMED
