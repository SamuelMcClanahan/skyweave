from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from skyweave.camera.check_common import (
    LiveRead,
    MotionCameraState,
    MotionPacketResult,
    PacketStats,
    _print_motion_summary,
    _read_single_live_frame,
    _should_print,
    _write_jsonl,
    _write_pgm_snapshot,
)
from skyweave.camera.motion import FrameDiffMotionPacketBuilder, MotionPacketConfig
from skyweave.camera.source import OpenCVCameraSource
from skyweave.timestamps import monotonic_ns


def _run_live_motion_parallel(
    args: argparse.Namespace,
    config: MotionPacketConfig,
    sources: list[OpenCVCameraSource],
    stats: dict[int, PacketStats],
    snapshot_dir: Path | None,
    snapshots_written: set[int],
    loop_latencies_ms: list[float],
    writer,
) -> int:
    states = {source.camera_id: MotionCameraState() for source in sources}
    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        for _ in range(args.frames):
            loop_start = time.perf_counter()
            futures = [
                executor.submit(_process_motion_source, source, states[source.camera_id], config)
                for source in sources
            ]
            for future in futures:
                result = future.result()
                _record_motion_result(args, result, stats, snapshot_dir, snapshots_written, writer)
            loop_latencies_ms.append((time.perf_counter() - loop_start) * 1000.0)

    _print_motion_summary(args, stats, loop_latencies_ms)
    if args.jsonl:
        print(f"log_path={args.jsonl}")
    if args.snapshot_dir:
        print(f"snapshot_dir={args.snapshot_dir}")
    return 0

def _process_motion_source(
    source: OpenCVCameraSource,
    state: MotionCameraState,
    config: MotionPacketConfig,
) -> MotionPacketResult:
    read = _read_single_live_frame(source)
    frame = read.frame
    if frame is None:
        return MotionPacketResult(read=read, packet=None, build_latency_ms=0.0)

    builder = state.builder
    if builder is None or builder.image_width != frame.image_width or builder.image_height != frame.image_height:
        builder = FrameDiffMotionPacketBuilder(
            source.camera_id,
            frame.image_width,
            frame.image_height,
            config=config,
            source_id=f"camera{source.camera_id}",
        )
        state.builder = builder
        state.previous_frame = None

    start = time.perf_counter()
    packet = builder.build(
        state.previous_frame,
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
    state.previous_frame = frame.gray
    return MotionPacketResult(read=read, packet=packet, build_latency_ms=build_latency_ms)

def _record_motion_result(
    args: argparse.Namespace,
    result: MotionPacketResult,
    stats: dict[int, PacketStats],
    snapshot_dir: Path | None,
    snapshots_written: set[int],
    writer,
) -> None:
    source = result.read.source
    frame = result.read.frame
    camera_stats = stats[source.camera_id]
    if frame is None or result.packet is None:
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
        return

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

    packet = result.packet
    read = result.read
    camera_stats.record(
        frame,
        packet,
        result.build_latency_ms,
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
            f"build_latency={result.build_latency_ms:.3f}ms"
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
            "build_latency_ms": result.build_latency_ms,
            "grab_latency_ms": read.grab_latency_ms,
            "retrieve_latency_ms": read.retrieve_latency_ms,
            "gray_latency_ms": read.gray_latency_ms,
        },
    )
