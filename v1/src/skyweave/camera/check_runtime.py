from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from skyweave.camera.check_common import (
    EncodedFrameStats,
    PacketStats,
    _decode_fourcc,
    _fps_from_ms,
    _percentile,
    _print_motion_summary,
    _read_live_frames,
    _should_print,
    _write_jsonl,
    _write_pgm_snapshot,
)
from skyweave.camera.check_jpeg import _run_live_jpeg
from skyweave.camera.check_parallel import _run_live_motion_parallel
from skyweave.camera.motion import FrameDiffMotionPacketBuilder, MotionPacketConfig, synthetic_motion_frames
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource
from skyweave.timestamps import monotonic_ns


def _run_synthetic(args: argparse.Namespace, config: MotionPacketConfig, writer) -> int:
    builder = FrameDiffMotionPacketBuilder(0, args.width, args.height, config=config, source_id="headless_cam0")
    frames = synthetic_motion_frames(args.width, args.height, args.frames, args.square_size)

    packet_latencies: list[float] = []
    total_blobs = 0
    total_motion_pixels = 0
    previous = None
    for frame_seq, frame in enumerate(frames):
        ts_ns = int(round(frame_seq / args.fps * 1_000_000_000))
        start = time.perf_counter()
        packet = builder.build(previous, frame, frame_seq, ts_ns)
        latency_ms = (time.perf_counter() - start) * 1000.0
        previous = frame
        motion_pixels = sum(blob.area_px for blob in packet.blobs)
        total_blobs += len(packet.blobs)
        total_motion_pixels += motion_pixels
        packet_latencies.append(latency_ms)
        event = {
            "frame_seq": frame_seq,
            "ts_ns": ts_ns,
            "n_blobs": len(packet.blobs),
            "n_patches": len(packet.motion_patches),
            "motion_pixels": motion_pixels,
            "latency_ms": latency_ms,
        }
        if _should_print(frame_seq, args.console_every):
            print(
                f"frame={frame_seq:03d} blobs={event['n_blobs']} patches={event['n_patches']} "
                f"motion_pixels={motion_pixels} latency={latency_ms:.3f}ms"
            )
        if writer:
            writer.write(json.dumps(event, sort_keys=True) + "\n")

    print(
        "camera_check "
        f"frames={args.frames} size={args.width}x{args.height} fps={args.fps:.1f} "
        f"total_blobs={total_blobs} avg_motion_pixels={total_motion_pixels / max(args.frames, 1):.1f} "
        f"latency_p50={_percentile(packet_latencies, 50.0):.3f}ms "
        f"latency_p95={_percentile(packet_latencies, 95.0):.3f}ms"
    )
    if args.jsonl:
        print(f"log_path={args.jsonl}")
    return 0

