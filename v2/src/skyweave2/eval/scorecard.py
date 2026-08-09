"""The detector scorecard: one command, one JSON, pass/fail (design §7).

Quality metrics need truth labels; health metrics run on any footage;
performance metrics are wall-time measurements. Two detectors are compared
by scorecards, never by identical masks (§7.3).

The built-in detector here is a TRIVIAL fixed-background-mean subtractor.
It exists only as scorecard plumbing proof (D3 brief scope) — the real
GMM2-like detector is D4 and will be scored by this same tool.

Determinism note: quality and health sections are deterministic for a
given clip+config. The perf section is inherently a wall-clock measurement;
``--deterministic-perf`` zeroes it so scorecard JSONs can be diffed as
regression artifacts without wall-clock leakage (repo determinism rule).

Usage:

    uv run skyweave2-score <clip-dir> [<config.json>] [--labels labels.jsonl]
        [--out scorecard.json] [--deterministic-perf]
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from skyweave2.eval.clips import iter_frames, read_clip_meta
from skyweave2.eval.labels import TruthLabel, read_labels


class ScorecardConfig(BaseModel):
    """Every threshold the scorer uses. Config values, never constants (§7.2)."""

    model_config = ConfigDict(extra="forbid")

    # Trivial background-mean detector (plumbing proof only).
    background_frames: int = Field(default=10, ge=1)
    diff_threshold_dn: float = Field(default=12.0, gt=0.0)
    min_area_px: int = Field(default=2, ge=1)

    # Truth matching.
    match_radius_px: float = Field(default=5.0, gt=0.0)

    # Health.
    steady_state_tail_fraction: float = Field(default=0.25, gt=0.0, le=1.0)
    warmup_band_fraction: float = Field(default=0.20, ge=0.0)

    # Provisional pass thresholds.
    recall_min: float = Field(default=0.95, ge=0.0, le=1.0)
    occupancy_max_pct: float = Field(default=0.5, ge=0.0)
    false_per_frame_max: float = Field(default=2.0, ge=0.0)


@dataclass(frozen=True)
class Component:
    centroid_u: float
    centroid_v: float
    area_px: int


def connected_components(mask: np.ndarray, min_area_px: int) -> list[Component]:
    """4-connectivity components of a boolean mask (two-pass union-find).

    numpy + dict union-find; adequate for scorecard duty, not a hot path.
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return []
    labels: dict[tuple[int, int], int] = {}
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for y, x in zip(ys.tolist(), xs.tolist(), strict=True):
        up = labels.get((y - 1, x))
        left = labels.get((y, x - 1))
        if up is None and left is None:
            labels[(y, x)] = len(parent)
            parent.append(len(parent))
        elif up is None:
            labels[(y, x)] = left
        elif left is None:
            labels[(y, x)] = up
        else:
            labels[(y, x)] = up
            union(up, left)

    sums: dict[int, list[float]] = {}
    for (y, x), raw in labels.items():
        root = find(raw)
        acc = sums.setdefault(root, [0.0, 0.0, 0.0])
        acc[0] += x
        acc[1] += y
        acc[2] += 1.0
    out = []
    for acc in sums.values():
        if acc[2] >= min_area_px:
            out.append(
                Component(
                    centroid_u=acc[0] / acc[2], centroid_v=acc[1] / acc[2], area_px=int(acc[2])
                )
            )
    return sorted(out, key=lambda c: (c.centroid_v, c.centroid_u))


@dataclass
class FrameResult:
    frame_seq: int
    components: list[Component]
    occupancy: float  # foreground fraction 0..1
    elapsed_s: float


