from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from skyweave.config import SimCheckConfig, load_config
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.geom import point_distance
from skyweave.fusion.kalman import TrackManager
from skyweave.fusion.triangulator import triangulate_detections
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.sim.generator import SyntheticPacketGenerator
from skyweave.sim.scene import build_scene

DEFAULT_BENCHMARK_CONFIG = "configs/sim.yaml"
DEFAULT_BENCHMARK_FRAMES = 300
DEFAULT_BENCHMARK_WARMUP = 30
STAGES = ("packet_generation", "alignment", "scoring", "peaks", "triangulation", "kalman", "total")


@dataclass
class BenchmarkResult:
    config_path: str
    scene: str
    voxel_size_m: float
    frames: int
    warmup: int
    aligned_frames: int
    measurement_frames: int
    dropped_packets: int
    not_visible_packets: int
    false_positive_packets: int
    peak_rmse_m: float
    track_rmse_m: float
    stage_ms: dict[str, list[float]] = field(default_factory=dict)

    @property
    def fps_p50(self) -> float:
        total_p50 = percentile(self.stage_ms["total"], 50.0)
        if total_p50 <= 0.0 or not math.isfinite(total_p50):
            return 0.0
        return 1000.0 / total_p50


def run_benchmark(
    config_path: str | Path = DEFAULT_BENCHMARK_CONFIG,
    frames: int = DEFAULT_BENCHMARK_FRAMES,
    warmup: int = DEFAULT_BENCHMARK_WARMUP,
) -> BenchmarkResult:
    if frames <= 0:
        raise ValueError("frames must be positive")
    if warmup < 0:
        raise ValueError("warmup must be non-negative")

    config = load_config(config_path).model_copy(deep=True)
    config.simulation.frames = frames + warmup
    return _run(config, str(config_path), frames, warmup)


def print_result(result: BenchmarkResult) -> None:
    total = result.stage_ms["total"]
    print(
        "benchmark_run "
        f"config={result.config_path} scene={result.scene} frames={result.frames} warmup={result.warmup} "
        f"voxel={result.voxel_size_m:.3f}m fps_p50={result.fps_p50:.2f} "
        f"total_p50={percentile(total, 50.0):.2f}ms total_p95={percentile(total, 95.0):.2f}ms "
        f"peak_rmse={result.peak_rmse_m:.3f}m track_rmse={result.track_rmse_m:.3f}m "
        f"aligned={result.aligned_frames} measurements={result.measurement_frames} "
        f"dropped={result.dropped_packets} not_visible={result.not_visible_packets} "
        f"false_pos={result.false_positive_packets}"
    )
    print("stage,p50_ms,p95_ms,p99_ms,total_ms,share_pct")
    total_sum = sum(total)
    for stage in STAGES:
        values = result.stage_ms[stage]
        stage_sum = sum(values)
        share = 100.0 * stage_sum / total_sum if total_sum > 0 else 0.0
        print(
            f"{stage},{percentile(values, 50.0):.3f},{percentile(values, 95.0):.3f},"
            f"{percentile(values, 99.0):.3f},{stage_sum:.3f},{share:.1f}"
        )


def _run(config: SimCheckConfig, config_path: str, frames: int, warmup: int) -> BenchmarkResult:
    grid = VoxelGrid.from_config(config.rayweave.grid)
    scene = build_scene(config.simulation)
    generator = SyntheticPacketGenerator(scene, config.simulation)
    aligner = TimeAligner(config.fusion.align_window_ns, config.fusion.min_cameras_per_frame)
    scorer = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)
    peak_extractor = PeakExtractor(grid, config.rayweave.peaks)
    tracks = TrackManager(config.kalman)

    stage_ms = {stage: [] for stage in STAGES}
    peak_errors: list[float] = []
    track_errors: list[float] = []
    aligned_frames = 0
    measurement_frames = 0
    dropped_packets = 0
    not_visible_packets = 0
    false_positive_packets = 0

    for frame_index, truth in enumerate(scene.truth):
        measured = frame_index >= warmup
        frame_start = time.perf_counter()

        start = time.perf_counter()
        frame = generator.make_frame(truth)
        packet_ms = elapsed_ms(start)

        dropped_packets += frame.dropped_packets if measured else 0
        not_visible_packets += frame.not_visible_packets if measured else 0
        false_positive_packets += frame.false_positive_packets if measured else 0

        start = time.perf_counter()
        aligned = aligner.align_frame(frame.motion_packets, frame.detection_packets)
        alignment_ms = elapsed_ms(start)

        scoring_ms = 0.0
        peaks_ms = 0.0
        triangulation_ms = 0.0
        kalman_ms = 0.0
        measurement = None
        track = None

        if aligned is None:
            start = time.perf_counter()
            track = tracks.update(None, frame.truth.ts_ns)
            kalman_ms = elapsed_ms(start)
        else:
            if measured:
                aligned_frames += 1

            start = time.perf_counter()
            scored = scorer.score(aligned)
            scoring_ms = elapsed_ms(start)

            start = time.perf_counter()
            peaks, measurements = peak_extractor.extract(scored)
            scored.volume.peaks = peaks
            measurement = measurements[0] if measurements else None
            peaks_ms = elapsed_ms(start)

            start = time.perf_counter()
            triangulate_detections(aligned.ts_ns, aligned.detection_packets, scene.cameras, config.fusion.pixel_noise_px)
            triangulation_ms = elapsed_ms(start)

            start = time.perf_counter()
            track = tracks.update(measurement, aligned.ts_ns)
            kalman_ms = elapsed_ms(start)

        total_ms = elapsed_ms(frame_start)
        if not measured:
            continue

        _record(stage_ms, packet_generation=packet_ms, alignment=alignment_ms, scoring=scoring_ms)
        _record(stage_ms, peaks=peaks_ms, triangulation=triangulation_ms, kalman=kalman_ms, total=total_ms)

        if measurement is not None:
            measurement_frames += 1
            peak_errors.append(point_distance(measurement.position, frame.truth.position))
        if track is not None:
            track_errors.append(point_distance(tuple(track.state[:3]), frame.truth.position))

    return BenchmarkResult(
        config_path=config_path,
        scene=scene.name,
        voxel_size_m=grid.voxel_size,
        frames=frames,
        warmup=warmup,
        aligned_frames=aligned_frames,
        measurement_frames=measurement_frames,
        dropped_packets=dropped_packets,
        not_visible_packets=not_visible_packets,
        false_positive_packets=false_positive_packets,
        peak_rmse_m=rmse(peak_errors),
        track_rmse_m=rmse(track_errors),
        stage_ms=stage_ms,
    )


def _record(stage_ms: dict[str, list[float]], **values: float) -> None:
    for stage, value in values.items():
        stage_ms[stage].append(value)


def elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def rmse(errors: list[float]) -> float:
    if not errors:
        return math.inf
    return math.sqrt(sum(error * error for error in errors) / len(errors))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct / 100.0
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[int(idx)]
    return ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo)
