"""S1 (readout stages): AE controller, read noise, black level, quantize."""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model import SensorModelSpec
from skyweave2.sensor_model.readout import AeState, ae_update, electrons_to_dn


def test_electrons_to_dn_hand_case():
    spec = SensorModelSpec(
        enable_read_noise=False, conversion_gain_e_per_dn=5.9, black_level_dn=64.0,
        adc_bits=10,
    )
    dn = electrons_to_dn(np.array([[590.0]]), 1.0, spec, np.random.default_rng(0))
    assert dn.dtype == np.uint16
    assert dn[0, 0] == 164  # 590 / 5.9 + 64


def test_quantize_clips_to_adc_range():
    spec = SensorModelSpec(enable_read_noise=False, black_level_dn=64.0, adc_bits=10)
    dn = electrons_to_dn(np.array([[1e9, 0.0]]), 4.0, spec, np.random.default_rng(0))
    assert dn[0, 0] == 1023
    assert dn[0, 1] == 64  # black level floor


def test_analog_gain_scales_signal():
    spec = SensorModelSpec(enable_read_noise=False, black_level_dn=0.0)
    e = np.array([[59.0]])
    dn1 = electrons_to_dn(e, 1.0, spec, np.random.default_rng(0))
    dn4 = electrons_to_dn(e, 4.0, spec, np.random.default_rng(0))
    assert dn4[0, 0] == 4 * dn1[0, 0]


def test_read_noise_statistics():
    spec = SensorModelSpec(
        enable_read_noise=True, read_noise_e=3.0, conversion_gain_e_per_dn=1.0,
        black_level_dn=512.0,
    )
    dn = electrons_to_dn(np.zeros((300, 300)), 1.0, spec, np.random.default_rng(5))
    assert abs(float(dn.mean()) - 512.0) < 0.1
    assert abs(float(dn.astype(np.float64).std()) - 3.0) < 0.15


def test_ae_full_rate_hand_case():
    """rate=1: gain jumps straight to gain * target/measured."""
    spec = SensorModelSpec(enable_ae=True, ae_rate=1.0, ae_target_y=0.45)
    state = ae_update(AeState(gain=1.0), mean_y_fraction=0.9, spec=spec)
    assert abs(state.gain - 0.5) < 1e-12


def test_ae_partial_rate_moves_in_log_space():
    spec = SensorModelSpec(enable_ae=True, ae_rate=0.5, ae_target_y=0.45)
    state = ae_update(AeState(gain=1.0), mean_y_fraction=0.9, spec=spec)
    assert abs(state.gain - np.exp(0.5 * np.log(0.5))) < 1e-12


def test_ae_clamps_to_bounds():
    spec = SensorModelSpec(enable_ae=True, ae_rate=1.0, ae_gain_min=0.5, ae_gain_max=4.0)
    dark = ae_update(AeState(gain=4.0), mean_y_fraction=1e-9, spec=spec)
    bright = ae_update(AeState(gain=0.5), mean_y_fraction=1.0, spec=spec)
    assert dark.gain == 4.0
    assert bright.gain == 0.5


def test_ae_disabled_returns_fixed_gain():
    spec = SensorModelSpec(enable_ae=False, analog_gain=2.5)
    state = ae_update(AeState(gain=7.0), mean_y_fraction=0.1, spec=spec)
    assert state.gain == 2.5
