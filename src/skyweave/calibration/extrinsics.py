from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from skyweave.calibration.charuco import CharucoBoardSpec, create_board_candidates, dictionary_candidates


@dataclass(frozen=True)
class CameraIntrinsics:
    label: str
    image_size: tuple[int, int]
    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray
    source_path: Path


@dataclass(frozen=True)
class ExtrinsicObservation:
    camera_id: int
    label: str
    device: str
    frame_seq: int
    image_size: tuple[int, int]
    dictionary: str
    corner_ids: np.ndarray
    corners_px: np.ndarray

    @property
    def corner_count(self) -> int:
        return int(self.corner_ids.size)


@dataclass(frozen=True)
class FixedBoardExtrinsicDataset:
    source_dir: Path
    board: CharucoBoardSpec
    observations_by_label: dict[str, list[ExtrinsicObservation]]
    intrinsics_by_label: dict[str, CameraIntrinsics]


@dataclass(frozen=True)
class CameraExtrinsics:
    camera_id: int
    label: str
    device: str
    image_size: tuple[int, int]
    intrinsics_file: Path
    T_world_cam: np.ndarray
    rms_px: float
    max_error_px: float
    observation_count: int
    corner_count: int
    board_pattern: str

    @property
    def t_world_cam_m(self) -> list[float]:
        return [float(value) for value in self.T_world_cam[:3, 3]]


@dataclass(frozen=True)
class FixedBoardExtrinsics:
    source_dir: Path
    board: CharucoBoardSpec
    world_frame: str
    cameras: list[CameraExtrinsics]
    rms_px: float
    board_pattern: str


def load_intrinsics(path: Path) -> CameraIntrinsics:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    label = str(data.get("label") or path.stem.replace("intrinsics_", ""))
    width = int(data["image_width"])
    height = int(data["image_height"])
    return CameraIntrinsics(
        label=label,
        image_size=(width, height),
        camera_matrix=np.asarray(data["camera_matrix"], dtype=np.float64).reshape(3, 3),
        dist_coeffs=np.asarray(data.get("dist_coeffs", []), dtype=np.float64).reshape(-1, 1),
        source_path=path,
    )


def load_fixed_board_dataset(
    source_dir: Path,
    intrinsic_paths: list[Path],
    min_corners: int = 24,
) -> FixedBoardExtrinsicDataset:
    manifest_path = source_dir / "manifest.yaml"
    observations_path = source_dir / "observations.jsonl"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing {manifest_path}")
    if not observations_path.exists():
        raise FileNotFoundError(f"missing {observations_path}")
    if not intrinsic_paths:
        raise ValueError("at least one intrinsics YAML is required")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    board = _board_spec_from_manifest(manifest)
    intrinsics = {item.label: item for item in (load_intrinsics(path) for path in intrinsic_paths)}
    observations = [_parse_observation(line) for line in observations_path.read_text(encoding="utf-8").splitlines()]
    grouped: dict[str, list[ExtrinsicObservation]] = {}
    for item in observations:
        if not item.get("accepted"):
            continue
        if int(item.get("corner_count", 0)) < min_corners:
            continue
        if not item.get("corner_ids") or not item.get("corners_px"):
            continue
        label = str(item.get("label") or f"camera_{int(item.get('camera_id', 0))}")
        if label not in intrinsics:
            continue
        observation = _observation_from_event(item)
        if observation.image_size != intrinsics[label].image_size:
            raise ValueError(
                f"{source_dir} observation for {label} is {observation.image_size}, "
                f"but {intrinsics[label].source_path} is {intrinsics[label].image_size}"
            )
        grouped.setdefault(label, []).append(observation)

    missing = sorted(label for label in intrinsics if label not in grouped)
    if missing:
        raise ValueError(f"no accepted observations found for intrinsics label(s): {', '.join(missing)}")

    return FixedBoardExtrinsicDataset(
        source_dir=source_dir,
        board=board,
        observations_by_label=grouped,
        intrinsics_by_label=intrinsics,
    )


def solve_fixed_board_extrinsics(dataset: FixedBoardExtrinsicDataset, world_frame: str = "charuco_board") -> FixedBoardExtrinsics:
    cv2, aruco = _import_aruco()
    dictionary_name = _dataset_dictionary(dataset)
    dictionary_id = getattr(aruco, dictionary_name, None)
    if dictionary_id is None:
        raise ValueError(f"OpenCV does not expose dictionary {dictionary_name!r}")
    dictionary = aruco.getPredefinedDictionary(dictionary_id)

    best: FixedBoardExtrinsics | None = None
    for board, pattern in create_board_candidates(aruco, dataset.board, dictionary):
        cameras = [
            _solve_camera_extrinsics(cv2, board, pattern, dataset.intrinsics_by_label[label], observations)
            for label, observations in sorted(dataset.observations_by_label.items(), key=lambda item: _camera_sort_key(item[1]))
        ]
        total_corners = sum(camera.corner_count for camera in cameras)
        rms = _weighted_rms([(camera.rms_px, camera.corner_count) for camera in cameras])
        result = FixedBoardExtrinsics(
            source_dir=dataset.source_dir,
            board=dataset.board,
            world_frame=world_frame,
            cameras=cameras,
            rms_px=rms,
            board_pattern=pattern,
        )
        if total_corners > 0 and (best is None or result.rms_px < best.rms_px):
            best = result
    assert best is not None
    return best


