"""S1 (optics stages): hand-computed expected outputs."""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model import SensorModelSpec, TruthCameraOptics
from skyweave2.sensor_model.optics import (
    apply_distortion_warp,
    apply_psf,
    apply_vignetting,
    decimate,
    gaussian_kernel,
    vignetting_map,
)


def test_vignetting_hand_case(camera):
    """At the principal point the factor is 1; at x=1 normalized it is cos^4 = 1/4."""
    spec = SensorModelSpec(vignetting_cos4_strength=1.0, vignetting_extra=0.0)
    rel = vignetting_map(camera, spec, (camera.height, camera.width))
    # Principal point (31.5, 23.5) sits between pixels; sample its neighbors.
    assert rel[23, 31] > 0.999
    # Pixel u=131.5 would be x=1 exactly; the grid is 64 wide, so verify with
    # a wider synthetic camera where the point exists.
    wide = TruthCameraOptics(width=264, height=48, fx=100.0, fy=100.0, cx=131.5, cy=23.5)
    # u=231.5 not on grid either; use u=231, x=0.995 -> cos^4=(1/(1+x^2))^2
    rel_wide = vignetting_map(wide, SensorModelSpec(), (48, 264))
    x = (231 - 131.5) / 100.0
    y = (23 - 23.5) / 100.0
    expected = (1.0 / (1.0 + x * x + y * y)) ** 2
    assert abs(rel_wide[23, 231] - expected) < 1e-12


def test_vignetting_half_strength(camera):
    spec = SensorModelSpec(vignetting_cos4_strength=0.5)
    rel = vignetting_map(camera, spec, (camera.height, camera.width))
    full = vignetting_map(camera, SensorModelSpec(vignetting_cos4_strength=1.0),
                          (camera.height, camera.width))
    assert np.allclose(rel, 1.0 - 0.5 * (1.0 - full), atol=1e-12)


def test_vignetting_darkens_corners(camera):
    spec = SensorModelSpec()
    image = np.ones((camera.height, camera.width))
    out = apply_vignetting(image, camera, spec)
    assert out[0, 0] < out[23, 31] <= 1.0


def test_psf_kernel_is_normalized_gaussian():
    kernel = gaussian_kernel(sigma=1.0, radius_sigmas=4.0)
    assert len(kernel) == 9
    assert abs(kernel.sum() - 1.0) < 1e-12
    # Hand value: unnormalized center 1, +-1 = exp(-0.5).
    expected_ratio = np.exp(-0.5)
    assert abs(kernel[5] / kernel[4] - expected_ratio) < 1e-12


def test_psf_preserves_flat_field_and_energy(camera):
    spec = SensorModelSpec(psf_sigma_px=0.8)
    flat = np.full((48, 64), 3.7)
    assert np.allclose(apply_psf(flat, spec), 3.7, atol=1e-12)
    impulse = np.zeros((49, 65))
    impulse[24, 32] = 1.0
    blurred = apply_psf(impulse, spec)
    assert abs(blurred.sum() - 1.0) < 1e-9  # interior impulse: energy conserved
    assert blurred[24, 32] == blurred.max()


def test_distortion_zero_coeffs_is_identity(camera, clean_spec):
    rng = np.random.default_rng(3)
    image = rng.random((48, 64))
    out = apply_distortion_warp(image, camera, clean_spec)
    assert np.array_equal(out, image)


def test_distortion_warp_matches_hand_forward_model(camera):
    """Warped coordinate image round-trips through an independently written
    forward Brown-Conrady model back to the output pixel position."""
    dist = (-0.15, 0.03, 0.001, -0.002, 0.0)
    optics = TruthCameraOptics(
        width=64, height=48, fx=100.0, fy=100.0, cx=31.5, cy=23.5, dist=dist
    )
    spec = SensorModelSpec()
    u_image, v_image = np.meshgrid(
        np.arange(48, dtype=np.float64), np.arange(64, dtype=np.float64), indexing="ij"
    )[1], np.meshgrid(
        np.arange(48, dtype=np.float64), np.arange(64, dtype=np.float64), indexing="ij"
    )[0]
    warped_u = apply_distortion_warp(u_image, optics, spec)
    warped_v = apply_distortion_warp(v_image, optics, spec)

    def forward(x, y):  # hand-written Brown-Conrady, independent of the code
        k1, k2, p1, p2, k3 = dist
        r2 = x * x + y * y
        radial = 1 + k1 * r2 + k2 * r2**2 + k3 * r2**3
        return (
            x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x),
            y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y,
        )

    # Interior pixels only: border samples are edge-clamped by design.
    for v_s, u_s in ((24, 40), (12, 20), (30, 50)):
        x_ideal = (warped_u[v_s, u_s] - 31.5) / 100.0
        y_ideal = (warped_v[v_s, u_s] - 23.5) / 100.0
        x_back, y_back = forward(x_ideal, y_ideal)
        assert abs(x_back * 100.0 + 31.5 - u_s) < 2e-2
        assert abs(y_back * 100.0 + 23.5 - v_s) < 2e-2


