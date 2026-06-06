from __future__ import annotations

import math
from typing import Any

import numpy as np

from skyweave.fusion.geom import CameraCalib
from skyweave.messages import Track, VizCamera


def viz_camera(camera: CameraCalib, fps: float, online: bool = True) -> dict[str, Any]:
    fx = float(camera.K[0, 0])
    fy = float(camera.K[1, 1])
    fov_h = math.degrees(2.0 * math.atan(camera.width / (2.0 * fx)))
    fov_v = math.degrees(2.0 * math.atan(camera.height / (2.0 * fy)))
    return VizCamera(
        id=camera.id,
        position=[float(x) for x in camera.position],
        rotation_quat=rotation_quat_xyzw(camera.T_world_cam[:3, :3]),
        fov_h_deg=fov_h,
        fov_v_deg=fov_v,
        fps=float(fps),
        online=online,
    ).model_dump(mode="json")


def track_telemetry(track: Track | None, measurement_ts_ns: int | None = None) -> dict[str, Any]:
    if track is None:
        return {
            "track_id": None,
            "status": "no_track",
            "update_count": 0,
            "miss_count": 0,
            "position_m": [0.0, 0.0, 0.0],
            "velocity_mps": [0.0, 0.0, 0.0],
            "speed_mps": 0.0,
            "covariance_diag": [],
            "measurement_age_ms": None,
        }
    state = track.state
    velocity = state[3:6]
    speed = math.sqrt(sum(value * value for value in velocity))
    covariance = np.asarray(track.covariance, dtype=np.float64)
    measurement_age_ms = None
    if measurement_ts_ns is not None:
        measurement_age_ms = max(0.0, (track.last_update_ts_ns - measurement_ts_ns) / 1_000_000.0)
    return {
        "track_id": track.id,
        "status": track.status,
        "update_count": track.update_count,
        "miss_count": track.miss_count,
        "position_m": [float(value) for value in state[:3]],
        "velocity_mps": [float(value) for value in velocity],
        "speed_mps": float(speed),
        "covariance_diag": [float(value) for value in np.diag(covariance).tolist()],
        "measurement_age_ms": measurement_age_ms,
    }


def rotation_quat_xyzw(rotation: np.ndarray) -> list[float]:
    m = np.asarray(rotation, dtype=np.float64)
    trace = float(np.trace(m))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    else:
        i = int(np.argmax(np.diag(m)))
        if i == 0:
            s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
            w = (m[2, 1] - m[1, 2]) / s
            x = 0.25 * s
            y = (m[0, 1] + m[1, 0]) / s
            z = (m[0, 2] + m[2, 0]) / s
        elif i == 1:
            s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
            w = (m[0, 2] - m[2, 0]) / s
            x = (m[0, 1] + m[1, 0]) / s
            y = 0.25 * s
            z = (m[1, 2] + m[2, 1]) / s
        else:
            s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
            w = (m[1, 0] - m[0, 1]) / s
            x = (m[0, 2] + m[2, 0]) / s
            y = (m[1, 2] + m[2, 1]) / s
            z = 0.25 * s
    quat = np.asarray([x, y, z, w], dtype=np.float64)
    quat /= max(float(np.linalg.norm(quat)), 1e-12)
    return [float(value) for value in quat]
