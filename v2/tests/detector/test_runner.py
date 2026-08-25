"""K1, K2, K3, K5, K6, K7, K8: runner pipeline and contract compliance."""

from __future__ import annotations

import numpy as np
import pytest

from skyweave2.contracts import full_to_proc, proc_to_full
from skyweave2.contracts.serialize import pack, unpack
from skyweave2.detector import components as components_mod
from skyweave2.detector.cap import DetectorStats
from skyweave2.detector.config import Backend
from skyweave2.detector.runner import detect_clip, run_detector, scorecard_for_detections
from skyweave2.eval.labels import TruthLabel
from tests.detector.conftest import FULL_H, FULL_W, moving_blob_clip


def test_k1_fixed_bg_finds_blob_and_scorecard_plumbing_works(tmp_path, base_config):
    """K1: the sanity backend detects the blob; D3 scorecard consumes it."""
    clip_dir, truths = moving_blob_clip(tmp_path)
    config = base_config.model_copy(update={"backend": Backend.FIXED_BG})
    observations, results = detect_clip(clip_dir, config)
    assert observations, "fixed_bg found nothing"

    labels = [
        TruthLabel(frame_seq=seq, u=uv[0], v=uv[1]) for seq, uv in truths.items()
    ]
    card = scorecard_for_detections(clip_dir, config, results, labels=labels,
                                    deterministic_perf=True)
    assert card["quality"]["recall"] > 0.9
    assert card["quality"]["centroid_err_px_mean"] < 1.0
    assert card["detector"].startswith("fixed_bg")


def test_k2_mog2_determinism(tmp_path, base_config):
    """K2: two runs produce byte-identical observation streams."""
    clip_dir, _ = moving_blob_clip(tmp_path)
    obs_a, _ = detect_clip(clip_dir, base_config)
    obs_b, _ = detect_clip(clip_dir, base_config)
    assert len(obs_a) > 0
    assert [o.model_dump() for o in obs_a] == [o.model_dump() for o in obs_b]
    assert obs_a[0].envelope.session_uuid == obs_b[0].envelope.session_uuid


def test_k3_resolution_sweep_maps_centroids_to_full_res(tmp_path, base_config):
    """K3: three proc resolutions -> three scorecards; centroids come back in
    full-resolution coordinates per the D0 half-pixel law (T2 cross-check on
    real detector output)."""
    clip_dir, truths = moving_blob_clip(tmp_path)
    sweep = ((FULL_W, FULL_H), (256, 144), (192, 108))
    labels = [TruthLabel(frame_seq=s, u=uv[0], v=uv[1]) for s, uv in truths.items()]
    cards = []
    for width, height in sweep:
        config = base_config.model_copy(update={"proc_width": width, "proc_height": height})
        observations, results = detect_clip(clip_dir, config)
        assert observations, f"no detections at {width}x{height}"
        for obs in observations:
            truth = truths.get(obs.envelope.frame_seq)
            if truth is None:
                continue
            err = float(np.hypot(obs.u - truth[0], obs.v - truth[1]))
            scale = FULL_W / width
            assert err < 1.0 * scale + 0.5, (
                f"{width}x{height}: full-res centroid err {err:.2f} px"
            )
            # Explicit T2 cross-check: round-trip through the scaling law.
            back = proc_to_full(full_to_proc(obs.u, scale), scale)
            assert abs(back - obs.u) < 1e-9
            assert obs.envelope.proc_width == width
        cards.append(
            scorecard_for_detections(clip_dir, config, results, labels=labels,
                                     deterministic_perf=True)
        )
    assert len(cards) == 3
    assert all(c["quality"]["recall"] > 0.8 for c in cards)


def test_k5_no_observations_during_warmup(tmp_path, base_config):
    """K5: blob present from frame 0, but nothing may be emitted before the
    warm-up schedule completes; measured warmup_frames is sane."""
    clip_dir, _ = moving_blob_clip(tmp_path, appear_at=0)
    observations, results = detect_clip(clip_dir, base_config)
    assert observations
    assert min(o.envelope.frame_seq for o in observations) >= base_config.warmup_frames
    card = scorecard_for_detections(clip_dir, base_config, results,
                                    deterministic_perf=True)
    assert card["health"]["warmup_frames"] >= base_config.warmup_frames
    assert card["health"]["warmup_frames"] < len(results)


