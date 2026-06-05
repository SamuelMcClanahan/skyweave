import numpy as np
import pytest

from skyweave.camera.motion import FrameDiffMotionPacketBuilder, MotionPacketConfig, synthetic_motion_frames
from skyweave.camera.source import ArrayCameraSource, CameraFrame, frame_to_gray
from skyweave.camera.check_common import _write_pgm_snapshot
from skyweave.camera.live_benchmark import _with_stress_evidence
from skyweave.cli.camera_check import main as camera_check_main
from skyweave.rayweave.patches import decode_rle_u8


def test_frame_diff_packet_builder_emits_bounded_motion_patches() -> None:
    config = MotionPacketConfig(threshold=16, min_area_px=1, max_patch_side_px=12, max_motion_pixels=20)
    builder = FrameDiffMotionPacketBuilder(0, 64, 48, config=config)
    frames = synthetic_motion_frames(64, 48, frames=4, square_size=10)

    packet = builder.build(frames[0], frames[1], frame_seq=1, capture_ts_ns=10)

    assert packet.header.source_type == "camera"
    assert packet.detector == "frame_diff_u8"
    assert packet.blobs
    assert packet.motion_patches
    assert all(blob.area_px <= config.max_motion_pixels for blob in packet.blobs)

    patch = packet.motion_patches[0]
    mask = decode_rle_u8(patch.payload, patch.bbox_w, patch.bbox_h)
    assert int(np.count_nonzero(mask)) <= config.max_motion_pixels
    assert patch.bbox_w <= config.max_patch_side_px
    assert patch.bbox_h <= config.max_patch_side_px


def test_opencv_frame_diff_backend_emits_bounded_motion_patches() -> None:
    pytest.importorskip("cv2")
    config = MotionPacketConfig(
        threshold=16,
        min_area_px=1,
        max_patch_side_px=12,
        max_motion_pixels=20,
        backend="opencv",
    )
    builder = FrameDiffMotionPacketBuilder(0, 64, 48, config=config)
    frames = synthetic_motion_frames(64, 48, frames=4, square_size=10)

    packet = builder.build(frames[0], frames[1], frame_seq=1, capture_ts_ns=10)

    assert packet.detector == "frame_diff_u8"
    assert packet.blobs
    assert packet.motion_patches
    assert all(blob.area_px <= config.max_motion_pixels for blob in packet.blobs)

    patch = packet.motion_patches[0]
    mask = decode_rle_u8(patch.payload, patch.bbox_w, patch.bbox_h)
    assert int(np.count_nonzero(mask)) <= config.max_motion_pixels
    assert patch.bbox_w <= config.max_patch_side_px
    assert patch.bbox_h <= config.max_patch_side_px


def test_opencv_contours_backend_emits_bounded_motion_patches() -> None:
    pytest.importorskip("cv2")
    config = MotionPacketConfig(
        threshold=16,
        min_area_px=1,
        max_patch_side_px=12,
        max_motion_pixels=20,
        backend="opencv_contours",
    )
    builder = FrameDiffMotionPacketBuilder(0, 64, 48, config=config)
    previous = np.zeros((48, 64), dtype=np.uint8)
    current = previous.copy()
    current[12:22, 20:30] = 255

    packet = builder.build(previous, current, frame_seq=1, capture_ts_ns=10)

    assert packet.detector == "frame_diff_u8"
    assert packet.blobs
    assert packet.motion_patches
    assert all(blob.area_px <= config.max_motion_pixels for blob in packet.blobs)

    patch = packet.motion_patches[0]
    mask = decode_rle_u8(patch.payload, patch.bbox_w, patch.bbox_h)
    assert int(np.count_nonzero(mask)) <= config.max_motion_pixels
    assert patch.bbox_w <= config.max_patch_side_px
    assert patch.bbox_h <= config.max_patch_side_px


def test_frame_diff_packet_builder_uses_optional_publish_timestamp() -> None:
    config = MotionPacketConfig(threshold=16, min_area_px=1)
    builder = FrameDiffMotionPacketBuilder(0, 16, 12, config=config)
    frames = synthetic_motion_frames(16, 12, frames=2, square_size=3)

    packet = builder.build(frames[0], frames[1], frame_seq=1, capture_ts_ns=100, publish_ts_ns=250)

    assert packet.header.capture_ts_ns == 100
    assert packet.header.publish_ts_ns == 250


