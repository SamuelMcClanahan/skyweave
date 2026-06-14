from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from skyweave.config import SimCheckConfig
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.geom import CameraCalib, point_distance
from skyweave.fusion.kalman import TrackManager
from skyweave.fusion.triangulator import triangulate_detections
from skyweave.messages import Measurement3D, RunSummary, SkyweaveModel, Track, VizCamera, VizFrame, WeavefieldVolume
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.sim.generator import SyntheticPacketGenerator
from skyweave.sim.scene import build_scene


STREAM_FILES = {
    "frames": "frames.jsonl",
    "weavefields": "weavefields.jsonl",
    "measurements": "measurements.jsonl",
    "tracks": "tracks.jsonl",
}


def export_synthetic_viz_bundle(
    config: SimCheckConfig,
    output_dir: str | Path,
    config_path: str = "configs/sim.yaml",
    max_frames: int | None = None,
) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)

    grid = VoxelGrid.from_config(config.rayweave.grid)
    scene = build_scene(config.simulation)
    generator = SyntheticPacketGenerator(scene, config.simulation)
    aligner = TimeAligner(config.fusion.align_window_ns, config.fusion.min_cameras_per_frame)
    scorer = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)
    peak_extractor = PeakExtractor(grid, config.rayweave.peaks)
    tracks = TrackManager(config.kalman)
    viz_cameras = [_viz_camera(camera, config.simulation.timestep_hz) for camera in scene.cameras.values()]

    _write_json(root / "manifest.json", _manifest(config, config_path, scene.name))
    _write_json(root / "grid.json", grid.spec.model_dump(mode="json"))
    _write_json(root / "cameras.json", [_camera_payload(camera, config.simulation.timestep_hz) for camera in scene.cameras.values()])

    peak_errors: list[float] = []
    track_errors: list[float] = []
    latencies_ms: list[float] = []
    dropped_packets = 0
    not_visible_packets = 0
    false_positive_packets = 0

    with _JsonlWriter(root / STREAM_FILES["frames"]) as frame_writer, _JsonlWriter(
        root / STREAM_FILES["weavefields"]
    ) as volume_writer, _JsonlWriter(root / STREAM_FILES["measurements"]) as measurement_writer, _JsonlWriter(
        root / STREAM_FILES["tracks"]
    ) as track_writer:
        frames = generator.frames()
        if max_frames is not None:
            frames = frames[:max_frames]
        for frame in frames:
            dropped_packets += frame.dropped_packets
            not_visible_packets += frame.not_visible_packets
            false_positive_packets += frame.false_positive_packets
            start = time.perf_counter()
            aligned = aligner.align_frame(frame.motion_packets, frame.detection_packets)
            volume: WeavefieldVolume | None = None
            measurements: list[Measurement3D] = []
            track: Track | None = None
            if aligned is None:
                track = tracks.update(None, frame.truth.ts_ns)
            else:
                scored = scorer.score(aligned)
                peaks, peak_measurements = peak_extractor.extract(scored)
                scored.volume.peaks = peaks
                volume = scored.volume
                measurements.extend(peak_measurements)
                tri = triangulate_detections(aligned.ts_ns, aligned.detection_packets, scene.cameras, config.fusion.pixel_noise_px)
                if tri is not None:
                    measurements.append(tri)
                track = tracks.update(peak_measurements[0] if peak_measurements else None, aligned.ts_ns)

            elapsed_ms = (time.perf_counter() - start) * 1000.0
            latencies_ms.append(elapsed_ms)
            if measurements:
                peak_errors.append(point_distance(measurements[0].position, frame.truth.position))
            if track is not None:
                track_errors.append(point_distance(tuple(track.state[:3]), frame.truth.position))

            if volume is not None:
                volume_writer.write(volume)
            for measurement in measurements:
                measurement_writer.write(measurement)
            if track is not None:
                track_writer.write(track)

            frame_writer.write(
                VizFrame(
                    frame_seq=frame.truth.frame_seq,
                    ts_ns=frame.truth.ts_ns,
                    tracks=[track] if track else [],
                    cameras=viz_cameras,
                    measurements=measurements,
                    weavefield_history=[volume] if volume else [],
                    truth_position=tuple(float(x) for x in frame.truth.position),
                    stats={
                        "latency_ms": elapsed_ms,
                        "dropped_packets": float(frame.dropped_packets),
                        "not_visible_packets": float(frame.not_visible_packets),
                        "false_positive_packets": float(frame.false_positive_packets),
                    },
                )
            )

    summary = _summary(
        scene.name,
        len(latencies_ms),
        grid.voxel_size,
        peak_errors,
        track_errors,
        latencies_ms,
        dropped_packets,
        not_visible_packets,
        false_positive_packets,
        config,
    )
    _write_json(root / "summary.json", summary.model_dump(mode="json"))
    return root


