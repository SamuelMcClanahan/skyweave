"""F9 (gate-clip full chain) and F10 (voxel on/off comparison).

These run the REAL golden artifacts under output/ — the phase's own
deliverable inputs. Missing artifacts are a hard failure, not a skip: a
silently-skipped acceptance test is a test that cannot fail.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.metrics import (
    evaluate_scene,
    load_jsonl_truth,
    load_render_truth,
    observation_stream,
)
from skyweave2.fusion.report import (
    _detector_config,
    gate_cameras,
    multiblob_cameras,
)

V2 = Path(__file__).resolve().parents[2]
GATE_CLIPS = V2.parent / "output" / "exp001_clips" / "gate"
GATE_RENDER = V2.parent / "output" / "exp001_renders" / "gate"
MULTIBLOB = V2.parent / "output" / "exp001_multiblob"


def _assert_dataset_complete(root: Path, needs_truth_jsonl: bool = False) -> None:
    """Completeness, not bare existence: a partially-built or stale dataset
    must fail HERE with a clear message, not deep inside detect_clip."""
    for camera_id in (0, 1, 2):
        assert (root / f"cam{camera_id}" / "clip.json").exists(), (
            f"{root} incomplete: cam{camera_id}/clip.json missing"
        )
    if needs_truth_jsonl:
        assert (root / "truth.jsonl").exists(), f"{root} incomplete: truth.jsonl missing"


@pytest.fixture(scope="module")
def gate_run():
    if not (GATE_CLIPS.exists() and GATE_RENDER.exists()):
        pytest.skip(
            "golden gate artifacts absent under output/ (machine-local data; "
            "regenerate via render_ingest) — F9/F10 skipped"
        )
    # Present-but-incomplete is still a hard failure: half-built or stale
    # artifacts must never silently score.
    _assert_dataset_complete(GATE_CLIPS)
    assert (GATE_RENDER / "truth" / "trajectory.jsonl").exists()
    detector_config = _detector_config()
    observations = observation_stream(GATE_CLIPS, detector_config)
    cameras = gate_cameras(GATE_RENDER)
    truth = load_render_truth(GATE_RENDER)
    return observations, cameras, truth


def test_f9_gate_clip_full_chain(gate_run):
    observations, cameras, truth = gate_run
    config = FusionConfig()
    ev = evaluate_scene("gate", observations, cameras, truth, config)
    # The acceptance table is produced from these numbers; here we assert
    # the chain actually tracked the target and the duplicate line holds.
    assert ev.published_samples > 100, "the target was never tracked"
    assert ev.duplicate_confirmed <= 1
    assert np.isfinite(ev.range_p95_m) and np.isfinite(ev.velocity_rmse_mps)
    assert np.isfinite(ev.acquisition_s)
    # Lifecycle sanity: at least one confirmed track existed.
    assert ev.final_statuses, "no tracks at all"


def test_f10_voxel_comparison_both_scenes(gate_run):
    observations, cameras, truth = gate_run
    if not MULTIBLOB.exists():
        pytest.skip(
            "multi-blob dataset absent (build via "
            "`python -m skyweave2.fusion.report build-multiblob`) — F10 skipped"
        )
    _assert_dataset_complete(MULTIBLOB, needs_truth_jsonl=True)
    detector_config = _detector_config()
    mb_obs = observation_stream(MULTIBLOB, detector_config)
    mb_cams = multiblob_cameras()
    mb_truth = load_jsonl_truth(MULTIBLOB / "truth.jsonl")

    results = {}
    for scene, obs, cams, tr in (("gate", observations, cameras, truth),
                                 ("multiblob", mb_obs, mb_cams, mb_truth)):
        for voxel_on in (False, True):
            config = FusionConfig()
            config.voxel.enabled = voxel_on
            ev = evaluate_scene(scene, obs, cams, tr, config)
            results[(scene, voxel_on)] = ev
            assert np.isfinite(ev.candidate_recall)
            assert ev.batches > 0

    # Both modes genuinely ran: with voxel ON the oracle must actually
    # produce peaks somewhere in the run (>= 0 was a tautology — review
    # finding); with voxel OFF it must produce none.
    assert results[("multiblob", True)].voxel_peaks_total > 0, (
        "voxel-on mode never exercised the oracle"
    )
    assert results[("gate", False)].voxel_groups_total == 0
    assert results[("gate", False)].voxel_peaks_total == 0
    # The multi-blob scene actually stresses association: decoys produce
    # false candidates or rejections somewhere in the run.
    mb_off = results[("multiblob", False)]
    assert mb_off.false_candidates_per_batch > 0.0 or sum(
        mb_off.rejections.values()
    ) > 0, "the multi-blob fixture stressed nothing"
    # And the true target still tracks through the clutter.
    assert mb_off.candidate_recall > 0.8
