from __future__ import annotations

from dataclasses import dataclass

from skyweave.messages import DetectionPacket, MotionPacket


@dataclass(frozen=True)
class AlignedEvidence:
    ts_ns: int
    motion_packets: list[MotionPacket]
    detection_packets: list[DetectionPacket]

    @property
    def source_packet_ids(self) -> list[str]:
        return [
            f"{p.header.source_id}:{p.header.frame_seq}"
            for p in self.motion_packets
        ]


class TimeAligner:
    def __init__(self, window_ns: int, min_cameras: int) -> None:
        self.window_ns = window_ns
        self.min_cameras = min_cameras

    def align_frame(
        self,
        motion_packets: list[MotionPacket],
        detection_packets: list[DetectionPacket] | None = None,
    ) -> AlignedEvidence | None:
        if len(motion_packets) < self.min_cameras:
            return None
        timestamps = [p.header.capture_ts_ns for p in motion_packets]
        if max(timestamps) - min(timestamps) > self.window_ns:
            return None
        return AlignedEvidence(
            ts_ns=int(round(sum(timestamps) / len(timestamps))),
            motion_packets=motion_packets,
            detection_packets=detection_packets or [],
        )