def test_decimate_box_average_hand_case():
    spec = SensorModelSpec(oversample=2)
    image = np.array([[1.0, 3.0, 10.0, 20.0], [5.0, 7.0, 30.0, 40.0]])
    out = decimate(image, spec)
    assert out.shape == (1, 2)
    assert abs(out[0, 0] - 4.0) < 1e-12  # mean(1, 3, 5, 7)
    assert abs(out[0, 1] - 25.0) < 1e-12  # mean(10, 20, 30, 40)


def test_decimate_identity_at_oversample_one():
    spec = SensorModelSpec(oversample=1)
    image = np.arange(12, dtype=np.float64).reshape(3, 4)
    assert np.array_equal(decimate(image, spec), image)


def test_margin_crop_is_exact_central_crop_when_undistorted(camera):
    """Zero distortion + margin: the warp reduces to the central crop."""
    spec = SensorModelSpec(warp_margin_px=4)
    rng = np.random.default_rng(6)
    margined = rng.random((48 + 8, 64 + 8))
    out = apply_distortion_warp(margined, camera, spec)
    assert out.shape == (48, 64)
    assert np.array_equal(out, margined[4:-4, 4:-4])


def test_margin_supplies_real_content_to_the_warp():
    """Barrel distortion pulls border samples OUTSIDE the nominal frame; the
    margin must serve real rendered content there, where a crop-then-warp
    would silently edge-clamp (the exact artifact the margin was frozen to
    prevent)."""
    dist = (-0.3, 0.0, 0.0, 0.0, 0.0)
    optics = TruthCameraOptics(
        width=64, height=48, fx=100.0, fy=100.0, cx=31.5, cy=23.5, dist=dist
    )
    margin = 4
    spec_margin = SensorModelSpec(warp_margin_px=margin)
    spec_plain = SensorModelSpec(warp_margin_px=0)

    # Coordinate ramp over the MARGINED grid, in nominal-frame coordinates
    # (margin columns carry negative / > width-1 values: real content).
    u = np.meshgrid(
        np.arange(48 + 2 * margin, dtype=np.float64),
        np.arange(64 + 2 * margin, dtype=np.float64),
        indexing="ij",
    )[1]
    ramp_margined = u - margin
    warped = apply_distortion_warp(ramp_margined, optics, spec_margin)
    cropped_first = apply_distortion_warp(ramp_margined[margin:-margin, margin:-margin],
                                          optics, spec_plain)

    def forward(x, y):  # independent Brown-Conrady forward model
        k1 = dist[0]
        r2 = x * x + y * y
        return x * (1 + k1 * r2), y * (1 + k1 * r2)

    # Border pixel (u_s=0, v_s=24): its ideal sample sits ~1 px outside the
    # nominal frame. The margin path must return that real ramp value...
    u_s, v_s = 0, 24
    x_ideal_reported = (warped[v_s, u_s] + 0.5 - 32.0) / 100.0  # ramp value -> normalized
    x_back, _ = forward(x_ideal_reported, (v_s - 23.5) / 100.0)
    assert abs(x_back * 100.0 + 31.5 - u_s) < 5e-2
    assert warped[v_s, u_s] < 0.0  # sampled from genuine margin content
    # ...while the crop-first path clamps and lands measurably elsewhere.
    assert abs(cropped_first[v_s, u_s] - warped[v_s, u_s]) > 0.3
