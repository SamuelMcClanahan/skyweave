from __future__ import annotations

import pytest

from skyweave2.sensor_model import SensorModelSpec, TruthCameraOptics


@pytest.fixture()
def camera() -> TruthCameraOptics:
    return TruthCameraOptics(
        width=64, height=48, fx=100.0, fy=100.0, cx=31.5, cy=23.5
    )


@pytest.fixture()
def clean_spec() -> SensorModelSpec:
    """Every stochastic stage off; deterministic pass-through physics."""
    return SensorModelSpec(
        enable_vignetting=False,
        enable_psf=False,
        enable_distortion=False,
        enable_shot_noise=False,
        enable_dark=False,
        enable_prnu=False,
        enable_read_noise=False,
        enable_ae=False,
        enable_mosaic=False,
    )