class _JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def __enter__(self) -> "_JsonlWriter":
        self._fh = self.path.open("w", encoding="utf-8")
        return self

    def __exit__(self, *_exc) -> None:
        if self._fh:
            self._fh.close()

    def write(self, model: SkyweaveModel) -> None:
        if self._fh is None:
            raise RuntimeError("writer is not open")
        self._fh.write(model.model_dump_json() + "\n")


def _manifest(config: SimCheckConfig, config_path: str, scene: str) -> dict:
    created = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "format": "skyweave_viz_bundle",
        "created_utc": created.isoformat(),
        "config_path": config_path,
        "scene": scene,
        "streams": STREAM_FILES,
        "notes": "JSONL streams use one complete JSON object per line.",
    }


def _camera_payload(camera: CameraCalib, fps: float) -> dict:
    return {
        "viz": _viz_camera(camera, fps).model_dump(mode="json"),
        "image_width": camera.width,
        "image_height": camera.height,
        "intrinsics": camera.K.tolist(),
        "distortion": camera.D.tolist(),
        "T_world_cam": camera.T_world_cam.tolist(),
    }


def _viz_camera(camera: CameraCalib, fps: float) -> VizCamera:
    fx = float(camera.K[0, 0])
    fy = float(camera.K[1, 1])
    fov_h = math.degrees(2.0 * math.atan(camera.width / (2.0 * fx)))
    fov_v = math.degrees(2.0 * math.atan(camera.height / (2.0 * fy)))
    return VizCamera(
        id=camera.id,
        position=[float(x) for x in camera.position],
        rotation_quat=_rotation_quat_xyzw(camera.T_world_cam[:3, :3]),
        fov_h_deg=fov_h,
        fov_v_deg=fov_v,
        fps=fps,
        online=True,
    )


def _rotation_quat_xyzw(rotation: np.ndarray) -> list[float]:
    m = np.asarray(rotation, dtype=np.float64)
    trace = float(np.trace(m))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    else:
        i = int(np.argmax(np.diag(m)))
        if i == 0:
            s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
            w = (m[2, 1] - m[1, 2]) / s
            x = 0.25 * s
            y = (m[0, 1] + m[1, 0]) / s
            z = (m[0, 2] + m[2, 0]) / s
        elif i == 1:
            s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
            w = (m[0, 2] - m[2, 0]) / s
            x = (m[0, 1] + m[1, 0]) / s
            y = 0.25 * s
            z = (m[1, 2] + m[2, 1]) / s
        else:
            s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
            w = (m[1, 0] - m[0, 1]) / s
            x = (m[0, 2] + m[2, 0]) / s
            y = (m[1, 2] + m[2, 1]) / s
            z = 0.25 * s
    quat = np.asarray([x, y, z, w], dtype=np.float64)
    quat /= max(float(np.linalg.norm(quat)), 1e-12)
    return [float(value) for value in quat]


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
    config: SimCheckConfig,
) -> RunSummary:
    peak_rmse = _rmse(peak_errors)
    track_rmse = _rmse(track_errors)
    return RunSummary(
        scene=scene,
        frames=frames,
        voxel_size_m=voxel_size,
        peak_rmse_m=peak_rmse,
        track_rmse_m=track_rmse,
        max_track_error_m=max(track_errors) if track_errors else math.inf,
        latency_p50_ms=statistics.median(latencies_ms) if latencies_ms else math.inf,
        latency_p95_ms=_percentile(latencies_ms, 95.0) if latencies_ms else math.inf,
        dropped_packets=dropped_packets,
        not_visible_packets=not_visible_packets,
        false_positive_packets=false_positive_packets,
        passed=peak_rmse <= config.pass_peak_rmse_m and track_rmse <= config.pass_track_rmse_m,
    )


def _rmse(errors: list[float]) -> float:
    if not errors:
        return math.inf
    return math.sqrt(sum(error * error for error in errors) / len(errors))


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct / 100.0
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[int(idx)]
    return ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo)


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
