from __future__ import annotations

import pytest

from skyweave2.contracts import CameraModel, ClockDomain, FrameEnvelope, Observation2D


@pytest.fixture()
def hand_camera() -> CameraModel:
    """Camera at (0, -10, 0) looking north (+y). Derived by hand in the D0 spec:

    optical axis +Z_cam = world +y; +X_cam (right) = world +x;
    +Y_cam (down) = world -z. fx = fy = 1000, cx = 320, cy = 240.
    World point (1, 0, 2): p_cam = (1, -2, 10) -> u = 420, v = 40.
    """
    return CameraModel(
        camera_id=0,
        width=640,
        height=480,
        k=((1000.0, 0.0, 320.0), (0.0, 1000.0, 240.0), (0.0, 0.0, 1.0)),
        dist=(0.0, 0.0, 0.0, 0.0, 0.0),
        t_world_cam=(
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, -10.0),
            (0.0, -1.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
        calibration_rev="hand-case-r1",
    )


@pytest.fixture()
def envelope() -> FrameEnvelope:
    return FrameEnvelope(
        camera_id=3,
        session_uuid="11111111-2222-3333-4444-555555555555",
        frame_seq=42,
        capture_ts_ns=1_400_000_000,
        clock_domain=ClockDomain.SYNTHETIC,
        time_sync_error_ms=0.5,
        exposure_us=4000.0,
        gain_db=6.0,
        full_width=2304,
        full_height=1296,
        proc_width=1536,
        proc_height=864,
        calibration_rev="cal-r3",
        detector_rev="det-r1",
        line_readout_us=14.0,
    )


@pytest.fixture()
def observation(envelope: FrameEnvelope) -> Observation2D:
    return Observation2D(
        envelope=envelope,
        obs_id=0,
        u=1201.25,
        v=655.5,
        cov_uu=0.8,
        cov_uv=0.1,
        cov_vv=0.9,
        bbox_x=1195,
        bbox_y=650,
        bbox_w=12,
        bbox_h=11,
        area_px=41,
        persistence_count=3,
        confidence=0.9,
        local_blob_id=7,
        evidence_ref="patch/3/42/0",
    )
