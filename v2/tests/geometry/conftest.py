from __future__ import annotations

import numpy as np
import pytest

from skyweave2.contracts import CameraModel, ClockDomain, FrameEnvelope, Observation2D
from skyweave2.geometry import GeometryConfig

CAL_REV = "geom-test-r1"


def look_at_camera(
    camera_id: int,
    position: tuple[float, float, float],
    target: tuple[float, float, float],
    f: float = 2000.0,
    width: int = 2304,
    height: int = 1296,
) -> CameraModel:
    """OpenCV camera at ``position`` with the optical axis toward ``target``."""
    pos = np.asarray(position, dtype=np.float64)
    z_cam = np.asarray(target, dtype=np.float64) - pos
    z_cam /= np.linalg.norm(z_cam)
    up = np.array([0.0, 0.0, 1.0])
    if abs(float(z_cam @ up)) > 1.0 - 1e-9:
        up = np.array([0.0, 1.0, 0.0])
    x_cam = np.cross(z_cam, up)
    x_cam /= np.linalg.norm(x_cam)
    y_cam = np.cross(z_cam, x_cam)
    t = np.eye(4)
    t[:3, 0], t[:3, 1], t[:3, 2], t[:3, 3] = x_cam, y_cam, z_cam, pos
    return CameraModel(
        camera_id=camera_id,
        width=width,
        height=height,
        k=((f, 0.0, (width - 1) / 2.0), (0.0, f, (height - 1) / 2.0), (0.0, 0.0, 1.0)),
        t_world_cam=tuple(tuple(float(x) for x in row) for row in t),
        calibration_rev=CAL_REV,
    )


def observation_for(
    camera: CameraModel,
    u: float,
    v: float,
    obs_id: int = 0,
    sigma_px: float = 0.5,
) -> Observation2D:
    envelope = FrameEnvelope(
        camera_id=camera.camera_id,
        session_uuid=f"00000000-0000-0000-0000-{camera.camera_id:012d}",
        frame_seq=10,
        capture_ts_ns=1_000_000_000 + camera.camera_id,
        clock_domain=ClockDomain.SYNTHETIC,
        time_sync_error_ms=0.1,
        exposure_us=4000.0,
        full_width=camera.width,
        full_height=camera.height,
        proc_width=camera.width,
        proc_height=camera.height,
        calibration_rev=CAL_REV,
        detector_rev="det-test-r1",
    )
    return Observation2D(
        envelope=envelope,
        obs_id=obs_id,
        u=u,
        v=v,
        cov_uu=sigma_px**2,
        cov_vv=sigma_px**2,
        bbox_x=int(u) - 4,
        bbox_y=int(v) - 4,
        bbox_w=8,
        bbox_h=8,
        area_px=20,
    )


def exact_observations(
    cameras: list[CameraModel], point: tuple[float, float, float], sigma_px: float = 0.5
) -> list[Observation2D]:
    obs = []
    for cam in cameras:
        proj = cam.project(np.asarray(point, dtype=np.float64))
        assert proj is not None, f"point behind camera {cam.camera_id}"
        obs.append(observation_for(cam, proj[0], proj[1], sigma_px=sigma_px))
    return obs


@pytest.fixture()
def config() -> GeometryConfig:
    return GeometryConfig()
