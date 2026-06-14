import numpy as np

from skyweave.fusion.geom import CameraCalib, look_at_pose, make_intrinsics, project_point, ray_from_pixel


def test_projection_and_ray_round_trip() -> None:
    camera = CameraCalib(
        id=0,
        K=make_intrinsics(640, 480, 360.0),
        D=np.zeros(5),
        width=640,
        height=480,
        T_world_cam=look_at_pose(np.array([0.0, -2.0, 1.0]), np.array([0.0, 0.0, 1.0])),
    )
    point = np.array([0.2, 0.5, 1.1])
    pixel = project_point(point, camera)
    assert pixel is not None
    origin, direction = ray_from_pixel(pixel[0], pixel[1], camera)
    to_point = (point - origin) / np.linalg.norm(point - origin)
    assert np.dot(direction, to_point) > 0.999999

