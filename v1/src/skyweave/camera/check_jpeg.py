from __future__ import annotations

import argparse
import time
from pathlib import Path

from skyweave.camera.check_common import (
    EncodedFrameStats,
    _encode_jpeg_frame,
    _fps_from_ms,
    _percentile,
    _read_live_frames,
    _should_print,
    _write_jsonl,
    _write_pgm_snapshot,
)
from skyweave.camera.source import CameraOpenError, OpenCVCameraSource
from skyweave.timestamps import monotonic_ns


def _run_live_jpeg(
    args: argparse.Namespace,
    sources: list[OpenCVCameraSource],
    stats: dict[int, EncodedFrameStats],
    snapshot_dir: Path | None,
    snapshots_written: set[int],
    loop_latencies_ms: list[float],
    writer,
) -> int:
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CameraOpenError("OpenCV is required for JPEG frame encoding. Install with .[camera].") from exc

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
                        "mode": "live_jpeg_frame",
                        "camera_id": source.camera_id,
                        "failures": camera_stats.read_failures,
                    },
                )
                continue

            if snapshot_dir is not None and source.camera_id not in snapshots_written:
                snapshot_path = _write_pgm_snapshot(snapshot_dir, frame)
                snapshots_written.add(source.camera_id)
                print(f"snapshot_written camera_id={source.camera_id} path={snapshot_path}")

            encoded_bytes, encode_latency_ms = _encode_jpeg_frame(cv2, frame, args.jpeg_quality)
            publish_ts_ns = monotonic_ns()
            camera_stats.record(
                frame,
                encoded_bytes,
                encode_latency_ms,
                publish_ts_ns,
                read.grab_latency_ms,
                read.retrieve_latency_ms,
                read.gray_latency_ms,
            )
            packet_latency_ms = (publish_ts_ns - frame.capture_ts_ns) / 1_000_000.0

            if _should_print(frame.frame_seq, args.console_every):
                print(
                    f"cam={source.camera_id} frame={frame.frame_seq:04d} "
                    f"size={frame.image_width}x{frame.image_height} "
                    f"jpeg_bytes={encoded_bytes} encode_latency={encode_latency_ms:.3f}ms "
                    f"packet_latency={packet_latency_ms:.3f}ms"
                )
            _write_jsonl(
                writer,
                {
                    "event": "jpeg_frame",
                    "mode": "live",
                    "camera_id": source.camera_id,
                    "frame_seq": frame.frame_seq,
                    "capture_ts_ns": frame.capture_ts_ns,
                    "publish_ts_ns": publish_ts_ns,
                    "image_width": frame.image_width,
                    "image_height": frame.image_height,
                    "jpeg_quality": args.jpeg_quality,
                    "encoded_bytes": encoded_bytes,
                    "encode_latency_ms": encode_latency_ms,
                    "packet_latency_ms": packet_latency_ms,
                    "grab_latency_ms": read.grab_latency_ms,
                    "retrieve_latency_ms": read.retrieve_latency_ms,
                    "gray_latency_ms": read.gray_latency_ms,
                },
            )
        loop_latencies_ms.append((time.perf_counter() - loop_start) * 1000.0)

    if args.profile_stages:
        print(
            "camera_check_jpeg_loop "
            f"frames={args.frames} "
            f"loop_latency_p50={_percentile(loop_latencies_ms, 50.0):.3f}ms "
            f"loop_latency_p95={_percentile(loop_latencies_ms, 95.0):.3f}ms "
            f"loop_fps_p50={_fps_from_ms(_percentile(loop_latencies_ms, 50.0)):.2f}"
        )

    for camera_id in sorted(stats):
        camera_stats = stats[camera_id]
        print(
            "camera_check_jpeg "
            f"camera_id={camera_id} frames={camera_stats.frames} read_failures={camera_stats.read_failures} "
            f"effective_fps={camera_stats.effective_fps:.2f} "
            f"jpeg_quality={args.jpeg_quality} "
            f"bytes_p50={_percentile(camera_stats.encoded_bytes, 50.0):.0f} "
            f"bytes_p95={_percentile(camera_stats.encoded_bytes, 95.0):.0f} "
            f"mbps={camera_stats.mbps:.2f} "
            f"packet_latency_p50={_percentile(camera_stats.packet_latencies_ms, 50.0):.3f}ms "
            f"packet_latency_p95={_percentile(camera_stats.packet_latencies_ms, 95.0):.3f}ms "
            f"encode_latency_p50={_percentile(camera_stats.encode_latencies_ms, 50.0):.3f}ms "
            f"encode_latency_p95={_percentile(camera_stats.encode_latencies_ms, 95.0):.3f}ms"
        )
        if args.profile_stages:
            print(
                "camera_check_jpeg_stage "
                f"camera_id={camera_id} "
                f"grab_p50={_percentile(camera_stats.grab_latencies_ms, 50.0):.3f}ms "
                f"grab_p95={_percentile(camera_stats.grab_latencies_ms, 95.0):.3f}ms "
                f"retrieve_p50={_percentile(camera_stats.retrieve_latencies_ms, 50.0):.3f}ms "
                f"retrieve_p95={_percentile(camera_stats.retrieve_latencies_ms, 95.0):.3f}ms "
                f"gray_p50={_percentile(camera_stats.gray_latencies_ms, 50.0):.3f}ms "
                f"gray_p95={_percentile(camera_stats.gray_latencies_ms, 95.0):.3f}ms "
                f"encode_p50={_percentile(camera_stats.encode_latencies_ms, 50.0):.3f}ms "
                f"encode_p95={_percentile(camera_stats.encode_latencies_ms, 95.0):.3f}ms"
            )

    if args.jsonl:
        print(f"log_path={args.jsonl}")
    if args.snapshot_dir:
        print(f"snapshot_dir={args.snapshot_dir}")
    return 0
