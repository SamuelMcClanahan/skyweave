from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave.camera.motion import FrameDiffMotionPacketBuilder, MotionPacketConfig
from skyweave.camera.source import CameraFrame
from skyweave.config import SimulationConfig
from skyweave.fusion.geom import CameraCalib, project_point, world_to_camera
from skyweave.messages import MotionPacket
from skyweave.sim.scene import GroundTruthSample, SyntheticScene


@dataclass(frozen=True)
class RenderedCameraMeta:
    camera_id: int
    visible: bool
    projected: tuple[float, float] | None
    radius_px: float
    depth_m: float | None


@dataclass(frozen=True)
class RenderedFrame:
    truth: GroundTruthSample
    camera_frames: list[CameraFrame]
    camera_meta: dict[int, RenderedCameraMeta]


class RenderedFrameGenerator:
    def __init__(self, scene: SyntheticScene, config: SimulationConfig) -> None:
        self.scene = scene
        self.config = config
        self.rng = np.random.default_rng(config.seed)

    def frames(self) -> list[RenderedFrame]:
        frames: list[RenderedFrame] = []
        previous_projected: dict[int, tuple[float, float, float]] = {}
        for sample in self.scene.truth:
            frame, previous_projected = self._make_frame(sample, previous_projected)
            frames.append(frame)
        return frames

    def _make_frame(
        self,
        truth: GroundTruthSample,
        previous_projected: dict[int, tuple[float, float, float]],
    ) -> tuple[RenderedFrame, dict[int, tuple[float, float, float]]]:
        camera_frames: list[CameraFrame] = []
        camera_meta: dict[int, RenderedCameraMeta] = {}
        next_projected: dict[int, tuple[float, float, float]] = {}
        for camera_id, camera in self.scene.cameras.items():
            gray, meta = self._render_camera(camera, truth, previous_projected.get(camera_id))
            if meta.projected is not None and meta.depth_m is not None:
                next_projected[camera_id] = (meta.projected[0], meta.projected[1], meta.radius_px)
            camera_frames.append(
                CameraFrame(
                    camera_id=camera_id,
                    frame_seq=truth.frame_seq,
                    capture_ts_ns=truth.ts_ns,
                    gray=gray,
                )
            )
            camera_meta[camera_id] = meta
        return RenderedFrame(truth=truth, camera_frames=camera_frames, camera_meta=camera_meta), next_projected

    def _render_camera(
        self,
        camera: CameraCalib,
        truth: GroundTruthSample,
        previous_projected: tuple[float, float, float] | None,
    ) -> tuple[np.ndarray, RenderedCameraMeta]:
        frame = np.full(
            (self.config.image_height, self.config.image_width),
            _u8_intensity(self.config.render_background_intensity, "render_background_intensity"),
            dtype=np.float32,
        )
        _draw_debug_grid(frame, self.config.render_background_intensity)
        if self.config.render_noise_std > 0.0:
            frame += self.rng.normal(0.0, float(self.config.render_noise_std), size=frame.shape)

        projected = project_point(truth.position, camera)
        depth_m: float | None = None
        radius_px = 0.0
        if projected is not None:
            point_cam = world_to_camera(truth.position, camera)
            depth_m = float(point_cam[2])
            radius_px = max(1.0, float(camera.K[0, 0]) * float(self.config.render_object_radius_m) / max(depth_m, 1e-6))
            _draw_object(
                frame,
                projected[0],
                projected[1],
                radius_px,
                _u8_intensity(self.config.render_object_intensity, "render_object_intensity"),
                self.config.render_object_shape,
            )
            if previous_projected is not None and self.config.render_trail_alpha > 0.0:
                px, py, pr = previous_projected
                trail_intensity = _blend_intensity(
                    self.config.render_background_intensity,
                    self.config.render_object_intensity,
                    self.config.render_trail_alpha,
                )
                _draw_object(frame, px, py, pr, trail_intensity, self.config.render_object_shape)

        if self.config.render_blur_px > 0:
            frame = _box_blur(frame, int(self.config.render_blur_px))

        gray = np.ascontiguousarray(np.clip(frame, 0, 255).astype(np.uint8))
        return gray, RenderedCameraMeta(
            camera_id=camera.id,
            visible=projected is not None,
            projected=projected,
            radius_px=radius_px,
            depth_m=depth_m,
        )