def test_array_camera_source_emits_deterministic_frames_and_timestamps() -> None:
    frames = synthetic_motion_frames(16, 12, frames=2, square_size=3)
    source = ArrayCameraSource(camera_id=7, frames=frames, timestamps_ns=[10, 20])

    first = source.read()
    second = source.read()

    assert first is not None
    assert second is not None
    assert first.camera_id == 7
    assert first.frame_seq == 0
    assert second.frame_seq == 1
    assert first.capture_ts_ns == 10
    assert second.capture_ts_ns == 20
    assert first.image_width == 16
    assert first.image_height == 12
    assert np.array_equal(first.gray, frames[0])
    assert source.read() is None


def test_frame_to_gray_handles_gray_bgr_bgra_and_yuyv_shapes() -> None:
    gray = np.array([[1, 2], [3, 4]], dtype=np.uint8)
    assert np.array_equal(frame_to_gray(gray), gray)

    bgr = np.array([[[0, 0, 255], [255, 0, 0]]], dtype=np.uint8)
    converted = frame_to_gray(bgr)
    assert converted.shape == (1, 2)
    assert converted.dtype == np.uint8
    assert int(converted[0, 0]) == 76
    assert int(converted[0, 1]) == 29

    bgra = np.array([[[0, 255, 0, 255]]], dtype=np.uint8)
    assert int(frame_to_gray(bgra)[0, 0]) == 149

    yuyv_like = np.array([[[12, 99], [34, 77]]], dtype=np.uint8)
    assert np.array_equal(frame_to_gray(yuyv_like), np.array([[12, 34]], dtype=np.uint8))


def test_camera_check_synthetic_mode_remains_backward_compatible(tmp_path, capsys) -> None:
    log_path = tmp_path / "camera_check.jsonl"

    rc = camera_check_main(
        [
            "--frames",
            "3",
            "--width",
            "32",
            "--height",
            "24",
            "--square-size",
            "4",
            "--console-every",
            "99",
            "--jsonl",
            str(log_path),
        ]
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert "camera_check frames=3" in output
    assert log_path.exists()
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 3


def test_write_pgm_snapshot_writes_grayscale_frame(tmp_path) -> None:
    gray = np.array([[0, 127], [200, 255]], dtype=np.uint8)
    frame = CameraFrame(camera_id=2, frame_seq=4, capture_ts_ns=10, gray=gray)

    path = _write_pgm_snapshot(tmp_path, frame)

    assert path.name == "camera2_frame0004_2x2.pgm"
    assert path.read_bytes() == b"P5\n2 2\n255\n" + bytes([0, 127, 200, 255])


def test_live_benchmark_stress_evidence_replaces_packet_motion_and_aligns_timestamp() -> None:
    live_builder = FrameDiffMotionPacketBuilder(1, 64, 48)
    stress_builder = FrameDiffMotionPacketBuilder(1, 64, 48)
    blank = np.zeros((48, 64), dtype=np.uint8)
    stress_prev = np.zeros((48, 64), dtype=np.uint8)
    stress_current = stress_prev.copy()
    stress_current[12:17, 20:25] = 255
    live_packet = live_builder.build(None, blank, frame_seq=3, capture_ts_ns=100)
    stress_packet = stress_builder.build(stress_prev, stress_current, frame_seq=9, capture_ts_ns=900)

    stressed = _with_stress_evidence(live_packet, stress_packet, capture_ts_ns=123)

    assert stressed.camera_id == live_packet.camera_id
    assert stressed.header.source_id == live_packet.header.source_id
    assert stressed.header.frame_seq == live_packet.header.frame_seq
    assert stressed.header.capture_ts_ns == 123
    assert stressed.header.publish_ts_ns == 123
    assert stressed.blobs == stress_packet.blobs
    assert stressed.motion_patches == stress_packet.motion_patches
    assert len(stressed.blobs) == 1
    assert len(stressed.motion_patches) == 1
    patch = stressed.motion_patches[0]
    assert patch.bbox_w == 5
    assert patch.bbox_h == 5
    mask = decode_rle_u8(patch.payload, patch.bbox_w, patch.bbox_h)
    assert int(np.count_nonzero(mask)) == 25
