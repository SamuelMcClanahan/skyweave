from __future__ import annotations

import json

import numpy as np
import pytest

from skyweave.calibration.charuco import CharucoBoardSpec
from skyweave.calibration.charuco import create_board
from skyweave.calibration.extrinsics import (
    CameraIntrinsics,
    CameraExtrinsics,
    FixedBoardExtrinsics,
    _solve_camera_extrinsics,
    _object_and_image_points,
    _observation_object_and_image_points,
    _t_world_cam_from_board_to_camera,
    extrinsics_to_yaml,
    load_fixed_board_dataset,
)


def test_load_fixed_board_dataset_groups_accepted_observations_by_label(tmp_path) -> None:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    (capture_dir / "manifest.yaml").write_text(
        """
board:
  squares_x: 8
  squares_y: 6
  square_mm: 30.0
  marker_mm: 21.6
  dictionary: DICT_4X4
""".strip(),
        encoding="utf-8",
    )
    _write_intrinsics(tmp_path / "intrinsics_cam1.yaml", "cam1")
    _write_intrinsics(tmp_path / "intrinsics_cam2.yaml", "cam2")
    accepted = {
        "accepted": True,
        "camera_id": 0,
        "label": "cam1",
        "device": "/dev/video0",
        "frame_seq": 7,
        "image_width": 1280,
        "image_height": 800,
        "dictionary": "DICT_4X4_50",
        "corner_count": 4,
        "corner_ids": [0, 1, 2, 3],
        "corners_px": [[10.0, 20.0], [30.0, 20.0], [10.0, 40.0], [30.0, 40.0]],
    }
    cam2 = {**accepted, "camera_id": 1, "label": "cam2", "device": "/dev/video2"}
    rejected = {**accepted, "accepted": False, "label": "cam1", "corner_ids": [], "corners_px": []}
    (capture_dir / "observations.jsonl").write_text(
        "\n".join(json.dumps(item) for item in [rejected, accepted, cam2]) + "\n",
        encoding="utf-8",
    )

    dataset = load_fixed_board_dataset(
        capture_dir,
        [tmp_path / "intrinsics_cam1.yaml", tmp_path / "intrinsics_cam2.yaml"],
        min_corners=4,
    )

    assert dataset.board.squares_x == 8
    assert sorted(dataset.observations_by_label) == ["cam1", "cam2"]
    assert dataset.observations_by_label["cam1"][0].camera_id == 0
    assert dataset.intrinsics_by_label["cam2"].image_size == (1280, 800)


def test_load_fixed_board_dataset_requires_each_intrinsics_label_to_have_observations(tmp_path) -> None:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    (capture_dir / "manifest.yaml").write_text(
        "board: {squares_x: 8, squares_y: 6, square_mm: 30.0, marker_mm: 21.6, dictionary: DICT_4X4}",
        encoding="utf-8",
    )
    (capture_dir / "observations.jsonl").write_text("", encoding="utf-8")
    _write_intrinsics(tmp_path / "intrinsics_cam1.yaml", "cam1")

    with pytest.raises(ValueError, match="cam1"):
        load_fixed_board_dataset(capture_dir, [tmp_path / "intrinsics_cam1.yaml"], min_corners=4)


def test_object_and_image_points_use_charuco_corner_ids() -> None:
    class Board:
        def getChessboardCorners(self):
            return np.array(
                [
                    [0.0, 0.0, 0.0],
                    [0.03, 0.0, 0.0],
                    [0.0, 0.03, 0.0],
                ],
                dtype=np.float32,
            )

    observation = type(
        "Observation",
        (),
        {
            "corner_ids": np.array([2, 0], dtype=np.int32),
            "corners_px": np.array([[120.0, 220.0], [100.0, 200.0]], dtype=np.float32),
        },
    )()

    object_points, image_points = _object_and_image_points(Board(), [observation])

    np.testing.assert_allclose(object_points, [[0.0, 0.03, 0.0], [0.0, 0.0, 0.0]])
    np.testing.assert_allclose(image_points, [[120.0, 220.0], [100.0, 200.0]])


def test_observation_object_and_image_points_prefers_opencv_board_matching() -> None:
    class Board:
        def __init__(self):
            self.calls = []

        def matchImagePoints(self, corners, ids):
            self.calls.append((corners.copy(), ids.copy()))
            return (
                np.array([[[0.0, 0.0, 0.0]], [[0.03, 0.0, 0.0]]], dtype=np.float32),
                np.array([[[100.0, 200.0]], [[120.0, 220.0]]], dtype=np.float32),
            )

    observation = type(
        "Observation",
        (),
        {
            "label": "cam1",
            "frame_seq": 11,
            "corner_ids": np.array([0, 1], dtype=np.int32),
            "corners_px": np.array([[100.0, 200.0], [120.0, 220.0]], dtype=np.float32),
        },
    )()
    board = Board()

    object_points, image_points = _observation_object_and_image_points(board, observation)

    assert len(board.calls) == 1
    assert board.calls[0][0].shape == (2, 1, 2)
    assert board.calls[0][1].shape == (2, 1)
    np.testing.assert_allclose(object_points, [[0.0, 0.0, 0.0], [0.03, 0.0, 0.0]])
    np.testing.assert_allclose(image_points, [[100.0, 200.0], [120.0, 220.0]])


