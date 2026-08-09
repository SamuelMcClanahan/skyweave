"""Full-chain evaluation: detector -> fusion -> tracks, scored against truth.

Produces the manifest acceptance statistics (range/cross-range p95,
velocity RMSE, acquisition time, duplicate confirmed tracks), track-filter
covariance coverage (with the D1 F-distribution caveat at 3 cameras),
candidate/voxel statistics for the F10 comparison, and the lifecycle and
rejection audit numbers.

Range/cross-range follow the D1 budget convention: line of sight is +y
(north), cross-range is x/z.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from skyweave2.contracts import CameraModel
from skyweave2.detector.config import DetectorConfig
from skyweave2.detector.runner import detect_clip
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import RunResult, run_stream
from skyweave2.geometry import GeometryConfig

_CHI2_3DOF = (3.5267, 8.0249, 14.1564)


@dataclass
class Truth:
    """Per-frame truth positions (world m) and the shared-FOV entry time."""

    positions: dict[int, np.ndarray]  # frame -> (3,)
    velocity: np.ndarray  # constant-velocity truth (3,)
    entry_s: float
    fps: float

    def at_batch(self, batch_index: int) -> np.ndarray | None:
        return self.positions.get(batch_index)


def load_render_truth(render_dir: str | Path) -> Truth:
    """Truth from an exp001 render's sidecars."""
    render_dir = Path(render_dir)
    rows = [json.loads(line) for line in
            open(render_dir / "truth" / "trajectory.jsonl", encoding="utf-8")]
    dataset = json.loads((render_dir / "dataset.json").read_text())
    positions = {}
    for row in rows:
        visible = any(p.get("visible") for p in row["projections"].values())
        if visible:
            positions[row["frame_seq"]] = np.asarray(row["target_world_m"])
    frames = sorted(positions)
    velocity = np.zeros(3)
    if len(frames) >= 2:
        dt = (frames[-1] - frames[0]) / 30.0
        velocity = (positions[frames[-1]] - positions[frames[0]]) / dt
    entry_s = float(
        dataset.get("provisional_scene_choices", {}).get("shared_fov_entry_s")
        or frames[0] / 30.0
    )
    return Truth(positions=positions, velocity=velocity, entry_s=entry_s, fps=30.0)


def load_jsonl_truth(path: str | Path, fps: float = 30.0) -> Truth:
    """Truth from a fixture-scene truth.jsonl (frame_seq + target_world_m)."""
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    positions = {
        r["frame_seq"]: np.asarray(r["target_world_m"])
        for r in rows if r.get("visible", True) and r.get("target_world_m") is not None
    }
    frames = sorted(positions)
    velocity = np.zeros(3)
    if len(frames) >= 2:
        dt = (frames[-1] - frames[0]) / fps
        velocity = (positions[frames[-1]] - positions[frames[0]]) / dt
    return Truth(positions=positions, velocity=velocity,
                 entry_s=frames[0] / fps if frames else 0.0, fps=fps)


