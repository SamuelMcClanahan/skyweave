"""S2 (full-chain determinism) and S3 (cross-camera noise independence)."""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model import SensorModelSpec, render_frame


def _radiance(shape=(48, 64)):
    rng = np.random.default_rng(42)
    return 0.35 + 0.1 * rng.random((*shape, 3))


def test_s2_full_chain_byte_identical(camera):
    spec = SensorModelSpec()
    radiance = _radiance()
    y1, ae1 = render_frame(radiance, camera, spec, dataset_seed=7, camera_id=0, frame_seq=3)
    y2, ae2 = render_frame(radiance, camera, spec, dataset_seed=7, camera_id=0, frame_seq=3)
    assert y1.dtype == np.uint8
    assert np.array_equal(y1, y2)
    assert y1.tobytes() == y2.tobytes()
    assert ae1.gain == ae2.gain


def test_s2_different_seed_differs(camera):
    spec = SensorModelSpec()
    radiance = _radiance()
    y1, _ = render_frame(radiance, camera, spec, dataset_seed=7, camera_id=0, frame_seq=3)
    y2, _ = render_frame(radiance, camera, spec, dataset_seed=8, camera_id=0, frame_seq=3)
    assert not np.array_equal(y1, y2)


def _noise_sample(camera, spec, camera_id, frame_seq, radiance):
    """Pure noise sample via dataset-seed pair difference.

    Subtracting two renders that differ ONLY in dataset seed cancels every
    shared deterministic component — signal, FPN, and the quantization
    phase of the shared signal field (which a naive noisy-minus-clean
    residual retains and which correlates across cameras even with
    perfectly independent streams).
    """
    y_a, _ = render_frame(radiance, camera, spec, 7, camera_id, frame_seq)
    y_b, _ = render_frame(radiance, camera, spec, 1007, camera_id, frame_seq)
    return y_a.astype(np.float64) - y_b.astype(np.float64)


def test_s3_cross_camera_noise_independent(camera):
    """Same radiance, same frame, different camera: noise correlation ~ 0.

    Negative control: the same (camera, frame) sample correlates with
    itself at 1.0, so a shared-stream bug cannot pass this test.
    """
    spec = SensorModelSpec(enable_ae=False)
    radiance = _radiance()
    r0 = _noise_sample(camera, spec, 0, 0, radiance)
    r1 = _noise_sample(camera, spec, 1, 0, radiance)
    corr = float(np.corrcoef(r0.ravel(), r1.ravel())[0, 1])
    assert abs(corr) < 0.05, f"cross-camera noise correlation {corr}"
    control = float(np.corrcoef(r0.ravel(), r0.ravel())[0, 1])
    assert control > 0.999


def test_s3_temporal_noise_independent_when_fpn_disabled(camera):
    """With fixed-pattern stages off, frames of one camera decorrelate too."""
    spec = SensorModelSpec(enable_ae=False, enable_dark=False, enable_prnu=False)
    radiance = _radiance()
    r0 = _noise_sample(camera, spec, 0, 0, radiance)
    r1 = _noise_sample(camera, spec, 0, 1, radiance)
    corr = float(np.corrcoef(r0.ravel(), r1.ravel())[0, 1])
    assert abs(corr) < 0.05, f"cross-frame noise correlation {corr}"


def test_s3_fpn_is_shared_across_frames_by_design(camera):
    """DSNU/PRNU are per-camera fixed patterns: frames of one camera stay
    positively correlated through them — physical, and required for a
    background model to have something realistic to learn.

    Uses noisy-minus-clean residuals deliberately: FPN must NOT cancel here.
    """
    spec = SensorModelSpec(enable_ae=False)
    clean = spec.model_copy(
        update={
            "enable_shot_noise": False,
            "enable_read_noise": False,
            "enable_dark": False,
            "enable_prnu": False,
        }
    )
    radiance = _radiance()
    ref, _ = render_frame(radiance, camera, clean, 7, 0, 0)
    r0 = render_frame(radiance, camera, spec, 7, 0, 0)[0].astype(np.float64) - ref
    r1 = render_frame(radiance, camera, spec, 7, 0, 1)[0].astype(np.float64) - ref
    corr = float(np.corrcoef(r0.ravel(), r1.ravel())[0, 1])
    assert corr > 0.1, f"expected FPN-driven cross-frame correlation, got {corr}"


def test_pipeline_consumes_margined_stage_a_renders(camera):
    """The stage A -> B hand-off: render_frame must accept a radiance grid
    carrying the warp margin and emit the nominal sensor grid. With the PSF
    off and no distortion the result is bit-identical to running the central
    crop through a margin-free chain."""
    spec_margin = SensorModelSpec(warp_margin_px=4, enable_psf=False)
    spec_plain = SensorModelSpec(warp_margin_px=0, enable_psf=False)
    rng = np.random.default_rng(21)
    margined = 0.3 + 0.1 * rng.random((48 + 8, 64 + 8, 3))
    y_margin, _ = render_frame(margined, camera, spec_margin, 7, 0, 0)
    y_plain, _ = render_frame(margined[4:-4, 4:-4], camera, spec_plain, 7, 0, 0)
    assert y_margin.shape == (48, 64)
    assert np.array_equal(y_margin, y_plain)


def test_pipeline_rejects_wrong_grid(camera):
    spec = SensorModelSpec(oversample=2)
    try:
        render_frame(np.zeros((48, 64)), camera, spec, 7, 0, 0)
    except ValueError as err:
        assert "oversample" in str(err)
    else:  # pragma: no cover
        raise AssertionError("wrong radiance grid must raise")


def test_ae_transient_dark_step_raises_gain(camera):
    """The §5.3 requirement: a brightness step produces a gain transient."""
    spec = SensorModelSpec(enable_ae=True)
    state = None
    gains = []
    for frame in range(10):
        level = 0.4 if frame < 3 else 0.02
        y, state = render_frame(
            np.full((48, 64, 3), level), camera, spec, 7, 0, frame, state
        )
        gains.append(state.gain)
    assert gains[-1] > gains[2] * 1.5, f"AE did not respond to dark step: {gains}"
