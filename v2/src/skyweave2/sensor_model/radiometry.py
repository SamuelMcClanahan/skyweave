"""Radiometry stages: radiance to electrons, shot noise, dark/DSNU, PRNU, full well.

Everything works in float64 electron images on the sensor grid. Fixed-pattern
maps (DSNU, PRNU) are functions of a per-camera FPN generator only — they
must be constant across frames for one camera and independent across
cameras. Temporal noise (shot, dark shot) uses the per-frame generator.
"""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model.config import SensorModelSpec


def radiance_to_electrons(radiance: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """Mean signal electrons: radiance x exposure x folded QE/aperture/area scale."""
    return (
        np.asarray(radiance, dtype=np.float64)
        * spec.exposure_s
        * spec.electrons_per_radiance_second
    )


def apply_shot_noise(
    electrons: np.ndarray, spec: SensorModelSpec, rng: np.random.Generator
) -> np.ndarray:
    """Poisson shot noise on the mean signal electrons."""
    if not spec.enable_shot_noise:
        return electrons
    return rng.poisson(np.clip(electrons, 0.0, None)).astype(np.float64)


def dsnu_map(
    shape: tuple[int, int], spec: SensorModelSpec, fpn_rng: np.random.Generator
) -> np.ndarray:
    """Per-pixel dark-current spread factor (1 + N(0, dsnu_fraction)), floored at 0."""
    return np.clip(1.0 + fpn_rng.normal(0.0, spec.dsnu_fraction, size=shape), 0.0, None)


def prnu_map(
    shape: tuple[int, int], spec: SensorModelSpec, fpn_rng: np.random.Generator
) -> np.ndarray:
    """Per-pixel gain spread factor (1 + N(0, prnu_fraction)), floored at 0."""
    return np.clip(1.0 + fpn_rng.normal(0.0, spec.prnu_fraction, size=shape), 0.0, None)


def apply_dark(
    electrons: np.ndarray,
    spec: SensorModelSpec,
    dsnu: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Add dark-current electrons with the fixed DSNU pattern and dark shot noise."""
    if not spec.enable_dark:
        return electrons
    dark_mean = spec.dark_current_e_per_s * spec.exposure_s * dsnu
    if electrons.ndim == 3:
        dark_mean = dark_mean[:, :, None] * np.ones(electrons.shape[2])
    if spec.enable_shot_noise:
        dark = rng.poisson(dark_mean).astype(np.float64)
    else:
        dark = dark_mean
    return electrons + dark


def apply_prnu(electrons: np.ndarray, spec: SensorModelSpec, prnu: np.ndarray) -> np.ndarray:
    """Multiply by the fixed per-pixel response pattern."""
    if not spec.enable_prnu:
        return electrons
    if electrons.ndim == 3:
        return electrons * prnu[:, :, None]
    return electrons * prnu


def apply_full_well(electrons: np.ndarray, spec: SensorModelSpec) -> np.ndarray:
    """Saturate at the full-well capacity."""
    return np.clip(electrons, 0.0, spec.full_well_e)
