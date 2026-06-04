from __future__ import annotations

import argparse
import gc
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from skyweave.camera.motion import MotionPacketConfig
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource
from skyweave.cli.camera_check import MotionCameraState, _percentile, _process_motion_source
from skyweave.config import load_config
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.kalman import TrackManager
from skyweave.messages import MotionPacket
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.sim.generator import SyntheticFrame, SyntheticPacketGenerator
from skyweave.sim.scene import build_scene

STAGES = ("camera_packets", "alignment", "scoring", "peaks", "kalman", "total")


@dataclass
class LiveBenchmarkResult:
    config_path: str
    frames: int
    warmup: int
    camera_frames: int
    read_failures: int
    aligned_frames: int
    measurement_frames: int
    rayweave_input: str
    stage_ms: dict[str, list[float]] = field(default_factory=dict)

    @property
    def fps_p50(self) -> float:
        total_p50 = _percentile(self.stage_ms["total"], 50.0)
        return 1000.0 / total_p50 if total_p50 > 0.0 and math.isfinite(total_p50) else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark live camera packet generation plus Rayweave scoring.")
    parser.add_argument("--config", default="configs/sim_mvp_ov9281_100hz_numba.yaml")
    parser.add_argument("--devices", required=True, help="Comma-separated live camera devices.")
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--warmup-frames", type=int, default=30)
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--fps", type=float, default=None)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--motion-backend", choices=("python", "opencv", "opencv_contours"), default="opencv")
    parser.add_argument(
        "--align-window-ms",
        type=float,
        default=None,
        help="Override the packet alignment window for live timing diagnostics.",
    )
    parser.add_argument(
        "--rayweave-input",
        choices=("live", "stress-patches"),
        default="stress-patches",
        help="Use live motion packets as-is or replace them with deterministic patch workload.",
    )
    parser.add_argument("--stress-patch-size", type=int, default=None)
    parser.add_argument("--console-every", type=int, default=0)
    parser.add_argument("--enable-gc", action="store_true", help="Keep Python cyclic GC enabled during the hot loop.")
    args = parser.parse_args(argv)

    devices = [item.strip() for item in args.devices.split(",") if item.strip()]
    if not devices:
        parser.error("--devices must include at least one device")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc must be exactly 4 characters")

    result = run_live_benchmark(args, devices)
    print_result(result)
    return 0


def run_live_benchmark(args: argparse.Namespace, devices: list[str]) -> LiveBenchmarkResult:
    config = load_config(args.config).model_copy(deep=True)
    width = args.width or config.simulation.image_width
    height = args.height or config.simulation.image_height
    fps = args.fps or config.simulation.timestep_hz
    patch_size = args.stress_patch_size or config.simulation.patch_size_px
    config.simulation.image_width = width
    config.simulation.image_height = height
    config.simulation.timestep_hz = fps
    config.simulation.frames = max(args.frames + args.warmup_frames, 2)
    config.simulation.patch_size_px = patch_size
    align_window_ns = (
        config.fusion.align_window_ns
        if args.align_window_ms is None
        else int(max(args.align_window_ms, 0.0) * 1_000_000)
    )

    grid = VoxelGrid.from_config(config.rayweave.grid)
    scene = build_scene(config.simulation)
    stress_frames = (
        SyntheticPacketGenerator(scene, config.simulation).frames()
        if args.rayweave_input == "stress-patches"
        else []
    )
    aligner = TimeAligner(align_window_ns, config.fusion.min_cameras_per_frame)
    scorer = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)
    peak_extractor = PeakExtractor(grid, config.rayweave.peaks)
    tracks = TrackManager(config.kalman)

    sources = _open_sources(devices, width, height, fps, args.fourcc)
    motion_config = MotionPacketConfig(backend=args.motion_backend)
    states = {source.camera_id: MotionCameraState() for source in sources}
    stage_ms = {stage: [] for stage in STAGES}
    camera_frames = 0
    read_failures = 0
    aligned_frames = 0
    measurement_frames = 0

    gc_was_enabled = gc.isenabled()
    if not args.enable_gc:
        gc.disable()

    try:
        with ThreadPoolExecutor(max_workers=len(sources)) as executor:
            futures = _submit_motion_jobs(executor, sources, states, motion_config)
            for frame_index in range(args.frames + args.warmup_frames):
                measured = frame_index >= args.warmup_frames
                total_start = time.perf_counter()

                start = time.perf_counter()
                results = [future.result() for future in futures]
                packet_ms = _elapsed_ms(start)
                futures = (
                    _submit_motion_jobs(executor, sources, states, motion_config)
                    if frame_index + 1 < args.frames + args.warmup_frames
                    else []
                )
                packets = [result.packet for result in results if result.packet is not None]
                read_failures += sum(1 for result in results if result.packet is None) if measured else 0
                camera_frames += len(packets) if measured else 0

                if args.rayweave_input == "stress-patches":
                    packets = _with_stress_frame(packets, stress_frames[frame_index % len(stress_frames)])

                start = time.perf_counter()
                aligned = aligner.align_frame(packets)
                alignment_ms = _elapsed_ms(start)

                scoring_ms = 0.0
                peaks_ms = 0.0
                kalman_ms = 0.0
                measurement = None

                if aligned is None:
                    start = time.perf_counter()
                    tracks.update(None, int(time.monotonic_ns()))
                    kalman_ms = _elapsed_ms(start)
                else:
                    aligned_frames += 1 if measured else 0

                    start = time.perf_counter()
                    scored = scorer.score(aligned)
                    scoring_ms = _elapsed_ms(start)

                    start = time.perf_counter()
                    peaks, measurements = peak_extractor.extract(scored)
                    scored.volume.peaks = peaks
                    measurement = measurements[0] if measurements else None
                    peaks_ms = _elapsed_ms(start)

                    start = time.perf_counter()
                    tracks.update(measurement, aligned.ts_ns)
                    kalman_ms = _elapsed_ms(start)

                total_ms = _elapsed_ms(total_start)
                if measured:
                    stage_ms["camera_packets"].append(packet_ms)
                    stage_ms["alignment"].append(alignment_ms)
                    stage_ms["scoring"].append(scoring_ms)
                    stage_ms["peaks"].append(peaks_ms)
                    stage_ms["kalman"].append(kalman_ms)
                    stage_ms["total"].append(total_ms)
                    measurement_frames += 1 if measurement is not None else 0
                    if args.console_every and (frame_index - args.warmup_frames) % args.console_every == 0:
                        print(
                            f"frame={frame_index - args.warmup_frames:04d} packets={len(packets)} "
                            f"aligned={aligned is not None} total={total_ms:.3f}ms scoring={scoring_ms:.3f}ms"
                        )
    finally:
        if gc_was_enabled and not args.enable_gc:
            gc.enable()
        for source in sources:
            source.close()

    return LiveBenchmarkResult(
        config_path=str(args.config),
        frames=args.frames,
        warmup=args.warmup_frames,
        camera_frames=camera_frames,
        read_failures=read_failures,
        aligned_frames=aligned_frames,
        measurement_frames=measurement_frames,
        rayweave_input=args.rayweave_input,
        stage_ms=stage_ms,
    )


