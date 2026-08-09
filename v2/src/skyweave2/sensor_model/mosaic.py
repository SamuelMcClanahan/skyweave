"""Bayer mosaic (BGGR, Provisional), bilinear demosaic, and the tiny ISP tail.

CFA is BGGR per the Luckfox issue thread — Provisional until verified
against the SC3336 driver during the bench session (D3 brief). The pattern
is a config field so a driver surprise is a one-line change.

The mosaic stage exists because a 2-px target through mosaic+demosaic
acquires a phase-dependent centroid bias; skipping it makes tiny targets
behave better in sim than reality (design §5.3). The Bayer round-trip
experiment in ``experiments.py`` measures exactly that cost.
"""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model.config import SensorModelSpec

_CFA_LAYOUTS = {
    # 2x2 tile, channel index per (row % 2, col % 2); 0=R, 1=G, 2=B.
    "BGGR": ((2, 1), (1, 0)),
    "RGGB": ((0, 1), (1, 2)),
    "GBRG": ((1, 2), (0, 1)),
    "GRBG": ((1, 0), (2, 1)),
}


def cfa_channel_map(pattern: str, shape: tuple[int, int]) -> np.ndarray:
    """(H, W) array of channel indices (0=R, 1=G, 2=B) for the CFA."""
    if pattern not in _CFA_LAYOUTS:
        raise ValueError(f"unknown CFA pattern {pattern!r}; known: {sorted(_CFA_LAYOUTS)}")
    tile = np.asarray(_CFA_LAYOUTS[pattern], dtype=np.int64)
    height, width = shape
    reps = (height + 1) // 2, (width + 1) // 2
    return np.tile(tile, reps)[:height, :width]


def mosaic(rgb_dn: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """Sample one CFA channel per pixel from a (H, W, 3) DN image."""
    if rgb_dn.ndim != 3 or rgb_dn.shape[2] != 3:
        raise ValueError(f"mosaic expects (H, W, 3), got {rgb_dn.shape}")
    channel = cfa_channel_map(spec.cfa_pattern, rgb_dn.shape[:2])
    v, u = np.meshgrid(
        np.arange(rgb_dn.shape[0]), np.arange(rgb_dn.shape[1]), indexing="ij"
    )
    return rgb_dn[v, u, channel]


def _conv3(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """3x3 zero-padded convolution via shifts (numpy only)."""
    padded = np.pad(image.astype(np.float64), 1, mode="constant")
    out = np.zeros_like(image, dtype=np.float64)
    for dr in range(3):
        for dc in range(3):
            weight = kernel[dr, dc]
            if weight != 0.0:
                out += weight * padded[dr : dr + image.shape[0], dc : dc + image.shape[1]]
    return out


_KERNEL_RB = np.array([[1.0, 2.0, 1.0], [2.0, 4.0, 2.0], [1.0, 2.0, 1.0]])
_KERNEL_G = np.array([[0.0, 1.0, 0.0], [1.0, 4.0, 1.0], [0.0, 1.0, 0.0]])


def demosaic(raw: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """Bilinear demosaic: normalized masked interpolation per channel.

    Convolving (raw * mask) and (mask) with the bilinear kernel and dividing
    yields exact bilinear weights everywhere, including image borders.
    """
    channel = cfa_channel_map(spec.cfa_pattern, raw.shape)
    raw = raw.astype(np.float64)
    out = np.empty((*raw.shape, 3), dtype=np.float64)
    for ch, kernel in ((0, _KERNEL_RB), (1, _KERNEL_G), (2, _KERNEL_RB)):
        mask = (channel == ch).astype(np.float64)
        num = _conv3(raw * mask, kernel)
        den = _conv3(mask, kernel)
        out[:, :, ch] = num / np.where(den > 0.0, den, 1.0)
    return out


def isp_to_y_u8(rgb_dn: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """Black-level subtract, normalize, CCM, gamma, luma weights, U8.

    Input is linear DN (float or the quantized uint16 image, promoted).
    """
    max_dn = float(2**spec.adc_bits - 1)
    signal = (rgb_dn.astype(np.float64) - spec.black_level_dn) / (max_dn - spec.black_level_dn)
    signal = np.clip(signal, 0.0, 1.0)
    ccm = np.asarray(spec.ccm, dtype=np.float64)
    # einsum, not @: Accelerate's batched matmul raises spurious FP-status
    # warnings on large (H, W, 3) stacks even for finite [0, 1] inputs.
    signal = np.clip(np.einsum("hwc,dc->hwd", signal, ccm), 0.0, 1.0)
    encoded = signal ** (1.0 / spec.gamma)
    weights = np.asarray(spec.y_weights, dtype=np.float64)
    y = np.clip(np.einsum("hwc,c->hw", encoded, weights), 0.0, 1.0)
    return np.rint(y * 255.0).astype(np.uint8)


def dn_to_y_u8(rgb_dn: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """The §5.1 tail: mosaic → demosaic → CCM/gamma/Y when enabled, else ISP only."""
    if spec.enable_mosaic:
        return isp_to_y_u8(demosaic(mosaic(rgb_dn, spec), spec), spec)
    return isp_to_y_u8(rgb_dn, spec)
