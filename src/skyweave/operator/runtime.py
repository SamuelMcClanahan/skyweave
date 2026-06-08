from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from skyweave.calibration.charuco import CharucoBoardSpec, detect_charuco, draw_annotated_detection
from skyweave.calibration.charuco_live_capture import _resize_gray, _sharpness_score
from skyweave.calibration.charuco_live_state import (
    LiveTrackTelemetry,
    _fps_from_times,
    _is_running,
    _record_read_failure,
    _requested_index,
    _select_camera,
)
from skyweave.camera.check_common import MotionCameraState, _read_live_frames
from skyweave.camera.live_benchmark import DEFAULT_LIVE_BENCHMARK_CONFIG, _with_stress_frame
from skyweave.camera.motion import FrameDiffMotionPacketBuilder
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource
from skyweave.config import SimCheckConfig, load_config
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.kalman import TrackManager
from skyweave.messages import Measurement3D, MotionPacket, Track, WeavefieldVolume
from skyweave.operator.calibration import load_extrinsic_camera_calibs, scale_camera_calibs
from skyweave.operator.state import OperatorState, PipelineStatus
from skyweave.operator.viz import track_telemetry, viz_camera
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.sim.generator import SyntheticFrame, SyntheticPacketGenerator
from skyweave.sim.rendered import RenderedFrame, RenderedFrameGenerator
from skyweave.sim.scene import build_scene
from skyweave.timestamps import monotonic_ns


@dataclass(frozen=True)
class OperatorRuntimeOptions:
    config_path: str = DEFAULT_LIVE_BENCHMARK_CONFIG
    extrinsics_path: str = "configs/extrinsics.yaml"
    target_hz: float = 30.0


@dataclass
class _Pipeline:
    config: SimCheckConfig
    motion_states: dict[int, MotionCameraState]
    grid: VoxelGrid
    aligner: TimeAligner
    scorer: RayweaveScorer
    peak_extractor: PeakExtractor
    tracks: TrackManager
    cameras: dict[int, Any]
    viz_cameras: list[dict[str, Any]]
    stress_frames: list[SyntheticFrame]
    rendered_frames: list[RenderedFrame]
    effective_mode: str
    reason: str
    truth_error_history: deque[tuple[float, float, float]]