def _run_live(args: argparse.Namespace, config: MotionPacketConfig, devices: list[str], writer) -> int:
    sources: list[OpenCVCameraSource] = []
    stats: dict[int, PacketStats] = {}
    frame_stats: dict[int, EncodedFrameStats] = {}
    snapshot_dir = Path(args.snapshot_dir) if args.snapshot_dir else None
    snapshots_written: set[int] = set()

    for camera_id, device in enumerate(devices):
        source = OpenCVCameraSource(
            camera_id=camera_id,
            device=device,
            width=args.width,
            height=args.height,
            fps=args.fps,
            fourcc=args.fourcc,
        )
        try:
            source.open()
            settings = source.effective_settings()
        except CameraOpenError as exc:
            print(f"camera_open_failed camera_id={camera_id} device={device} error={exc}", file=sys.stderr)
            _write_jsonl(
                writer,
                {
                    "event": "camera_open_failed",
                    "mode": "live",
                    "camera_id": camera_id,
                    "device": device,
                    "error": str(exc),
                },
            )
            continue

        print(
            "camera_opened "
            f"camera_id={camera_id} device={device} "
            f"effective_size={settings['width']:.0f}x{settings['height']:.0f} "
            f"effective_fps={settings['fps']:.2f} "
            f"effective_fourcc={_decode_fourcc(settings['fourcc'])}"
        )
        _write_jsonl(
            writer,
            {
                "event": "camera_opened",
                "mode": "live",
                "camera_id": camera_id,
                "device": device,
                "effective_width": settings["width"],
                "effective_height": settings["height"],
                "effective_fps": settings["fps"],
                "effective_fourcc": _decode_fourcc(settings["fourcc"]),
            },
        )
        sources.append(source)
        stats[camera_id] = PacketStats()
        frame_stats[camera_id] = EncodedFrameStats()

    if not sources:
        print("camera_check_live failed=no_cameras_opened", file=sys.stderr)
        return 2

    for _ in range(max(args.warmup_frames, 0)):
        _read_live_frames(sources)

    loop_latencies_ms: list[float] = []
    try:
        if args.live_pipeline == "jpeg-frame":
            return _run_live_jpeg(args, sources, frame_stats, snapshot_dir, snapshots_written, loop_latencies_ms, writer)
        if args.parallel_cameras:
            return _run_live_motion_parallel(args, config, sources, stats, snapshot_dir, snapshots_written, loop_latencies_ms, writer)

        builders: dict[int, FrameDiffMotionPacketBuilder] = {}
        previous_frames: dict[int, object] = {}
        for _ in range(args.frames):
            loop_start = time.perf_counter()
            for read in _read_live_frames(sources):
                source = read.source
                frame = read.frame
                camera_stats = stats[source.camera_id]
                if frame is None:
                    camera_stats.read_failures += 1
                    if _should_print(camera_stats.read_failures, args.console_every):
                        print(f"camera_read_failed camera_id={source.camera_id} failures={camera_stats.read_failures}")
                    _write_jsonl(
                        writer,
                        {
                            "event": "camera_read_failed",
                            "mode": "live",
                            "camera_id": source.camera_id,
                            "failures": camera_stats.read_failures,
                        },
                    )
                    continue

                if snapshot_dir is not None and source.camera_id not in snapshots_written:
                    snapshot_path = _write_pgm_snapshot(snapshot_dir, frame)
                    snapshots_written.add(source.camera_id)
                    print(f"snapshot_written camera_id={source.camera_id} path={snapshot_path}")
                    _write_jsonl(
                        writer,
                        {
                            "event": "snapshot_written",
                            "mode": "live",
                            "camera_id": source.camera_id,
                            "frame_seq": frame.frame_seq,
                            "path": str(snapshot_path),
                        },
                    )

                builder = builders.get(source.camera_id)
                if builder is None or builder.image_width != frame.image_width or builder.image_height != frame.image_height:
                    builder = FrameDiffMotionPacketBuilder(
                        source.camera_id,
                        frame.image_width,
                        frame.image_height,
                        config=config,
                        source_id=f"camera{source.camera_id}",
                    )
                    builders[source.camera_id] = builder
                    previous_frames[source.camera_id] = None

                start = time.perf_counter()
                packet = builder.build(
                    previous_frames[source.camera_id],
                    frame.gray,
                    frame.frame_seq,
                    frame.capture_ts_ns,
                )
                publish_ts_ns = monotonic_ns()
                packet = packet.model_copy(
                    update={
                        "header": packet.header.model_copy(update={"publish_ts_ns": publish_ts_ns}),
                    }
                )
                build_latency_ms = (time.perf_counter() - start) * 1000.0
                previous_frames[source.camera_id] = frame.gray
                camera_stats.record(
                    frame,
                    packet,
                    build_latency_ms,
                    read.grab_latency_ms,
                    read.retrieve_latency_ms,
                    read.gray_latency_ms,
                )
                motion_pixels = sum(blob.area_px for blob in packet.blobs)
                packet_latency_ms = (packet.header.publish_ts_ns - frame.capture_ts_ns) / 1_000_000.0

                if _should_print(frame.frame_seq, args.console_every):
                    print(
                        f"cam={source.camera_id} frame={frame.frame_seq:04d} "
                        f"size={frame.image_width}x{frame.image_height} "
                        f"blobs={len(packet.blobs)} patches={len(packet.motion_patches)} "
                        f"motion_pixels={motion_pixels} packet_latency={packet_latency_ms:.3f}ms "
                        f"build_latency={build_latency_ms:.3f}ms"
                    )
                _write_jsonl(
                    writer,
                    {
                        "event": "motion_packet",
                        "mode": "live",
                        "camera_id": source.camera_id,
                        "frame_seq": frame.frame_seq,
                        "capture_ts_ns": frame.capture_ts_ns,
                        "publish_ts_ns": packet.header.publish_ts_ns,
                        "image_width": frame.image_width,
                        "image_height": frame.image_height,
                        "n_blobs": len(packet.blobs),
                        "n_patches": len(packet.motion_patches),
                        "motion_pixels": motion_pixels,
                        "packet_latency_ms": packet_latency_ms,
                        "build_latency_ms": build_latency_ms,
                        "grab_latency_ms": read.grab_latency_ms,
                        "retrieve_latency_ms": read.retrieve_latency_ms,
                        "gray_latency_ms": read.gray_latency_ms,
                    },
                )
            loop_latencies_ms.append((time.perf_counter() - loop_start) * 1000.0)
    finally:
        for source in sources:
            source.close()

    if args.profile_stages:
        print(
            "camera_check_live_loop "
            f"frames={args.frames} "
            f"loop_latency_p50={_percentile(loop_latencies_ms, 50.0):.3f}ms "
            f"loop_latency_p95={_percentile(loop_latencies_ms, 95.0):.3f}ms "
            f"loop_fps_p50={_fps_from_ms(_percentile(loop_latencies_ms, 50.0)):.2f}"
        )

    for camera_id in sorted(stats):
        camera_stats = stats[camera_id]
        print(
            "camera_check_live "
            f"camera_id={camera_id} frames={camera_stats.frames} read_failures={camera_stats.read_failures} "
            f"effective_fps={camera_stats.effective_fps:.2f} "
            f"total_blobs={camera_stats.total_blobs} total_patches={camera_stats.total_patches} "
            f"avg_motion_pixels={camera_stats.total_motion_pixels / max(camera_stats.frames, 1):.1f} "
            f"packet_latency_p50={_percentile(camera_stats.packet_latencies_ms, 50.0):.3f}ms "
            f"packet_latency_p95={_percentile(camera_stats.packet_latencies_ms, 95.0):.3f}ms "
            f"build_latency_p50={_percentile(camera_stats.build_latencies_ms, 50.0):.3f}ms "
            f"build_latency_p95={_percentile(camera_stats.build_latencies_ms, 95.0):.3f}ms"
        )
        if args.profile_stages:
            print(
                "camera_check_live_stage "
                f"camera_id={camera_id} "
                f"grab_p50={_percentile(camera_stats.grab_latencies_ms, 50.0):.3f}ms "
                f"grab_p95={_percentile(camera_stats.grab_latencies_ms, 95.0):.3f}ms "
                f"retrieve_p50={_percentile(camera_stats.retrieve_latencies_ms, 50.0):.3f}ms "
                f"retrieve_p95={_percentile(camera_stats.retrieve_latencies_ms, 95.0):.3f}ms "
                f"gray_p50={_percentile(camera_stats.gray_latencies_ms, 50.0):.3f}ms "
                f"gray_p95={_percentile(camera_stats.gray_latencies_ms, 95.0):.3f}ms "
                f"build_p50={_percentile(camera_stats.build_latencies_ms, 50.0):.3f}ms "
                f"build_p95={_percentile(camera_stats.build_latencies_ms, 95.0):.3f}ms"
            )

    if args.jsonl:
        print(f"log_path={args.jsonl}")
    if args.snapshot_dir:
        print(f"snapshot_dir={args.snapshot_dir}")
    return 0
