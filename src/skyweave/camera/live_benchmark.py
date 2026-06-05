from __future__ import annotations

import gc
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from skyweave.camera.check_common import MotionCameraState, _percentile
from skyweave.camera.check_parallel import _process_motion_source
from skyweave.camera.motion import DEFAULT_OPTIMIZED_MOTION_BACKEND, MotionPacketConfig
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource
from skyweave.config import load_config
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.kalman import TrackManager
from skyweave.messages import MotionPacket
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.sim.generator import SyntheticFrame, SyntheticPacketGenerator
from skyweave.sim.scene import build_scene

DEFAULT_LIVE_BENCHMARK_CONFIG = "configs/sim_mvp_ov9281_100hz_numba.yaml"
DEFAULT_LIVE_BENCHMARK_FOURCC = "MJPG"
DEFAULT_LIVE_BENCHMARK_FRAMES = 300
DEFAULT_LIVE_BENCHMARK_WARMUP_FRAMES = 30
STAGES = ("camera_packets", "alignment", "scoring", "peaks", "kalman", "total")


@dataclass(frozen=True)
class LiveBenchmarkOptions:
    config_path: str = DEFAULT_LIVE_BENCHMARK_CONFIG
    devices: list[str] = field(default_factory=list)
    frames: int = DEFAULT_LIVE_BENCHMARK_FRAMES
    warmup_frames: int = DEFAULT_LIVE_BENCHMARK_WARMUP_FRAMES
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    fourcc: str | None = DEFAULT_LIVE_BENCHMARK_FOURCC
    motion_backend: str = DEFAULT_OPTIMIZED_MOTION_BACKEND
    align_window_ms: float | None = None
    rayweave_input: str = "stress-patches"
    stress_patch_size: int | None = None
    console_every: int = 0
    enable_gc: bool = False


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


def run_live_benchmark(options: LiveBenchmarkOptions) -> LiveBenchmarkResult:
    config = load_config(options.config_path).model_copy(deep=True)
    width = options.width or config.simulation.image_width
    height = options.height or config.simulation.image_height
    fps = options.fps or config.simulation.timestep_hz
    patch_size = options.stress_patch_size or config.simulation.patch_size_px
    config.simulation.image_width = width
    config.simulation.image_height = height
    config.simulation.timestep_hz = fps
    config.simulation.frames = max(options.frames + options.warmup_frames, 2)
    config.simulation.patch_size_px = patch_size
    align_window_ns = (
        config.fusion.align_window_ns
        if options.align_window_ms is None
        else int(max(options.align_window_ms, 0.0) * 1_000_000)
    )

    grid = VoxelGrid.from_config(config.rayweave.grid)
    scene = build_scene(config.simulation)
    stress_frames = (
        SyntheticPacketGenerator(scene, config.simulation).frames()
        if options.rayweave_input == "stress-patches"
        else []
    )
    aligner = TimeAligner(align_window_ns, config.fusion.min_cameras_per_frame)
    scorer = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)
    peak_extractor = PeakExtractor(grid, config.rayweave.peaks)
    tracks = TrackManager(config.kalman)

    sources = _open_sources(options.devices, width, height, fps, options.fourcc)
    motion_config = MotionPacketConfig(backend=options.motion_backend)
    states = {source.camera_id: MotionCameraState() for source in sources}
    stage_ms = {stage: [] for stage in STAGES}
    camera_frames = 0
    read_failures = 0
    aligned_frames = 0
    measurement_frames = 0

    gc_was_enabled = gc.isenabled()
    if not options.enable_gc:
        gc.disable()

    try:
        with ThreadPoolExecutor(max_workers=len(sources)) as executor:
            futures = _submit_motion_jobs(executor, sources, states, motion_config)
            for frame_index in range(options.frames + options.warmup_frames):
                measured = frame_index >= options.warmup_frames
                total_start = time.perf_counter()

                start = time.perf_counter()
                results = [future.result() for future in futures]
                packet_ms = _elapsed_ms(start)
                futures = (
                    _submit_motion_jobs(executor, sources, states, motion_config)
                    if frame_index + 1 < options.frames + options.warmup_frames
                    else []
                )
                packets = [result.packet for result in results if result.packet is not None]
                read_failures += sum(1 for result in results if result.packet is None) if measured else 0
                camera_frames += len(packets) if measured else 0

                if options.rayweave_input == "stress-patches":
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
                    if options.console_every and (frame_index - options.warmup_frames) % options.console_every == 0:
                        print(
                            f"frame={frame_index - options.warmup_frames:04d} packets={len(packets)} "
                            f"aligned={aligned is not None} total={total_ms:.3f}ms scoring={scoring_ms:.3f}ms"
                        )
    finally:
        if gc_was_enabled and not options.enable_gc:
            gc.enable()
        for source in sources:
            source.close()

    return LiveBenchmarkResult(
        config_path=options.config_path,
        frames=options.frames,
        warmup=options.warmup_frames,
        camera_frames=camera_frames,
        read_failures=read_failures,
        aligned_frames=aligned_frames,
        measurement_frames=measurement_frames,
        rayweave_input=options.rayweave_input,
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