def run_background_mean_detector(
    clip_dir: str | Path, config: ScorecardConfig
) -> list[FrameResult]:
    """The plumbing-proof detector: |frame - mean(first K frames)| > threshold."""
    meta = read_clip_meta(clip_dir)
    if meta.frame_count <= config.background_frames:
        raise ValueError(
            f"clip has {meta.frame_count} frames; needs more than "
            f"background_frames={config.background_frames}"
        )
    background = np.zeros((meta.height, meta.width), dtype=np.float64)
    results: list[FrameResult] = []
    for seq, frame in iter_frames(clip_dir):
        start = time.perf_counter()
        if seq < config.background_frames:
            background += frame.astype(np.float64) / config.background_frames
            results.append(FrameResult(seq, [], 0.0, time.perf_counter() - start))
            continue
        mask = np.abs(frame.astype(np.float64) - background) > config.diff_threshold_dn
        components = connected_components(mask, config.min_area_px)
        results.append(
            FrameResult(
                seq,
                components,
                float(mask.mean()),
                time.perf_counter() - start,
            )
        )
    return results


def _p95(values: list[float]) -> float:
    return float(np.percentile(np.asarray(values), 95)) if values else float("nan")


def _quality_metrics(
    results: list[FrameResult], labels: list[TruthLabel], config: ScorecardConfig
) -> dict:
    by_frame: dict[int, list[TruthLabel]] = {}
    for label in labels:
        if label.visible:
            by_frame.setdefault(label.frame_seq, []).append(label)

    scored = [r for r in results if r.frame_seq >= config.background_frames]
    eligible = [r for r in scored if by_frame.get(r.frame_seq)]
    matched_frames = 0
    centroid_errors: list[float] = []
    false_counts: list[int] = []
    for result in scored:
        truths = by_frame.get(result.frame_seq, [])
        used: set[int] = set()
        frame_matched = False
        for truth in truths:
            best_i, best_d = -1, float("inf")
            for i, comp in enumerate(result.components):
                if i in used:
                    continue
                d = float(np.hypot(comp.centroid_u - truth.u, comp.centroid_v - truth.v))
                if d < best_d:
                    best_i, best_d = i, d
            if best_i >= 0 and best_d <= config.match_radius_px:
                used.add(best_i)
                centroid_errors.append(best_d)
                frame_matched = True
        if truths and frame_matched:
            matched_frames += 1
        false_counts.append(len(result.components) - len(used))

    return {
        "eligible_frames": len(eligible),
        "recall": (matched_frames / len(eligible)) if eligible else float("nan"),
        "centroid_err_px_mean": (
            float(np.mean(centroid_errors)) if centroid_errors else float("nan")
        ),
        "centroid_err_px_p95": _p95(centroid_errors),
        "false_per_frame": float(np.mean(false_counts)) if false_counts else float("nan"),
    }


def _health_metrics(results: list[FrameResult], config: ScorecardConfig) -> dict:
    scored = [r for r in results if r.frame_seq >= config.background_frames]
    if not scored:
        raise ValueError("no scored frames after background warmup")
    occupancy = np.asarray([r.occupancy for r in scored])
    tail = max(1, int(len(occupancy) * config.steady_state_tail_fraction))
    steady = float(np.median(occupancy[-tail:]))
    band = config.warmup_band_fraction * max(steady, 1e-9)
    warmup = len(occupancy)
    for i, value in enumerate(occupancy):
        if abs(float(value) - steady) <= band:
            warmup = i
            break
    return {
        "occupancy_pct": float(occupancy.mean() * 100.0),
        "warmup_frames": int(warmup + len(results) - len(scored)),
        "component_count": float(np.mean([len(r.components) for r in scored])),
        "steady_state_occupancy_pct": steady * 100.0,
    }


def _perf_metrics(results: list[FrameResult], deterministic: bool) -> dict:
    if deterministic:
        return {"fps": 0.0, "peak_mem_mb": 0.0, "latency_ms_p95": 0.0, "deterministic": True}
    elapsed = [r.elapsed_s for r in results]
    total = sum(elapsed)
    # ru_maxrss units are platform-dependent: bytes on macOS, kilobytes on
    # Linux (the Jetson this will actually run on).
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = peak_rss if sys.platform == "darwin" else peak_rss * 1024
    return {
        "fps": (len(results) / total) if total > 0 else float("nan"),
        "peak_mem_mb": peak_bytes / 1e6,
        "latency_ms_p95": _p95([e * 1000.0 for e in elapsed]),
        "deterministic": False,
    }


