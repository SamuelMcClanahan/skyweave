from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from skyweave.camera.opencv_runtime import configure_opencv_runtime
from skyweave.messages import MotionBlob, MotionPacket, MotionPatch, PacketHeader
from skyweave.rayweave.patches import encode_rle_u8

PYTHON_MOTION_BACKEND = "python"
OPENCV_MOTION_BACKEND = "opencv"
OPENCV_CONTOUR_MOTION_BACKEND = "opencv_contours"
AUTO_MOTION_BACKEND = "auto"
MOTION_BACKEND_CHOICES = (
    AUTO_MOTION_BACKEND,
    PYTHON_MOTION_BACKEND,
    OPENCV_MOTION_BACKEND,
    OPENCV_CONTOUR_MOTION_BACKEND,
)
DEFAULT_MOTION_BACKEND = AUTO_MOTION_BACKEND
DEFAULT_OPTIMIZED_MOTION_BACKEND = AUTO_MOTION_BACKEND
_OPENCV_AVAILABLE: bool | None = None


@dataclass(frozen=True)
class MotionPacketConfig:
    threshold: int = 32
    min_area_px: int = 4
    max_components: int = 8
    max_patch_side_px: int = 64
    max_motion_pixels: int = 225
    merge_radius_px: int = 0
    fill_fragments: bool = False
    backend: str = DEFAULT_MOTION_BACKEND


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

    def build(
        self,
        previous: np.ndarray | None,
        current: np.ndarray,
        frame_seq: int,
        capture_ts_ns: int,
        publish_ts_ns: int | None = None,
    ) -> MotionPacket:
        current_u8 = _validate_frame(current, self.image_width, self.image_height)
        if previous is None:
            blobs: list[MotionBlob] = []
            patches: list[MotionPatch] = []
        else:
            previous_u8 = _validate_frame(previous, self.image_width, self.image_height)
            backend = resolve_motion_backend(self.config.backend)
            if backend == OPENCV_MOTION_BACKEND:
                diff, blobs, patches = self._motion_evidence_opencv(previous_u8, current_u8)
            elif backend == OPENCV_CONTOUR_MOTION_BACKEND:
                diff, blobs, patches = self._motion_evidence_opencv_contours(previous_u8, current_u8)
            elif backend == PYTHON_MOTION_BACKEND:
                diff = np.abs(current_u8.astype(np.int16) - previous_u8.astype(np.int16)).astype(np.uint8)
                blobs, patches = self._motion_evidence(diff)
            else:
                raise ValueError(f"unsupported motion backend {backend!r}")

        header = PacketHeader(
            source_id=self.source_id,
            source_type="camera",
            frame_seq=frame_seq,
            capture_ts_ns=capture_ts_ns,
            publish_ts_ns=capture_ts_ns if publish_ts_ns is None else publish_ts_ns,
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
        mask = _merge_motion_mask(mask, self.config.merge_radius_px, fill_fragments=self.config.fill_fragments)
        components = _connected_components(mask)
        components = [component for component in components if component.shape[0] >= self.config.min_area_px]
        components.sort(key=lambda component: component.shape[0], reverse=True)

        blobs: list[MotionBlob] = []
        patches: list[MotionPatch] = []
        for blob_id, component in enumerate(components[: self.config.max_components]):
            bounded = _bounded_component(component, self.config, fill_fragments=self.config.fill_fragments)
            if bounded.size == 0:
                continue
            ys = bounded[:, 0]
            xs = bounded[:, 1]
            x0 = int(xs.min())
            y0 = int(ys.min())
            x1 = int(xs.max()) + 1
            y1 = int(ys.max()) + 1
            if self.config.fill_fragments:
                patch_mask = np.full((y1 - y0, x1 - x0), 255, dtype=np.uint8)
                ys_rel, xs_rel = np.nonzero(patch_mask)
                ys = ys_rel + y0
                xs = xs_rel + x0
                patch_mask, ys, xs = _limit_patch_pixels(patch_mask, ys, xs, x0, y0, self.config.max_motion_pixels)
            else:
                patch_mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
                patch_mask[ys - y0, xs - x0] = 255

            values = diff[ys, xs].astype(np.float64)
            area = int(xs.size)
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

    def _motion_evidence_opencv(self, previous: np.ndarray, current: np.ndarray) -> tuple[np.ndarray, list[MotionBlob], list[MotionPatch]]:
        cv2 = _load_cv2()

        diff = cv2.absdiff(current, previous)
        _, mask = cv2.threshold(diff, self.config.threshold - 1, 255, cv2.THRESH_BINARY)
        mask = _merge_motion_mask_opencv(
            cv2,
            mask,
            self.config.merge_radius_px,
            fill_fragments=self.config.fill_fragments,
        )
        if cv2.countNonZero(mask) == 0:
            return diff, [], []
        n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=4)
        component_ids = [
            component_id
            for component_id in range(1, n_labels)
            if int(stats[component_id, cv2.CC_STAT_AREA]) >= self.config.min_area_px
        ]
        component_ids.sort(key=lambda component_id: int(stats[component_id, cv2.CC_STAT_AREA]), reverse=True)

        blobs: list[MotionBlob] = []
        patches: list[MotionPatch] = []
        for blob_id, component_id in enumerate(component_ids[: self.config.max_components]):
            x0 = int(stats[component_id, cv2.CC_STAT_LEFT])
            y0 = int(stats[component_id, cv2.CC_STAT_TOP])
            width = int(stats[component_id, cv2.CC_STAT_WIDTH])
            height = int(stats[component_id, cv2.CC_STAT_HEIGHT])
            area = int(stats[component_id, cv2.CC_STAT_AREA])
            bounded = _bounded_bbox(x0, y0, width, height, centroids[component_id], self.config)
            patch_mask, ys, xs = _extract_opencv_patch(
                labels,
                component_id,
                bounded,
                self.config.max_motion_pixels,
                fill_fragments=self.config.fill_fragments,
            )
            if patch_mask.size == 0 or xs.size == 0:
                continue

            values = diff[ys, xs].astype(np.float64)
            bx, by, bw, bh = bounded
            bounded_area = int(xs.size)
            confidence = min(1.0, bounded_area / max(float(self.config.max_motion_pixels), 1.0))
            blobs.append(
                MotionBlob(
                    blob_id=blob_id,
                    cx=float(centroids[component_id][0]),
                    cy=float(centroids[component_id][1]),
                    bbox_x=bx,
                    bbox_y=by,
                    bbox_w=bw,
                    bbox_h=bh,
                    area_px=bounded_area,
                    mean_diff=float(values.mean()),
                    max_diff=float(values.max()),
                    confidence=confidence,
                )
            )
            patches.append(
                MotionPatch(
                    bbox_x=bx,
                    bbox_y=by,
                    bbox_w=bw,
                    bbox_h=bh,
                    encoding="rle_u8",
                    payload=encode_rle_u8(patch_mask),
                    value_scale=1.0,
                )
            )
        return diff, blobs, patches

    def _motion_evidence_opencv_contours(
        self,
        previous: np.ndarray,
        current: np.ndarray,
    ) -> tuple[np.ndarray, list[MotionBlob], list[MotionPatch]]:
        cv2 = _load_cv2()

        diff = cv2.absdiff(current, previous)
        _, mask = cv2.threshold(diff, self.config.threshold - 1, 255, cv2.THRESH_BINARY)
        mask = _merge_motion_mask_opencv(
            cv2,
            mask,
            self.config.merge_radius_px,
            fill_fragments=self.config.fill_fragments,
        )
        if cv2.countNonZero(mask) == 0:
            return diff, [], []

        contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = [
            contour
            for contour in contours
            if contour.shape[0] >= 1 and cv2.contourArea(contour) >= max(float(self.config.min_area_px - 1), 0.0)
        ]
        contours.sort(key=cv2.contourArea, reverse=True)

        blobs: list[MotionBlob] = []
        patches: list[MotionPatch] = []
        for blob_id, contour in enumerate(contours[: self.config.max_components]):
            x0, y0, width, height = cv2.boundingRect(contour)
            moments = cv2.moments(contour)
            if abs(moments["m00"]) > 1e-9:
                centroid = np.asarray([moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]], dtype=np.float64)
            else:
                centroid = np.asarray([x0 + (width - 1) / 2.0, y0 + (height - 1) / 2.0], dtype=np.float64)

            bounded = _bounded_bbox(x0, y0, width, height, centroid, self.config)
            patch_mask, ys, xs = _extract_contour_patch(cv2, contour, bounded, self.config.max_motion_pixels)
            if patch_mask.size == 0 or xs.size == 0:
                continue

            values = diff[ys, xs].astype(np.float64)
            bx, by, bw, bh = bounded
            bounded_area = int(xs.size)
            confidence = min(1.0, bounded_area / max(float(self.config.max_motion_pixels), 1.0))
            blobs.append(
                MotionBlob(
                    blob_id=blob_id,
                    cx=float(centroid[0]),
                    cy=float(centroid[1]),
                    bbox_x=bx,
                    bbox_y=by,
                    bbox_w=bw,
                    bbox_h=bh,
                    area_px=bounded_area,
                    mean_diff=float(values.mean()),
                    max_diff=float(values.max()),
                    confidence=confidence,
                )
            )
            patches.append(
                MotionPatch(
                    bbox_x=bx,
                    bbox_y=by,
                    bbox_w=bw,
                    bbox_h=bh,
                    encoding="rle_u8",
                    payload=encode_rle_u8(patch_mask),
                    value_scale=1.0,
                )
            )
        return diff, blobs, patches


