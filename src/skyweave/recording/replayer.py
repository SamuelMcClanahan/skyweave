from __future__ import annotations

import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterator, TypeVar

import msgpack
import numpy as np

from skyweave.config import SimCheckConfig
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.geom import point_distance
from skyweave.fusion.kalman import TrackManager
from skyweave.fusion.triangulator import triangulate_detections
from skyweave.messages import DetectionPacket, MotionPacket, RunSummary, SkyweaveModel
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.recording.recorder import STREAM_FILES
from skyweave.sim.scene import build_scene

T = TypeVar("T", bound=SkyweaveModel)


def replay_session(session_dir: str | Path) -> RunSummary:
    root = Path(session_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    recorded_summary = _read_recorded_summary(root)
    config = SimCheckConfig.model_validate(manifest["config"])
    scene = build_scene(config.simulation)
    grid = VoxelGrid.from_config(config.rayweave.grid)
    aligner = TimeAligner(config.fusion.align_window_ns, config.fusion.min_cameras_per_frame)
    scorer = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)
    peak_extractor = PeakExtractor(grid, config.rayweave.peaks)
    tracks = TrackManager(config.kalman)

    motion_by_frame = _group_by_frame(_read_stream(root / STREAM_FILES["motion_packets"], MotionPacket))
    detections_by_frame = _group_by_frame(_read_stream(root / STREAM_FILES["detection_packets"], DetectionPacket))
    truth_by_frame = {sample.frame_seq: sample for sample in scene.truth}

    peak_errors: list[float] = []
    track_errors: list[float] = []
    latencies_ms: list[float] = []

    for frame_seq in sorted(motion_by_frame):
        truth = truth_by_frame.get(frame_seq)
        if truth is None:
            continue
        start = time.perf_counter()
        aligned = aligner.align_frame(motion_by_frame[frame_seq], detections_by_frame.get(frame_seq, []))
        if aligned is None:
            tracks.update(None, truth.ts_ns)
            continue

        scored = scorer.score(aligned)
        peaks, measurements = peak_extractor.extract(scored)
        scored.volume.peaks = peaks
        measurement = measurements[0] if measurements else None
        triangulate_detections(aligned.ts_ns, aligned.detection_packets, scene.cameras, config.fusion.pixel_noise_px)
        track = tracks.update(measurement, aligned.ts_ns)

        if measurement:
            peak_errors.append(point_distance(measurement.position, truth.position))
        if track:
            track_errors.append(point_distance(tuple(track.state[:3]), truth.position))
        latencies_ms.append((time.perf_counter() - start) * 1000.0)

    return _summary(
        scene.name,
        len(scene.truth),
        grid.voxel_size,
        peak_errors,
        track_errors,
        latencies_ms,
        int(recorded_summary.get("dropped_packets", 0)),
        int(recorded_summary.get("not_visible_packets", 0)),
        int(recorded_summary.get("false_positive_packets", 0)),
        config.pass_peak_rmse_m,
        config.pass_track_rmse_m,
    )


def _read_stream(path: Path, model_type: type[T]) -> Iterator[T]:
    if not path.exists():
        return
    with path.open("rb") as fh:
        unpacker = msgpack.Unpacker(fh, raw=False)
        for payload in unpacker:
            yield model_type.model_validate(payload)


def _group_by_frame(packets: Iterator[MotionPacket] | Iterator[DetectionPacket]):
    grouped = defaultdict(list)
    for packet in packets:
        grouped[packet.header.frame_seq].append(packet)
    return grouped


def _read_recorded_summary(root: Path) -> dict:
    path = root / "summary.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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
    peak_rmse = _rmse(peak_errors)
    track_rmse = _rmse(track_errors)
    max_track = max(track_errors) if track_errors else math.inf
    p50 = statistics.median(latencies_ms) if latencies_ms else math.inf
    p95 = _percentile(latencies_ms, 95.0) if latencies_ms else math.inf
    passed = peak_rmse <= pass_peak_rmse and track_rmse <= pass_track_rmse and len(peak_errors) == frames
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
    ordered = sorted(values)
    idx = (len(ordered) - 1) * percentile / 100.0
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[int(idx)]
    return ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo)
