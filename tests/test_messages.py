from skyweave.messages import MotionBlob, MotionPacket, MotionPatch, PacketHeader
from skyweave.transport.pack import pack_model, unpack_model


def test_motion_packet_msgpack_round_trip() -> None:
    packet = MotionPacket(
        header=PacketHeader(
            source_id="sim_cam0",
            source_type="synthetic",
            frame_seq=1,
            capture_ts_ns=10,
            publish_ts_ns=11,
        ),
        camera_id=0,
        image_width=640,
        image_height=480,
        blobs=[
            MotionBlob(
                blob_id=1,
                cx=10.0,
                cy=11.0,
                bbox_x=8,
                bbox_y=9,
                bbox_w=5,
                bbox_h=5,
                area_px=25,
                mean_diff=255.0,
                max_diff=255.0,
                confidence=1.0,
            )
        ],
        motion_patches=[
            MotionPatch(
                bbox_x=8,
                bbox_y=9,
                bbox_w=5,
                bbox_h=5,
                encoding="rle_u8",
                payload=b"abc",
            )
        ],
        detector="test",
    )

    restored = unpack_model(pack_model(packet), MotionPacket)
    assert restored == packet