def write_extrinsics(calibration: FixedBoardExtrinsics, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(extrinsics_to_yaml(calibration), sort_keys=False), encoding="utf-8")


def extrinsics_to_yaml(calibration: FixedBoardExtrinsics) -> dict[str, Any]:
    return {
        "world_frame": calibration.world_frame,
        "world_origin": "fixed_charuco_board",
        "transform_convention": "X_world = T_world_cam @ X_cam",
        "fixed_board_assumption": "All observations used for this solve saw the same unmoved physical ChArUco board pose.",
        "board": {
            "squares_x": calibration.board.squares_x,
            "squares_y": calibration.board.squares_y,
            "square_mm": calibration.board.square_length_m * 1000.0,
            "marker_mm": calibration.board.marker_length_m * 1000.0,
            "dictionary": calibration.board.dictionary,
        },
        "rms_reprojection_error_px": calibration.rms_px,
        "board_pattern": calibration.board_pattern,
        "source_dir": str(calibration.source_dir),
        "cameras": [
            {
                "camera_id": camera.camera_id,
                "label": camera.label,
                "device": camera.device,
                "image_width": camera.image_size[0],
                "image_height": camera.image_size[1],
                "intrinsics_file": str(camera.intrinsics_file),
                "T_world_cam": camera.T_world_cam.tolist(),
                "t_world_cam_m": camera.t_world_cam_m,
                "rms_reprojection_error_px": camera.rms_px,
                "max_reprojection_error_px": camera.max_error_px,
                "accepted_observations": camera.observation_count,
                "accepted_corners": camera.corner_count,
                "board_pattern": camera.board_pattern,
            }
            for camera in calibration.cameras
        ],
    }


