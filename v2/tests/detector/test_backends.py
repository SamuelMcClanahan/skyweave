"""Backend behavior: fixed_bg, mog2, ive_approx (includes K9)."""

from __future__ import annotations

import numpy as np

from skyweave2.detector.config import Backend
from skyweave2.detector.runner import detect_clip, scorecard_for_detections
from skyweave2.eval.labels import TruthLabel
from tests.detector.conftest import moving_blob_clip


def test_ive_approx_learns_background_and_detects_change(base_config, tmp_path):
    clip_dir, truths = moving_blob_clip(tmp_path)
    config = base_config.model_copy(update={"backend": Backend.IVE_APPROX})
    observations, _ = detect_clip(clip_dir, config)
    assert observations, "ive_approx found nothing"
    errors = []
    for obs in observations:
        truth = truths.get(obs.envelope.frame_seq)
        if truth:
            errors.append(float(np.hypot(obs.u - truth[0], obs.v - truth[1])))
    assert errors and float(np.median(errors)) < 2.0


def test_ive_approx_is_deterministic(base_config, tmp_path):
    clip_dir, _ = moving_blob_clip(tmp_path)
    config = base_config.model_copy(update={"backend": Backend.IVE_APPROX})
    obs_a, _ = detect_clip(clip_dir, config)
    obs_b, _ = detect_clip(clip_dir, config)
    assert [o.model_dump() for o in obs_a] == [o.model_dump() for o in obs_b]


def test_ive_approx_knobs_change_behavior(base_config, tmp_path):
    """The IVE-shaped knobs must actually control the model: an extreme
    variance threshold suppresses detection."""
    clip_dir, _ = moving_blob_clip(tmp_path)
    loose = base_config.model_copy(update={"backend": Backend.IVE_APPROX})
    strict = loose.model_copy(
        update={"ive_approx": loose.ive_approx.model_copy(update={"var_init": 40000.0,
                                                                  "var_min": 40000.0})}
    )
    obs_loose, _ = detect_clip(clip_dir, loose)
    obs_strict, _ = detect_clip(clip_dir, strict)
    assert len(obs_loose) > 0
    assert len(obs_strict) < len(obs_loose)  # a 200-DN-sigma gate eats the blob


def test_k9_ive_approx_scorecard_within_band_of_mog2(base_config, tmp_path):
    """K9: comparable scorecards, never bit parity. Declared band: recall
    within 0.1, centroid err mean within 0.5 px, false/frame within 1.0."""
    clip_dir, truths = moving_blob_clip(tmp_path)
    labels = [TruthLabel(frame_seq=s, u=uv[0], v=uv[1]) for s, uv in truths.items()]
    cards = {}
    for backend in (Backend.MOG2, Backend.IVE_APPROX):
        config = base_config.model_copy(update={"backend": backend})
        _, results = detect_clip(clip_dir, config)
        cards[backend] = scorecard_for_detections(
            clip_dir, config, results, labels=labels, deterministic_perf=True
        )
    mog2_q = cards[Backend.MOG2]["quality"]
    ive_q = cards[Backend.IVE_APPROX]["quality"]
    assert mog2_q["recall"] > 0.9
    assert abs(mog2_q["recall"] - ive_q["recall"]) <= 0.1
    assert abs(mog2_q["centroid_err_px_mean"] - ive_q["centroid_err_px_mean"]) <= 0.5
    assert abs(mog2_q["false_per_frame"] - ive_q["false_per_frame"]) <= 1.0


def test_mog2_uses_config_knobs(base_config, tmp_path):
    """A sky-high varThreshold suppresses mog2 detections: knobs are wired."""
    clip_dir, _ = moving_blob_clip(tmp_path)
    strict = base_config.model_copy(
        update={"mog2": base_config.mog2.model_copy(update={"var_threshold": 5000.0})}
    )
    obs_default, _ = detect_clip(clip_dir, base_config)
    obs_strict, _ = detect_clip(clip_dir, strict)
    assert len(obs_default) > 0
    assert len(obs_strict) < len(obs_default)


def test_fixed_bg_requires_warmup(base_config, tmp_path):
    import pytest

    from skyweave2.detector.backends import FixedBgBackend

    config = base_config.model_copy(update={"backend": Backend.FIXED_BG})
    backend = FixedBgBackend(config)
    with pytest.raises(ValueError, match="warm-up"):
        backend.apply(np.zeros((8, 8), dtype=np.uint8), warming_up=False)
