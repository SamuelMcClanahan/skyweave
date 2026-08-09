"""Regression tests for the D4 adversarial-review findings."""

from __future__ import annotations

import json

import numpy as np

from skyweave2.contracts import proc_to_full
from skyweave2.detector.components import MaskComponent
from skyweave2.detector.config import DetectorConfig
from skyweave2.detector.persistence import PersistenceFilter
from skyweave2.detector.runner import detect_clip, scorecard_for_detections
from skyweave2.eval.labels import TruthLabel, write_labels
from skyweave2.render_ingest import ingest_render
from skyweave2.sensor_model import SensorModelSpec, TruthCameraOptics, render_frame
from tests.detector.conftest import FULL_H, FULL_W, moving_blob_clip


def _component(u: float, v: float) -> MaskComponent:
    return MaskComponent(centroid_u=u, centroid_v=v, area_px=5,
                         bbox_x=int(u) - 1, bbox_y=int(v) - 1, bbox_w=3, bbox_h=3)


def test_flicker_cannot_steal_an_adjacent_track():
    """Review finding: raster-order greedy let a one-frame flicker inherit a
    persistent track's count. Nearest-pair-first assignment must give the
    track to the true object (1 px away), never the flicker (10 px away)."""
    config = DetectorConfig(persistence_frames=2, persistence_gate_px=12.0)
    filt = PersistenceFilter(config)
    assert filt.update([_component(0.0, 0.0)]) == []  # count 1, not emitted
    # Flicker sorts first (lower v) but is farther from the track.
    emitted = filt.update([_component(10.0, 0.0), _component(0.0, 1.0)])
    assert len(emitted) == 1
    comp, count, blob_id = emitted[0]
    assert (comp.centroid_u, comp.centroid_v) == (0.0, 1.0)  # the TRUE object
    assert count == 2 and blob_id == 0
    # Next frame the flicker is gone; the true object keeps its identity.
    emitted = filt.update([_component(0.0, 2.0)])
    assert [(c.centroid_v, n, b) for c, n, b in emitted] == [(2.0, 3, 0)]


def test_scaling_law_applied_exactly(monkeypatch, tmp_path, base_config):
    """Review finding: the old K3 tolerance swallowed the half-pixel term.
    Pin the law itself: a synthetic component at exact proc coordinates must
    emit at exactly (u_proc + 0.5) * s - 0.5 (hand-computed), not u_proc * s."""
    from skyweave2.detector import runner as runner_mod

    fixed = [_component(10.0, 20.0)]

    def fake_find_components(mask, min_area_px, max_area_px):
        return list(fixed)

    monkeypatch.setattr(runner_mod, "find_components", fake_find_components)
    clip_dir, _ = moving_blob_clip(tmp_path)
    config = base_config.model_copy(
        update={"proc_width": 192, "proc_height": 108, "persistence_frames": 1}
    )
    observations, _ = detect_clip(clip_dir, config)
    scale_x, scale_y = FULL_W / 192, FULL_H / 108
    expected_u = (10.0 + 0.5) * scale_x - 0.5  # hand-written D0 law
    expected_v = (20.0 + 0.5) * scale_y - 0.5
    assert abs(observations[0].u - expected_u) < 1e-12
    assert abs(observations[0].v - expected_v) < 1e-12
    assert observations[0].u != 10.0 * scale_x  # the mutation the review ran
    assert proc_to_full(10.0, scale_x) == expected_u


def test_scorecard_filters_labels_by_camera(tmp_path, base_config):
    """Review finding: multi-camera labels scored against one camera's clip
    inflated the recall denominator. camera_id filtering must fix it."""
    clip_dir, truths = moving_blob_clip(tmp_path)  # cam0 truth in frames 15+
    labels = [TruthLabel(frame_seq=s, camera_id=0, u=uv[0], v=uv[1])
              for s, uv in truths.items()]
    # Other cameras see the target in frames where cam0 does NOT (11-14):
    # unfiltered scoring counts those frames eligible and unmatched,
    # deflating recall — the reviewer's denominator-inflation scenario.
    phantom = [TruthLabel(frame_seq=s, camera_id=c, u=10.0, v=10.0)
               for s in (11, 12, 13, 14) for c in (1, 2)]
    _, results = detect_clip(clip_dir, base_config)
    unfiltered = scorecard_for_detections(
        clip_dir, base_config, results, labels=labels + phantom,
        deterministic_perf=True)
    filtered = scorecard_for_detections(
        clip_dir, base_config, results, labels=labels + phantom,
        deterministic_perf=True, camera_id=0)
    own_only = scorecard_for_detections(
        clip_dir, base_config, results, labels=labels,
        deterministic_perf=True)
    assert filtered["quality"] == own_only["quality"]
    assert unfiltered["quality"]["eligible_frames"] > own_only["quality"]["eligible_frames"]
    assert unfiltered["quality"]["recall"] < own_only["quality"]["recall"]


def test_negative_clip_recall_gate_not_applied(tmp_path, base_config):
    """Review finding: a target-free clip always failed overall because the
    recall gate compared NaN. With zero eligible frames the gate is N/A."""
    from skyweave2.eval.clips import write_clip
    from tests.detector.conftest import flat_frame

    clip_dir = tmp_path / "neg"
    write_clip([flat_frame() for _ in range(30)], clip_dir, fps=30.0, source="neg")
    _, results = detect_clip(clip_dir, base_config)
    card = scorecard_for_detections(clip_dir, base_config, results, labels=[],
                                    deterministic_perf=True)
    assert card["quality"]["eligible_frames"] == 0
    assert "recall" not in card["pass"]
    assert card["pass"]["overall"] is True  # clean negative clip passes