def print_result(result: LiveBenchmarkResult) -> None:
    total = result.stage_ms["total"]
    print(
        "live_benchmark_run "
        f"config={result.config_path} frames={result.frames} warmup={result.warmup} "
        f"input={result.rayweave_input} fps_p50={result.fps_p50:.2f} "
        f"total_p50={_percentile(total, 50.0):.2f}ms total_p95={_percentile(total, 95.0):.2f}ms "
        f"camera_frames={result.camera_frames} read_failures={result.read_failures} "
        f"aligned={result.aligned_frames} measurements={result.measurement_frames}"
    )
    print("stage,p50_ms,p95_ms,p99_ms,total_ms,share_pct")
    total_sum = sum(total)
    for stage in STAGES:
        values = result.stage_ms[stage]
        stage_sum = sum(values)
        share = 100.0 * stage_sum / total_sum if total_sum > 0.0 else 0.0
        print(
            f"{stage},{_percentile(values, 50.0):.3f},{_percentile(values, 95.0):.3f},"
            f"{_percentile(values, 99.0):.3f},{stage_sum:.3f},{share:.1f}"
        )


def _open_sources(devices: list[str], width: int, height: int, fps: float, fourcc: str | None) -> list[OpenCVCameraSource]:
    sources: list[OpenCVCameraSource] = []
    for camera_id, device in enumerate(devices):
        source = OpenCVCameraSource(
            camera_id=camera_id,
            device=device,
            width=width,
            height=height,
            fps=fps,
            fourcc=fourcc,
        )
        try:
            source.open()
            settings = source.effective_settings()
        except CameraOpenError:
            for opened in sources:
                opened.close()
            raise
        print(
            "camera_opened "
            f"camera_id={camera_id} device={device} "
            f"effective_size={settings['width']:.0f}x{settings['height']:.0f} "
            f"effective_fps={settings['fps']:.2f}"
        )
        sources.append(source)
    return sources


def _submit_motion_jobs(
    executor: ThreadPoolExecutor,
    sources: list[OpenCVCameraSource],
    states: dict[int, MotionCameraState],
    motion_config: MotionPacketConfig,
):
    return [
        executor.submit(_process_motion_source, source, states[source.camera_id], motion_config)
        for source in sources
    ]


def _with_stress_frame(live_packets: list[MotionPacket], stress_frame: SyntheticFrame) -> list[MotionPacket]:
    if not live_packets:
        return []
    capture_ts_ns = int(round(sum(packet.header.capture_ts_ns for packet in live_packets) / len(live_packets)))
    stress_by_camera = {packet.camera_id: packet for packet in stress_frame.motion_packets}
    return [
        _with_stress_evidence(packet, stress_by_camera[packet.camera_id], capture_ts_ns)
        for packet in live_packets
        if packet.camera_id in stress_by_camera
    ]


def _with_stress_evidence(live_packet: MotionPacket, stress_packet: MotionPacket, capture_ts_ns: int) -> MotionPacket:
    header = live_packet.header.model_copy(
        update={
            "capture_ts_ns": capture_ts_ns,
            "publish_ts_ns": max(live_packet.header.publish_ts_ns, capture_ts_ns),
        }
    )
    return live_packet.model_copy(
        update={
            "header": header,
            "blobs": stress_packet.blobs,
            "motion_patches": stress_packet.motion_patches,
            "detector": f"{live_packet.detector}+stress_synthetic",
        }
    )


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


if __name__ == "__main__":
    sys.exit(main())
