"""T3: Brown-Conrady distortion round trip."""

from __future__ import annotations

import numpy as np

from skyweave2.contracts import CameraModel


def _camera_with_distortion() -> CameraModel:
    return CameraModel(
        camera_id=1,
        width=2304,
        height=1296,
        k=((1995.0, 0.0, 1151.5), (0.0, 1995.0, 647.5), (0.0, 0.0, 1.0)),
        dist=(-0.12, 0.035, 0.0008, -0.0005, -0.004),
        t_world_cam=(
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
        calibration_rev="distortion-test-r1",
    )


def test_distort_undistort_round_trip() -> None:
    camera = _camera_with_distortion()
    for x in np.linspace(-0.35, 0.35, 9):
        for y in np.linspace(-0.22, 0.22, 9):
            x_d, y_d = camera.distort_normalized(float(x), float(y))
            x_u, y_u = camera.undistort_normalized(x_d, y_d)
            assert abs(x_u - x) < 1e-9
            assert abs(y_u - y) < 1e-9


def test_project_unproject_round_trip_with_distortion() -> None:
    camera = _camera_with_distortion()
    point = np.array([3.0, 1.5, 40.0])
    u, v = camera.project(point)
    origin, direction = camera.unproject(u, v)
    to_point = point - origin
    assert np.allclose(to_point / np.linalg.norm(to_point), direction, atol=1e-8)


def test_zero_distortion_is_identity() -> None:
    camera = _camera_with_distortion().model_copy(update={"dist": (0.0, 0.0, 0.0, 0.0, 0.0)})
    x_d, y_d = camera.distort_normalized(0.1, -0.2)
    assert x_d == 0.1 and y_d == -0.2