def resolve_motion_backend(backend: str) -> str:
    if backend == AUTO_MOTION_BACKEND:
        return OPENCV_MOTION_BACKEND if opencv_motion_available() else PYTHON_MOTION_BACKEND
    if backend in MOTION_BACKEND_CHOICES:
        return backend
    raise ValueError(f"unsupported motion backend {backend!r}")


def opencv_motion_available() -> bool:
    global _OPENCV_AVAILABLE
    if _OPENCV_AVAILABLE is not None:
        return _OPENCV_AVAILABLE
    try:
        _load_cv2()
    except RuntimeError:
        _OPENCV_AVAILABLE = False
    else:
        _OPENCV_AVAILABLE = True
    return _OPENCV_AVAILABLE


def _load_cv2():
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("OpenCV backend requires the camera extra: pip install -e '.[camera]'") from exc
    configure_opencv_runtime(cv2)
    return cv2


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


def _bounded_component(component: np.ndarray, config: MotionPacketConfig, *, fill_fragments: bool = False) -> np.ndarray:
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
    if not fill_fragments and bounded.shape[0] > max_pixels:
        indices = np.linspace(0, bounded.shape[0] - 1, max_pixels, dtype=np.int32)
        bounded = bounded[indices]
    return bounded


def _bounded_bbox(
    x0: int,
    y0: int,
    width: int,
    height: int,
    centroid: np.ndarray,
    config: MotionPacketConfig,
) -> tuple[int, int, int, int]:
    side = max(config.max_patch_side_px, 1)
    if width <= side and height <= side:
        return x0, y0, width, height

    cx = int(round(float(centroid[0])))
    cy = int(round(float(centroid[1])))
    bx = max(x0, min(x0 + width - 1, cx - side // 2))
    by = max(y0, min(y0 + height - 1, cy - side // 2))
    bx = min(bx, x0 + width - min(width, side))
    by = min(by, y0 + height - min(height, side))
    return bx, by, min(width, side), min(height, side)


def _extract_opencv_patch(
    labels: np.ndarray,
    component_id: int,
    bbox: tuple[int, int, int, int],
    max_motion_pixels: int,
    *,
    fill_fragments: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x0, y0, width, height = bbox
    component_mask = labels[y0 : y0 + height, x0 : x0 + width] == component_id
    if fill_fragments:
        component_mask = _fill_bbox_mask(component_mask)
    ys_rel, xs_rel = np.nonzero(component_mask)
    if xs_rel.size == 0:
        return np.empty((0, 0), dtype=np.uint8), ys_rel, xs_rel
    max_pixels = max(max_motion_pixels, 1)
    if xs_rel.size > max_pixels:
        indices = np.linspace(0, xs_rel.size - 1, max_pixels, dtype=np.int32)
        ys_rel = ys_rel[indices]
        xs_rel = xs_rel[indices]
    ys = ys_rel + y0
    xs = xs_rel + x0
    patch_mask = np.zeros((height, width), dtype=np.uint8)
    patch_mask[ys_rel, xs_rel] = 255
    return patch_mask, ys, xs


def _merge_motion_mask(mask: np.ndarray, radius: int, *, fill_fragments: bool = False) -> np.ndarray:
    radius = int(radius)
    if radius <= 0 or not np.any(mask):
        return mask
    dilated = _binary_dilate(mask, radius)
    if fill_fragments:
        return dilated
    return _binary_erode(dilated, radius)


def _merge_motion_mask_opencv(cv2_module, mask: np.ndarray, radius: int, *, fill_fragments: bool = False) -> np.ndarray:
    radius = int(radius)
    if radius <= 0:
        return mask
    kernel_size = radius * 2 + 1
    kernel = cv2_module.getStructuringElement(cv2_module.MORPH_ELLIPSE, (kernel_size, kernel_size))
    if fill_fragments:
        return cv2_module.dilate(mask, kernel, iterations=1)
    return cv2_module.morphologyEx(mask, cv2_module.MORPH_CLOSE, kernel)


def _binary_dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    offsets = _disk_offsets(radius)
    padded = np.pad(mask.astype(bool), radius, mode="constant", constant_values=False)
    output = np.zeros(mask.shape, dtype=bool)
    height, width = mask.shape
    for dy, dx in offsets:
        y0 = radius + dy
        x0 = radius + dx
        output |= padded[y0 : y0 + height, x0 : x0 + width]
    return output


def _binary_erode(mask: np.ndarray, radius: int) -> np.ndarray:
    offsets = _disk_offsets(radius)
    padded = np.pad(mask.astype(bool), radius, mode="constant", constant_values=True)
    output = np.ones(mask.shape, dtype=bool)
    height, width = mask.shape
    for dy, dx in offsets:
        y0 = radius + dy
        x0 = radius + dx
        output &= padded[y0 : y0 + height, x0 : x0 + width]
    return output


def _disk_offsets(radius: int) -> list[tuple[int, int]]:
    radius = max(int(radius), 0)
    r2 = radius * radius
    return [
        (dy, dx)
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
        if dy * dy + dx * dx <= r2
    ]


def _fill_bbox_mask(mask: np.ndarray) -> np.ndarray:
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return mask
    filled = np.zeros(mask.shape, dtype=bool)
    filled[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1] = True
    return filled


def _limit_patch_pixels(
    patch_mask: np.ndarray,
    ys: np.ndarray,
    xs: np.ndarray,
    x0: int,
    y0: int,
    max_motion_pixels: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    max_pixels = max(max_motion_pixels, 1)
    if xs.size <= max_pixels:
        return patch_mask, ys, xs
    indices = np.linspace(0, xs.size - 1, max_pixels, dtype=np.int32)
    ys = ys[indices]
    xs = xs[indices]
    sparse_mask = np.zeros(patch_mask.shape, dtype=np.uint8)
    sparse_mask[ys - y0, xs - x0] = 255
    return sparse_mask, ys, xs


def _extract_contour_patch(
    cv2_module,
    contour: np.ndarray,
    bbox: tuple[int, int, int, int],
    max_motion_pixels: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x0, y0, width, height = bbox
    if width <= 0 or height <= 0:
        empty = np.empty(0, dtype=np.int64)
        return np.empty((0, 0), dtype=np.uint8), empty, empty

    local = contour.copy()
    local[:, 0, 0] -= x0
    local[:, 0, 1] -= y0
    patch_mask = np.zeros((height, width), dtype=np.uint8)
    cv2_module.drawContours(patch_mask, [local], -1, 255, thickness=cv2_module.FILLED)
    ys_rel, xs_rel = np.nonzero(patch_mask)
    if xs_rel.size == 0:
        return np.empty((0, 0), dtype=np.uint8), ys_rel, xs_rel

    max_pixels = max(max_motion_pixels, 1)
    if xs_rel.size > max_pixels:
        indices = np.linspace(0, xs_rel.size - 1, max_pixels, dtype=np.int32)
        ys_rel = ys_rel[indices]
        xs_rel = xs_rel[indices]
        sparse_mask = np.zeros((height, width), dtype=np.uint8)
        sparse_mask[ys_rel, xs_rel] = 255
        patch_mask = sparse_mask
    ys = ys_rel + y0
    xs = xs_rel + x0
    return patch_mask, ys, xs
