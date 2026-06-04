from __future__ import annotations

import numpy as np

from skyweave.calibration.charuco import serialize_detection_payload
from skyweave.cli.charuco_capture import _capture_targets, _load_camera_config, _parse_labels


def test_serialize_detection_payload_converts_numpy_arrays() -> None:
    payload = {
        "charuco_ids": np.array([[1], [2]], dtype=np.int32),
        "charuco_corners": np.array([[[10.5, 20.25]], [[30.0, 40.0]]], dtype=np.float32),
        "marker_ids": np.array([[7]], dtype=np.int32),
        "marker_corners": [np.array([[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]], dtype=np.float32)],
    }

    observation = serialize_detection_payload(payload)

    assert observation.corner_ids == [1, 2]
    assert observation.corners_px == [[10.5, 20.25], [30.0, 40.0]]
    assert observation.marker_ids == [7]
    assert observation.marker_corners_px == [[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]]


def test_parse_labels_defaults_and_validates_count() -> None:
    assert _parse_labels(None, 2) == ["camera_0", "camera_1"]
    assert _parse_labels("north,south", 2) == ["north", "south"]


def test_load_camera_config_uses_labels_and_stable_paths(tmp_path) -> None:
    config = tmp_path / "cameras.yaml"
    config.write_text(
        """
cameras:
- label: cam1
  device: /dev/video0
  id_path: path0
- label: cam2
  device: /dev/video2
  id_path: path1
""".strip(),
        encoding="utf-8",
    )

    targets = _load_camera_config(config)

    assert [(target.label, target.device, target.id_path) for target in targets] == [
        ("cam1", "/dev/video0", "path0"),
        ("cam2", "/dev/video2", "path1"),
    ]


def test_capture_targets_prefers_explicit_devices() -> None:
    args = type(
        "Args",
        (),
        {"devices": "/dev/video4", "labels": "cam3", "camera_config": "unused.yaml"},
    )()

    targets = _capture_targets(args)

    assert [(target.label, target.device) for target in targets] == [("cam3", "/dev/video4")]
