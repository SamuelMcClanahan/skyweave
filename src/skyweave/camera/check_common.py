from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from skyweave.camera.motion import FrameDiffMotionPacketBuilder
from skyweave.camera.source import CameraFrame, OpenCVCameraSource
from skyweave.messages import MotionPacket


@dataclass
class PacketStats:
    frames: int = 0
    read_failures: int = 0
    total_blobs: int = 0
    total_patches: int = 0
    total_motion_pixels: int = 0
    first_ts_ns: int | None = None
    last_ts_ns: int | None = None
    packet_latencies_ms: list[float] = field(default_factory=list)
    build_latencies_ms: list[float] = field(default_factory=list)
    grab_latencies_ms: list[float] = field(default_factory=list)
    retrieve_latencies_ms: list[float] = field(default_factory=list)
    gray_latencies_ms: list[float] = field(default_factory=list)

    def record(
        self,
        frame: CameraFrame,
        packet: MotionPacket,
        build_latency_ms: float,
        grab_latency_ms: float = 0.0,
        retrieve_latency_ms: float = 0.0,
        gray_latency_ms: float = 0.0,
    ) -> None:
        motion_pixels = sum(blob.area_px for blob in packet.blobs)
        packet_latency_ms = (packet.header.publish_ts_ns - frame.capture_ts_ns) / 1_000_000.0
        self.frames += 1
        self.total_blobs += len(packet.blobs)
        self.total_patches += len(packet.motion_patches)
        self.total_motion_pixels += motion_pixels
        self.packet_latencies_ms.append(packet_latency_ms)
        self.build_latencies_ms.append(build_latency_ms)
        self.grab_latencies_ms.append(grab_latency_ms)
        self.retrieve_latencies_ms.append(retrieve_latency_ms)
        self.gray_latencies_ms.append(gray_latency_ms)
        self.first_ts_ns = frame.capture_ts_ns if self.first_ts_ns is None else min(self.first_ts_ns, frame.capture_ts_ns)
        self.last_ts_ns = frame.capture_ts_ns if self.last_ts_ns is None else max(self.last_ts_ns, frame.capture_ts_ns)

    @property
    def effective_fps(self) -> float:
        if self.frames < 2 or self.first_ts_ns is None or self.last_ts_ns is None:
            return 0.0
        elapsed_s = (self.last_ts_ns - self.first_ts_ns) / 1_000_000_000.0
        return (self.frames - 1) / elapsed_s if elapsed_s > 0.0 else 0.0


@dataclass
class EncodedFrameStats:
    frames: int = 0
    read_failures: int = 0
    total_bytes: int = 0
    first_ts_ns: int | None = None
    last_ts_ns: int | None = None
    encoded_bytes: list[float] = field(default_factory=list)
    encode_latencies_ms: list[float] = field(default_factory=list)
    packet_latencies_ms: list[float] = field(default_factory=list)
    grab_latencies_ms: list[float] = field(default_factory=list)
    retrieve_latencies_ms: list[float] = field(default_factory=list)
    gray_latencies_ms: list[float] = field(default_factory=list)

    def record(
        self,
        frame: CameraFrame,
        encoded_bytes: int,
        encode_latency_ms: float,
        publish_ts_ns: int,
        grab_latency_ms: float,
        retrieve_latency_ms: float,
        gray_latency_ms: float,
    ) -> None:
        packet_latency_ms = (publish_ts_ns - frame.capture_ts_ns) / 1_000_000.0
        self.frames += 1
        self.total_bytes += encoded_bytes
        self.encoded_bytes.append(float(encoded_bytes))
        self.encode_latencies_ms.append(encode_latency_ms)
        self.packet_latencies_ms.append(packet_latency_ms)
        self.grab_latencies_ms.append(grab_latency_ms)
        self.retrieve_latencies_ms.append(retrieve_latency_ms)
        self.gray_latencies_ms.append(gray_latency_ms)
        self.first_ts_ns = frame.capture_ts_ns if self.first_ts_ns is None else min(self.first_ts_ns, frame.capture_ts_ns)
        self.last_ts_ns = frame.capture_ts_ns if self.last_ts_ns is None else max(self.last_ts_ns, frame.capture_ts_ns)

    @property
    def effective_fps(self) -> float:
        if self.frames < 2 or self.first_ts_ns is None or self.last_ts_ns is None:
            return 0.0
        elapsed_s = (self.last_ts_ns - self.first_ts_ns) / 1_000_000_000.0
        return (self.frames - 1) / elapsed_s if elapsed_s > 0.0 else 0.0

    @property
    def mbps(self) -> float:
        if self.frames < 2 or self.first_ts_ns is None or self.last_ts_ns is None:
            return 0.0
        elapsed_s = (self.last_ts_ns - self.first_ts_ns) / 1_000_000_000.0
        return self.total_bytes * 8.0 / elapsed_s / 1_000_000.0 if elapsed_s > 0.0 else 0.0


