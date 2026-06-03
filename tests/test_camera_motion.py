import numpy as np

from skyweave.camera.motion import FrameDiffMotionPacketBuilder, MotionPacketConfig, synthetic_motion_frames
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
