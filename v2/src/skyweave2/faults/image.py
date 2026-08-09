"""Tier I: faulted U8 clips regenerated from the kept radiance EXRs.

Uses the D3 sensor model's own switches (read noise, shot noise, PSF blur,
exposure scale) plus a mid-clip brightness step, so an image fault is
expressed exactly as the sensor model expresses it — no bespoke image math
that could drift from the shipping chain.

Tier I is expensive (it re-renders U8 from radiance), so the campaign runs
it on a bounded frame window rather than the full 450-frame clip; the
window is a runner argument, never a magnitude.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from skyweave2.sensor_model import SensorModelSpec, TruthCameraOptics, render_frame


@dataclass(frozen=True)
class ImageFault:
    read_noise_sigma_dn: float | None = None
    shot_noise: bool | None = None
    blur_px: float | None = None
    exposure_scale: float | None = None
    brightness_step_dn: float | None = None
    step_at_s: float = 7.5  # manifest comment: "step at t = 7.5 s (AE transient)"


def spec_for(fault: ImageFault, base: SensorModelSpec | None = None) -> SensorModelSpec:
    """Translate a Tier I fault into sensor-model switches.

    read_noise_sigma_dn is a DN-domain quantity; the sensor model's
    ``read_noise_e`` is in electrons, so it is converted with the spec's own
    conversion gain rather than assumed.
    """
    spec = base or SensorModelSpec(enable_ae=False)
    updates: dict = {}
    if fault.read_noise_sigma_dn is not None:
        if fault.read_noise_sigma_dn <= 0.0:
            updates["enable_read_noise"] = False
        else:
            updates["enable_read_noise"] = True
            updates["read_noise_e"] = (
                fault.read_noise_sigma_dn * spec.conversion_gain_e_per_dn
            )
    if fault.shot_noise is not None:
        updates["enable_shot_noise"] = bool(fault.shot_noise)
    if fault.blur_px is not None:
        if fault.blur_px <= 0.0:
            updates["enable_psf"] = False
        else:
            updates["enable_psf"] = True
            updates["psf_sigma_px"] = fault.blur_px
    if fault.exposure_scale is not None:
        updates["exposure_s"] = spec.exposure_s * fault.exposure_scale
    return spec.model_copy(update=updates)


def render_faulted_clip(
    radiance_dir: str | Path,
    optics: TruthCameraOptics,
    fault: ImageFault,
    dataset_seed: int,
    camera_id: int,
    frames: range,
    fps: float = 30.0,
    base_spec: SensorModelSpec | None = None,
) -> list[np.ndarray]:
    """Regenerate U8 frames from kept radiance under one image fault."""
    spec = spec_for(fault, base_spec)
    radiance_dir = Path(radiance_dir)
    step_frame = int(round(fault.step_at_s * fps))
    out: list[np.ndarray] = []
    ae_state = None
    for frame_seq in frames:
        path = radiance_dir / f"radiance_{frame_seq:06d}.npy"
        if not path.exists():
            raise FileNotFoundError(f"missing radiance for frame {frame_seq}: {path}")
        radiance = np.load(path).astype(np.float64)
        if fault.brightness_step_dn and frame_seq >= step_frame:
            # A DN-domain step expressed in radiance through the same scale
            # the sensor model uses, so the step lands where it would in a
            # real AE transient.
            scale = spec.conversion_gain_e_per_dn / (
                spec.exposure_s * spec.electrons_per_radiance_second
            )
            radiance = radiance + fault.brightness_step_dn * scale
        y_u8, ae_state = render_frame(radiance, optics, spec, dataset_seed,
                                      camera_id, frame_seq, ae_state=ae_state)
        out.append(y_u8)
    return out