class OperatorRuntime:
    def __init__(
        self,
        state: OperatorState,
        board: CharucoBoardSpec,
        options: OperatorRuntimeOptions | None = None,
    ) -> None:
        self.state = state
        self.board = board
        self.options = options or OperatorRuntimeOptions(
            config_path=state.config_path,
            extrinsics_path=state.extrinsics_path,
        )
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="skyweave-operator-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.state.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError:
            self.state.set_runtime_status("failed", "OpenCV is required. Install with: python -m pip install -e '.[camera,viz]'")
            self.state.stop()
            return

        sources: list[OpenCVCameraSource] = []
        pipeline: _Pipeline | None = None
        settings_revision = -1
        tracking_revision = -1
        camera_signature: tuple[int, int, float, str, int] | None = None
        frame_times: dict[int, list[float]] = {idx: [] for idx in range(len(self.state.live.cameras))}
        active_specs = [self.board for _ in self.state.live.cameras]
        cached_detections = [("none", 0, 0) for _ in self.state.live.cameras]
        frame_seq = 0

        try:
            while _is_running(self.state.live):
                settings, revision = self.state.live.settings_snapshot()
                _requested_mode, next_tracking_revision = self.state.tracking_snapshot()
                next_signature = settings.camera.capture_signature()
                if revision != settings_revision or next_tracking_revision != tracking_revision:
                    settings_revision = revision
                    tracking_revision = next_tracking_revision
                    pipeline = self._build_pipeline(settings)
                    frame_times = {idx: [] for idx in pipeline.cameras}
                    active_specs = [self.board for _ in range(len(self.state.live.cameras))]
                    cached_detections = [("none", 0, 0) for _ in range(len(self.state.live.cameras))]

                if pipeline is not None and pipeline.effective_mode in {"stress", "rendered"}:
                    if sources:
                        for source in sources:
                            source.close()
                        sources = []
                    loop_start = time.perf_counter()
                    self._apply_camera_selection()
                    synthetic_start = time.perf_counter()
                    if pipeline.effective_mode == "rendered":
                        packets, rendered_frame, truth_position = self._rendered_packets(pipeline, settings, frame_seq)
                    else:
                        packets, truth_position = self._synthetic_packets(pipeline, frame_seq)
                        rendered_frame = None
                    camera_read_ms = _elapsed_ms(synthetic_start)
                    preview_start = time.perf_counter()
                    if rendered_frame is not None:
                        self._update_rendered_preview(cv2, rendered_frame, packets, settings, frame_times, frame_seq)
                    else:
                        self._update_synthetic_preview(cv2, packets, settings, frame_times, frame_seq)
                    preview_ms = _elapsed_ms(preview_start)
                    self._run_fusion_frame(
                        pipeline,
                        packets,
                        frame_seq,
                        loop_start,
                        {
                            "camera_read_ms": camera_read_ms,
                            "motion_ms": camera_read_ms if pipeline.effective_mode == "rendered" else 0.0,
                            "preview_ms": preview_ms,
                        },
                        truth_position=truth_position,
                    )
                    frame_seq += 1
                    elapsed = time.perf_counter() - loop_start
                    target = 1.0 / self.options.target_hz if self.options.target_hz > 0.0 else 0.0
                    if target > elapsed:
                        time.sleep(target - elapsed)
                    continue

                if not sources or camera_signature != next_signature:
                    for source in sources:
                        source.close()
                    sources = self._open_sources(settings.camera)
                    camera_signature = next_signature if sources else None
                    for item in (pipeline.motion_states.values() if pipeline else []):
                        item.builder = None
                        item.previous_frame = None

                if not sources:
                    time.sleep(0.5)
                    continue

                loop_start = time.perf_counter()
                self._apply_camera_selection()
                read_start = time.perf_counter()
                reads = _read_live_frames(sources)
                camera_read_ms = _elapsed_ms(read_start)
                motion_start = time.perf_counter()
                packets = self._motion_packets(reads, pipeline, settings)
                motion_ms = _elapsed_ms(motion_start)
                preview_start = time.perf_counter()
                self._update_camera_preview(
                    cv2,
                    reads,
                    packets,
                    settings,
                    active_specs,
                    cached_detections,
                    frame_times,
                )
                preview_ms = _elapsed_ms(preview_start)
                self._run_fusion_frame(
                    pipeline,
                    packets,
                    frame_seq,
                    loop_start,
                    {
                        "camera_read_ms": camera_read_ms,
                        "motion_ms": motion_ms,
                        "preview_ms": preview_ms,
                    },
                )
                frame_seq += 1

                elapsed = time.perf_counter() - loop_start
                target = 1.0 / self.options.target_hz if self.options.target_hz > 0.0 else 0.0
                if target > elapsed:
                    time.sleep(target - elapsed)
        except Exception as exc:
            self.state.set_runtime_status("failed", str(exc))
            self.state.live.update_error(str(exc))
        finally:
            for source in sources:
                source.close()
            self.state.set_runtime_status("stopped" if self.state.runtime_error is None else "failed", self.state.runtime_error)

    def _apply_camera_selection(self) -> None:
        requested_index = _requested_index(self.state.live)
        with self.state.live.lock:
            selected_index = self.state.live.selected_index
            camera_count = len(self.state.live.cameras)
        if 0 <= requested_index < camera_count and requested_index != selected_index:
            _select_camera(self.state.live, requested_index)

    def _build_pipeline(self, settings) -> _Pipeline:
        config = load_config(self.state.config_path).model_copy(deep=True)
        config.simulation.image_width = settings.camera.width
        config.simulation.image_height = settings.camera.height
        config.simulation.timestep_hz = settings.camera.fps
        config.kalman = settings.kalman.to_kalman_config()
        with self.state.lock:
            config.simulation.scene = self.state.simulation_scene
            _apply_scene_preset(config)
            config.fusion.min_cameras_per_frame = self.state.fusion.min_cameras_per_frame
            config.fusion.pixel_noise_px = self.state.fusion.pixel_noise_px
            config.rayweave.scorer.min_supporting_cameras = self.state.rayweave.scorer.min_supporting_cameras
            config.rayweave.scorer.evidence_mode = self.state.rayweave.scorer.evidence_mode
            if self.state.rayweave.scorer.top_k_voxels is not None:
                config.rayweave.scorer.top_k_voxels = self.state.rayweave.scorer.top_k_voxels
            config.rayweave.peaks.threshold_percentile = self.state.rayweave.peaks.threshold_percentile
            config.rayweave.peaks.max_peaks = self.state.rayweave.peaks.max_peaks
            config.rayweave.peaks.soft_argmax_radius_voxels = self.state.rayweave.peaks.soft_argmax_radius_voxels
            config.rayweave.peaks.soft_argmax_beta = self.state.rayweave.peaks.soft_argmax_beta

        requested_mode = self.state.requested_mode
        extrinsic_cameras, calibration = load_extrinsic_camera_calibs(self.state.extrinsics_path)
        self.state.set_calibration(calibration)

        if requested_mode in {"auto", "real"} and extrinsic_cameras:
            self.state.live.configure_cameras(list(self.state.devices))
            cameras = scale_camera_calibs(extrinsic_cameras, settings.camera.width, settings.camera.height)
            effective_mode = "real"
            reason = "loaded calibrated extrinsics"
            stress_frames: list[SyntheticFrame] = []
            rendered_frames: list[RenderedFrame] = []
        else:
            scene = build_scene(config.simulation)
            cameras = scene.cameras
            if requested_mode == "rendered":
                stress_frames = []
                rendered_frames = RenderedFrameGenerator(scene, config.simulation).frames()
                self.state.live.ensure_camera_count(len(cameras), device_prefix="rendered://cam")
                effective_mode = "rendered"
                reason = "rendered synthetic frames selected"
            else:
                stress_frames = SyntheticPacketGenerator(scene, config.simulation).frames()
                rendered_frames = []
                self.state.live.ensure_camera_count(len(cameras))
                effective_mode = "stress"
            if requested_mode == "real":
                reason = calibration.message
            elif requested_mode == "auto":
                reason = f"auto fallback: {calibration.message}"
            elif requested_mode == "stress":
                reason = "stress mode selected"

        grid = VoxelGrid.from_config(config.rayweave.grid)
        try:
            scorer = RayweaveScorer(grid, cameras, config.rayweave.scorer)
        except ImportError:
            config.rayweave.scorer.backend = "python_numpy"
            scorer = RayweaveScorer(grid, cameras, config.rayweave.scorer)
            reason = f"{reason}; scorer fallback python_numpy"

        pipeline = _Pipeline(
            config=config,
            motion_states={idx: MotionCameraState() for idx in cameras},
            grid=grid,
            aligner=TimeAligner(config.fusion.align_window_ns, config.fusion.min_cameras_per_frame),
            scorer=scorer,
            peak_extractor=PeakExtractor(grid, config.rayweave.peaks),
            tracks=TrackManager(config.kalman),
            cameras=cameras,
            viz_cameras=[viz_camera(camera, settings.camera.fps, online=True) for camera in cameras.values()],
            stress_frames=stress_frames,
            rendered_frames=rendered_frames,
            effective_mode=effective_mode,
            reason=reason,
            truth_error_history=deque(maxlen=max(30, int(settings.camera.fps * 10))),
        )
        self.state.set_pipeline(PipelineStatus(mode=effective_mode, reason=reason))
        self.state.set_runtime_status("running")
        return pipeline

    def _synthetic_packets(self, pipeline: _Pipeline, frame_seq: int) -> tuple[list[MotionPacket], tuple[float, float, float] | None]:
        if not pipeline.stress_frames:
            return [], None
        frame_index = frame_seq % len(pipeline.stress_frames)
        if frame_seq > 0 and frame_index == 0:
            _reset_synthetic_loop(pipeline)
        stress_frame = pipeline.stress_frames[frame_index]
        capture_ts_ns = monotonic_ns()
        packets = [
            packet.model_copy(
                update={
                    "header": packet.header.model_copy(
                        update={
                            "frame_seq": frame_seq,
                            "capture_ts_ns": capture_ts_ns,
                            "publish_ts_ns": capture_ts_ns,
                        }
                    )
                }
            )
            for packet in stress_frame.motion_packets
        ]
        truth_position = tuple(float(value) for value in stress_frame.truth.position)
        return packets, truth_position

    def _rendered_packets(
        self,
        pipeline: _Pipeline,
        settings,
        frame_seq: int,
    ) -> tuple[list[MotionPacket], RenderedFrame | None, tuple[float, float, float] | None]:
        if not pipeline.rendered_frames:
            return [], None, None
        frame_index = frame_seq % len(pipeline.rendered_frames)
        if frame_seq > 0 and frame_index == 0:
            _reset_synthetic_loop(pipeline)
        rendered_frame = pipeline.rendered_frames[frame_index]
        capture_ts_ns = monotonic_ns()
        motion_config = settings.motion.to_motion_config()
        packets: list[MotionPacket] = []
        for frame in rendered_frame.camera_frames:
            state = pipeline.motion_states[frame.camera_id]
            builder = state.builder
            if (
                builder is None
                or builder.image_width != frame.image_width
                or builder.image_height != frame.image_height
                or builder.config != motion_config
            ):
                builder = FrameDiffMotionPacketBuilder(
                    frame.camera_id,
                    frame.image_width,
                    frame.image_height,
                    config=motion_config,
                    source_id=f"rendered_cam{frame.camera_id}",
                )
                state.builder = builder
                state.previous_frame = None
            packet = builder.build(state.previous_frame, frame.gray, frame_seq, capture_ts_ns, publish_ts_ns=capture_ts_ns)
            state.previous_frame = frame.gray
            packets.append(packet)
        truth_position = tuple(float(value) for value in rendered_frame.truth.position)
        return packets, rendered_frame, truth_position

    def _open_sources(self, camera_settings) -> list[OpenCVCameraSource]:
        sources: list[OpenCVCameraSource] = []
        for camera_id, device in enumerate(self.state.devices):
            source = OpenCVCameraSource(
                camera_id=camera_id,
                device=device,
                width=camera_settings.width,
                height=camera_settings.height,
                fps=camera_settings.fps,
                fourcc=camera_settings.fourcc,
            )
            try:
                source.open()
            except CameraOpenError as exc:
                self.state.live.set_camera_status(camera_id, "failed", str(exc))
                for opened in sources:
                    opened.close()
                self.state.set_runtime_status("camera_failed", str(exc))
                return []
            self.state.live.set_camera_status(camera_id, "running")
            sources.append(source)
        for _ in range(max(camera_settings.warmup_frames, 0)):
            for source in sources:
                source.read()
        return sources

    def _motion_packets(self, reads, pipeline: _Pipeline | None, settings) -> list[MotionPacket]:
        if pipeline is None:
            return []
        packets = []
        motion_config = settings.motion.to_motion_config()
        for read in reads:
            frame = read.frame
            state = pipeline.motion_states[read.source.camera_id]
            if frame is None:
                _record_read_failure(self.state.live, read.source.camera_id)
                continue
            builder = state.builder
            if (
                builder is None
                or builder.image_width != frame.image_width
                or builder.image_height != frame.image_height
                or builder.config != motion_config
            ):
                builder = FrameDiffMotionPacketBuilder(
                    read.source.camera_id,
                    frame.image_width,
                    frame.image_height,
                    config=motion_config,
                    source_id=f"camera{read.source.camera_id}",
                )
                state.builder = builder
                state.previous_frame = None
            packet = builder.build(state.previous_frame, frame.gray, frame.frame_seq, frame.capture_ts_ns)
            packet = packet.model_copy(
                update={"header": packet.header.model_copy(update={"publish_ts_ns": monotonic_ns()})}
            )
            state.previous_frame = frame.gray
            packets.append(packet)
        if pipeline.effective_mode == "stress" and packets and pipeline.stress_frames:
            stress_frame = pipeline.stress_frames[packets[0].header.frame_seq % len(pipeline.stress_frames)]
            return _with_stress_frame(packets, stress_frame)
        return packets

    def _update_camera_preview(
        self,
        cv2,
        reads,
        packets: list[MotionPacket],
        settings,
        active_specs: list[CharucoBoardSpec],
        cached_detections: list[tuple[str, int, int]],
        frame_times: dict[int, list[float]],
    ) -> None:
        with self.state.live.lock:
            selected_index = self.state.live.selected_index
        packets_by_camera = {packet.camera_id: packet for packet in packets}
        preview_stride = max(1, int(round(settings.camera.fps / 8.0)))
        for read in reads:
            frame = read.frame
            camera_id = read.source.camera_id
            if frame is None:
                continue

            start = time.perf_counter()
            should_detect = frame.frame_seq % settings.camera.detect_every == 0
            with self.state.live.lock:
                previous_sharpness = self.state.live.cameras[camera_id].sharpness
                needs_first_preview = self.state.live.frame_jpegs[camera_id] is None
            should_encode_preview = needs_first_preview or frame.frame_seq % preview_stride == 0
            sharpness = previous_sharpness
            annotated = None
            frame_jpeg = None

            if should_detect or should_encode_preview:
                display_gray = _resize_gray(cv2, frame.gray, settings.camera.display_scale)
            else:
                display_gray = None

            if should_encode_preview and display_gray is not None:
                sharpness = _sharpness_score(cv2, display_gray)

            if should_detect and display_gray is not None:
                detection, payload = detect_charuco(display_gray, active_specs[camera_id])
                cached_detections[camera_id] = (detection.dictionary, detection.marker_count, detection.corner_count)
                if detection.corner_count >= settings.camera.min_lock_corners:
                    active_specs[camera_id] = CharucoBoardSpec(
                        squares_x=active_specs[camera_id].squares_x,
                        squares_y=active_specs[camera_id].squares_y,
                        square_length_m=active_specs[camera_id].square_length_m,
                        marker_length_m=active_specs[camera_id].marker_length_m,
                        dictionary=detection.dictionary,
                    )
                if should_encode_preview and camera_id == selected_index:
                    annotated = draw_annotated_detection(display_gray, payload)

            dictionary, marker_count, corner_count = cached_detections[camera_id]
            if should_encode_preview and display_gray is not None:
                if annotated is None:
                    annotated = cv2.cvtColor(display_gray, cv2.COLOR_GRAY2BGR)
                packet = packets_by_camera.get(camera_id)
                if packet is not None:
                    _draw_motion_blobs(cv2, annotated, packet)
                ok, encoded = cv2.imencode(
                    ".jpg",
                    annotated,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.camera.jpeg_quality)],
                )
                frame_jpeg = bytes(encoded) if ok else None
            latency_ms = (time.perf_counter() - start) * 1000.0
            times = frame_times.setdefault(camera_id, [])
            times.append(time.perf_counter())
            if len(times) > 30:
                times.pop(0)
            self.state.live.record_camera_frame(
                camera_index=camera_id,
                frame_seq=frame.frame_seq,
                detection_dictionary=dictionary,
                marker_count=marker_count,
                corner_count=corner_count,
                latency_ms=latency_ms,
                sharpness=sharpness,
                capture_fps=_fps_from_times(times),
                frame_jpeg=frame_jpeg,
            )

    def _update_synthetic_preview(
        self,
        cv2,
        packets: list[MotionPacket],
        settings,
        frame_times: dict[int, list[float]],
        frame_seq: int,
    ) -> None:
        packets_by_camera = {packet.camera_id: packet for packet in packets}
        with self.state.live.lock:
            camera_count = len(self.state.live.cameras)
            needs_first = [self.state.live.frame_jpegs[index] is None for index in range(camera_count)]
        preview_stride = max(1, int(round(settings.camera.fps / 8.0)))
        should_encode = frame_seq % preview_stride == 0
        width = max(1, int(round(settings.camera.width * settings.camera.display_scale)))
        height = max(1, int(round(settings.camera.height * settings.camera.display_scale)))
        for camera_id in range(camera_count):
            start = time.perf_counter()
            packet = packets_by_camera.get(camera_id)
            frame_jpeg = None
            sharpness = 0.0
            if should_encode or needs_first[camera_id]:
                annotated = cv2.cvtColor(
                    _synthetic_preview_gray(width, height, camera_id, frame_seq),
                    cv2.COLOR_GRAY2BGR,
                )
                if packet is not None:
                    _draw_motion_blobs(cv2, annotated, packet)
                sharpness = _sharpness_score(cv2, cv2.cvtColor(annotated, cv2.COLOR_BGR2GRAY))
                ok, encoded = cv2.imencode(
                    ".jpg",
                    annotated,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.camera.jpeg_quality)],
                )
                frame_jpeg = bytes(encoded) if ok else None
            latency_ms = (time.perf_counter() - start) * 1000.0
            times = frame_times.setdefault(camera_id, [])
            times.append(time.perf_counter())
            if len(times) > 30:
                times.pop(0)
            self.state.live.record_camera_frame(
                camera_index=camera_id,
                frame_seq=frame_seq,
                detection_dictionary="synthetic",
                marker_count=0,
                corner_count=0,
                latency_ms=latency_ms,
                sharpness=sharpness,
                capture_fps=_fps_from_times(times),
                frame_jpeg=frame_jpeg,
            )

    def _update_rendered_preview(
        self,
        cv2,
        rendered_frame: RenderedFrame,
        packets: list[MotionPacket],
        settings,
        frame_times: dict[int, list[float]],
        frame_seq: int,
    ) -> None:
        packets_by_camera = {packet.camera_id: packet for packet in packets}
        with self.state.live.lock:
            needs_first = [
                self.state.live.frame_jpegs[index] is None
                for index in range(len(self.state.live.cameras))
            ]
        preview_stride = max(1, int(round(settings.camera.fps / 8.0)))
        should_encode = frame_seq % preview_stride == 0
        for camera_frame in rendered_frame.camera_frames:
            camera_id = camera_frame.camera_id
            start = time.perf_counter()
            packet = packets_by_camera.get(camera_id)
            meta = rendered_frame.camera_meta.get(camera_id)
            frame_jpeg = None
            sharpness = 0.0
            if should_encode or (camera_id < len(needs_first) and needs_first[camera_id]):
                display_gray = _resize_gray(cv2, camera_frame.gray, settings.camera.display_scale)
                annotated = cv2.cvtColor(display_gray, cv2.COLOR_GRAY2BGR)
                if meta is not None and meta.projected is not None:
                    sx = annotated.shape[1] / max(float(camera_frame.image_width), 1.0)
                    sy = annotated.shape[0] / max(float(camera_frame.image_height), 1.0)
                    center = (int(round(meta.projected[0] * sx)), int(round(meta.projected[1] * sy)))
                    cv2.circle(annotated, center, 5, (80, 255, 120), 1)
                    cv2.putText(
                        annotated,
                        "truth",
                        (center[0] + 6, max(12, center[1] - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.35,
                        (80, 255, 120),
                        1,
                        cv2.LINE_AA,
                    )
                if packet is not None:
                    _draw_motion_blobs(cv2, annotated, packet)
                label = "visible" if meta is not None and meta.visible else "not visible"
                if meta is not None and meta.visible and meta.depth_m is not None:
                    label = f"{label} r={meta.radius_px:.1f}px z={meta.depth_m:.1f}m"
                cv2.putText(
                    annotated,
                    f"cam{camera_id} {label} thr={settings.motion.threshold}",
                    (8, 16),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.42,
                    (220, 235, 245),
                    1,
                    cv2.LINE_AA,
                )
                sharpness = _sharpness_score(cv2, cv2.cvtColor(annotated, cv2.COLOR_BGR2GRAY))
                ok, encoded = cv2.imencode(
                    ".jpg",
                    annotated,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.camera.jpeg_quality)],
                )
                frame_jpeg = bytes(encoded) if ok else None
            latency_ms = (time.perf_counter() - start) * 1000.0
            times = frame_times.setdefault(camera_id, [])
            times.append(time.perf_counter())
            if len(times) > 30:
                times.pop(0)
            self.state.live.record_camera_frame(
                camera_index=camera_id,
                frame_seq=frame_seq,
                detection_dictionary="rendered",
                marker_count=len(packet.blobs) if packet is not None else 0,
                corner_count=len(packet.motion_patches) if packet is not None else 0,
                latency_ms=latency_ms,
                sharpness=sharpness,
                capture_fps=_fps_from_times(times),
                frame_jpeg=frame_jpeg,
            )

    def _run_fusion_frame(
        self,
        pipeline: _Pipeline | None,
        packets: list[MotionPacket],
        frame_seq: int,
        loop_start: float,
        stage_ms: dict[str, float],
        truth_position: tuple[float, float, float] | None = None,
    ) -> None:
        if pipeline is None:
            return
        start = time.perf_counter()
        aligned = pipeline.aligner.align_frame(packets)
        alignment_ms = _elapsed_ms(start)
        volume: WeavefieldVolume | None = None
        measurements: list[Measurement3D] = []
        track: Track | None = None
        scoring_ms = peaks_ms = kalman_ms = 0.0
        if aligned is None:
            start = time.perf_counter()
            track = pipeline.tracks.update(None, monotonic_ns())
            kalman_ms = _elapsed_ms(start)
            ts_ns = monotonic_ns()
        else:
            ts_ns = aligned.ts_ns
            start = time.perf_counter()
            scored = pipeline.scorer.score(aligned)
            scoring_ms = _elapsed_ms(start)
            start = time.perf_counter()
            peaks, measurements = pipeline.peak_extractor.extract(scored)
            scored.volume.peaks = peaks
            volume = scored.volume
            peaks_ms = _elapsed_ms(start)
            start = time.perf_counter()
            track = pipeline.tracks.update(measurements, aligned.ts_ns)
            kalman_ms = _elapsed_ms(start)

        track_payloads = [track.model_dump(mode="json")] if track else []
        truth_metrics = _update_truth_metrics(pipeline, track, truth_position)
        measurement_payloads = [measurement.model_dump(mode="json") for measurement in measurements]
        volume_payloads = [volume.model_dump(mode="json")] if volume else []
        total_ms = _elapsed_ms(loop_start)
        target_ms = 1000.0 / self.options.target_hz if self.options.target_hz > 0.0 else 0.0
        status = PipelineStatus(
            mode=pipeline.effective_mode,
            reason=pipeline.reason,
            frame_seq=frame_seq,
            aligned=aligned is not None,
            packet_count=len(packets),
            blob_count=sum(len(packet.blobs) for packet in packets),
            patch_count=sum(len(packet.motion_patches) for packet in packets),
            measurement_count=len(measurements),
            track_count=len(track_payloads),
            camera_read_ms=stage_ms.get("camera_read_ms", 0.0),
            motion_ms=stage_ms.get("motion_ms", 0.0),
            preview_ms=stage_ms.get("preview_ms", 0.0),
            alignment_ms=alignment_ms,
            scoring_ms=scoring_ms,
            peaks_ms=peaks_ms,
            kalman_ms=kalman_ms,
            total_ms=total_ms,
            target_sleep_ms=max(0.0, target_ms - total_ms),
            truth_error_m=truth_metrics["truth_error_m"],
            track_rmse_m=truth_metrics["track_rmse_m"],
            track_axis_rmse_m=truth_metrics["track_axis_rmse_m"],
            truth_error_count=truth_metrics["truth_error_count"],
        )
        self.state.set_pipeline(status)
        telemetry = track_telemetry(track, measurements[0].ts_ns if measurements else None)
        self.state.live.update_track(LiveTrackTelemetry(**telemetry))
        frame = {
            "frame_seq": frame_seq,
            "ts_ns": ts_ns,
            "tracks": track_payloads,
            "cameras": pipeline.viz_cameras,
            "measurements": measurement_payloads,
            "weavefield_history": volume_payloads,
            "truth_position": truth_position,
            "stats": {
                "fps": self.state.live.capture_fps,
                "latency_ms": total_ms,
                "n_tracks": len(track_payloads),
                "n_cameras": len(pipeline.viz_cameras),
                "n_voxels": len(volume.voxels) if volume else 0,
                "mode": pipeline.effective_mode,
                "scene": pipeline.config.simulation.scene,
                "room_revision": self.state.room.revision,
                "truth_error_m": truth_metrics["truth_error_m"],
                "track_rmse_m": truth_metrics["track_rmse_m"],
                "track_error_x_rmse_m": truth_metrics["track_axis_rmse_m"][0] if truth_metrics["track_axis_rmse_m"] else None,
                "track_error_y_rmse_m": truth_metrics["track_axis_rmse_m"][1] if truth_metrics["track_axis_rmse_m"] else None,
                "track_error_z_rmse_m": truth_metrics["track_axis_rmse_m"][2] if truth_metrics["track_axis_rmse_m"] else None,
                "truth_error_count": truth_metrics["truth_error_count"],
            },
            "room": self.state.room.to_dict(),
        }
        self.state.set_viz_frame(frame)


