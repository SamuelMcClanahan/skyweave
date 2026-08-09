"""T1: transform and projection conventions, hand-derived, plus a v1 audit."""

from __future__ import annotations

import numpy as np
import pytest

from skyweave2.contracts import CameraModel


def test_hand_projection_case(hand_camera: CameraModel) -> None:
    projected = hand_camera.project(np.array([1.0, 0.0, 2.0]))
    assert projected is not None
    u, v = projected
    assert u == pytest.approx(420.0, abs=1e-9)
    assert v == pytest.approx(40.0, abs=1e-9)


def test_point_behind_camera_returns_none(hand_camera: CameraModel) -> None:
    assert hand_camera.project(np.array([0.0, -20.0, 0.0])) is None


def test_unproject_inverts_project(hand_camera: CameraModel) -> None:
    target = np.array([1.0, 0.0, 2.0])
    u, v = hand_camera.project(target)
    origin, direction = hand_camera.unproject(u, v)
    # The target must lie on the ray.
    to_target = target - origin
    distance = np.linalg.norm(to_target)
    assert np.allclose(to_target / distance, direction, atol=1e-9)


def test_camera_center_maps_to_translation(hand_camera: CameraModel) -> None:
    assert np.allclose(hand_camera.position_world(), [0.0, -10.0, 0.0])


def test_v1_projection_agrees_with_frozen_convention(hand_camera: CameraModel) -> None:
    """Audit: v1 geom must implement the same T_world_cam and camera axes."""
    skyweave = pytest.importorskip("skyweave.fusion.geom")
    calib = skyweave.CameraCalib(
        id=0,
        K=hand_camera.k_matrix(),
        D=np.zeros(5),
        width=hand_camera.width,
        height=hand_camera.height,
        T_world_cam=np.asarray(hand_camera.t_world_cam, dtype=np.float64),
    )
    rng = np.random.default_rng(7)
    checked = 0
    for _ in range(200):
        point = np.array([rng.uniform(-2, 2), rng.uniform(2, 40), rng.uniform(-2, 2)])
        ours = hand_camera.project(point)
        theirs = skyweave.project_point(point, calib)
        if theirs is None:
            continue  # v1 also bounds-checks; only compare in-frame points
        assert ours is not None
        assert ours[0] == pytest.approx(theirs[0], abs=1e-9)
        assert ours[1] == pytest.approx(theirs[1], abs=1e-9)
        checked += 1
    assert checked > 50
