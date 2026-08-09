"""Optics stages: vignetting, PSF blur, Brown-Conrady warp, decimation.

All functions operate on float64 radiance arrays of shape (H, W) or
(H, W, 3) and return new arrays. The radiance grid may be an integer
``oversample`` multiple of the sensor grid; ``decimate`` performs the box
average that models pixel fill factor.

numpy only (no scipy): the PSF is a separable Gaussian convolved with
explicit padding, and the warp is bilinear sampling at inverse-mapped
coordinates.
"""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model.config import SensorModelSpec, TruthCameraOptics


def _as_channels(image: np.ndarray) -> tuple[np.ndarray, bool]:
    """Return (H, W, C) view and whether the input was 2-D."""
    if image.ndim == 2:
        return image[:, :, None], True
    if image.ndim == 3:
        return image, False
    raise ValueError(f"expected (H, W) or (H, W, C) image, got shape {image.shape}")


def pixel_grid(height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Pixel-center coordinate grids (u right, v down), D0 convention."""
    v, u = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    return u, v


def _grid_intrinsics(
    optics: TruthCameraOptics, shape: tuple[int, int], margin_px: int = 0
) -> tuple[float, float, float, float]:
    """Intrinsics rescaled to a (possibly oversampled, possibly margined) grid.

    ``margin_px`` is the warp margin per side in SENSOR pixels; on the
    margined grid the principal point shifts by the margin before the
    D0 half-pixel scaling law is applied.
    """
    height, width = shape
    scale = width / (optics.width + 2 * margin_px)
    if abs(scale - height / (optics.height + 2 * margin_px)) > 1e-9:
        raise ValueError("radiance grid must be a uniform scale of the (margined) sensor grid")
    fx, fy = optics.fx * scale, optics.fy * scale
    cx = (optics.cx + margin_px + 0.5) * scale - 0.5
    cy = (optics.cy + margin_px + 0.5) * scale - 0.5
    return fx, fy, cx, cy


def vignetting_map(
    optics: TruthCameraOptics, spec: SensorModelSpec, shape: tuple[int, int]
) -> np.ndarray:
    """Relative illumination: cos^4 falloff plus a swept extra radial term."""
    fx, fy, cx, cy = _grid_intrinsics(optics, shape, spec.warp_margin_px)
    u, v = pixel_grid(*shape)
    x = (u - cx) / fx
    y = (v - cy) / fy
    cos_theta = 1.0 / np.sqrt(1.0 + x * x + y * y)
    relative = 1.0 - spec.vignetting_cos4_strength * (1.0 - cos_theta**4)
    relative = relative * (1.0 - spec.vignetting_extra * (x * x + y * y))
    return np.clip(relative, 0.0, None)


def apply_vignetting(
    image: np.ndarray, optics: TruthCameraOptics, spec: SensorModelSpec
) -> np.ndarray:
    channels, was_2d = _as_channels(np.asarray(image, dtype=np.float64))
    rel = vignetting_map(optics, spec, channels.shape[:2])[:, :, None]
    out = channels * rel
    return out[:, :, 0] if was_2d else out


def gaussian_kernel(sigma: float, radius_sigmas: float) -> np.ndarray:
    """Normalized 1-D Gaussian kernel; radius = ceil(radius_sigmas * sigma)."""
    radius = max(1, int(np.ceil(radius_sigmas * sigma)))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    return kernel / kernel.sum()


def _convolve_axis(image: np.ndarray, kernel: np.ndarray, axis: int) -> np.ndarray:
    """Separable convolution along one axis with edge-replicate padding."""
    radius = len(kernel) // 2
    pad = [(0, 0)] * image.ndim
    pad[axis] = (radius, radius)
    padded = np.pad(image, pad, mode="edge")
    out = np.zeros_like(image)
    for k, weight in enumerate(kernel):
        sl = [slice(None)] * image.ndim
        sl[axis] = slice(k, k + image.shape[axis])
        out += weight * padded[tuple(sl)]
    return out


def apply_psf(image: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """Gaussian PSF. Sigma is in SENSOR pixels; on an oversampled grid the
    kernel widens by the oversample factor so the sensor-referred blur is
    identical either way."""
    channels, was_2d = _as_channels(np.asarray(image, dtype=np.float64))
    kernel = gaussian_kernel(spec.psf_sigma_px * spec.oversample, spec.psf_kernel_radius_sigmas)
    out = _convolve_axis(channels, kernel, axis=0)
    out = _convolve_axis(out, kernel, axis=1)
    return out[:, :, 0] if was_2d else out


def _bilinear_sample(channels: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bilinear sample (H, W, C) at float pixel coords; edge-clamped."""
    height, width = channels.shape[:2]
    u = np.clip(u, 0.0, width - 1.0)
    v = np.clip(v, 0.0, height - 1.0)
    u0 = np.floor(u).astype(np.int64)
    v0 = np.floor(v).astype(np.int64)
    u1 = np.minimum(u0 + 1, width - 1)
    v1 = np.minimum(v0 + 1, height - 1)
    fu = (u - u0)[:, :, None]
    fv = (v - v0)[:, :, None]
    top = channels[v0, u0] * (1.0 - fu) + channels[v0, u1] * fu
    bottom = channels[v1, u0] * (1.0 - fu) + channels[v1, u1] * fu
    return top * (1.0 - fv) + bottom * fv


def undistort_normalized(
    x_d: np.ndarray,
    y_d: np.ndarray,
    dist: tuple[float, float, float, float, float],
    iterations: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized fixed-point inversion of the Brown-Conrady forward model.

    Same math as the D0 contract camera (fixed-point on the radial/tangential
    split), applied to arrays.
    """
    k1, k2, p1, p2, k3 = dist
    x, y = x_d.copy(), y_d.copy()
    for _ in range(iterations):
        r2 = x * x + y * y
        radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        dx = 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        dy = p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        x = (x_d - dx) / radial
        y = (y_d - dy) / radial
    return x, y


def crop_margin(image: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """Central crop removing the warp margin (used when the warp is disabled)."""
    if spec.warp_margin_px == 0:
        return image
    m = spec.warp_margin_px * spec.oversample
    return image[m:-m, m:-m].copy()


def apply_distortion_warp(
    image: np.ndarray, optics: TruthCameraOptics, spec: SensorModelSpec
) -> np.ndarray:
    """Warp the ideal pinhole render into the distorted image the lens forms.

    A scene ray at ideal normalized (x, y) lands at distorted normalized
    Brown-Conrady(x, y) on the real sensor. So for each OUTPUT (sensor)
    pixel, undistort its normalized position to find where that ray sits in
    the pinhole render, and bilinear-sample there. Truth coefficients only;
    the estimator's calibration is a separately perturbed object.

    The input may carry ``spec.warp_margin_px`` of rendered margin per side;
    samples that fall outside the nominal frame then hit real rendered
    content instead of the edge clamp (the margin's entire purpose). The
    output is always the nominal sensor grid (x oversample).
    """
    channels, was_2d = _as_channels(np.asarray(image, dtype=np.float64))
    if not any(abs(c) > 0.0 for c in optics.dist):
        out = crop_margin(channels, spec)
        return out[:, :, 0] if was_2d else out
    margin = spec.warp_margin_px
    fx, fy, cx, cy = _grid_intrinsics(optics, channels.shape[:2], margin)
    out_shape = (
        channels.shape[0] - 2 * margin * spec.oversample,
        channels.shape[1] - 2 * margin * spec.oversample,
    )
    offset = margin * spec.oversample
    u, v = pixel_grid(*out_shape)
    x_d = (u + offset - cx) / fx
    y_d = (v + offset - cy) / fy
    x, y = undistort_normalized(x_d, y_d, optics.dist, spec.distortion_invert_iterations)
    out = _bilinear_sample(channels, x * fx + cx, y * fy + cy)
    return out[:, :, 0] if was_2d else out


def decimate(image: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """Box-average an oversampled radiance grid down to the sensor grid.

    The box filter is the pixel fill-factor model from §5.1. Identity when
    ``oversample`` is 1.
    """
    if spec.oversample == 1:
        return np.asarray(image, dtype=np.float64).copy()
    channels, was_2d = _as_channels(np.asarray(image, dtype=np.float64))
    height, width, n_ch = channels.shape
    factor = spec.oversample
    if height % factor or width % factor:
        raise ValueError(
            f"oversampled shape {(height, width)} is not divisible by oversample={factor}"
        )
    out = channels.reshape(height // factor, factor, width // factor, factor, n_ch).mean(
        axis=(1, 3)
    )
    return out[:, :, 0] if was_2d else out
