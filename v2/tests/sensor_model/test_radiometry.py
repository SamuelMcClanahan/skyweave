"""S1 (radiometry stages): hand-computed expected outputs."""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model import SensorModelSpec
from skyweave2.sensor_model.pipeline import fpn_maps, fpn_rng
from skyweave2.sensor_model.radiometry import (
    apply_dark,
    apply_full_well,
    apply_prnu,
    apply_shot_noise,
    dsnu_map,
    prnu_map,
    radiance_to_electrons,
)


def test_radiance_to_electrons_hand_case():
    spec = SensorModelSpec(exposure_s=0.003, electrons_per_radiance_second=2.25e6)
    electrons = radiance_to_electrons(np.array([[0.5]]), spec)
    assert abs(electrons[0, 0] - 3375.0) < 1e-9  # 0.5 * 0.003 * 2.25e6


def test_shot_noise_poisson_statistics():
    spec = SensorModelSpec(enable_shot_noise=True)
    rng = np.random.default_rng(11)
    mean_e = 400.0
    noisy = apply_shot_noise(np.full((200, 200), mean_e), spec, rng)
    assert abs(noisy.mean() - mean_e) < 1.0
    assert abs(noisy.var() - mean_e) < 15.0  # Poisson: var == mean


def test_shot_noise_disabled_is_identity():
    spec = SensorModelSpec(enable_shot_noise=False)
    e = np.array([[123.4]])
    assert np.array_equal(apply_shot_noise(e, spec, np.random.default_rng(0)), e)


def test_dark_hand_case_no_noise():
    spec = SensorModelSpec(
        enable_dark=True, enable_shot_noise=False, dark_current_e_per_s=100.0,
        exposure_s=0.003, dsnu_fraction=0.0,
    )
    dsnu = np.ones((2, 2))
    out = apply_dark(np.zeros((2, 2)), spec, dsnu, np.random.default_rng(0))
    assert np.allclose(out, 0.3)  # 100 e/s * 3 ms


def test_dsnu_prnu_patterns_are_per_camera_fixed_and_independent():
    spec = SensorModelSpec(dsnu_fraction=0.1, prnu_fraction=0.02)
    dsnu_a1, prnu_a1 = fpn_maps((32, 32), spec, dataset_seed=7, camera_id=0)
    dsnu_a2, prnu_a2 = fpn_maps((32, 32), spec, dataset_seed=7, camera_id=0)
    dsnu_b, _ = fpn_maps((32, 32), spec, dataset_seed=7, camera_id=1)
    assert np.array_equal(dsnu_a1, dsnu_a2)  # fixed pattern: identical every call
    assert np.array_equal(prnu_a1, prnu_a2)
    assert not np.array_equal(dsnu_a1, dsnu_b)  # different camera, different pattern
    corr = np.corrcoef((dsnu_a1 - 1).ravel(), (dsnu_b - 1).ravel())[0, 1]
    assert abs(corr) < 0.1
    # DSNU and PRNU of the same camera come from independent spawned streams.
    corr_dp = np.corrcoef((dsnu_a1 - 1).ravel(), (prnu_a1 - 1).ravel())[0, 1]
    assert abs(corr_dp) < 0.1


def test_dsnu_map_statistics():
    spec = SensorModelSpec(dsnu_fraction=0.1)
    gen, _ = fpn_rng(0, 0)
    pattern = dsnu_map((256, 256), spec, gen)
    assert abs(pattern.mean() - 1.0) < 0.01
    assert abs(pattern.std() - 0.1) < 0.01


def test_prnu_hand_case():
    spec = SensorModelSpec(enable_prnu=True)
    electrons = np.full((2, 2), 100.0)
    pattern = np.array([[1.0, 1.1], [0.9, 1.0]])
    out = apply_prnu(electrons, spec, pattern)
    assert np.allclose(out, [[100.0, 110.0], [90.0, 100.0]])


def test_prnu_map_uses_configured_fraction():
    spec = SensorModelSpec(prnu_fraction=0.02)
    _, gen = fpn_rng(0, 0)
    pattern = prnu_map((256, 256), spec, gen)
    assert abs(pattern.std() - 0.02) < 0.005


def test_full_well_clips():
    spec = SensorModelSpec(full_well_e=6000.0)
    out = apply_full_well(np.array([[100.0, 9000.0, -5.0]]), spec)
    assert np.array_equal(out, [[100.0, 6000.0, 0.0]])