def test_t_world_cam_from_board_to_camera_inverts_opencv_pose() -> None:
    rotation_cam_board = np.array(
        [
            [0.0, -1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    t_cam_board = np.array([0.4, -0.2, 1.5], dtype=np.float64)
    point_world = np.array([0.2, 0.1, 0.0], dtype=np.float64)
    point_cam = rotation_cam_board @ point_world + t_cam_board

    T_world_cam = _t_world_cam_from_board_to_camera(rotation_cam_board, t_cam_board)
    round_tripped = T_world_cam @ np.array([point_cam[0], point_cam[1], point_cam[2], 1.0])

    np.testing.assert_allclose(round_tripped[:3], point_world, atol=1.0e-12)


def test_solve_camera_extrinsics_with_real_opencv_charuco_board(tmp_path) -> None:
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "aruco"):
        pytest.skip("OpenCV aruco module is not available")
    aruco = cv2.aruco
    dictionary = aruco.getPredefinedDictionary(aruco.DICT_4X4_50)
    board = create_board(
        aruco,
        CharucoBoardSpec(8, 6, 0.03, 0.0216, "DICT_4X4"),
        dictionary,
    )
    if not hasattr(board, "matchImagePoints"):
        pytest.skip("OpenCV board.matchImagePoints is not available")

    object_points = np.asarray(board.getChessboardCorners(), dtype=np.float32).reshape(-1, 3)
    corner_ids = np.arange(len(object_points), dtype=np.int32)
    camera_matrix = np.array([[900.0, 0.0, 640.0], [0.0, 900.0, 400.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    dist_coeffs = np.zeros((5, 1), dtype=np.float64)
    rvec = np.array([[0.08], [-0.16], [0.03]], dtype=np.float64)
    tvec = np.array([[0.02], [-0.01], [0.75]], dtype=np.float64)
    corners_px, _ = cv2.projectPoints(object_points, rvec, tvec, camera_matrix, dist_coeffs)
    rotation_cam_board, _ = cv2.Rodrigues(rvec)
    expected = _t_world_cam_from_board_to_camera(rotation_cam_board, tvec)
    observation = type(
        "Observation",
        (),
        {
            "camera_id": 0,
            "label": "cam1",
            "device": "/dev/video0",
            "frame_seq": 1,
            "corner_ids": corner_ids,
            "corners_px": corners_px.reshape(-1, 2).astype(np.float32),
        },
    )()

    result = _solve_camera_extrinsics(
        cv2,
        board,
        "current",
        CameraIntrinsics(
            label="cam1",
            image_size=(1280, 800),
            camera_matrix=camera_matrix,
            dist_coeffs=dist_coeffs,
            source_path=tmp_path / "intrinsics_cam1.yaml",
        ),
        [observation],
    )

    assert result.rms_px < 1.0e-3
    np.testing.assert_allclose(result.T_world_cam, expected, atol=1.0e-5)


def test_extrinsics_to_yaml_is_plain_data() -> None:
    board = CharucoBoardSpec(8, 6, 0.03, 0.0216, "DICT_4X4")
    camera = CameraExtrinsics(
        camera_id=0,
        label="cam1",
        device="/dev/video0",
        image_size=(1280, 800),
        intrinsics_file=tmp_path_like("configs/intrinsics_cam1.yaml"),
        T_world_cam=np.array(
            [
                [1.0, 0.0, 0.0, 0.1],
                [0.0, 1.0, 0.0, 0.2],
                [0.0, 0.0, 1.0, 0.3],
                [0.0, 0.0, 0.0, 1.0],
            ]
        ),
        rms_px=0.42,
        max_error_px=0.9,
        observation_count=3,
        corner_count=90,
        board_pattern="current",
    )
    payload = extrinsics_to_yaml(
        FixedBoardExtrinsics(
            source_dir=tmp_path_like("data/calibration/fixed"),
            board=board,
            world_frame="charuco_board",
            cameras=[camera],
            rms_px=0.42,
            board_pattern="current",
        )
    )

    assert payload["transform_convention"] == "X_world = T_world_cam @ X_cam"
    assert payload["cameras"][0]["t_world_cam_m"] == [0.1, 0.2, 0.3]
    assert payload["cameras"][0]["accepted_corners"] == 90


def _write_intrinsics(path, label: str) -> None:
    path.write_text(
        f"""
label: {label}
image_width: 1280
image_height: 800
camera_matrix:
- [900.0, 0.0, 640.0]
- [0.0, 900.0, 400.0]
- [0.0, 0.0, 1.0]
dist_coeffs: [0.0, 0.0, 0.0, 0.0, 0.0]
""".strip(),
        encoding="utf-8",
    )


def tmp_path_like(path: str):
    from pathlib import Path

    return Path(path)
