from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave.config import SimulationConfig
from skyweave.fusion.geom import CameraCalib, look_at_pose, make_intrinsics


@dataclass(frozen=True)
class GroundTruthSample:
    frame_seq: int
    ts_ns: int
    position: np.ndarray
    velocity: np.ndarray


@dataclass(frozen=True)
class SyntheticScene:
    name: str
    cameras: dict[int, CameraCalib]
    truth: list[GroundTruthSample]


def build_scene(config: SimulationConfig) -> SyntheticScene:
    cameras = _build_cameras(config)
    truth = _build_truth(config)
    return SyntheticScene(name=config.scene, cameras=cameras, truth=truth)


def _build_cameras(config: SimulationConfig) -> dict[int, CameraCalib]:
    K = make_intrinsics(config.image_width, config.image_height, config.focal_length_px)
    D = np.zeros(5, dtype=np.float64)
    target = np.array([0.0, 0.35, 1.25], dtype=np.float64)
    positions = {
        0: np.array([0.0, -2.2, 1.05], dtype=np.float64),
        1: np.array([-2.0, 0.9, 1.15], dtype=np.float64),
        2: np.array([2.0, 0.9, 1.15], dtype=np.float64),
    }
    return {
        camera_id: CameraCalib(
            id=camera_id,
            K=K.copy(),
            D=D.copy(),
            width=config.image_width,
            height=config.image_height,
            T_world_cam=look_at_pose(position, target),
        )
        for camera_id, position in positions.items()
    }


def _build_truth(config: SimulationConfig) -> list[GroundTruthSample]:
    dt = 1.0 / config.timestep_hz
    points = []
    for frame in range(config.frames):
        t = frame / max(config.frames - 1, 1)
        if config.scene == "constant_velocity":
            position = np.array([-1.1 + 2.2 * t, 0.1 + 0.8 * t, 1.15], dtype=np.float64)
        else:
            position = np.array(
                [
                    -1.2 + 2.4 * t,
                    -0.2 + 1.55 * t,
                    0.85 + 0.55 * np.sin(np.pi * t),
                ],
                dtype=np.float64,
            )
        points.append(position)

    truth: list[GroundTruthSample] = []
    for frame, position in enumerate(points):
        if frame == 0:
            velocity = (points[1] - points[0]) / dt if len(points) > 1 else np.zeros(3)
        else:
            velocity = (points[frame] - points[frame - 1]) / dt
        truth.append(
            GroundTruthSample(
                frame_seq=frame,
                ts_ns=int(round(frame * dt * 1_000_000_000)),
                position=position,
                velocity=velocity,
            )
        )
    return truth

