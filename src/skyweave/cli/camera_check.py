from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from skyweave.camera.motion import FrameDiffMotionPacketBuilder, MotionPacketConfig, synthetic_motion_frames


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a headless camera packet-generation smoke check.")
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--fps", type=float, default=100.0)
    parser.add_argument("--square-size", type=int, default=18)
    parser.add_argument("--threshold", type=int, default=32)
    parser.add_argument("--max-patch-side", type=int, default=64)
    parser.add_argument("--max-motion-pixels", type=int, default=225)
    parser.add_argument("--console-every", type=int, default=1)
    parser.add_argument("--jsonl", default=None, help="Optional JSONL output path.")
    args = parser.parse_args(argv)

    config = MotionPacketConfig(
        threshold=args.threshold,
        max_patch_side_px=args.max_patch_side,
        max_motion_pixels=args.max_motion_pixels,
    )
    builder = FrameDiffMotionPacketBuilder(0, args.width, args.height, config=config, source_id="headless_cam0")
    frames = synthetic_motion_frames(args.width, args.height, args.frames, args.square_size)
    jsonl_path = Path(args.jsonl) if args.jsonl else None
    writer = jsonl_path.open("w", encoding="utf-8") if jsonl_path else None

    packet_latencies: list[float] = []
    total_blobs = 0
    total_motion_pixels = 0
    previous = None
    try:
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
            if frame_seq % max(args.console_every, 1) == 0:
                print(
                    f"frame={frame_seq:03d} blobs={event['n_blobs']} patches={event['n_patches']} "
                    f"motion_pixels={motion_pixels} latency={latency_ms:.3f}ms"
                )
            if writer:
                writer.write(json.dumps(event, sort_keys=True) + "\n")
    finally:
        if writer:
            writer.close()

    print(
        "camera_check "
        f"frames={args.frames} size={args.width}x{args.height} fps={args.fps:.1f} "
        f"total_blobs={total_blobs} avg_motion_pixels={total_motion_pixels / max(args.frames, 1):.1f} "
        f"latency_p50={_percentile(packet_latencies, 50.0):.3f}ms "
        f"latency_p95={_percentile(packet_latencies, 95.0):.3f}ms"
    )
    if jsonl_path:
        print(f"log_path={jsonl_path}")
    return 0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct / 100.0
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo)


if __name__ == "__main__":
    sys.exit(main())