@dataclass(frozen=True)
class LiveRead:
    source: OpenCVCameraSource
    frame: CameraFrame | None
    grab_latency_ms: float
    retrieve_latency_ms: float
    gray_latency_ms: float


@dataclass
class MotionCameraState:
    builder: FrameDiffMotionPacketBuilder | None = None
    previous_frame: object | None = None


@dataclass(frozen=True)
class MotionPacketResult:
    read: LiveRead
    packet: MotionPacket | None
    build_latency_ms: float

def _read_live_frames(sources: list[OpenCVCameraSource]) -> list[LiveRead]:
    if len(sources) == 1:
        return [_read_single_live_frame(sources[0])]

    capture_timestamps: dict[int, tuple[int | None, float]] = {}
    for source in sources:
        start = time.perf_counter()
        capture_ts_ns = source.grab()
        grab_ms = (time.perf_counter() - start) * 1000.0
        capture_timestamps[source.camera_id] = (capture_ts_ns, grab_ms)

    frames: list[LiveRead] = []
    for source in sources:
        capture_ts_ns, grab_ms = capture_timestamps[source.camera_id]
        if capture_ts_ns is None:
            frames.append(LiveRead(source, None, grab_ms, 0.0, 0.0))
            continue
        frame, retrieve_ms, gray_ms = source.retrieve_timed(capture_ts_ns)
        frames.append(LiveRead(source, frame, grab_ms, retrieve_ms, gray_ms))
    return frames

def _read_single_live_frame(source: OpenCVCameraSource) -> LiveRead:
    start = time.perf_counter()
    capture_ts_ns = source.grab()
    grab_ms = (time.perf_counter() - start) * 1000.0
    if capture_ts_ns is None:
        return LiveRead(source, None, grab_ms, 0.0, 0.0)
    frame, retrieve_ms, gray_ms = source.retrieve_timed(capture_ts_ns)
    return LiveRead(source, frame, grab_ms, retrieve_ms, gray_ms)

def _print_motion_summary(args: argparse.Namespace, stats: dict[int, PacketStats], loop_latencies_ms: list[float]) -> None:
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

def _open_jsonl(path: str | None):
    if not path:
        return None
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.open("w", encoding="utf-8")

def _write_jsonl(writer, event: dict) -> None:
    if writer:
        writer.write(json.dumps(event, sort_keys=True) + "\n")

def _write_pgm_snapshot(snapshot_dir: Path, frame: CameraFrame) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"camera{frame.camera_id}_frame{frame.frame_seq:04d}_{frame.image_width}x{frame.image_height}.pgm"
    with path.open("wb") as output:
        output.write(f"P5\n{frame.image_width} {frame.image_height}\n255\n".encode("ascii"))
        output.write(frame.gray.astype("uint8", copy=False).tobytes(order="C"))
    return path

def _encode_jpeg_frame(cv2_module, frame: CameraFrame, quality: int) -> tuple[int, float]:
    start = time.perf_counter()
    ok, encoded = cv2_module.imencode(".jpg", frame.gray, [int(cv2_module.IMWRITE_JPEG_QUALITY), int(quality)])
    encode_ms = (time.perf_counter() - start) * 1000.0
    if not ok:
        raise RuntimeError(f"failed to JPEG-encode camera {frame.camera_id} frame {frame.frame_seq}")
    return int(encoded.nbytes), encode_ms

def _decode_fourcc(value: float) -> str:
    code = int(value)
    text = "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))
    return text if all(32 <= ord(char) <= 126 for char in text) else str(code)

def _should_print(sequence: int, console_every: int) -> bool:
    return console_every > 0 and sequence % console_every == 0

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

def _fps_from_ms(milliseconds: float) -> float:
    return 1000.0 / milliseconds if milliseconds > 0.0 else 0.0
