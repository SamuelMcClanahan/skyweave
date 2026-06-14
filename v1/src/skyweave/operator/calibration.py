from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import yaml

from skyweave.calibration.extrinsics import load_intrinsics
from skyweave.fusion.geom import CameraCalib
from skyweave.operator.state import CalibrationStatus


def load_extrinsic_camera_calibs(path: str | Path) -> tuple[dict[int, CameraCalib], CalibrationStatus]:
    extrinsics_path = Path(path)
    if not extrinsics_path.exists():
        return {}, CalibrationStatus(
            extrinsics_path=str(extrinsics_path),
            loaded=False,
            message=f"missing {extrinsics_path}",
        )

    data = yaml.safe_load(extrinsics_path.read_text(encoding="utf-8")) or {}
    cameras_payload = data.get("cameras") or []
    if not isinstance(cameras_payload, list) or not cameras_payload:
        return {}, CalibrationStatus(
            extrinsics_path=str(extrinsics_path),
            loaded=False,
            message="extrinsics file has no cameras",
        )

    calibs: dict[int, CameraCalib] = {}
    summaries: list[dict[str, Any]] = []
    for item in cameras_payload:
        if not isinstance(item, dict):
            continue
        camera_id = int(item["camera_id"])
        intrinsics_path = _resolve_path(extrinsics_path, str(item["intrinsics_file"]))
        intrinsics = load_intrinsics(intrinsics_path)
        width = int(item.get("image_width", intrinsics.image_size[0]))
        height = int(item.get("image_height", intrinsics.image_size[1]))
        calibs[camera_id] = CameraCalib(
            id=camera_id,
            K=intrinsics.camera_matrix.copy(),
            D=intrinsics.dist_coeffs.copy(),
            width=width,
            height=height,
            T_world_cam=np.asarray(item["T_world_cam"], dtype=np.float64).reshape(4, 4),
        )
        summaries.append(
            {
                "camera_id": camera_id,
                "label": item.get("label", f"cam{camera_id + 1}"),
                "device": item.get("device", ""),
                "image_width": width,
                "image_height": height,
                "rms_reprojection_error_px": item.get("rms_reprojection_error_px"),
                "max_reprojection_error_px": item.get("max_reprojection_error_px"),
                "t_world_cam_m": item.get("t_world_cam_m", []),
            }
        )

    status = CalibrationStatus(
        extrinsics_path=str(extrinsics_path),
        loaded=bool(calibs),
        camera_count=len(calibs),
        rms_reprojection_error_px=data.get("rms_reprojection_error_px"),
        message="loaded" if calibs else "extrinsics file had no parseable cameras",
        cameras=summaries,
    )
    return calibs, status


def scale_camera_calibs(
    cameras: dict[int, CameraCalib],
    width: int,
    height: int,
) -> dict[int, CameraCalib]:
    scaled = {}
    for camera_id, camera in cameras.items():
        if camera.width == width and camera.height == height:
            scaled[camera_id] = camera
            continue
        sx = width / camera.width
        sy = height / camera.height
        k = camera.K.copy()
        k[0, 0] *= sx
        k[0, 2] *= sx
        k[1, 1] *= sy
        k[1, 2] *= sy
        scaled[camera_id] = CameraCalib(
            id=camera.id,
            K=k,
            D=camera.D.copy(),
            width=width,
            height=height,
            T_world_cam=camera.T_world_cam.copy(),
        )
    return scaled


def _resolve_path(anchor: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    candidate = anchor.parent / path
    if candidate.exists():
        return candidate
    return path
