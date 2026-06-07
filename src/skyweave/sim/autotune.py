from __future__ import annotations

import contextlib
import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from skyweave.camera.motion import MotionPacketConfig
from skyweave.config import SimCheckConfig, load_config
from skyweave.messages import RunSummary
from skyweave.sim.check import SIM_SOURCE_CHOICES, run_sim_check


@dataclass(frozen=True)
class TuneCandidate:
    motion_threshold: int
    motion_min_area_px: int
    motion_max_components: int
    fusion_min_cameras_per_frame: int
    rayweave_min_supporting_cameras: int
    peak_threshold_percentile: float
    kalman_sigma_accel_mps2: float
    kalman_measurement_var_scale: float

    @classmethod
    def from_config(cls, config: SimCheckConfig) -> TuneCandidate:
        return cls(
            motion_threshold=MotionPacketConfig().threshold,
            motion_min_area_px=MotionPacketConfig().min_area_px,
            motion_max_components=MotionPacketConfig().max_components,
            fusion_min_cameras_per_frame=config.fusion.min_cameras_per_frame,
            rayweave_min_supporting_cameras=config.rayweave.scorer.min_supporting_cameras,
            peak_threshold_percentile=config.rayweave.peaks.threshold_percentile,
            kalman_sigma_accel_mps2=config.kalman.sigma_accel_mps2,
            kalman_measurement_var_scale=config.kalman.measurement_var_scale,
        )

    def replace(self, **kwargs: Any) -> TuneCandidate:
        values = self.__dict__.copy()
        values.update(kwargs)
        return TuneCandidate(**values)

    def motion_config(self) -> MotionPacketConfig:
        return MotionPacketConfig(
            threshold=self.motion_threshold,
            min_area_px=self.motion_min_area_px,
            max_components=self.motion_max_components,
        )

    def profile_settings(self) -> dict[str, Any]:
        return {
            "motion": {
                "threshold": self.motion_threshold,
                "min_area_px": self.motion_min_area_px,
                "max_components": self.motion_max_components,
            },
            "fusion": {
                "min_cameras_per_frame": self.fusion_min_cameras_per_frame,
            },
            "rayweave": {
                "scorer": {
                    "min_supporting_cameras": self.rayweave_min_supporting_cameras,
                },
                "peaks": {
                    "threshold_percentile": self.peak_threshold_percentile,
                },
            },
            "kalman": {
                "sigma_accel_mps2": self.kalman_sigma_accel_mps2,
                "measurement_var_scale": self.kalman_measurement_var_scale,
            },
        }


@dataclass(frozen=True)
class TuneResult:
    candidate: TuneCandidate
    summary: RunSummary
    score: float
    eval_index: int


@dataclass(frozen=True)
class AutotuneResult:
    baseline: TuneResult
    best: TuneResult
    evaluations: list[TuneResult]


class _NullLogger:
    path = "autotune:null"

    def event(self, *_args: Any, **_kwargs: Any) -> None:
        return

    def close(self) -> None:
        return


def run_autotune(
    config_path: str | Path,
    *,
    source: str = "rendered",
    passes: int = 2,
    max_evals: int = 72,
    frames_limit: int | None = None,
) -> AutotuneResult:
    if source not in SIM_SOURCE_CHOICES:
        raise ValueError(f"source must be one of {', '.join(SIM_SOURCE_CHOICES)}")
    if frames_limit is not None and frames_limit < 2:
        raise ValueError("frames_limit must be at least 2")

    base_config = load_config(config_path)
    if frames_limit is not None:
        base_config.simulation.frames = min(base_config.simulation.frames, frames_limit)
    base_config.logging.console_every = max(base_config.simulation.frames + 1, 1_000_000)
    camera_count = int(base_config.simulation.camera_count)
    support_values = _support_values(camera_count)

    baseline_candidate = TuneCandidate.from_config(base_config)
    evaluations: list[TuneResult] = []
    baseline = _evaluate(base_config, baseline_candidate, source, len(evaluations))
    evaluations.append(baseline)
    best = baseline

    search_space = [
        ("motion_threshold", [16, 24, 32, 40, 48, 64, 80, 96]),
        ("motion_min_area_px", [2, 4, 8, 12, 16, 24]),
        ("motion_max_components", [1, 2, 4, 8]),
        ("fusion_min_cameras_per_frame", support_values),
        ("rayweave_min_supporting_cameras", support_values),
        ("peak_threshold_percentile", [96.0, 97.5, 98.5, 99.0, 99.5, 99.8]),
        ("kalman_sigma_accel_mps2", [0.5, 1.0, 2.0, 3.5, 6.0, 9.0, 12.0]),
        ("kalman_measurement_var_scale", [0.5, 1.0, 1.5, 2.5, 4.0, 8.0]),
    ]

    for _pass_index in range(max(1, passes)):
        improved_this_pass = False
        for field_name, values in search_space:
            if len(evaluations) >= max_evals:
                break
            local_best = best
            for value in _unique_values(values):
                if len(evaluations) >= max_evals:
                    break
                candidate = best.candidate.replace(**{field_name: value})
                if candidate == best.candidate:
                    continue
                result = _evaluate(base_config, candidate, source, len(evaluations))
                evaluations.append(result)
                if result.score < local_best.score:
                    local_best = result
            if local_best.score < best.score:
                best = local_best
                improved_this_pass = True
        if not improved_this_pass or len(evaluations) >= max_evals:
            break

    return AutotuneResult(baseline=baseline, best=best, evaluations=evaluations)


