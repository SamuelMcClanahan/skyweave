from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave.config import SimulationConfig
from skyweave.fusion.geom import project_point
from skyweave.messages import (
    Detection,
    DetectionPacket,
    MotionBlob,
    MotionPacket,
    MotionPatch,
    PacketHeader,
)
from skyweave.rayweave.patches import encode_rle_u8
from skyweave.sim.scene import GroundTruthSample, SyntheticScene


@dataclass(frozen=True)
class SyntheticFrame:
    truth: GroundTruthSample
    motion_packets: list[MotionPacket]
    detection_packets: list[DetectionPacket]
    dropped_packets: int
    not_visible_packets: int
    not_visible_camera_ids: list[int]
    false_positive_packets: int


class SyntheticPacketGenerator:
    def __init__(self, scene: SyntheticScene, config: SimulationConfig) -> None:
        self.scene = scene
        self.config = config
        self.rng = np.random.default_rng(config.seed)

    def frames(self) -> list[SyntheticFrame]:
        return [self.make_frame(sample) for sample in self.scene.truth]

    def make_frame(self, truth: GroundTruthSample) -> SyntheticFrame:
        motion_packets: list[MotionPacket] = []
        detection_packets: list[DetectionPacket] = []
        dropped = 0
        not_visible_camera_ids: list[int] = []
        false_pos = 0
        for camera_id, camera in self.scene.cameras.items():
            if self.rng.random() < self.config.dropout_probability:
                dropped += 1
                continue
            projected = project_point(truth.position, camera)
            if projected is None:
                not_visible_camera_ids.append(camera_id)
                continue

            jitter_ns = int(self.rng.normal(0.0, self.config.timestamp_jitter_ms) * 1_000_000.0)
            capture_ts_ns = truth.ts_ns + jitter_ns
            header = PacketHeader(
                source_id=f"sim_cam{camera_id}",
                source_type="synthetic",
                frame_seq=truth.frame_seq,
                capture_ts_ns=capture_ts_ns,
                publish_ts_ns=capture_ts_ns,
            )

            u = float(projected[0] + self.rng.normal(0.0, self.config.pixel_noise_std_px))
            v = float(projected[1] + self.rng.normal(0.0, self.config.pixel_noise_std_px))
            patch, blob, detection = self._target_evidence(u, v, blob_id=0)
            patches = [patch]
            blobs = [blob]
            detections = [detection]

            if self.rng.random() < self.config.false_positive_probability:
                fp_u = float(self.rng.uniform(0, self.config.image_width - 1))
                fp_v = float(self.rng.uniform(0, self.config.image_height - 1))
                fp_patch, fp_blob, fp_detection = self._target_evidence(fp_u, fp_v, blob_id=99)
                patches.append(fp_patch)
                blobs.append(fp_blob)
                detections.append(fp_detection)
                false_pos += 1

            motion_packets.append(
                MotionPacket(
                    header=header,
                    camera_id=camera_id,
                    image_width=self.config.image_width,
                    image_height=self.config.image_height,
                    blobs=blobs,
                    motion_patches=patches,
                    detector="synthetic_packet_generator",
                )
            )
            detection_packets.append(
                DetectionPacket(
                    header=header,
                    camera_id=camera_id,
                    detections=detections,
                )
            )

        return SyntheticFrame(
            truth=truth,
            motion_packets=motion_packets,
            detection_packets=detection_packets,
            dropped_packets=dropped,
            not_visible_packets=len(not_visible_camera_ids),
            not_visible_camera_ids=not_visible_camera_ids,
            false_positive_packets=false_pos,
        )

    def _target_evidence(self, u: float, v: float, blob_id: int) -> tuple[MotionPatch, MotionBlob, Detection]:
        size = max(self.config.patch_size_px, 1)
        half = size // 2
        cx = int(round(u))
        cy = int(round(v))
        x0 = max(0, min(self.config.image_width - size, cx - half))
        y0 = max(0, min(self.config.image_height - size, cy - half))
        mask = np.full((size, size), 255, dtype=np.uint8)
        payload = encode_rle_u8(mask)
        patch = MotionPatch(
            bbox_x=x0,
            bbox_y=y0,
            bbox_w=size,
            bbox_h=size,
            encoding="rle_u8",
            payload=payload,
            value_scale=1.0,
        )
        mean_u = x0 + (size - 1) / 2.0
        mean_v = y0 + (size - 1) / 2.0
        blob = MotionBlob(
            blob_id=blob_id,
            cx=mean_u,
            cy=mean_v,
            bbox_x=x0,
            bbox_y=y0,
            bbox_w=size,
            bbox_h=size,
            area_px=size * size,
            mean_diff=255.0,
            max_diff=255.0,
            confidence=1.0 if blob_id == 0 else 0.25,
        )
        detection = Detection(
            cx=mean_u,
            cy=mean_v,
            bbox_x=x0,
            bbox_y=y0,
            bbox_w=size,
            bbox_h=size,
            area_px=size * size,
            confidence=blob.confidence,
        )
        return patch, blob, detection
