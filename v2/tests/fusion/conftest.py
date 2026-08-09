from __future__ import annotations

import numpy as np
import pytest

from skyweave2.contracts import CameraModel, ClockDomain, FrameEnvelope, Observation2D
from skyweave2.fusion.config import FusionConfig

CAL_REV = "fusion-test-r1"
FPS = 30.0
FRAME_NS = 33_333_333
RANGE_M = 243.84
ALT_M = 20.0
BASELINE_M = 20.0
F_PX = 1995.3
WIDTH, HEIGHT = 2304, 1296


def look_at_camera(camera_id: int, position, target) -> CameraModel:
    pos = np.asarray(position, dtype=np.float64)
    z_cam = np.asarray(target, dtype=np.float64) - pos
    z_cam /= np.linalg.norm(z_cam)
    up = np.array([0.0, 0.0, 1.0])
    x_cam = np.cross(z_cam, up)
    x_cam /= np.linalg.norm(x_cam)
    y_cam = np.cross(z_cam, x_cam)
    t = np.eye(4)
    t[:3, 0], t[:3, 1], t[:3, 2], t[:3, 3] = x_cam, y_cam, z_cam, pos
    return CameraModel(
        camera_id=camera_id,
        width=WIDTH,
        height=HEIGHT,
        k=((F_PX, 0.0, (WIDTH - 1) / 2), (0.0, F_PX, (HEIGHT - 1) / 2), (0.0, 0.0, 1.0)),
        t_world_cam=tuple(tuple(float(v) for v in row) for row in t),
        calibration_rev=CAL_REV,
    )


@pytest.fixture()
def cameras() -> dict[int, CameraModel]:
    """The exp001 trio: 20 m east-west baseline aimed at the crossing point."""
    aim = (0.0, RANGE_M, ALT_M)
    return {
        i: look_at_camera(i, (x, 0.0, 1.5), aim)
        for i, x in enumerate((-BASELINE_M / 2, 0.0, BASELINE_M / 2))
    }


@pytest.fixture()
def config() -> FusionConfig:
    return FusionConfig()


def make_observation(
    camera: CameraModel,
    u: float,
    v: float,
    frame_seq: int,
    obs_id: int = 0,
    ts_ns: int | None = None,
    sigma2: float = 0.25,
    session: str | None = None,
) -> Observation2D:
    envelope = FrameEnvelope(
        camera_id=camera.camera_id,
        session_uuid=session or f"00000000-0000-0000-0000-{camera.camera_id:012d}",
        frame_seq=frame_seq,
        capture_ts_ns=ts_ns if ts_ns is not None else frame_seq * FRAME_NS,
        clock_domain=ClockDomain.SYNTHETIC,
        time_sync_error_ms=0.1,
        exposure_us=3000.0,
        full_width=WIDTH,
        full_height=HEIGHT,
        proc_width=1536,
        proc_height=864,
        calibration_rev=CAL_REV,
        detector_rev="det-fusion-r1",
    )
    return Observation2D(
        envelope=envelope,
        obs_id=obs_id,
        u=u,
        v=v,
        cov_uu=sigma2,
        cov_vv=sigma2,
        bbox_x=int(u) - 3,
        bbox_y=int(v) - 3,
        bbox_w=6,
        bbox_h=6,
        area_px=12,
    )


def observe_point(
    cameras: dict[int, CameraModel],
    point,
    frame_seq: int,
    camera_ids=None,
    noise_px: float = 0.0,
    rng: np.random.Generator | None = None,
    obs_id: int = 0,
) -> list[Observation2D]:
    """Project a world point into the chosen cameras as observations."""
    out = []
    for camera_id in sorted(camera_ids if camera_ids is not None else cameras):
        camera = cameras[camera_id]
        proj = camera.project(np.asarray(point, dtype=np.float64))
        if proj is None:
            continue
        u, v = proj
        if noise_px > 0.0 and rng is not None:
            u += float(rng.normal(0.0, noise_px))
            v += float(rng.normal(0.0, noise_px))
        out.append(make_observation(camera, u, v, frame_seq, obs_id=obs_id))
    return out


def target_position(frame_seq: int, speed: float = 30.0, mid_frame: float = 224.5):
    """Manifest trajectory: lateral constant velocity through the crossing."""
    t = frame_seq / FPS
    mid_t = mid_frame / FPS
    return np.array([speed * (t - mid_t), RANGE_M, ALT_M])