def _solve_camera_extrinsics(
    cv2,
    board,
    board_pattern: str,
    intrinsics: CameraIntrinsics,
    observations: list[ExtrinsicObservation],
) -> CameraExtrinsics:
    object_points, image_points = _object_and_image_points(board, observations)
    if len(object_points) < 4:
        raise ValueError(f"{intrinsics.label} needs at least 4 ChArUco corners for solvePnP")

    ok, rvec, tvec = cv2.solvePnP(
        object_points,
        image_points,
        intrinsics.camera_matrix,
        intrinsics.dist_coeffs,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if not ok:
        raise RuntimeError(f"solvePnP failed for {intrinsics.label}")
    rotation_cam_board, _ = cv2.Rodrigues(rvec)
    T_world_cam = _t_world_cam_from_board_to_camera(rotation_cam_board, tvec)
    projected, _ = cv2.projectPoints(
        object_points,
        rvec,
        tvec,
        intrinsics.camera_matrix,
        intrinsics.dist_coeffs,
    )
    errors = np.linalg.norm(projected.reshape(-1, 2) - image_points.reshape(-1, 2), axis=1)
    first = observations[0]
    return CameraExtrinsics(
        camera_id=first.camera_id,
        label=first.label,
        device=first.device,
        image_size=intrinsics.image_size,
        intrinsics_file=intrinsics.source_path,
        T_world_cam=T_world_cam,
        rms_px=float(np.sqrt(np.mean(errors**2))),
        max_error_px=float(np.max(errors)),
        observation_count=len(observations),
        corner_count=int(len(object_points)),
        board_pattern=board_pattern,
    )


def _object_and_image_points(board, observations: list[ExtrinsicObservation]) -> tuple[np.ndarray, np.ndarray]:
    object_chunks: list[np.ndarray] = []
    image_chunks: list[np.ndarray] = []
    for observation in observations:
        object_points, image_points = _observation_object_and_image_points(board, observation)
        if len(object_points) == 0:
            continue
        object_chunks.append(object_points)
        image_chunks.append(image_points)
    if not object_chunks:
        return np.empty((0, 3), dtype=np.float32), np.empty((0, 2), dtype=np.float32)
    return (
        np.concatenate(object_chunks, axis=0).astype(np.float32, copy=False).reshape(-1, 3),
        np.concatenate(image_chunks, axis=0).astype(np.float32, copy=False).reshape(-1, 2),
    )


def _observation_object_and_image_points(board, observation: ExtrinsicObservation) -> tuple[np.ndarray, np.ndarray]:
    corners = np.asarray(observation.corners_px, dtype=np.float32).reshape(-1, 1, 2)
    ids = np.asarray(observation.corner_ids, dtype=np.int32).reshape(-1, 1)
    if len(corners) != len(ids):
        raise ValueError(
            f"{observation.label} frame {observation.frame_seq} has {len(corners)} corners but {len(ids)} corner ids"
        )
    if hasattr(board, "matchImagePoints"):
        object_points, image_points = board.matchImagePoints(corners, ids)
        return _reshape_object_points(object_points), _reshape_image_points(image_points)
    return _fallback_observation_object_and_image_points(board, observation)


def _fallback_observation_object_and_image_points(board, observation: ExtrinsicObservation) -> tuple[np.ndarray, np.ndarray]:
    board_corners = np.asarray(board.getChessboardCorners(), dtype=np.float32).reshape(-1, 3)
    object_points: list[np.ndarray] = []
    image_points: list[np.ndarray] = []
    for corner_id, corner_px in zip(observation.corner_ids, observation.corners_px):
        index = int(corner_id)
        if index < 0 or index >= len(board_corners):
            continue
        object_points.append(board_corners[index])
        image_points.append(np.asarray(corner_px, dtype=np.float32))
    if not object_points:
        return np.empty((0, 3), dtype=np.float32), np.empty((0, 2), dtype=np.float32)
    return np.asarray(object_points, dtype=np.float32).reshape(-1, 3), np.asarray(image_points, dtype=np.float32).reshape(-1, 2)


def _reshape_object_points(points) -> np.ndarray:
    if points is None:
        return np.empty((0, 3), dtype=np.float32)
    return np.asarray(points, dtype=np.float32).reshape(-1, 3)


def _reshape_image_points(points) -> np.ndarray:
    if points is None:
        return np.empty((0, 2), dtype=np.float32)
    return np.asarray(points, dtype=np.float32).reshape(-1, 2)


def _t_world_cam_from_board_to_camera(rotation_cam_board: np.ndarray, t_cam_board: np.ndarray) -> np.ndarray:
    rotation_cam_board = np.asarray(rotation_cam_board, dtype=np.float64).reshape(3, 3)
    t_cam_board = np.asarray(t_cam_board, dtype=np.float64).reshape(3)
    T_world_cam = np.eye(4, dtype=np.float64)
    T_world_cam[:3, :3] = rotation_cam_board.T
    T_world_cam[:3, 3] = -rotation_cam_board.T @ t_cam_board
    return T_world_cam


def _board_spec_from_manifest(manifest: dict[str, Any]) -> CharucoBoardSpec:
    board = manifest.get("board") or {}
    return CharucoBoardSpec(
        squares_x=int(board["squares_x"]),
        squares_y=int(board["squares_y"]),
        square_length_m=float(board["square_mm"]) / 1000.0,
        marker_length_m=float(board["marker_mm"]) / 1000.0,
        dictionary=str(board["dictionary"]),
    )


def _observation_from_event(item: dict[str, Any]) -> ExtrinsicObservation:
    return ExtrinsicObservation(
        camera_id=int(item.get("camera_id", 0)),
        label=str(item.get("label") or f"camera_{int(item.get('camera_id', 0))}"),
        device=str(item.get("device") or ""),
        frame_seq=int(item.get("frame_seq", 0)),
        image_size=(int(item["image_width"]), int(item["image_height"])),
        dictionary=str(item.get("dictionary") or ""),
        corner_ids=np.asarray(item["corner_ids"], dtype=np.int32).reshape(-1),
        corners_px=np.asarray(item["corners_px"], dtype=np.float32).reshape(-1, 2),
    )


def _parse_observation(line: str) -> dict[str, Any]:
    import json

    return json.loads(line) if line.strip() else {}


def _dataset_dictionary(dataset: FixedBoardExtrinsicDataset) -> str:
    for observations in dataset.observations_by_label.values():
        for observation in observations:
            if observation.dictionary:
                return observation.dictionary
    return dictionary_candidates(dataset.board.dictionary)[0]


def _weighted_rms(values: list[tuple[float, int]]) -> float:
    numerator = sum((rms**2) * count for rms, count in values)
    denominator = sum(count for _rms, count in values)
    if denominator <= 0:
        return 0.0
    return float(np.sqrt(numerator / denominator))


def _camera_sort_key(observations: list[ExtrinsicObservation]) -> tuple[int, str]:
    if not observations:
        return 0, ""
    first = observations[0]
    return first.camera_id, first.label


def _import_aruco():
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("OpenCV is required. Install with: python -m pip install -e '.[camera]'") from exc
    if not hasattr(cv2, "aruco"):
        raise RuntimeError("OpenCV aruco module is required. Install opencv-contrib-python-headless.")
    return cv2, cv2.aruco