def rendered_motion_packets(
    rendered_frame: RenderedFrame,
    builders: dict[int, FrameDiffMotionPacketBuilder],
    previous_frames: dict[int, np.ndarray],
    config: MotionPacketConfig | None = None,
    publish_ts_ns: int | None = None,
) -> list[MotionPacket]:
    packets: list[MotionPacket] = []
    for frame in rendered_frame.camera_frames:
        builder = builders.get(frame.camera_id)
        if (
            builder is None
            or builder.image_width != frame.image_width
            or builder.image_height != frame.image_height
            or (config is not None and builder.config != config)
        ):
            builder = FrameDiffMotionPacketBuilder(
                frame.camera_id,
                frame.image_width,
                frame.image_height,
                config=config,
                source_id=f"rendered_cam{frame.camera_id}",
            )
            builders[frame.camera_id] = builder
        packet = builder.build(
            previous_frames.get(frame.camera_id),
            frame.gray,
            frame.frame_seq,
            frame.capture_ts_ns,
            publish_ts_ns=publish_ts_ns,
        )
        previous_frames[frame.camera_id] = frame.gray
        packets.append(packet)
    return packets


def _draw_object(frame: np.ndarray, cx: float, cy: float, radius_px: float, intensity: int, shape: str) -> None:
    radius = max(1, int(round(radius_px)))
    x0 = max(0, int(np.floor(cx - radius - 1)))
    y0 = max(0, int(np.floor(cy - radius - 1)))
    x1 = min(frame.shape[1], int(np.ceil(cx + radius + 2)))
    y1 = min(frame.shape[0], int(np.ceil(cy + radius + 2)))
    if x0 >= x1 or y0 >= y1:
        return

    yy, xx = np.mgrid[y0:y1, x0:x1]
    shape_name = str(shape).strip().lower()
    if shape_name == "triangle":
        dx = (xx - cx) / max(radius_px, 1e-6)
        dy = (yy - cy) / max(radius_px, 1e-6)
        mask = (dy >= -0.9) & (dy <= 0.9) & (np.abs(dx) <= (0.95 - dy * 0.35))
    else:
        mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius_px**2
    patch = frame[y0:y1, x0:x1]
    patch[mask] = float(intensity)


def _draw_debug_grid(frame: np.ndarray, background: int) -> None:
    step = max(24, min(frame.shape) // 8)
    intensity = float(min(int(background) + 18, 255))
    frame[::step, :] = intensity
    frame[:, ::step] = intensity


def _box_blur(frame: np.ndarray, radius: int) -> np.ndarray:
    radius = max(0, radius)
    if radius <= 0:
        return frame
    padded = np.pad(frame, radius, mode="edge")
    integral = np.pad(padded, ((1, 0), (1, 0)), mode="constant").cumsum(axis=0).cumsum(axis=1)
    kernel = 2 * radius + 1
    height, width = frame.shape
    total = (
        integral[kernel : kernel + height, kernel : kernel + width]
        - integral[:height, kernel : kernel + width]
        - integral[kernel : kernel + height, :width]
        + integral[:height, :width]
    )
    return total / float(kernel * kernel)


def _u8_intensity(value: int, name: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 255:
        raise ValueError(f"{name} must be between 0 and 255")
    return parsed


def _blend_intensity(background: int, foreground: int, alpha: float) -> int:
    clipped = min(max(float(alpha), 0.0), 1.0)
    return _u8_intensity(round(float(background) * (1.0 - clipped) + float(foreground) * clipped), "trail_intensity")