def assemble_scorecard(
    results: list[FrameResult],
    config: ScorecardConfig,
    labels: list[TruthLabel] | None,
    clip_name: str,
    detector_name: str,
    deterministic_perf: bool = False,
    camera_id: int | None = None,
) -> dict:
    """Metrics + pass gates over pre-computed per-frame results.

    Shared by the D3 plumbing detector and the D4 detector runner: any
    detector that produces ``FrameResult`` rows (full-resolution centroids)
    is scored by exactly this code — the comparable-scorecard principle.

    ``camera_id``: when given, only labels for that camera count. A clip is
    one camera's footage; scoring it against a multi-camera labels file
    would inflate the recall denominator and pull centroid matching toward
    other cameras' positions.

    The recall gate applies only when eligible frames exist: a target-free
    negative clip is judged on occupancy and false-per-frame, not on the
    recall of targets it does not contain.
    """
    health = _health_metrics(results, config)
    scorecard: dict = {
        "schema": "skyweave2-scorecard/1",
        "clip": clip_name,
        "detector": detector_name,
        "config": config.model_dump(),
        "health": health,
        "perf": _perf_metrics(results, deterministic_perf),
    }
    checks: dict[str, bool] = {
        "occupancy": health["occupancy_pct"] < config.occupancy_max_pct,
    }
    if labels is not None:
        if camera_id is not None:
            labels = [label for label in labels if label.camera_id == camera_id]
        quality = _quality_metrics(results, labels, config)
        scorecard["quality"] = quality
        if quality["eligible_frames"] > 0:
            checks["recall"] = bool(quality["recall"] >= config.recall_min)
        checks["false_per_frame"] = bool(quality["false_per_frame"] < config.false_per_frame_max)
    scorecard["pass"] = {**checks, "overall": all(checks.values())}
    return _sanitize_nonfinite(scorecard)


def score_clip(
    clip_dir: str | Path,
    config: ScorecardConfig | None = None,
    labels: list[TruthLabel] | None = None,
    deterministic_perf: bool = False,
) -> dict:
    """Run the plumbing detector and produce the scorecard dictionary."""
    config = config or ScorecardConfig()
    results = run_background_mean_detector(clip_dir, config)
    return assemble_scorecard(
        results,
        config,
        labels,
        clip_name=str(clip_dir),
        detector_name="background-mean (plumbing proof; real detector lands in D4)",
        deterministic_perf=deterministic_perf,
    )


def _sanitize_nonfinite(value):
    """NaN/inf -> None recursively: json.dump would otherwise emit invalid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize_nonfinite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_nonfinite(v) for v in value]
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Score a clip; emit scorecard.json")
    parser.add_argument("clip", help="clip directory (clip.json + frame_*.npy)")
    parser.add_argument("config", nargs="?", help="ScorecardConfig JSON file (optional)")
    parser.add_argument("--labels", help="truth labels JSONL (enables quality metrics)")
    parser.add_argument("--out", default="scorecard.json")
    parser.add_argument(
        "--deterministic-perf",
        action="store_true",
        help="zero the perf section so the JSON is byte-diffable (no wall clock)",
    )
    args = parser.parse_args(argv)
    config = (
        ScorecardConfig.model_validate(json.loads(Path(args.config).read_text()))
        if args.config
        else ScorecardConfig()
    )
    labels = read_labels(args.labels) if args.labels else None
    scorecard = score_clip(
        args.clip, config, labels, deterministic_perf=args.deterministic_perf
    )
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(scorecard, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {args.out} (overall pass: {scorecard['pass']['overall']})")


if __name__ == "__main__":
    main()