def test_dataset_id_distinguishes_gate_and_negative():
    from skyweave2.dataset import dataset_id

    manifest = {"a": 1}
    gate = dataset_id(manifest, "b", "s", 7, "rev", variant="gate")
    negative = dataset_id(manifest, "b", "s", 7, "rev", variant="negative")
    assert gate != negative


def test_k4_jitter_is_error_by_construction(tmp_path):
    """Review finding: recording jittered centers as truth cancels the
    injected jitter. The builder must record the NOMINAL path as truth."""
    from skyweave2.detector.repeatability import build_noise_fixture_clip

    kwargs = dict(width=192, height=108, truth_uv=(40.0, 40.0), fwhm_px=5.0,
                  n_frames=20, dataset_seed=3, appear_after=5)
    quiet = SensorModelSpec(enable_shot_noise=False, enable_dark=False,
                            enable_prnu=False, enable_read_noise=False,
                            enable_ae=False)
    truths_no_jitter = build_noise_fixture_clip(tmp_path / "a", spec=quiet,
                                                jitter_sigma_px=0.0, **kwargs)
    truths_jitter = build_noise_fixture_clip(tmp_path / "b", spec=quiet,
                                             jitter_sigma_px=1.0, **kwargs)
    # Truth is the nominal path either way...
    assert truths_no_jitter == truths_jitter
    # ...while the rendered frames differ (the jitter went into the pixels).
    fa = np.load(tmp_path / "a" / "frame_000010.npy")
    fb = np.load(tmp_path / "b" / "frame_000010.npy")
    assert not np.array_equal(fa, fb)


def _mini_render_dir(tmp_path, negative: bool = False):
    """Fabricate an exp001-shaped render dir: margined radiance npy + sidecars."""
    width, height, margin = 96, 64, 4
    render_dir = tmp_path / "render"
    truth_dir = render_dir / "truth"
    truth_dir.mkdir(parents=True)
    record = {
        "camera_id": 0, "nominal_width": width, "nominal_height": height,
        "margin_px": margin, "fx": 200.0, "fy": 200.0,
        "cx": (width - 1) / 2, "cy": (height - 1) / 2,
        "render_width": width + 2 * margin, "render_height": height + 2 * margin,
        "t_world_cam": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    }
    (truth_dir / "cameras.json").write_text(json.dumps([record]))
    (render_dir / "dataset.json").write_text(json.dumps(
        {"dataset_id": "test-dataset", "seed": 7, "negative": negative}))
    cam_dir = render_dir / "cam0"
    cam_dir.mkdir()
    labels = []
    v_grid, u_grid = np.meshgrid(
        np.arange(height + 2 * margin, dtype=np.float64),
        np.arange(width + 2 * margin, dtype=np.float64), indexing="ij")
    for seq in range(6):
        radiance = np.full((height + 2 * margin, width + 2 * margin), 0.4)
        if not negative and seq >= 2:
            cu = 30.0 + 3.0 * seq + margin  # margined-grid coords
            cv = 32.0 + margin
            radiance = radiance + 1.2 * np.exp(
                -0.5 * (((u_grid - cu) / 2.0) ** 2 + ((v_grid - cv) / 2.0) ** 2))
            labels.append(TruthLabel(frame_seq=seq, camera_id=0,
                                     u=cu - margin, v=cv - margin))
        np.save(cam_dir / f"radiance_{seq:06d}.npy",
                radiance.astype(np.float16))
    write_labels(labels + [TruthLabel(frame_seq=0, camera_id=1, u=1.0, v=1.0)],
                 truth_dir / "labels.jsonl")
    return render_dir


def test_ingest_render_produces_scoreable_clips(tmp_path):
    """Review finding (top): no radiance-to-clip path existed. The ingest
    module must turn a render dir into clips + per-camera labels, consuming
    the warp margin, and the output must be deterministic."""
    render_dir = _mini_render_dir(tmp_path)
    produced = ingest_render(render_dir, tmp_path / "clips")
    assert produced["clips"]["0"]["frames"] == 6
    assert produced["clips"]["0"]["labels"] == 4  # cam1 label filtered out
    clip_json = json.loads((tmp_path / "clips" / "cam0" / "clip.json").read_text())
    assert clip_json["width"] == 96 and clip_json["height"] == 64  # margin consumed
    assert "test-dataset" in clip_json["source"]

    ingest_render(render_dir, tmp_path / "clips_b")
    fa = np.load(tmp_path / "clips" / "cam0" / "frame_000003.npy")
    fb = np.load(tmp_path / "clips_b" / "cam0" / "frame_000003.npy")
    assert np.array_equal(fa, fb)

    # The margined radiance actually went through the sensor model with the
    # margin consumed: an independent render_frame call must agree.
    spec = SensorModelSpec(warp_margin_px=4)
    optics = TruthCameraOptics(width=96, height=64, fx=200.0, fy=200.0,
                               cx=47.5, cy=31.5)
    radiance = np.load(render_dir / "cam0" / "radiance_000000.npy").astype(np.float64)
    y_expected, _ = render_frame(radiance, optics, spec, 7, 0, 0)
    assert np.array_equal(np.load(tmp_path / "clips" / "cam0" / "frame_000000.npy"),
                          y_expected)
