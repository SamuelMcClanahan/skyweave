from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from skyweave.calibration.charuco import CharucoBoardSpec, create_board_candidates, dictionary_candidates


@dataclass(frozen=True)
class IntrinsicDataset:
    source_dir: Path
    label: str
    device: str
    image_size: tuple[int, int]
    board: CharucoBoardSpec
    dictionary: str
    corners: list[np.ndarray]
    ids: list[np.ndarray]

    @property
    def view_count(self) -> int:
        return len(self.corners)


@dataclass(frozen=True)
class IntrinsicCalibration:
    label: str
    rms_px: float
    image_size: tuple[int, int]
    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray
    per_view_errors_px: list[float]
    board_pattern: str
    source_dir: Path
    accepted_views: int

    @property
    def focal_lengths_px(self) -> tuple[float, float]:
        return float(self.camera_matrix[0, 0]), float(self.camera_matrix[1, 1])

    @property
    def principal_point_px(self) -> tuple[float, float]:
        return float(self.camera_matrix[0, 2]), float(self.camera_matrix[1, 2])


def load_intrinsic_dataset(source_dir: Path, min_corners: int = 24) -> IntrinsicDataset:
    manifest_path = source_dir / "manifest.yaml"
    observations_path = source_dir / "observations.jsonl"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing {manifest_path}")
    if not observations_path.exists():
        raise FileNotFoundError(f"missing {observations_path}")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    board = _board_spec_from_manifest(manifest)
    observations = [_parse_observation(line) for line in observations_path.read_text(encoding="utf-8").splitlines()]
    accepted = [
        item
        for item in observations
        if item.get("accepted")
        and int(item.get("corner_count", 0)) >= min_corners
        and item.get("corner_ids")
        and item.get("corners_px")
    ]
    if not accepted:
        raise ValueError(f"{source_dir} has no accepted observations with at least {min_corners} corners")

    label = str(accepted[0].get("label") or _manifest_label(manifest))
    device = str(accepted[0].get("device") or _manifest_device(manifest))
    width = int(accepted[0]["image_width"])
    height = int(accepted[0]["image_height"])
    dictionary = _observation_dictionary(accepted, board.dictionary)
    corners = [_corners_array(item["corners_px"]) for item in accepted]
    ids = [_ids_array(item["corner_ids"]) for item in accepted]
    return IntrinsicDataset(
        source_dir=source_dir,
        label=label,
        device=device,
        image_size=(width, height),
        board=board,
        dictionary=dictionary,
        corners=corners,
        ids=ids,
    )


def calibrate_intrinsics(dataset: IntrinsicDataset) -> IntrinsicCalibration:
    cv2, aruco = _import_aruco()
    dictionary_id = getattr(aruco, dataset.dictionary, None)
    if dictionary_id is None:
        raise ValueError(f"OpenCV does not expose dictionary {dataset.dictionary!r}")
    dictionary = aruco.getPredefinedDictionary(dictionary_id)

    best: IntrinsicCalibration | None = None
    for board, pattern in create_board_candidates(aruco, dataset.board, dictionary):
        result = _run_charuco_calibration(cv2, aruco, dataset, board, pattern)
        if best is None or result.rms_px < best.rms_px:
            best = result
    assert best is not None
    return best


def write_intrinsics(calibration: IntrinsicCalibration, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(intrinsics_to_yaml(calibration), sort_keys=False), encoding="utf-8")


def intrinsics_to_yaml(calibration: IntrinsicCalibration) -> dict[str, Any]:
    fx, fy = calibration.focal_lengths_px
    cx, cy = calibration.principal_point_px
    return {
        "label": calibration.label,
        "image_width": calibration.image_size[0],
        "image_height": calibration.image_size[1],
        "camera_matrix": calibration.camera_matrix.tolist(),
        "dist_coeffs": calibration.dist_coeffs.reshape(-1).tolist(),
        "fx_px": fx,
        "fy_px": fy,
        "cx_px": cx,
        "cy_px": cy,
        "rms_reprojection_error_px": calibration.rms_px,
        "per_view_error_px": calibration.per_view_errors_px,
        "accepted_views": calibration.accepted_views,
        "board_pattern": calibration.board_pattern,
        "source_dir": str(calibration.source_dir),
    }


def _run_charuco_calibration(cv2, aruco, dataset: IntrinsicDataset, board, pattern: str) -> IntrinsicCalibration:
    camera_matrix = np.zeros((3, 3), dtype=np.float64)
    dist_coeffs = np.zeros((5, 1), dtype=np.float64)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1.0e-9)
    (
        rms,
        camera_matrix,
        dist_coeffs,
        _rvecs,
        _tvecs,
        _std_intrinsics,
        _std_extrinsics,
        per_view_errors,
    ) = aruco.calibrateCameraCharucoExtended(
        dataset.corners,
        dataset.ids,
        board,
        dataset.image_size,
        camera_matrix,
        dist_coeffs,
        flags=0,
        criteria=criteria,
    )
    return IntrinsicCalibration(
        label=dataset.label,
        rms_px=float(rms),
        image_size=dataset.image_size,
        camera_matrix=np.asarray(camera_matrix, dtype=float),
        dist_coeffs=np.asarray(dist_coeffs, dtype=float),
        per_view_errors_px=_flat_float_list(per_view_errors),
        board_pattern=pattern,
        source_dir=dataset.source_dir,
        accepted_views=dataset.view_count,
    )


def _board_spec_from_manifest(manifest: dict[str, Any]) -> CharucoBoardSpec:
    board = manifest.get("board") or {}
    return CharucoBoardSpec(
        squares_x=int(board["squares_x"]),
        squares_y=int(board["squares_y"]),
        square_length_m=float(board["square_mm"]) / 1000.0,
        marker_length_m=float(board["marker_mm"]) / 1000.0,
        dictionary=str(board["dictionary"]),
    )


def _parse_observation(line: str) -> dict[str, Any]:
    import json

    return json.loads(line) if line.strip() else {}


def _observation_dictionary(observations: list[dict[str, Any]], fallback: str) -> str:
    for item in observations:
        dictionary = str(item.get("dictionary") or "").strip()
        if dictionary and dictionary != "none":
            return dictionary
    return dictionary_candidates(fallback)[0]


def _manifest_label(manifest: dict[str, Any]) -> str:
    cameras = manifest.get("cameras") or []
    if cameras and isinstance(cameras[0], dict):
        return str(cameras[0].get("label") or "camera")
    return "camera"


def _manifest_device(manifest: dict[str, Any]) -> str:
    cameras = manifest.get("cameras") or []
    if cameras and isinstance(cameras[0], dict):
        return str(cameras[0].get("device") or "")
    return ""


def _corners_array(corners_px: list[list[float]]) -> np.ndarray:
    return np.asarray(corners_px, dtype=np.float32).reshape(-1, 1, 2)


def _ids_array(corner_ids: list[int]) -> np.ndarray:
    return np.asarray(corner_ids, dtype=np.int32).reshape(-1, 1)


def _flat_float_list(values) -> list[float]:
    if values is None:
        return []
    return [float(value) for value in np.asarray(values, dtype=float).reshape(-1)]


def _import_aruco():
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("OpenCV is required. Install with: python -m pip install -e '.[camera]'") from exc
    if not hasattr(cv2, "aruco"):
        raise RuntimeError("OpenCV aruco module is required. Install opencv-contrib-python-headless.")
    return cv2, cv2.aruco
