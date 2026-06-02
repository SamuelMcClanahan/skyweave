from __future__ import annotations

import numpy as np

from skyweave.fusion.geom import CameraCalib, ray_from_pixel
from skyweave.messages import DetectionPacket, Measurement3D


def triangulate_detections(
    ts_ns: int,
    detection_packets: list[DetectionPacket],
    cameras: dict[int, CameraCalib],
    pixel_noise_px: float,
) -> Measurement3D | None:
    rays: list[tuple[np.ndarray, np.ndarray, int]] = []
    for packet in detection_packets:
        if not packet.detections:
            continue
        det = max(packet.detections, key=lambda item: item.confidence)
        origin, direction = ray_from_pixel(det.cx, det.cy, cameras[packet.camera_id])
        rays.append((origin, direction, packet.camera_id))

    if len(rays) < 2:
        return None

    A = np.zeros((3, 3), dtype=np.float64)
    b = np.zeros(3, dtype=np.float64)
    I = np.eye(3, dtype=np.float64)
    for origin, direction, _camera_id in rays:
        P = I - np.outer(direction, direction)
        A += P
        b += P @ origin

    try:
        position = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None

    residuals = []
    for origin, direction, _camera_id in rays:
        residuals.append(np.linalg.norm(np.cross(position - origin, direction)))
    residual = float(np.mean(residuals)) if residuals else 1.0
    base_var = max(residual, 0.03) ** 2 + (pixel_noise_px * 0.01) ** 2

    return Measurement3D(
        ts_ns=ts_ns,
        source="triangulation",
        position=tuple(float(x) for x in position),
        covariance=np.diag([base_var, base_var, base_var]).tolist(),
        score=float(len(rays)),
        supporting_camera_ids=[camera_id for *_rest, camera_id in rays],
    )

