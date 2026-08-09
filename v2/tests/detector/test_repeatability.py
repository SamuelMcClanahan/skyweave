"""K4: the repeatability harness recovers a known injected sigma."""

from __future__ import annotations

import numpy as np

from skyweave2.detector.config import Backend, DetectorConfig
from skyweave2.detector.repeatability import (
    build_noise_fixture_clip,
    measure_repeatability,
)
from skyweave2.sensor_model import SensorModelSpec


def _quiet_spec() -> SensorModelSpec:
    """Sensor chain with the noise stages nearly silent so the injected
    jitter dominates the measured sigma."""
    return SensorModelSpec(
        enable_shot_noise=False,
        enable_dark=False,
        enable_prnu=False,
        read_noise_e=0.5,
        enable_ae=False,
        psf_sigma_px=0.6,
    )


def _config(width: int, height: int) -> DetectorConfig:
    return DetectorConfig(
        backend=Backend.MOG2,
        proc_width=width,
        proc_height=height,
        warmup_frames=12,
        persistence_frames=1,  # repeatability wants every frame's centroid
        min_area_px=2,
    )


def test_k4_recovers_injected_sigma(tmp_path):
    width, height = 192, 108
    injected = 0.6  # px, per axis
    truths = build_noise_fixture_clip(
        tmp_path / "clip", width=width, height=height,
        truth_uv=(40.3, 40.7), fwhm_px=5.0, n_frames=90,
        dataset_seed=7, spec=_quiet_spec(), jitter_sigma_px=injected,
        appear_after=12,
    )
    result = measure_repeatability(tmp_path / "clip", _config(width, height), truths)
    assert result.matched_fraction > 0.9
    # Measured sigma = sqrt(injected^2 + detector floor^2); with the quiet
    # chain the floor is small, so recovery within ~35% brackets the truth.
    assert 0.65 * injected < result.sigma_px < 1.6 * injected, (
        f"sigma {result.sigma_px:.3f} vs injected {injected}"
    )


def test_k4_zero_jitter_floor_is_small(tmp_path):
    """No injected jitter: what remains is the detector+noise floor, and it
    must sit well under the injected case above."""
    width, height = 192, 108
    truths = build_noise_fixture_clip(
        tmp_path / "clip", width=width, height=height,
        truth_uv=(40.3, 40.7), fwhm_px=5.0, n_frames=90,
        dataset_seed=7, spec=_quiet_spec(), jitter_sigma_px=0.0,
        appear_after=12,
    )
    result = measure_repeatability(tmp_path / "clip", _config(width, height), truths)
    assert result.matched_fraction > 0.9
    # The drift axis (u) carries real motion artifacts — mask-edge phase
    # wobble and MOG2 trailing ghosts — so the floor lives mostly in u.
    # The cross axis is the clean noise floor and must stay small.
    assert result.sigma_v_px < 0.3
    assert result.sigma_px < 0.7
    assert abs(result.bias_v_px) < 0.3


def test_repeatability_is_deterministic(tmp_path):
    width, height = 192, 108
    truths = build_noise_fixture_clip(
        tmp_path / "a", width=width, height=height, truth_uv=(40.0, 40.0),
        fwhm_px=5.0, n_frames=40, dataset_seed=3, spec=_quiet_spec(), appear_after=12,
    )
    truths_b = build_noise_fixture_clip(
        tmp_path / "b", width=width, height=height, truth_uv=(40.0, 40.0),
        fwhm_px=5.0, n_frames=40, dataset_seed=3, spec=_quiet_spec(), appear_after=12,
    )
    assert truths == truths_b
    r_a = measure_repeatability(tmp_path / "a", _config(width, height), truths)
    r_b = measure_repeatability(tmp_path / "b", _config(width, height), truths_b)
    assert r_a == r_b
    fa = np.load(tmp_path / "a" / "frame_000020.npy")
    fb = np.load(tmp_path / "b" / "frame_000020.npy")
    assert np.array_equal(fa, fb)
