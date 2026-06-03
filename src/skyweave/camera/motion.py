from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from skyweave.messages import MotionBlob, MotionPacket, MotionPatch, PacketHeader
from skyweave.rayweave.patches import encode_rle_u8


@dataclass(frozen=True)
class MotionPacketConfig:
    threshold: int = 32
    min_area_px: int = 4
    max_components: int = 8
    max_patch_side_px: int = 64
    max_motion_pixels: int = 225


class FrameDiffMotionPacketBuilder:
    def __init__(
        self,
        camera_id: int,
        image_width: int,
        image_height: int,
        config: MotionPacketConfig | None = None,
        source_id: str | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.image_width = image_width
        self.image_height = image_height
        self.config = config or MotionPacketConfig()
        self.source_id = source_id or f"camera{camera_id}"

    def build(self, previous: np.ndarray | None, current: np.ndarray, frame_seq: int, capture_ts_ns: int) -> MotionPacket:
        current_u8 = _validate_frame(current, self.image_width, self.image_height)
        if previous is None:
            blobs: list[MotionBlob] = []
            patches: list[MotionPatch] = []
        else:
            previous_u8 = _validate_frame(previous, self.image_width, self.image_height)
            diff = np.abs(current_u8.astype(np.int16) - previous_u8.astype(np.int16)).astype(np.uint8)
            blobs, patches = self._motion_evidence(diff)

        header = PacketHeader(
            source_id=self.source_id,
            source_type="camera",
            frame_seq=frame_seq,
            capture_ts_ns=capture_ts_ns,
            publish_ts_ns=capture_ts_ns,
        )
        return MotionPacket(
            header=header,
            camera_id=self.camera_id,
            image_width=self.image_width,
            image_height=self.image_height,
            blobs=blobs,
            motion_patches=patches,
            detector="frame_diff_u8",
        )

    def _motion_evidence(self, diff: np.ndarray) -> tuple[list[MotionBlob], list[MotionPatch]]:
        mask = diff >= self.config.threshold
        components = _connected_components(mask)
        components = [component for component in components if component.shape[0] >= self.config.min_area_px]
        components.sort(key=lambda component: component.shape[0], reverse=True)

        blobs: list[MotionBlob] = []
        patches: list[MotionPatch] = []
        for blob_id, component in enumerate(components[: self.config.max_components]):
            bounded = _bounded_component(component, self.config)
            if bounded.size == 0:
                continue
            ys = bounded[:, 0]
            xs = bounded[:, 1]
            x0 = int(xs.min())
            y0 = int(ys.min())
            x1 = int(xs.max()) + 1
            y1 = int(ys.max()) + 1
            patch_mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
            patch_mask[ys - y0, xs - x0] = 255

            values = diff[ys, xs].astype(np.float64)
            area = int(bounded.shape[0])
            confidence = min(1.0, area / max(float(self.config.max_motion_pixels), 1.0))
            blobs.append(
                MotionBlob(
                    blob_id=blob_id,
                    cx=float(xs.mean()),
                    cy=float(ys.mean()),
                    bbox_x=x0,
                    bbox_y=y0,
                    bbox_w=x1 - x0,
                    bbox_h=y1 - y0,
                    area_px=area,
                    mean_diff=float(values.mean()),
                    max_diff=float(values.max()),
                    confidence=confidence,
                )
            )
            patches.append(
                MotionPatch(
                    bbox_x=x0,
                    bbox_y=y0,
                    bbox_w=x1 - x0,
                    bbox_h=y1 - y0,
                    encoding="rle_u8",
                    payload=encode_rle_u8(patch_mask),
                    value_scale=1.0,
                )
            )
        return blobs, patches


def synthetic_motion_frames(width: int, height: int, frames: int, square_size: int) -> list[np.ndarray]:
    output: list[np.ndarray] = []
    usable_w = max(width - square_size - 1, 1)
    usable_h = max(height - square_size - 1, 1)
    for frame_seq in range(frames):
        t = frame_seq / max(frames - 1, 1)
        x0 = int(round(1 + usable_w * t))
        y0 = int(round(height * 0.45 + usable_h * 0.15 * np.sin(np.pi * t)))
        x0 = max(0, min(width - square_size, x0))
        y0 = max(0, min(height - square_size, y0))
        frame = np.zeros((height, width), dtype=np.uint8)
        frame[y0 : y0 + square_size, x0 : x0 + square_size] = 255
        output.append(frame)
    return output


def _validate_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    arr = np.asarray(frame)
    if arr.shape != (height, width):
        raise ValueError(f"frame shape {arr.shape} does not match {(height, width)}")
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8)
    return arr


def _connected_components(mask: np.ndarray) -> list[np.ndarray]:
    visited = np.zeros(mask.shape, dtype=bool)
    components: list[np.ndarray] = []
    starts = np.argwhere(mask)
    height, width = mask.shape
    for start_arr in starts:
        start = (int(start_arr[0]), int(start_arr[1]))
        if visited[start]:
            continue
        queue: deque[tuple[int, int]] = deque([start])
        visited[start] = True
        coords: list[tuple[int, int]] = []
        while queue:
            y, x = queue.popleft()
            coords.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    queue.append((ny, nx))
        components.append(np.asarray(coords, dtype=np.int32))
    return components


def _bounded_component(component: np.ndarray, config: MotionPacketConfig) -> np.ndarray:
    bounded = component
    side = max(config.max_patch_side_px, 1)
    ys = bounded[:, 0]
    xs = bounded[:, 1]
    if xs.max() - xs.min() + 1 > side or ys.max() - ys.min() + 1 > side:
        cx = int(round(float(xs.mean())))
        cy = int(round(float(ys.mean())))
        x0 = cx - side // 2
        y0 = cy - side // 2
        keep = (x0 <= xs) & (xs < x0 + side) & (y0 <= ys) & (ys < y0 + side)
        bounded = bounded[keep]
    max_pixels = max(config.max_motion_pixels, 1)
    if bounded.shape[0] > max_pixels:
        indices = np.linspace(0, bounded.shape[0] - 1, max_pixels, dtype=np.int32)
        bounded = bounded[indices]
    return bounded