def _apply_scene_preset(config) -> None:
    scene = config.simulation.scene
    if scene == "pixel_plane_crossing":
        config.simulation.camera_count = 7
        config.simulation.camera_layout = "dispersed_perimeter"
        config.simulation.room_size_m = (42.0, 24.0, 14.0)
        config.simulation.camera_height_m = 1.6
        config.simulation.camera_target_m = (0.0, 0.0, 8.8)
        config.simulation.camera_margin_m = 1.5
        config.simulation.patch_size_px = 3
        config.simulation.render_background_intensity = 28
        config.simulation.render_object_intensity = 245
        config.simulation.render_object_radius_m = 0.035
        config.simulation.render_object_shape = "disk"
        config.simulation.render_noise_std = 1.5
        config.simulation.render_blur_px = 0
        config.simulation.render_trail_alpha = 0.0
        config.rayweave.grid.origin_m = (-12.0, -7.0, 6.0)
        config.rayweave.grid.dims = (48, 28, 16)
        config.rayweave.grid.voxel_size_m = 0.50
        return

    if config.simulation.camera_layout == "dispersed_perimeter":
        config.simulation.camera_layout = "room_perimeter"
        config.simulation.room_size_m = (4.4, 4.4, 2.6)
        config.simulation.camera_height_m = 1.15
        config.simulation.camera_target_m = (0.0, 0.35, 1.25)
        config.simulation.camera_margin_m = 0.20
        config.simulation.patch_size_px = 5
        config.simulation.render_background_intensity = 36
        config.simulation.render_object_intensity = 230
        config.simulation.render_object_radius_m = 0.06
        config.simulation.render_object_shape = "disk"
        config.simulation.render_noise_std = 0.0
        config.simulation.render_blur_px = 0
        config.simulation.render_trail_alpha = 0.0
        config.rayweave.grid.origin_m = (-2.0, -2.0, 0.0)
        config.rayweave.grid.dims = (96, 96, 64)
        config.rayweave.grid.voxel_size_m = 0.05


