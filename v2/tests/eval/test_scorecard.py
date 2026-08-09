"""S5 (known-fixture scorecard) and S6 (truth-free health metrics)."""

from __future__ import annotations

import json

import numpy as np

from skyweave2.eval.clips import write_clip
from skyweave2.eval.labels import TruthLabel, write_labels
from skyweave2.eval.scorecard import (
    ScorecardConfig,
    connected_components,
    score_clip,
)
from skyweave2.eval.scorecard import (
    main as scorecard_main,
)

BG = 20
CONFIG = ScorecardConfig(background_frames=5, diff_threshold_dn=15.0, min_area_px=2)


def _square(frame: np.ndarray, cu: int, cv: int, half: int, value: int) -> None:
    frame[cv - half : cv + half + 1, cu - half : cu + half + 1] = value


def _fixture_clip(tmp_path, n_frames=25, with_false_blob=True):
    """Flat background; moving 5x5 target after warmup; optional static false
    blob that appears only after warmup (so the background never learns it)."""
    frames = []
    labels = []
    for seq in range(n_frames):
        frame = np.full((64, 96), BG, dtype=np.uint8)
        if seq >= CONFIG.background_frames:
            cu, cv = 20 + (seq - CONFIG.background_frames), 30
            _square(frame, cu, cv, 2, 200)
            labels.append(TruthLabel(frame_seq=seq, u=float(cu), v=float(cv)))
            if with_false_blob:
                _square(frame, 80, 50, 1, 200)
        frames.append(frame)
    clip_dir = tmp_path / "clip"
    write_clip(frames, clip_dir, fps=30.0, source="fixture")
    return clip_dir, labels


def test_connected_components_hand_case():
    mask = np.zeros((6, 8), dtype=bool)
    mask[1:3, 1:3] = True  # 4-px component at centroid (1.5, 1.5)
    mask[4, 5:8] = True  # 3-px line at centroid (6.0, 4.0)
    mask[0, 7] = True  # 1-px speck, dropped by min_area
    comps = connected_components(mask, min_area_px=2)
    assert len(comps) == 2
    assert (comps[0].centroid_u, comps[0].centroid_v, comps[0].area_px) == (1.5, 1.5, 4)
    assert (comps[1].centroid_u, comps[1].centroid_v, comps[1].area_px) == (6.0, 4.0, 3)


def test_connected_components_merges_u_shape():
    """Two arms joined at the bottom must union into one component."""
    mask = np.zeros((5, 5), dtype=bool)
    mask[0:4, 0] = True
    mask[0:4, 2] = True
    mask[3, 1] = True
    comps = connected_components(mask, min_area_px=2)
    assert len(comps) == 1
    assert comps[0].area_px == 9


def test_s5_scorecard_reproduces_known_numbers(tmp_path):
    clip_dir, labels = _fixture_clip(tmp_path)
    card = score_clip(clip_dir, CONFIG, labels, deterministic_perf=True)
    quality = card["quality"]
    assert quality["eligible_frames"] == 20
    assert quality["recall"] == 1.0
    assert quality["centroid_err_px_mean"] < 0.05  # square centroid is exact
    assert abs(quality["false_per_frame"] - 1.0) < 1e-9  # exactly the static blob
    assert card["pass"]["recall"] is True
    assert card["pass"]["false_per_frame"] is True


def test_s5_recall_counts_misses(tmp_path):
    clip_dir, labels = _fixture_clip(tmp_path, with_false_blob=False)
    # Claim the target is also visible in two frames where nothing was drawn.
    phantom = [
        TruthLabel(frame_seq=2, u=50.0, v=50.0),
        TruthLabel(frame_seq=3, u=50.0, v=50.0),
    ]
    card = score_clip(clip_dir, CONFIG, labels + phantom, deterministic_perf=True)
    # Phantom frames are before warmup: not scored, so they must not be
    # counted eligible either.
    assert card["quality"]["eligible_frames"] == 20
    assert card["quality"]["recall"] == 1.0
    # Phantoms placed after warmup DO lower recall.
    phantom_late = [
        TruthLabel(frame_seq=23, u=5.0, v=5.0, object_id=9),
        TruthLabel(frame_seq=24, u=5.0, v=5.0, object_id=9),
    ]
    card = score_clip(clip_dir, CONFIG, labels + phantom_late, deterministic_perf=True)
    assert card["quality"]["recall"] == 1.0  # frames still have >=1 match
    assert card["quality"]["false_per_frame"] == 0.0


def test_s5_recall_counts_real_misses_and_fails_the_gate(tmp_path):
    """Target absent in 2 of 20 eligible frames: recall must be exactly 0.9
    and the recall gate (0.95) must FAIL — the pass section is observed
    both ways, not only at its trivial all-true value."""
    frames = []
    labels = []
    for seq in range(25):
        frame = np.full((64, 96), BG, dtype=np.uint8)
        if CONFIG.background_frames <= seq < 23:  # absent in frames 23, 24
            cu, cv = 20 + (seq - CONFIG.background_frames), 30
            _square(frame, cu, cv, 2, 200)
        if seq >= CONFIG.background_frames:  # labels claim all 20 frames
            labels.append(TruthLabel(frame_seq=seq, u=float(20 + (seq - 5)), v=30.0))
        frames.append(frame)
    clip_dir = tmp_path / "clip"
    write_clip(frames, clip_dir, fps=30.0, source="miss-fixture")

    card = score_clip(clip_dir, CONFIG, labels, deterministic_perf=True)
    assert card["quality"]["eligible_frames"] == 20
    assert card["quality"]["recall"] == 18 / 20
    assert card["pass"]["recall"] is False
    assert card["pass"]["overall"] is False


