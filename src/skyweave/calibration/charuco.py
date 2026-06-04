from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class CharucoBoardSpec:
    squares_x: int
    squares_y: int
    square_length_m: float
    marker_length_m: float
    dictionary: str


@dataclass(frozen=True)
class CharucoDetection:
    detected: bool
    dictionary: str
    marker_count: int
    corner_count: int
    image_width: int
    image_height: int


@dataclass(frozen=True)
class SerializedCharucoObservation:
    corner_ids: list[int]
    corners_px: list[list[float]]
    marker_ids: list[int]
    marker_corners_px: list[list[list[float]]]


def dictionary_candidates(name: str) -> list[str]:
    normalized = name.strip().upper()
    aliases = {
        "4X4": "DICT_4X4",
        "5X5": "DICT_5X5",
        "6X6": "DICT_6X6",
        "7X7": "DICT_7X7",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"DICT_4X4", "ARUCO_DICT_4X4"}:
        return ["DICT_4X4_50", "DICT_4X4_100", "DICT_4X4_250", "DICT_4X4_1000"]
    if normalized in {"DICT_5X5", "ARUCO_DICT_5X5"}:
        return ["DICT_5X5_50", "DICT_5X5_100", "DICT_5X5_250", "DICT_5X5_1000"]
    if normalized in {"DICT_6X6", "ARUCO_DICT_6X6"}:
        return ["DICT_6X6_50", "DICT_6X6_100", "DICT_6X6_250", "DICT_6X6_1000"]
    if normalized in {"DICT_7X7", "ARUCO_DICT_7X7"}:
        return ["DICT_7X7_50", "DICT_7X7_100", "DICT_7X7_250", "DICT_7X7_1000"]
    return [normalized]


def detect_charuco(gray: np.ndarray, spec: CharucoBoardSpec) -> tuple[CharucoDetection, object | None]:
    cv2, aruco = _import_aruco()
    image = np.ascontiguousarray(gray.astype(np.uint8, copy=False))
    if image.ndim != 2:
        raise ValueError(f"expected grayscale image, got shape {image.shape}")

    best_detection: CharucoDetection | None = None
    best_payload = None
    for dictionary_name in dictionary_candidates(spec.dictionary):
        dictionary_id = getattr(aruco, dictionary_name, None)
        if dictionary_id is None:
            continue
        dictionary = aruco.getPredefinedDictionary(dictionary_id)
        board = create_board(aruco, spec, dictionary)
        marker_corners, marker_ids, _ = _detect_markers(aruco, image, dictionary)
        marker_count = 0 if marker_ids is None else int(len(marker_ids))
        charuco_corners, charuco_ids = _interpolate_charuco(
            aruco,
            marker_corners,
            marker_ids,
            image,
            board,
        )
        corner_count = 0 if charuco_ids is None else int(len(charuco_ids))
        detection = CharucoDetection(
            detected=corner_count > 0,
            dictionary=dictionary_name,
            marker_count=marker_count,
            corner_count=corner_count,
            image_width=int(image.shape[1]),
            image_height=int(image.shape[0]),
        )
        payload = {
            "dictionary": dictionary,
            "marker_corners": marker_corners,
            "marker_ids": marker_ids,
            "charuco_corners": charuco_corners,
            "charuco_ids": charuco_ids,
        }
        if best_detection is None or _score_detection(detection) > _score_detection(best_detection):
            best_detection = detection
            best_payload = payload

    if best_detection is None:
        raise ValueError(f"no OpenCV dictionary matched {spec.dictionary!r}")
    return best_detection, best_payload


def create_board(aruco, spec: CharucoBoardSpec, dictionary):
    if hasattr(aruco, "CharucoBoard"):
        try:
            return aruco.CharucoBoard(
                (spec.squares_x, spec.squares_y),
                spec.square_length_m,
                spec.marker_length_m,
                dictionary,
            )
        except TypeError:
            pass
    if hasattr(aruco, "CharucoBoard_create"):
        return aruco.CharucoBoard_create(
            spec.squares_x,
            spec.squares_y,
            spec.square_length_m,
            spec.marker_length_m,
            dictionary,
        )
    raise RuntimeError("OpenCV build does not include ChArUco board creation.")


def draw_annotated_detection(gray: np.ndarray, payload: object | None):
    cv2, aruco = _import_aruco()
    image = np.ascontiguousarray(gray.astype(np.uint8, copy=False))
    annotated = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if isinstance(payload, dict):
        marker_corners = payload.get("marker_corners")
        marker_ids = payload.get("marker_ids")
        charuco_corners = payload.get("charuco_corners")
        charuco_ids = payload.get("charuco_ids")
        if marker_ids is not None and len(marker_ids):
            aruco.drawDetectedMarkers(annotated, marker_corners, marker_ids)
        if charuco_ids is not None and len(charuco_ids):
            aruco.drawDetectedCornersCharuco(annotated, charuco_corners, charuco_ids)
    return annotated


def write_annotated_detection(gray: np.ndarray, payload: object | None, output: Path) -> None:
    cv2, _ = _import_aruco()
    annotated = draw_annotated_detection(gray, payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), annotated):
        raise RuntimeError(f"failed to write {output}")


def serialize_detection_payload(payload: object | None) -> SerializedCharucoObservation:
    if not isinstance(payload, dict):
        return SerializedCharucoObservation([], [], [], [])
    charuco_ids = payload.get("charuco_ids")
    charuco_corners = payload.get("charuco_corners")
    marker_ids = payload.get("marker_ids")
    marker_corners = payload.get("marker_corners")
    return SerializedCharucoObservation(
        corner_ids=_flatten_ids(charuco_ids),
        corners_px=_flatten_corners(charuco_corners),
        marker_ids=_flatten_ids(marker_ids),
        marker_corners_px=[_flatten_corners(corners) for corners in marker_corners] if marker_corners is not None else [],
    )


def _detect_markers(aruco, image: np.ndarray, dictionary):
    if hasattr(aruco, "ArucoDetector"):
        parameters = aruco.DetectorParameters()
        detector = aruco.ArucoDetector(dictionary, parameters)
        return detector.detectMarkers(image)
    parameters = aruco.DetectorParameters_create()
    return aruco.detectMarkers(image, dictionary, parameters=parameters)


def _interpolate_charuco(aruco, marker_corners, marker_ids, image: np.ndarray, board) -> tuple[object | None, object | None]:
    if marker_ids is None or len(marker_ids) == 0:
        return None, None
    count, corners, ids = aruco.interpolateCornersCharuco(marker_corners, marker_ids, image, board)
    if int(count) <= 0:
        return None, None
    return corners, ids


def _score_detection(detection: CharucoDetection) -> tuple[int, int]:
    return detection.corner_count, detection.marker_count


def _flatten_ids(ids) -> list[int]:
    if ids is None:
        return []
    arr = np.asarray(ids).reshape(-1)
    return [int(value) for value in arr]


def _flatten_corners(corners) -> list[list[float]]:
    if corners is None:
        return []
    arr = np.asarray(corners, dtype=float).reshape(-1, 2)
    return [[float(x), float(y)] for x, y in arr]


def _import_aruco():
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("OpenCV is required. Install with: python -m pip install -e '.[camera]'") from exc
    if not hasattr(cv2, "aruco"):
        raise RuntimeError("OpenCV aruco module is required. Install opencv-contrib-python-headless.")
    return cv2, cv2.aruco
