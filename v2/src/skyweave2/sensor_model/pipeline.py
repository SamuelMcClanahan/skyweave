"""The full stage-B chain, in exactly the SYNTHETIC_PIPELINE_DESIGN §5.1 order.

Seed policy (frozen, D3 brief):
- temporal noise: generator from ``(dataset_seed, camera_id, frame_seq)`` —
  independent across cameras and frames by construction;
- fixed-pattern noise (DSNU/PRNU): generator from ``(dataset_seed,
  camera_id)`` only, so the pattern is constant across frames for one
  camera and independent across cameras.

The AE controller is sequential state: process frames of one camera in
``frame_seq`` order, threading ``AeState`` through. Same manifest + seed +
frame order = byte-identical U8 output (test S2).

One deliberate approximation, recorded here: DSNU/PRNU are per-sensor-pixel
maps applied across the RGB planes BEFORE the mosaic (the frozen chain puts
the mosaic last). Because the maps are equal across channels, the CFA
sample at each site still carries exactly its own per-pixel pattern; only
the demosaic interpolation neighborhoods mix patterns, which is the same
mixing the real ISP performs.
"""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model import mosaic as mosaic_mod
from skyweave2.sensor_model import optics, radiometry
from skyweave2.sensor_model.config import SensorModelSpec, TruthCameraOptics
from skyweave2.sensor_model.readout import AeState, ae_update, electrons_to_dn


def frame_rng(dataset_seed: int, camera_id: int, frame_seq: int) -> np.random.Generator:
    """Temporal-noise generator for one (camera, frame)."""
    return np.random.default_rng(
        np.random.SeedSequence((dataset_seed, camera_id, frame_seq))
    )


def fpn_rng(dataset_seed: int, camera_id: int) -> tuple[np.random.Generator, np.random.Generator]:
    """(dsnu_rng, prnu_rng) for one camera; frame-independent by construction."""
    children = np.random.SeedSequence((dataset_seed, camera_id)).spawn(2)
    return np.random.default_rng(children[0]), np.random.default_rng(children[1])


def fpn_maps(
    shape: tuple[int, int], spec: SensorModelSpec, dataset_seed: int, camera_id: int
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic per-camera (dsnu, prnu) maps on the sensor grid."""
    dsnu_gen, prnu_gen = fpn_rng(dataset_seed, camera_id)
    return (
        radiometry.dsnu_map(shape, spec, dsnu_gen),
        radiometry.prnu_map(shape, spec, prnu_gen),
    )


def render_frame(
    radiance: np.ndarray,
    camera: TruthCameraOptics,
    spec: SensorModelSpec,
    dataset_seed: int,
    camera_id: int,
    frame_seq: int,
    ae_state: AeState | None = None,
) -> tuple[np.ndarray, AeState]:
    """Linear radiance in, U8 Y out, next AE state returned.

    ``radiance`` is (H*os, W*os) mono or (H*os, W*os, 3) RGB linear scene
    radiance from the renderer (os = ``spec.oversample``). Mono input is
    treated as colorless RGB so the mosaic stage still exercises its spatial
    resampling.
    """
    image = np.asarray(radiance, dtype=np.float64)
    if image.ndim == 2:
        image = np.repeat(image[:, :, None], 3, axis=2)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"radiance must be (H, W) or (H, W, 3), got {image.shape}")
    margin = spec.warp_margin_px
    expected = (
        (camera.height + 2 * margin) * spec.oversample,
        (camera.width + 2 * margin) * spec.oversample,
    )
    if image.shape[:2] != expected:
        raise ValueError(
            f"radiance grid {image.shape[:2]} != (sensor grid + 2*warp_margin_px)"
            f" x oversample {expected}"
        )

    if ae_state is None:
        ae_state = AeState.initial(spec)
    rng = frame_rng(dataset_seed, camera_id, frame_seq)

    # -- optics (§5.1 lines 1-4) ----------------------------------------
    # Vignetting and PSF run on the full margined grid; the warp consumes
    # the margin and always emits the nominal sensor grid (x oversample).
    if spec.enable_vignetting:
        image = optics.apply_vignetting(image, camera, spec)
    if spec.enable_psf:
        image = optics.apply_psf(image, spec)
    if spec.enable_distortion:
        image = optics.apply_distortion_warp(image, camera, spec)
    else:
        image = optics.crop_margin(image, spec)
    image = optics.decimate(image, spec)

    # -- radiometry (lines 5-9) -----------------------------------------
    electrons = radiometry.radiance_to_electrons(image, spec)
    electrons = radiometry.apply_shot_noise(electrons, spec, rng)
    dsnu, prnu = fpn_maps((camera.height, camera.width), spec, dataset_seed, camera_id)
    electrons = radiometry.apply_dark(electrons, spec, dsnu, rng)
    electrons = radiometry.apply_prnu(electrons, spec, prnu)
    electrons = radiometry.apply_full_well(electrons, spec)

    # -- readout (lines 10-12) ------------------------------------------
    gain = ae_state.gain if spec.enable_ae else spec.analog_gain
    rgb_dn = electrons_to_dn(electrons, gain, spec, rng)

    # -- mosaic / ISP (lines 13-14) -------------------------------------
    y_u8 = mosaic_mod.dn_to_y_u8(rgb_dn, spec)

    next_state = ae_update(ae_state, float(np.mean(y_u8)) / 255.0, spec)
    return y_u8, next_state