def test_s5_centroid_error_known_nonzero_value(tmp_path):
    """Labels offset by exactly 1 px from the drawn squares: mean and p95
    centroid error must both be 1.0 — a scorer hardcoding 0.0 fails here."""
    frames = []
    labels = []
    for seq in range(25):
        frame = np.full((64, 96), BG, dtype=np.uint8)
        if seq >= CONFIG.background_frames:
            cu, cv = 20 + (seq - CONFIG.background_frames), 30
            _square(frame, cu, cv, 2, 200)
            labels.append(TruthLabel(frame_seq=seq, u=float(cu + 1), v=float(cv)))
        frames.append(frame)
    clip_dir = tmp_path / "clip"
    write_clip(frames, clip_dir, fps=30.0, source="offset-fixture")

    card = score_clip(clip_dir, CONFIG, labels, deterministic_perf=True)
    assert abs(card["quality"]["centroid_err_px_mean"] - 1.0) < 1e-9
    assert abs(card["quality"]["centroid_err_px_p95"] - 1.0) < 1e-9
    assert card["quality"]["recall"] == 1.0  # 1 px is well inside match radius


def test_s5_fixture_occupancy_exceeds_gate_and_is_reported(tmp_path):
    """The standard fixture's occupancy (0.553%) sits above the 0.5% gate:
    the occupancy check and the overall aggregation must both report False."""
    clip_dir, labels = _fixture_clip(tmp_path)
    card = score_clip(clip_dir, CONFIG, labels, deterministic_perf=True)
    assert card["health"]["occupancy_pct"] > CONFIG.occupancy_max_pct
    assert card["pass"]["occupancy"] is False
    assert card["pass"]["overall"] is False
    # And a config with a looser gate flips it: thresholds live in config.
    loose = CONFIG.model_copy(update={"occupancy_max_pct": 1.0})
    card = score_clip(clip_dir, loose, labels, deterministic_perf=True)
    assert card["pass"]["occupancy"] is True
    assert card["pass"]["overall"] is True


def test_s6_warmup_and_component_count_hand_values(tmp_path):
    """Occupancy step fixture: 5 high-occupancy frames then steady state.

    Steady state = median of the last quarter (small blob, 9 px). The first
    scored frame within 20% of it is scored-index 5, so warmup_frames =
    5 + background_frames = 10. Exactly one component per scored frame."""
    frames = []
    for seq in range(25):
        frame = np.full((64, 96), BG, dtype=np.uint8)
        if CONFIG.background_frames <= seq < CONFIG.background_frames + 5:
            _square(frame, 30, 30, 5, 200)  # 11x11 = 121 px
        elif seq >= CONFIG.background_frames + 5:
            _square(frame, 30, 30, 1, 200)  # 3x3 = 9 px
        frames.append(frame)
    clip_dir = tmp_path / "clip"
    write_clip(frames, clip_dir, fps=30.0, source="warmup-fixture")

    card = score_clip(clip_dir, CONFIG, deterministic_perf=True)
    assert card["health"]["warmup_frames"] == 10
    assert card["health"]["component_count"] == 1.0
    expected_steady_pct = 9.0 / (64.0 * 96.0) * 100.0
    assert abs(card["health"]["steady_state_occupancy_pct"] - expected_steady_pct) < 1e-9


def test_s6_health_runs_without_truth(tmp_path):
    clip_dir, _ = _fixture_clip(tmp_path)
    card = score_clip(clip_dir, CONFIG, labels=None, deterministic_perf=True)
    assert "quality" not in card
    health = card["health"]
    assert 0.0 < health["occupancy_pct"] < 5.0
    assert health["component_count"] >= 1.0
    assert isinstance(health["warmup_frames"], int)
    assert set(card["pass"]) == {"occupancy", "overall"}


def test_s6_occupancy_hand_value(tmp_path):
    """25-px target + 9-px false blob on a 64x96 frame = 34/6144 px."""
    clip_dir, _ = _fixture_clip(tmp_path)
    card = score_clip(clip_dir, CONFIG, deterministic_perf=True)
    expected_pct = 34.0 / (64.0 * 96.0) * 100.0
    assert abs(card["health"]["occupancy_pct"] - expected_pct) < 0.02


def test_scorecard_cli_writes_json(tmp_path):
    clip_dir, labels = _fixture_clip(tmp_path)
    labels_path = tmp_path / "labels.jsonl"
    write_labels(labels, labels_path)
    out = tmp_path / "scorecard.json"
    scorecard_main(
        [str(clip_dir), "--labels", str(labels_path), "--out", str(out),
         "--deterministic-perf"]
    )
    card = json.loads(out.read_text())
    # Default config differs from the fixture's tuned config; the CLI ran the
    # default. Only structural assertions here.
    assert card["schema"] == "skyweave2-scorecard/1"
    assert card["perf"]["deterministic"] is True
    assert "pass" in card and "overall" in card["pass"]


def test_scorecard_deterministic_json(tmp_path):
    clip_dir, labels = _fixture_clip(tmp_path)
    a = score_clip(clip_dir, CONFIG, labels, deterministic_perf=True)
    b = score_clip(clip_dir, CONFIG, labels, deterministic_perf=True)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
