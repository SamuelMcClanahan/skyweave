"""SensorModelSpec: every stage toggleable, every parameter a field.

Two rules from the design doc are enforced by construction here:

- No baked constants: everything that shapes the output is a field, so
  sweeps are config edits and the shipped ``sensor-model.json`` is the
  complete description of what was applied.
- Truth separation: `TruthCameraOptics` is the RENDERER-side truth for
  vignetting/distortion. It is a separate object from any estimator
  `CameraModel` and must never be built by reference from one.

Labels: conversion gain, read noise, and full well are Measured-anchored
parameters (bench task, parallel); the defaults below are Provisional
plausible values for an SC3336-class sensor and carry no Measured claim.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TruthCameraOptics(BaseModel):
    """Renderer-truth optics for one camera: what the glass actually does.

    The estimator's calibration is a separately serialized, separately
    perturbable object; this one feeds the sensor model only.
    """

    model_config = ConfigDict(extra="forbid")

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    # Pinhole intrinsics on the SENSOR grid (pixel-center convention, D0 §2).
    fx: float = Field(gt=0.0)
    fy: float = Field(gt=0.0)
    cx: float
    cy: float
    # Brown-Conrady truth distortion applied as the stage-B warp.
    dist: tuple[float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0)


class SensorModelSpec(BaseModel):
    """The full stage-B parameter set, in §5.1 chain order."""

    model_config = ConfigDict(extra="forbid")

    # -- optics ----------------------------------------------------------
    enable_vignetting: bool = True
    vignetting_cos4_strength: float = Field(default=1.0, ge=0.0, le=1.0)
    vignetting_extra: float = 0.0  # extra radial falloff term, swept

    enable_psf: bool = True
    psf_sigma_px: float = Field(default=0.7, gt=0.0)  # swept 0.4-1.5
    psf_kernel_radius_sigmas: float = Field(default=4.0, gt=0.0)

    enable_distortion: bool = True
    distortion_invert_iterations: int = Field(default=20, ge=1)

    # Warp margin (sensor pixels per side) present in the INPUT radiance:
    # stage A renders (W + 2m, H + 2m) so the Brown-Conrady warp can sample
    # real content beyond the nominal frame instead of edge-clamping.
    warp_margin_px: int = Field(default=0, ge=0)

    oversample: int = Field(default=1, ge=1)  # radiance grid = oversample x sensor grid

    # -- radiometry ------------------------------------------------------
    # radiance -> electrons scale: e- = radiance * exposure_s * electrons_per_
    # radiance_second (QE, aperture, and pixel area folded into one swept
    # scale; only conversion gain below needs a bench anchor).
    exposure_s: float = Field(default=0.003, gt=0.0)  # exp001 Provisional amendment
    # Default places unit-order radiance mid-range at 3 ms without clipping:
    # 0.4 radiance -> ~2700 e- of a 6000 e- well. Swept, no Measured claim.
    electrons_per_radiance_second: float = Field(default=2.25e6, gt=0.0)

    enable_shot_noise: bool = True

    enable_dark: bool = True
    dark_current_e_per_s: float = Field(default=15.0, ge=0.0)  # swept
    dsnu_fraction: float = Field(default=0.10, ge=0.0)  # per-pixel dark spread

    enable_prnu: bool = True
    prnu_fraction: float = Field(default=0.01, ge=0.0)  # swept

    full_well_e: float = Field(default=6000.0, gt=0.0)  # from PTC once measured

    # -- readout ---------------------------------------------------------
    enable_ae: bool = True
    ae_target_y: float = Field(default=0.40, gt=0.0, lt=1.0)  # of full scale
    ae_rate: float = Field(default=0.25, gt=0.0, le=1.0)  # convergence per frame, swept
    ae_gain_min: float = Field(default=0.125, gt=0.0)  # digital attenuation floor
    ae_gain_max: float = Field(default=16.0, gt=0.0)
    analog_gain: float = Field(default=1.0, gt=0.0)  # used when AE disabled

    enable_read_noise: bool = True
    read_noise_e: float = Field(default=2.5, gt=0.0)  # measured once, swept +-2x
    read_noise_gain_slope: float = 0.0  # extra e- of read noise per unit gain

    # Conversion gain is THE bench-measured anchor (D3 bench task 1). This
    # default is Provisional (datasheet-plausible); no Measured claim exists
    # until the photon-transfer result lands in D3_SENSOR_NOTES.md.
    conversion_gain_e_per_dn: float = Field(default=5.9, gt=0.0)
    black_level_dn: float = Field(default=64.0, ge=0.0)
    adc_bits: int = Field(default=10, ge=8, le=16)

    # -- mosaic / ISP ----------------------------------------------------
    enable_mosaic: bool = True
    cfa_pattern: str = "BGGR"  # Provisional: verify against the Luckfox driver
    ccm: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    gamma: float = Field(default=2.2, gt=0.0)
    # BT.601 luma weights; the board ISP's exact matrix is a bench question.
    y_weights: tuple[float, float, float] = (0.299, 0.587, 0.114)