def test_k6_persistence_suppresses_single_frame_flicker(tmp_path, base_config):
    """K6: a one-frame blob never becomes an observation; an N-frame one does."""
    from skyweave2.eval.clips import write_clip
    from tests.detector.conftest import flat_frame, gaussian_blob_frame

    frames = [flat_frame() for _ in range(40)]
    frames[20] = gaussian_blob_frame((100.0, 100.0))  # single-frame flicker
    for seq in range(25, 31):  # six-frame persistent target elsewhere
        frames[seq] = gaussian_blob_frame((250.0, 60.0))
    clip_dir = tmp_path / "clip"
    write_clip(frames, clip_dir, fps=30.0, source="flicker-fixture")

    observations, _ = detect_clip(clip_dir, base_config)
    flicker = [o for o in observations if abs(o.u - 100.0) < 20 and abs(o.v - 100.0) < 20]
    persistent = [o for o in observations if abs(o.u - 250.0) < 20]
    assert flicker == []
    assert persistent, "persistent target was suppressed"
    assert all(o.persistence_count >= base_config.persistence_frames for o in observations)


def test_k7_observation_validity_and_serialization(tmp_path, base_config):
    """K7: covariance floor > 0, proc resolution declared, msgpack round-trip."""
    clip_dir, _ = moving_blob_clip(tmp_path)
    observations, _ = detect_clip(clip_dir, base_config)
    obs = observations[0]
    assert obs.cov_uu == base_config.centroid_cov_floor_px2 > 0
    assert obs.cov_vv == base_config.centroid_cov_floor_px2 > 0
    assert obs.envelope.proc_width == base_config.proc_width
    assert obs.envelope.proc_height == base_config.proc_height
    assert obs.evidence_ref is None
    restored = unpack(pack(obs), type(obs))
    assert restored == obs


def test_k8_cv2_absent_refusal(monkeypatch, tmp_path, base_config):
    """K8: without cv2 the detector refuses; no silent fallback (F-D0-8)."""
    monkeypatch.setattr(components_mod, "cv2", None)
    clip_dir, _ = moving_blob_clip(tmp_path)
    with pytest.raises(RuntimeError, match="OpenCV"):
        run_detector(clip_dir, base_config)


def test_runner_records_each_frames_accepted_bbox_overlap_pairs(
    monkeypatch, tmp_path, base_config
):
    from skyweave2.detector import runner as runner_mod
    from skyweave2.eval.clips import write_clip

    class NestedMaskBackend:
        def apply(self, frame, warming_up):
            mask = np.zeros(frame.shape, dtype=bool)
            mask[1, 1:8] = True
            mask[7, 1:8] = True
            mask[2:7, 1] = True
            mask[2:7, 7] = True
            mask[4, 3] = True
            return mask

    monkeypatch.setattr(runner_mod, "make_backend", lambda _config: NestedMaskBackend())
    clip_dir = tmp_path / "overlap-clip"
    write_clip(
        [np.zeros((12, 12), dtype=np.uint8) for _ in range(3)],
        clip_dir,
        fps=30.0,
        source="overlap-counter-test",
    )
    config = base_config.model_copy(
        update={
            "proc_width": 12,
            "proc_height": 12,
            "warmup_frames": 0,
            "open_radius_px": 0,
            "min_area_px": 1,
            "persistence_frames": 1,
        }
    )
    stats = DetectorStats()

    results = run_detector(clip_dir, config, stats=stats)

    assert [result.overlapping_bbox_pairs for result in results] == [1, 1, 1]
    assert stats.frames_with_overlapping_bboxes == 3
    assert stats.overlapping_bbox_pairs == 3
    assert "overlapping_bbox_pairs" not in stats.as_dict()
    diagnostics = stats.as_dict(include_overlap_diagnostics=True)
    assert diagnostics["frames_with_overlapping_bboxes"] == 3
    assert diagnostics["overlapping_bbox_pairs"] == 3


def test_cli_writes_observation_stream(tmp_path, base_config, capsys):
    import json

    from skyweave2.detector.runner import main as detect_main

    clip_dir, _ = moving_blob_clip(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(base_config.model_dump_json())
    out = tmp_path / "obs.jsonl"
    detect_main([str(clip_dir), str(config_path), "--out", str(out)])
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows
    assert {"u", "v", "envelope", "persistence_count"} <= set(rows[0])