def split_channels(
    samples: list[tuple[int, np.ndarray]], window_batches: int
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Two-channel split of ordered (batch_index, error_vec) samples.

    The SYSTEMATIC channel is the slowly-varying target-reference bias:
    a per-axis centered windowed mean over +-window_batches/2 EVENT-TIME
    batch indices (index-aware, so observation gaps do not skew the
    window). The RANDOM channel is the residual about that estimate.
    Deterministic and exact; returns (bias_series, residual_series)
    aligned with the input samples.
    """
    if not samples:
        return [], []
    half = max(window_batches // 2, 1)
    indices = np.array([s[0] for s in samples])
    errors = np.stack([np.asarray(s[1], dtype=np.float64) for s in samples])
    bias_series: list[np.ndarray] = []
    for index in indices:
        mask = np.abs(indices - index) <= half
        bias_series.append(errors[mask].mean(axis=0))
    residuals = [e - b for e, b in zip(errors, bias_series, strict=True)]
    return bias_series, residuals


@dataclass
class SceneEval:
    scene: str
    voxel_enabled: bool
    # Acceptance-table statistics (gate scene). TOTAL error and the
    # two-channel split (D5 amendment): cross-range p95 and velocity RMSE
    # gate on the RANDOM channel; the estimated target-reference bias has
    # its own gate. Totals are always reported beside — nothing hidden.
    range_p95_m: float = float("nan")
    cross_p95_m: float = float("nan")
    cross_p95_random_m: float = float("nan")
    velocity_rmse_mps: float = float("nan")
    velocity_rmse_random_mps: float = float("nan")
    # D5.2 amendment: velocity is unobservable at single-batch birth, so the
    # gate scores the SETTLED regime (after the manifest's convergence
    # window) and a separate gate bounds settling time. The transient is
    # always reported beside them.
    velocity_rmse_settled_mps: float = float("nan")
    velocity_rmse_transient_mps: float = float("nan")
    velocity_settling_s: float = float("nan")  # nan = never settled
    velocity_settled_samples: int = 0
    velocity_convergence_window_batches: int = 0
    bias_mean_m: float = float("nan")  # mean |windowed bias| over the run
    bias_max_m: float = float("nan")
    bias_axes_m: tuple[float, float, float] = (float("nan"),) * 3
    bias_window_batches: int = 0
    acquisition_s: float = float("nan")
    duplicate_confirmed: int = 0
    published_samples: int = 0
    coverage: tuple[float, float, float] = (float("nan"),) * 3
    # Candidate statistics (F10 comparison).
    batches: int = 0
    candidate_recall: float = float("nan")  # batches with a truth-matching result
    false_candidates_per_batch: float = float("nan")
    voxel_groups_total: int = 0
    voxel_peaks_total: int = 0
    fusion_runtime_s: float = 0.0
    # Lifecycle / rejection audit.
    final_statuses: dict[str, int] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    nis_rejects: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    audit_samples: list[str] = field(default_factory=list)


def observation_stream(
    clips_root: str | Path, detector_config: DetectorConfig
) -> list:
    """Detector over cam0/1/2 clips; one arrival-ordered stream."""
    observations = []
    for camera_id in (0, 1, 2):
        clip_dir = Path(clips_root) / f"cam{camera_id}"
        obs, _ = detect_clip(clip_dir, detector_config, camera_id=camera_id)
        observations.extend(obs)
    observations.sort(
        key=lambda o: (o.envelope.capture_ts_ns, o.envelope.camera_id, o.obs_id)
    )
    return observations


def evaluate_scene(
    scene: str,
    observations: list,
    cameras: dict[int, CameraModel],
    truth: Truth,
    config: FusionConfig,
    truth_match_m: float = 10.0,
    bias_window_batches: int = 31,
    velocity_convergence_window_batches: int = 0,
    velocity_gate_mps: float = float("inf"),
) -> SceneEval:
    """Score one scene. ``velocity_convergence_window_batches`` and
    ``velocity_gate_mps`` come from the MANIFEST (never defaults in code):
    the first declares the post-confirmation window excluded from the
    settled RMSE, the second is the threshold the settling-time search uses
    to decide when velocity error has settled."""
    start = time.perf_counter()
    run: RunResult = run_stream(
        observations, cameras, config,
        session_uuid=f"d5-{scene}-voxel{'on' if config.voxel.enabled else 'off'}",
        geometry_config=GeometryConfig(),
    )
    runtime = time.perf_counter() - start
    out = SceneEval(scene=scene, voxel_enabled=config.voxel.enabled)
    out.fusion_runtime_s = runtime
    out.batches = len(run.batches)
    out.voxel_groups_total = sum(b.voxel_groups for b in run.batches)
    out.voxel_peaks_total = sum(b.voxel_peaks for b in run.batches)

    # Candidate statistics against truth.
    truth_batches = 0
    matched_batches = 0
    false_candidates = 0
    for batch in run.batches:
        t = truth.at_batch(batch.batch_index)
        n_match = 0
        for _, result in batch.results:
            position = np.asarray(result.position)
            if t is not None and np.linalg.norm(position - t) <= truth_match_m:
                n_match += 1
            elif t is None or np.linalg.norm(position - t) > truth_match_m:
                false_candidates += 1
        if t is not None:
            truth_batches += 1
            if n_match:
                matched_batches += 1
    if truth_batches:
        out.candidate_recall = matched_batches / truth_batches
    out.false_candidates_per_batch = false_candidates / max(len(run.batches), 1)

    # Published-track statistics, two-channel per the D5 amendment.
    position_samples: list[tuple[int, np.ndarray]] = []
    velocity_samples: list[tuple[int, np.ndarray]] = []
    d2: list[float] = []
    confirmed_near_truth: set[int] = set()
    first_publish_s: float | None = None
    for batch_index, track in run.published:
        t = truth.at_batch(batch_index)
        if t is None:
            continue
        position = np.asarray(track.state[0:3])
        velocity = np.asarray(track.state[3:6])
        error = position - t
        if np.linalg.norm(error) <= truth_match_m:
            confirmed_near_truth.add(track.track_id)
            if first_publish_s is None:
                first_publish_s = batch_index / truth.fps
            position_samples.append((batch_index, error))
            velocity_samples.append((batch_index, velocity - truth.velocity))
            cov = np.asarray(track.covariance).reshape(6, 6)[0:3, 0:3]
            info = np.linalg.pinv(cov, hermitian=True)
            d2.append(float(error @ info @ error))
    out.published_samples = len(position_samples)
    out.bias_window_batches = bias_window_batches
    if position_samples:
        errors = np.stack([e for _, e in position_samples])
        out.range_p95_m = float(np.percentile(np.abs(errors[:, 1]), 95))
        out.cross_p95_m = float(
            np.percentile(np.hypot(errors[:, 0], errors[:, 2]), 95)
        )
        vel_errors = np.stack([e for _, e in velocity_samples])
        out.velocity_rmse_mps = float(np.sqrt(np.mean(np.sum(vel_errors**2, axis=1))))

        # Channel split: windowed-mean bias (systematic) vs residual (random).
        bias_series, residuals = split_channels(position_samples, bias_window_batches)
        bias_arr = np.stack(bias_series)
        residual_arr = np.stack(residuals)
        bias_norms = np.linalg.norm(bias_arr, axis=1)
        out.bias_mean_m = float(bias_norms.mean())
        out.bias_max_m = float(bias_norms.max())
        out.bias_axes_m = tuple(float(v) for v in bias_arr.mean(axis=0))
        out.cross_p95_random_m = float(
            np.percentile(np.hypot(residual_arr[:, 0], residual_arr[:, 2]), 95)
        )
        _, vel_residuals = split_channels(velocity_samples, bias_window_batches)
        vel_residual_arr = np.stack(vel_residuals)
        out.velocity_rmse_random_mps = float(
            np.sqrt(np.mean(np.sum(vel_residual_arr**2, axis=1)))
        )

        # -- settled-window velocity scoring (D5.2 amendment) -------------
        window = velocity_convergence_window_batches
        out.velocity_convergence_window_batches = window
        vel_indices = np.array([b for b, _ in velocity_samples])
        first_index = int(vel_indices.min())
        since_confirmation = vel_indices - first_index
        vel_norms = np.linalg.norm(vel_residual_arr, axis=1)

        settled_mask = since_confirmation >= window
        if settled_mask.any():
            out.velocity_rmse_settled_mps = float(
                np.sqrt(np.mean(vel_norms[settled_mask] ** 2))
            )
            out.velocity_settled_samples = int(settled_mask.sum())
        if (~settled_mask).any():
            out.velocity_rmse_transient_mps = float(
                np.sqrt(np.mean(vel_norms[~settled_mask] ** 2))
            )

        # Settling time: batches from confirmation until the velocity error
        # is settled and STAYS settled for the remainder of the run (a
        # momentary dip is not settling). "Settled" is evaluated on the same
        # quantity the gate bounds — a rolling RMSE over the declared
        # convergence window — not on individual samples: a per-sample test
        # against an RMSE threshold is a category error and trips on single
        # noise excursions. NaN when it never settles.
        order = np.argsort(vel_indices)
        ordered_norms = vel_norms[order]
        ordered_since = since_confirmation[order]
        n = len(ordered_norms)
        span = max(window, 1)
        rolling_ok = np.ones(n, dtype=bool)
        for i in range(n):
            chunk = ordered_norms[i : min(i + span, n)]
            rolling_ok[i] = float(np.sqrt(np.mean(chunk**2))) <= velocity_gate_mps
        stays_settled = np.ones(n, dtype=bool)
        broken = False
        for i in range(n - 1, -1, -1):
            broken = broken or not rolling_ok[i]
            stays_settled[i] = not broken
        if stays_settled.any():
            first_settled = int(np.argmax(stays_settled))
            out.velocity_settling_s = float(
                ordered_since[first_settled] / max(truth.fps, 1e-9)
            )
        d2_arr = np.asarray(d2)
        out.coverage = tuple(float(np.mean(d2_arr <= q)) for q in _CHI2_3DOF)
    if first_publish_s is not None:
        out.acquisition_s = first_publish_s - truth.entry_s
    out.duplicate_confirmed = max(len(confirmed_near_truth) - 1, 0)

    # Lifecycle / audit.
    out.final_statuses = {}
    for status in run.statuses.values():
        out.final_statuses[status] = out.final_statuses.get(status, 0) + 1
    for batch in run.batches:
        for rejection in batch.rejections:
            out.rejections[rejection.reason] = out.rejections.get(rejection.reason, 0) + 1
    out.nis_rejects = sum(1 for r in run.records if not r.accepted)
    for entry in run.excluded:
        out.excluded[entry.reason] = out.excluded.get(entry.reason, 0) + 1
    rejected_records = [r for r in run.records if not r.accepted][:2]
    accepted_records = [r for r in run.records if r.accepted][:2]
    out.audit_samples = [
        f"track {r.track_id} accepted={r.accepted} nis="
        f"{'None' if r.nis is None else f'{r.nis:.2f}'} obs={list(r.obs_ids)}"
        for r in accepted_records + rejected_records
    ]
    return out