def _update_truth_metrics(
    pipeline: _Pipeline,
    track: Track | None,
    truth_position: tuple[float, float, float] | None,
) -> dict[str, Any]:
    if track is not None and truth_position is not None:
        error = tuple(float(track.state[index]) - float(truth_position[index]) for index in range(3))
        pipeline.truth_error_history.append(error)
    if not pipeline.truth_error_history:
        return {
            "truth_error_m": None,
            "track_rmse_m": None,
            "track_axis_rmse_m": None,
            "truth_error_count": 0,
        }

    errors = list(pipeline.truth_error_history)
    latest = errors[-1]
    current = math.sqrt(sum(value * value for value in latest))
    axis_rmse = [
        math.sqrt(sum(error[axis] * error[axis] for error in errors) / len(errors))
        for axis in range(3)
    ]
    rmse = math.sqrt(sum(sum(value * value for value in error) for error in errors) / len(errors))
    return {
        "truth_error_m": current,
        "track_rmse_m": rmse,
        "track_axis_rmse_m": axis_rmse,
        "truth_error_count": len(errors),
    }


def _reset_synthetic_loop(pipeline: _Pipeline) -> None:
    pipeline.aligner = TimeAligner(pipeline.config.fusion.align_window_ns, pipeline.config.fusion.min_cameras_per_frame)
    pipeline.tracks = TrackManager(pipeline.config.kalman)
    pipeline.truth_error_history.clear()
    for state in pipeline.motion_states.values():
        state.previous_frame = None
        state.builder = None


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _synthetic_preview_gray(width: int, height: int, camera_id: int, frame_seq: int):
    import numpy as np

    frame = np.zeros((height, width), dtype=np.uint8)
    frame[:, :] = 8 + (camera_id * 7) % 22
    grid_step = max(8, min(width, height) // 8)
    frame[::grid_step, :] = 24
    frame[:, ::grid_step] = 24
    y = 6 + (camera_id * 13) % max(height - 12, 1)
    x0 = (frame_seq * 3 + camera_id * 17) % max(width, 1)
    x1 = min(width, x0 + max(6, width // 18))
    frame[max(0, y - 1) : min(height, y + 2), x0:x1] = 42
    return frame


def _draw_motion_blobs(cv2, image, packet: MotionPacket) -> None:
    if not packet.blobs:
        return
    sx = image.shape[1] / max(float(packet.image_width), 1.0)
    sy = image.shape[0] / max(float(packet.image_height), 1.0)
    for blob in packet.blobs:
        x0 = int(round(blob.bbox_x * sx))
        y0 = int(round(blob.bbox_y * sy))
        x1 = int(round((blob.bbox_x + blob.bbox_w) * sx))
        y1 = int(round((blob.bbox_y + blob.bbox_h) * sy))
        cv2.rectangle(image, (x0, y0), (x1, y1), (60, 235, 255), 2)
        cv2.circle(image, (int(round(blob.cx * sx)), int(round(blob.cy * sy))), 3, (60, 235, 255), -1)
