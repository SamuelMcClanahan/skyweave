from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from skyweave.config import load_config
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.geom import point_distance
from skyweave.fusion.kalman import TrackManager
from skyweave.fusion.triangulator import triangulate_detections
from skyweave.log import JsonlLogger
from skyweave.messages import RunSummary
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.recording.recorder import Recorder
from skyweave.sim.generator import SyntheticPacketGenerator
from skyweave.sim.scene import build_scene


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the headless Skyweave synthetic packet check.")
    parser.add_argument("--config", default="configs/sim.yaml", help="Path to the simulation YAML config.")
    parser.add_argument("--record", action="store_true", help="Record packets and outputs for replay.")
    parser.add_argument("--record-dir", default="data/recordings", help="Directory for recorded sessions.")
    parser.add_argument("--log-stages", action="store_true", help="Write per-frame stage timings to JSONL logs.")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.log_stages:
        config.logging.log_stage_timings = True
    logger = JsonlLogger(config.logging.log_dir)
    recorder = Recorder.create(args.record_dir, config, args.config) if args.record else None
    try:
        summary = run_sim_check(config, logger, recorder=recorder, config_path=args.config)
    finally:
        if recorder:
            recorder.close()
        logger.close()

    print(
        "synthetic_run "
        f"scene={summary.scene} frames={summary.frames} voxel={summary.voxel_size_m:.3f}m "
        f"peak_rmse={summary.peak_rmse_m:.3f}m track_rmse={summary.track_rmse_m:.3f}m "
        f"max_track_err={summary.max_track_error_m:.3f}m latency_p50={summary.latency_p50_ms:.2f}ms "
        f"latency_p95={summary.latency_p95_ms:.2f}ms dropped={summary.dropped_packets} "
        f"not_visible={summary.not_visible_packets} false_pos={summary.false_positive_packets} "
        f"pass={str(summary.passed).lower()}"
    )
    print(f"log_path={logger.path}")
    if recorder:
        print(f"session_path={recorder.session_dir}")
    return 0 if summary.passed else 1


def run_sim_check(
    config,
    logger: JsonlLogger,
    recorder: Recorder | None = None,
    config_path: str = "configs/sim.yaml",
) -> RunSummary:
    grid = VoxelGrid.from_config(config.rayweave.grid)
    _validate_grid(grid)
    scene = build_scene(config.simulation)
    generator = SyntheticPacketGenerator(scene, config.simulation)
    aligner = TimeAligner(config.fusion.align_window_ns, config.fusion.min_cameras_per_frame)
    scorer = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)
    peak_extractor = PeakExtractor(grid, config.rayweave.peaks)
    tracks = TrackManager(config.kalman)

    logger.event("app_start", config_path=str(Path(config_path)), scene=scene.name)

    peak_errors: list[float] = []
    track_errors: list[float] = []
    latencies_ms: list[float] = []
    dropped_packets = 0
    not_visible_packets = 0
    false_positive_packets = 0

    for frame in generator.frames():
        start = time.perf_counter()
        stage_ms = _empty_stage_timings()
        dropped_packets += frame.dropped_packets
        not_visible_packets += frame.not_visible_packets
        false_positive_packets += frame.false_positive_packets
        for camera_id in frame.not_visible_camera_ids:
            logger.event("camera_not_visible", camera_id=camera_id, frame_seq=frame.truth.frame_seq)
        if recorder:
            recorder.record_motion_packets(frame.motion_packets)
            recorder.record_detection_packets(frame.detection_packets)
        for packet in frame.motion_packets:
            logger.event(
                "motion_packet_published",
                camera_id=packet.camera_id,
                frame_seq=packet.header.frame_seq,
                n_blobs=len(packet.blobs),
                n_patches=len(packet.motion_patches),
            )

        stage_start = time.perf_counter()
        aligned = aligner.align_frame(frame.motion_packets, frame.detection_packets)
        stage_ms["alignment"] = _elapsed_ms(stage_start)
        if aligned is None:
            stage_start = time.perf_counter()
            track = tracks.update(None, frame.truth.ts_ns)
            stage_ms["kalman"] = _elapsed_ms(stage_start)
            stage_ms["total"] = _elapsed_ms(start)
            _log_stage_timings(config, logger, frame.truth.frame_seq, frame.truth.ts_ns, False, stage_ms)
            _console_frame(config, frame.truth.frame_seq, frame.truth.position, None, None, track, math.inf, math.inf, 0.0, 0.0)
            continue

        stage_start = time.perf_counter()
        scored = scorer.score(aligned)
        stage_ms["scoring"] = _elapsed_ms(stage_start)
        stage_start = time.perf_counter()
        peaks, measurements = peak_extractor.extract(scored)
        scored.volume.peaks = peaks
        measurement = measurements[0] if measurements else None
        stage_ms["peaks"] = _elapsed_ms(stage_start)
        stage_start = time.perf_counter()
        tri = triangulate_detections(aligned.ts_ns, aligned.detection_packets, scene.cameras, config.fusion.pixel_noise_px)
        stage_ms["triangulation"] = _elapsed_ms(stage_start)
        stage_start = time.perf_counter()
        track = tracks.update(measurement, aligned.ts_ns)
        stage_ms["kalman"] = _elapsed_ms(stage_start)
        if recorder:
            recorder.record_weavefield(scored.volume)
            if measurement:
                recorder.record_measurement(measurement)
            if tri:
                recorder.record_measurement(tri)
            if track:
                recorder.record_track(track)

        peak_error = point_distance(measurement.position, frame.truth.position) if measurement else math.inf
        track_error = point_distance(tuple(track.state[:3]), frame.truth.position) if track else math.inf
        peak_errors.append(peak_error)
        track_errors.append(track_error)
        elapsed_ms = _elapsed_ms(start)
        stage_ms["total"] = elapsed_ms
        latencies_ms.append(elapsed_ms)
        _log_stage_timings(config, logger, frame.truth.frame_seq, aligned.ts_ns, True, stage_ms)

        logger.event(
            "weavefield_volume_scored",
            ts_ns=aligned.ts_ns,
            n_sources=len(aligned.motion_packets),
            n_voxels=len(scored.volume.voxels),
            n_peaks=len(peaks),
            score_max=float(scored.dense_scores.max(initial=0.0)),
        )
        if measurement:
            logger.event(
                "measurement_created",
                source=measurement.source,
                position=measurement.position,
                score=measurement.score,
                covariance_diag=np.diag(np.asarray(measurement.covariance)).tolist(),
            )
        if tri:
            logger.event("measurement_created", source=tri.source, position=tri.position, score=tri.score)
        if track:
            logger.event("track_updated", track_id=track.id, position=track.state[:3], update_count=track.update_count)

        _console_frame(
            config,
            frame.truth.frame_seq,
            frame.truth.position,
            measurement.position if measurement else None,
            tri.position if tri else None,
            track,
            peak_error,
            track_error,
            measurement.score if measurement else 0.0,
            elapsed_ms,
        )

    summary = _summary(
        scene.name,
        len(scene.truth),
        grid.voxel_size,
        peak_errors,
        track_errors,
        latencies_ms,
        dropped_packets,
        not_visible_packets,
        false_positive_packets,
        config.pass_peak_rmse_m,
        config.pass_track_rmse_m,
    )
    logger.event("summary_stats", **summary.model_dump(mode="json"))
    if recorder:
        recorder.record_summary(summary)
    return summary