def write_operator_profile(
    path: str | Path,
    result: AutotuneResult,
    *,
    requested_mode: str = "rendered",
    profile_name: str = "autotuned",
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "profile_name": profile_name,
        "tracking": {"requested_mode": requested_mode},
        "settings": result.best.candidate.profile_settings(),
        "autotune": {
            "baseline": _summary_payload(result.baseline),
            "best": _summary_payload(result.best),
            "evaluations": len(result.evaluations),
        },
    }
    output.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return output


def print_autotune_summary(result: AutotuneResult, profile_path: str | Path | None = None) -> None:
    print(
        "autotune "
        f"evals={len(result.evaluations)} "
        f"baseline_track_rmse={result.baseline.summary.track_rmse_m:.4f}m "
        f"best_track_rmse={result.best.summary.track_rmse_m:.4f}m "
        f"baseline_peak_rmse={result.baseline.summary.peak_rmse_m:.4f}m "
        f"best_peak_rmse={result.best.summary.peak_rmse_m:.4f}m "
        f"best_score={result.best.score:.4f}"
    )
    print(f"best_settings={yaml.safe_dump(result.best.candidate.profile_settings(), sort_keys=False).strip()}")
    if profile_path is not None:
        print(f"profile_path={profile_path}")


def _evaluate(config: SimCheckConfig, candidate: TuneCandidate, source: str, eval_index: int) -> TuneResult:
    trial = config.model_copy(deep=True)
    trial.fusion.min_cameras_per_frame = candidate.fusion_min_cameras_per_frame
    trial.rayweave.scorer.min_supporting_cameras = candidate.rayweave_min_supporting_cameras
    trial.rayweave.peaks.threshold_percentile = candidate.peak_threshold_percentile
    trial.kalman.sigma_accel_mps2 = candidate.kalman_sigma_accel_mps2
    trial.kalman.measurement_var_scale = candidate.kalman_measurement_var_scale
    with contextlib.redirect_stdout(io.StringIO()):
        summary = run_sim_check(
            trial,
            _NullLogger(),  # type: ignore[arg-type]
            config_path=f"autotune:{eval_index}",
            source=source,
            motion_config=candidate.motion_config(),
        )
    return TuneResult(candidate=candidate, summary=summary, score=_score(summary), eval_index=eval_index)


def _score(summary: RunSummary) -> float:
    values = [summary.track_rmse_m, 0.45 * summary.peak_rmse_m, 0.08 * summary.max_track_error_m]
    if not all(math.isfinite(value) for value in values):
        return math.inf
    miss_penalty = 0.02 * (summary.not_visible_packets + summary.dropped_packets + summary.false_positive_packets)
    return sum(values) + miss_penalty


def _support_values(camera_count: int) -> list[int]:
    cap = max(1, min(camera_count, 6))
    return list(range(1, cap + 1))


def _unique_values(values: Iterable[Any]) -> list[Any]:
    output = []
    for value in values:
        if value not in output:
            output.append(value)
    return output


def _summary_payload(result: TuneResult) -> dict[str, Any]:
    return {
        "eval_index": result.eval_index,
        "score": result.score,
        "peak_rmse_m": result.summary.peak_rmse_m,
        "track_rmse_m": result.summary.track_rmse_m,
        "max_track_error_m": result.summary.max_track_error_m,
        "passed": result.summary.passed,
        "settings": result.candidate.profile_settings(),
    }
