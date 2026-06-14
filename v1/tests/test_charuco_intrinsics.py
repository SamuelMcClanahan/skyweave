from __future__ import annotations

import json

from skyweave.calibration.intrinsics import intrinsics_to_yaml, load_intrinsic_dataset


def test_load_intrinsic_dataset_reads_accepted_observations(tmp_path) -> None:
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
cameras:
  - label: cam1
    device: /dev/video0
""".strip(),
        encoding="utf-8",
    )
    event = {
        "accepted": True,
        "label": "cam1",
        "device": "/dev/video0",
        "image_width": 1280,
        "image_height": 800,
        "dictionary": "DICT_4X4_50",
        "corner_count": 4,
        "corner_ids": [0, 1, 2, 3],
        "corners_px": [[10.0, 20.0], [30.0, 20.0], [10.0, 40.0], [30.0, 40.0]],
    }
    rejected = {**event, "accepted": False, "corner_ids": [], "corners_px": []}
    (capture_dir / "observations.jsonl").write_text(
        json.dumps(rejected) + "\n" + json.dumps(event) + "\n",
        encoding="utf-8",
    )

    dataset = load_intrinsic_dataset(capture_dir, min_corners=4)

    assert dataset.label == "cam1"
    assert dataset.device == "/dev/video0"
    assert dataset.image_size == (1280, 800)
    assert dataset.dictionary == "DICT_4X4_50"
    assert dataset.view_count == 1
    assert dataset.corners[0].shape == (4, 1, 2)
    assert dataset.ids[0].shape == (4, 1)


def test_intrinsics_to_yaml_is_plain_data() -> None:
    class Calibration:
        label = "cam1"
        image_size = (1280, 800)
        camera_matrix = type("Matrix", (), {"tolist": lambda self: [[1, 0, 2], [0, 3, 4], [0, 0, 1]]})()
        dist_coeffs = type("Dist", (), {"reshape": lambda self, *_: self, "tolist": lambda self: [0.1, 0.2]})()
        rms_px = 0.42
        per_view_errors_px = [0.3, 0.5]
        accepted_views = 2
        board_pattern = "current"
        source_dir = "capture_dir"

        @property
        def focal_lengths_px(self):
            return 1.0, 3.0

        @property
        def principal_point_px(self):
            return 2.0, 4.0

    payload = intrinsics_to_yaml(Calibration())

    assert payload["label"] == "cam1"
    assert payload["rms_reprojection_error_px"] == 0.42
    assert payload["fx_px"] == 1.0
    assert payload["dist_coeffs"] == [0.1, 0.2]