def _console_frame(
    config,
    frame_seq: int,
    truth: np.ndarray,
    peak,
    triangulated,
    track,
    peak_error: float,
    track_error: float,
    score: float,
    latency_ms: float,
) -> None:
    every = max(config.logging.console_every, 1)
    if frame_seq % every != 0:
        return
    peak_text = _fmt_pos(peak)
    tri_text = _fmt_pos(triangulated)
    track_text = _fmt_pos(track.state[:3] if track else None)
    print(
        f"frame={frame_seq:03d} truth={_fmt_pos(truth)} peak={peak_text} tri={tri_text} "
        f"track={track_text} peak_err={peak_error:.3f}m track_err={track_error:.3f}m "
        f"score={score:.3f} latency={latency_ms:.2f}ms"
    )


def _fmt_pos(pos) -> str:
    if pos is None:
        return "(nan,nan,nan)"
    x, y, z = [float(v) for v in pos]
    return f"({x:.2f},{y:.2f},{z:.2f})"


def _empty_stage_timings() -> dict[str, float]:
    return {
        "alignment": 0.0,
        "scoring": 0.0,
        "peaks": 0.0,
        "triangulation": 0.0,
        "kalman": 0.0,
        "total": 0.0,
    }


def _log_stage_timings(
    config,
    logger: JsonlLogger,
    frame_seq: int,
    ts_ns: int,
    aligned: bool,
    stage_ms: dict[str, float],
) -> None:
    if not config.logging.log_stage_timings:
        return
    logger.event(
        "frame_stage_timings",
        frame_seq=frame_seq,
        ts_ns=ts_ns,
        aligned=aligned,
        stage_ms={stage: round(ms, 6) for stage, ms in stage_ms.items()},
    )


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _summary(
    scene: str,
    frames: int,
    voxel_size: float,
    peak_errors: list[float],
    track_errors: list[float],
    latencies_ms: list[float],
    dropped_packets: int,
    not_visible_packets: int,
    false_positive_packets: int,
    pass_peak_rmse: float,
    pass_track_rmse: float,
) -> RunSummary:
    finite_peak = [x for x in peak_errors if math.isfinite(x)]
    finite_track = [x for x in track_errors if math.isfinite(x)]
    peak_rmse = _rmse(finite_peak)
    track_rmse = _rmse(finite_track)
    max_track = max(finite_track) if finite_track else math.inf
    p50 = statistics.median(latencies_ms) if latencies_ms else math.inf
    p95 = _percentile(latencies_ms, 95.0) if latencies_ms else math.inf
    passed = peak_rmse <= pass_peak_rmse and track_rmse <= pass_track_rmse and len(finite_peak) == frames
    return RunSummary(
        scene=scene,
        frames=frames,
        voxel_size_m=voxel_size,
        peak_rmse_m=peak_rmse,
        track_rmse_m=track_rmse,
        max_track_error_m=max_track,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        dropped_packets=dropped_packets,
        not_visible_packets=not_visible_packets,
        false_positive_packets=false_positive_packets,
        passed=passed,
    )


def _rmse(errors: list[float]) -> float:
    if not errors:
        return math.inf
    return math.sqrt(sum(x * x for x in errors) / len(errors))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    idx = (len(ordered) - 1) * percentile / 100.0
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[int(idx)]
    return ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo)


def _validate_grid(grid: VoxelGrid) -> None:
    if grid.voxel_size <= 0:
        raise ValueError("voxel_size_m must be positive")
    if any(dim <= 0 for dim in grid.dims):
        raise ValueError("all grid dims must be positive")


if __name__ == "__main__":
    sys.exit(main())
