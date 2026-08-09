"""Readout stages: AE gain controller, read noise, black level, ADC quantize.

The AE controller exists because the C1 injection path bypasses RKAIQ:
the detector never sees an exposure transient unless this model produces
one (design §5.3). It is a deliberately simple proportional controller in
log-gain space with a configurable convergence rate — the rate is a swept
parameter, not a claim about the real ISP.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave2.sensor_model.config import SensorModelSpec


@dataclass
class AeState:
    """Sequential AE controller state for one camera."""

    gain: float

    @classmethod
    def initial(cls, spec: SensorModelSpec) -> AeState:
        return cls(gain=spec.analog_gain)


def ae_update(state: AeState, mean_y_fraction: float, spec: SensorModelSpec) -> AeState:
    """One AE step: move log-gain toward the target mean luma.

    ``mean_y_fraction`` is the previous frame's mean output luma as a
    fraction of full scale. The controller is deterministic; determinism of
    the chain depends only on frames being processed in sequence order.
    """
    if not spec.enable_ae:
        return AeState(gain=spec.analog_gain)
    measured = max(mean_y_fraction, 1e-6)
    desired = state.gain * (spec.ae_target_y / measured)
    log_next = (1.0 - spec.ae_rate) * np.log(state.gain) + spec.ae_rate * np.log(desired)
    gain = float(np.exp(log_next))
    return AeState(gain=float(np.clip(gain, spec.ae_gain_min, spec.ae_gain_max)))


def electrons_to_dn(
    electrons: np.ndarray,
    gain: float,
    spec: SensorModelSpec,
    rng: np.random.Generator,
) -> np.ndarray:
    """Analog gain, read noise, black level, and 10-bit quantization.

    Read noise is Gaussian in electrons at the given analog gain
    (``read_noise_e`` plus a swept gain-dependent slope), converted to DN by
    the measured conversion gain.
    """
    dn_per_e = gain / spec.conversion_gain_e_per_dn
    dn = np.asarray(electrons, dtype=np.float64) * dn_per_e
    if spec.enable_read_noise:
        sigma_e = spec.read_noise_e + spec.read_noise_gain_slope * (gain - 1.0)
        dn = dn + rng.normal(0.0, max(sigma_e, 0.0) * dn_per_e, size=dn.shape)
    dn = dn + spec.black_level_dn
    max_dn = float(2**spec.adc_bits - 1)
    return np.clip(np.rint(dn), 0.0, max_dn).astype(np.uint16)
