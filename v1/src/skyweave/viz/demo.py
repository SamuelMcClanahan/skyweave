"""
Demo script to test the visualizer with simulated data.

This creates synthetic tracks, cameras, and voxels and streams them to the viz server.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import time
from pathlib import Path

from skyweave.viz.server import VizServer, build_viz_frame

logger = logging.getLogger(__name__)

CAMERA_TARGET = (0.0, 0.0, 250.0)
MIN_SUPPORTING_CAMERAS = 2
MAX_CAMERA_RANGE_M = 1400.0
DEMO_FPS = 30.0
GRID_ORIGIN = (-3000.0, -20000.0, 0.0)
GRID_DIMS = (1200, 2200, 250)
VOXEL_SIZE_M = 10.0
RAY_EVIDENCE_RADIUS_M = 16.0
RAY_EVIDENCE_WINDOW_XY_M = 110.0
RAY_EVIDENCE_WINDOW_Z_M = 80.0
SCENARIOS = ("orbit-drone", "erratic-drone-solo", "airport-mixed", "high-altitude-plane")
SFO_BAY_CAMERA_TARGETS = [
    (6100.0, -17600.0, 1500.0),
    (6500.0, -16800.0, 1550.0),
    (6800.0, -16000.0, 1580.0),
    (6500.0, -15200.0, 1550.0),
    (6100.0, -14400.0, 1500.0),
]
CAMERA_ARRAYS: dict[str, list[tuple[float, float, float]]] = {
    "roof-triangle": [
        (0.0, -520.0, 45.0),
        (-470.0, 360.0, 38.0),
        (470.0, 360.0, 40.0),
    ],
    "perimeter-6": [
        (0.0, -850.0, 14.0),
        (-735.0, -425.0, 18.0),
        (-735.0, 425.0, 34.0),
        (0.0, 850.0, 28.0),
        (735.0, 425.0, 34.0),
        (735.0, -425.0, 18.0),
    ],
    "roofline-4": [
        (-600.0, -260.0, 32.0),
        (-200.0, -290.0, 26.0),
        (200.0, -290.0, 26.0),
        (600.0, -260.0, 32.0),
    ],
    "mixed-8": [
        (-900.0, -650.0, 16.0),
        (-300.0, -760.0, 24.0),
        (300.0, -760.0, 24.0),
        (900.0, -650.0, 16.0),
        (-760.0, 360.0, 42.0),
        (-260.0, 720.0, 36.0),
        (260.0, 720.0, 36.0),
        (760.0, 360.0, 42.0),
    ],
    "sfo-bay-10": [
        # Original bay cameras
        (1800.0, -18450.0, 12.0),
        (2350.0, -17400.0, 16.0),
        (2950.0, -16250.0, 18.0),
        (3500.0, -15150.0, 16.0),
        (4050.0, -14100.0, 12.0),
        # Downtown cameras for slow drone
        (-400.0, -500.0, 25.0),
        (400.0, -500.0, 25.0),
        (0.0, 500.0, 30.0),
        (-650.0, 950.0, 38.0),
        (650.0, 950.0, 38.0),
    ],
}
CAMERA_ARRAY_TARGETS = {
    "sfo-bay-10": [
        # Bay camera targets (original 5)
        (6100.0, -17600.0, 1500.0),
        (6500.0, -16800.0, 1550.0),
        (6800.0, -16000.0, 1580.0),
        (6500.0, -15200.0, 1550.0),
        (6100.0, -14400.0, 1500.0),
        # Downtown camera targets for slow drone support
        (0.0, 0.0, 180.0),  # CAM 5
        (0.0, 0.0, 180.0),  # CAM 6
        (0.0, 0.0, 180.0),  # CAM 7
        (0.0, 0.0, 180.0),  # CAM 8
        (0.0, 0.0, 180.0),  # CAM 9
    ],
}
CAMERA_ARRAY_FOVS = {
    "sfo-bay-10": (56.0, 38.0),
}
CAMERA_ARRAY_RANGES = {
    "sfo-bay-10": 7200.0,
}


def default_camera_array_for_scenario(scenario: str) -> str:
    if scenario in ("airport-mixed", "erratic-drone-solo", "high-altitude-plane"):
        return "sfo-bay-10"
    return "perimeter-6"


def look_at_quat(
    position: tuple[float, float, float],
    target: tuple[float, float, float] = CAMERA_TARGET,
) -> list[float]:
    """Quaternion rotating local +Z toward the target point."""
    dx = target[0] - position[0]
    dy = target[1] - position[1]
    dz = target[2] - position[2]
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length == 0:
        return [0.0, 0.0, 0.0, 1.0]

    vx, vy, vz = dx / length, dy / length, dz / length
    dot = vz
    if dot < -0.999999:
        return [1.0, 0.0, 0.0, 0.0]

    qx = -vy
    qy = vx
    qz = 0.0
    qw = 1.0 + dot
    q_len = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    return [qx / q_len, qy / q_len, qz / q_len, qw / q_len]


def rotate_vector_by_quat(
    vector: tuple[float, float, float],
    quat: list[float],
) -> tuple[float, float, float]:
    qx, qy, qz, qw = quat
    vx, vy, vz = vector

    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)

    return (
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    )


def world_to_camera_vector(
    vector: tuple[float, float, float],
    rotation_quat: list[float],
) -> tuple[float, float, float]:
    inverse_quat = [-rotation_quat[0], -rotation_quat[1], -rotation_quat[2], rotation_quat[3]]
    return rotate_vector_by_quat(vector, inverse_quat)


def visible_camera_ids(cameras: list[dict], position: list[float]) -> list[int]:
    visible_ids = []
    for camera in cameras:
        dx = position[0] - camera["position"][0]
        dy = position[1] - camera["position"][1]
        dz = position[2] - camera["position"][2]
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if distance > camera.get("max_range_m", MAX_CAMERA_RANGE_M):
            continue

        local_x, local_y, local_z = world_to_camera_vector((dx, dy, dz), camera["rotation_quat"])
        if local_z <= 0.0:
            continue

        h_angle = abs(math.degrees(math.atan2(local_x, local_z)))
        v_angle = abs(math.degrees(math.atan2(local_y, local_z)))
        if h_angle <= camera["fov_h_deg"] / 2.0 and v_angle <= camera["fov_v_deg"] / 2.0:
            visible_ids.append(camera["id"])

    return visible_ids


def create_demo_cameras(array_name: str) -> list[dict]:
    """Create a realistic demo camera array over the local ENU scene."""
    import random
    positions = CAMERA_ARRAYS[array_name]
    fov_h_deg, fov_v_deg = CAMERA_ARRAY_FOVS.get(array_name, (72.0, 52.0))
    max_range_m = CAMERA_ARRAY_RANGES.get(array_name, MAX_CAMERA_RANGE_M)
    return [
        {
            "id": camera_id,
            "position": [float(x), float(y), float(z)],
            "rotation_quat": look_at_quat((x, y, z), target=camera_target(array_name, camera_id)),
            "fov_h_deg": fov_h_deg,
            "fov_v_deg": fov_v_deg,
            "max_range_m": max_range_m,
            "fps": 100.0,
            "online": True,
            "fps_actual": 100.0 * (0.97 + random.random() * 0.05),
            "latency_ms": 20.0 + random.random() * 15.0,
            "dropped_frames": 0,
            "motion_pixel_count": 0,
        }
        for camera_id, (x, y, z) in enumerate(positions)
    ]


def camera_target(array_name: str, camera_id: int) -> tuple[float, float, float]:
    target = CAMERA_ARRAY_TARGETS.get(array_name, CAMERA_TARGET)
    if isinstance(target, list):
        return target[min(camera_id, len(target) - 1)]
    return target


def create_demo_track(
    track_id: int,
    frame: int,
    total_frames: int,
    supporting_camera_ids: list[int] | None = None,
) -> dict:
    """Create a demo track flying over downtown SF."""
    return build_track_payload(
        track_id=track_id,
        frame=frame,
        total_frames=total_frames,
        position_fn=orbit_drone_position,
        classification="drone",
        supporting_camera_ids=supporting_camera_ids or [],
    )


def create_erratic_drone_track(
    track_id: int,
    frame: int,
    total_frames: int,
    supporting_camera_ids: list[int] | None = None,
) -> dict:
    """Create a slow, erratic small-drone track."""
    return build_track_payload(
        track_id=track_id,
        frame=frame,
        total_frames=total_frames,
        position_fn=erratic_drone_position,
        classification="drone",
        supporting_camera_ids=supporting_camera_ids or [],
        covariance_scale=0.18,
    )


def create_plane_track(
    track_id: int,
    frame: int,
    total_frames: int,
    supporting_camera_ids: list[int] | None = None,
) -> dict:
    """Create a high-altitude aircraft track crossing the array."""
    return build_track_payload(
        track_id=track_id,
        frame=frame,
        total_frames=total_frames,
        position_fn=plane_position,
        classification="plane",
        supporting_camera_ids=supporting_camera_ids or [],
        covariance_scale=0.45,
    )


def create_slow_downtown_drone_track(
    track_id: int,
    frame: int,
    total_frames: int,
    supporting_camera_ids: list[int] | None = None,
) -> dict:
    """Create a slow downtown drone track with small radius orbit."""
    return build_track_payload(
        track_id=track_id,
        frame=frame,
        total_frames=total_frames,
        position_fn=slow_downtown_drone_position,
        classification="drone",
        supporting_camera_ids=supporting_camera_ids or [],
        covariance_scale=0.12,
    )


def orbit_drone_position(frame: int, total_frames: int) -> tuple[float, float, float]:
    t = frame / max(total_frames - 1, 1)
    radius = 600.0
    angle = t * 2.0 * math.pi
    return (
        radius * math.cos(angle),
        radius * math.sin(angle),
        300.0 + 50.0 * math.sin(t * 4.0 * math.pi),
    )


def erratic_drone_position(frame: int, total_frames: int) -> tuple[float, float, float]:
    phase = frame / max(total_frames - 1, 1)
    return (
        5200.0 + 250.0 * phase + 8.0 * math.sin(phase * 5.0 * math.pi),
        -16600.0 + 95.0 * math.sin(phase * 2.0 * math.pi + 0.7) + 8.0 * math.sin(phase * 9.0 * math.pi),
        115.0 + 8.0 * math.sin(phase * 4.0 * math.pi) + 3.0 * math.sin(phase * 13.0 * math.pi),
    )


def plane_position(frame: int, total_frames: int) -> tuple[float, float, float]:
    phase = frame / max(total_frames - 1, 1)
    return (
        5700.0 + 2400.0 * phase,
        -18600.0 + 5200.0 * phase + 180.0 * math.sin(phase * math.pi),
        1650.0 + 120.0 * math.sin(phase * math.pi),
    )


def slow_downtown_drone_position(frame: int, total_frames: int) -> tuple[float, float, float]:
    """Slow small-radius drone orbiting downtown SF area (near origin)."""
    phase = frame / max(total_frames - 1, 1)
    # Small orbit radius (200m), slow speed (15 m/s ~= 54 km/h)
    # Period: 2π * 200 / 15 ≈ 84 seconds at 30 FPS = 2520 frames
    angle = phase * 2.0 * math.pi * (total_frames / 2520.0)
    radius = 200.0
    return (
        radius * math.cos(angle),
        radius * math.sin(angle),
        180.0 + 15.0 * math.sin(phase * 3.0 * math.pi),  # Gentle altitude variation
    )


def build_track_payload(
    track_id: int,
    frame: int,
    total_frames: int,
    position_fn,
    classification: str,
    supporting_camera_ids: list[int],
    covariance_scale: float = 0.1,
) -> dict:
    x, y, z = position_fn(frame, total_frames)
    vx, vy, vz = estimate_velocity(position_fn, frame, total_frames)
    trail = build_trail(position_fn, frame, total_frames)
    has_support = len(supporting_camera_ids) >= MIN_SUPPORTING_CAMERAS
    status = "active" if frame > 5 and has_support else "candidate" if frame <= 5 else "coasting"
    classification_confidence = 0.85 if has_support else 0.45

    return {
        "id": track_id,
        "state": [x, y, z, vx, vy, vz],
        "covariance": [[covariance_scale] * 6 for _ in range(6)],
        "status": status,
        "classification": classification,
        "classification_confidence": classification_confidence,
        "created_ts_ns": 0,
        "last_update_ts_ns": int(time.time() * 1_000_000_000),
        "update_count": frame,
        "miss_count": 0 if has_support else 1,
        "trail": trail,
        "visible_camera_ids": supporting_camera_ids,
    }


def estimate_velocity(position_fn, frame: int, total_frames: int) -> tuple[float, float, float]:
    prev_frame = max(0, frame - 1)
    next_frame = min(total_frames - 1, frame + 1)
    if prev_frame == next_frame:
        return (0.0, 0.0, 0.0)
    prev_pos = position_fn(prev_frame, total_frames)
    next_pos = position_fn(next_frame, total_frames)
    dt_s = (next_frame - prev_frame) / DEMO_FPS
    return (
        (next_pos[0] - prev_pos[0]) / dt_s,
        (next_pos[1] - prev_pos[1]) / dt_s,
        (next_pos[2] - prev_pos[2]) / dt_s,
    )


def build_trail(position_fn, frame: int, total_frames: int) -> list[list[float]]:
    trail = []
    for i in range(max(0, frame - 60), frame + 1):
        x, y, z = position_fn(i, total_frames)
        ts_trail = int(i * (1.0 / DEMO_FPS) * 1_000_000_000)
        trail.append([x, y, z, ts_trail])
    return trail


def create_demo_voxels(
    frame: int,
    cameras: list[dict],
    track_position: list[float],
    supporting_camera_ids: list[int],
) -> list[dict]:
    """Create fused demo voxels from intersecting camera line-of-sight evidence."""
    if len(supporting_camera_ids) < MIN_SUPPORTING_CAMERAS:
        return []

    camera_by_id = {camera["id"]: camera for camera in cameras}
    supporting_cameras = [camera_by_id[camera_id] for camera_id in supporting_camera_ids if camera_id in camera_by_id]
    if len(supporting_cameras) < MIN_SUPPORTING_CAMERAS:
        return []

    center_ix, center_iy, center_iz = world_to_voxel(track_position)
    xy_radius_voxels = int(math.ceil(RAY_EVIDENCE_WINDOW_XY_M / VOXEL_SIZE_M))
    z_radius_voxels = int(math.ceil(RAY_EVIDENCE_WINDOW_Z_M / VOXEL_SIZE_M))

    voxels = []
    for ix in range(center_ix - xy_radius_voxels, center_ix + xy_radius_voxels + 1):
        for iy in range(center_iy - xy_radius_voxels, center_iy + xy_radius_voxels + 1):
            for iz in range(center_iz - z_radius_voxels, center_iz + z_radius_voxels + 1):
                if not voxel_in_bounds(ix, iy, iz):
                    continue

                center = voxel_center(ix, iy, iz)
                score = 0.0
                camera_hits = 0
                for camera in supporting_cameras:
                    camera_pos = tuple(camera["position"])
                    distance = distance_to_line_segment(center, camera_pos, tuple(track_position))
                    if distance > RAY_EVIDENCE_RADIUS_M:
                        continue
                    camera_hits += 1
                    score += 1.0 + (1.0 - distance / RAY_EVIDENCE_RADIUS_M) * 1.6

                if camera_hits >= MIN_SUPPORTING_CAMERAS:
                    truth_distance = distance_between(center, tuple(track_position))
                    focus_bonus = max(0.0, 1.0 - truth_distance / RAY_EVIDENCE_WINDOW_XY_M)
                    voxels.append(
                        {
                            "ix": ix,
                            "iy": iy,
                            "iz": iz,
                            "score": score + focus_bonus,
                            "support_count": camera_hits,
                        }
                    )

    return sorted(voxels, key=lambda item: item["score"], reverse=True)[:400]


def create_demo_weavefield(
    frame: int,
    cameras: list[dict],
    track_position: list[float],
    supporting_camera_ids: list[int],
) -> dict:
    """Create a demo weavefield volume over SF."""
    voxels = create_demo_voxels(frame, cameras, track_position, supporting_camera_ids)

    return {
        "ts_ns": int(time.time() * 1_000_000_000),
        "grid": {
            "frame_id": "world",
            "origin": list(GRID_ORIGIN),
            "voxel_size_m": VOXEL_SIZE_M,
            "dims": list(GRID_DIMS),
        },
        "voxels": voxels,
        "peaks": [],
        "decay_s": 1.0,
        "source_packet_ids": [f"cam{camera_id}" for camera_id in supporting_camera_ids],
    }


def create_combined_weavefield(frame: int, cameras: list[dict], tracks: list[dict]) -> dict:
    """Create one demo weavefield containing evidence for all supported tracks."""
    voxels = []
    source_packet_ids: set[str] = set()
    for track in tracks:
        supporting_ids = track.get("visible_camera_ids", [])
        track_voxels = create_demo_voxels(frame, cameras, track["state"][:3], supporting_ids)
        voxels.extend(track_voxels)
        source_packet_ids.update(f"cam{camera_id}" for camera_id in supporting_ids)

    sorted_voxels = sorted(voxels, key=lambda item: item["score"], reverse=True)[:800]

    # Extract peaks (top 5 highest-score voxels)
    peaks = []
    for voxel in sorted_voxels[:5]:
        x = GRID_ORIGIN[0] + (voxel["ix"] + 0.5) * VOXEL_SIZE_M
        y = GRID_ORIGIN[1] + (voxel["iy"] + 0.5) * VOXEL_SIZE_M
        z = GRID_ORIGIN[2] + (voxel["iz"] + 0.5) * VOXEL_SIZE_M
        peaks.append({"position": [x, y, z], "score": voxel["score"]})

    return {
        "ts_ns": int(time.time() * 1_000_000_000),
        "grid": {
            "frame_id": "world",
            "origin": list(GRID_ORIGIN),
            "voxel_size_m": VOXEL_SIZE_M,
            "dims": list(GRID_DIMS),
        },
        "voxels": sorted_voxels,
        "peaks": peaks,
        "decay_s": 1.0,
        "source_packet_ids": sorted(source_packet_ids),
    }


def create_scenario_tracks(scenario: str, frame: int, total_frames: int, cameras: list[dict]) -> list[dict]:
    if scenario == "orbit-drone":
        return [create_supported_track(create_demo_track, 1, frame, total_frames, cameras)]
    if scenario == "erratic-drone-solo":
        return [create_supported_track(create_erratic_drone_track, 1, frame, total_frames, cameras)]
    if scenario == "high-altitude-plane":
        return [create_supported_track(create_plane_track, 1, frame, total_frames, cameras)]
    if scenario == "airport-mixed":
        return [
            create_supported_track(create_erratic_drone_track, 1, frame, total_frames, cameras),
            create_supported_track(create_plane_track, 2, frame, total_frames, cameras),
            create_supported_track(create_slow_downtown_drone_track, 3, frame, total_frames, cameras),
        ]
    raise ValueError(f"unknown demo scenario: {scenario}")


def create_supported_track(track_fn, track_id: int, frame: int, total_frames: int, cameras: list[dict]) -> dict:
    provisional = track_fn(track_id, frame, total_frames)
    supporting_ids = visible_camera_ids(cameras, provisional["state"][:3])
    return track_fn(track_id, frame, total_frames, supporting_ids)


def world_to_voxel(position: list[float] | tuple[float, float, float]) -> tuple[int, int, int]:
    return (
        int((position[0] - GRID_ORIGIN[0]) / VOXEL_SIZE_M),
        int((position[1] - GRID_ORIGIN[1]) / VOXEL_SIZE_M),
        int((position[2] - GRID_ORIGIN[2]) / VOXEL_SIZE_M),
    )


def voxel_in_bounds(ix: int, iy: int, iz: int) -> bool:
    return 0 <= ix < GRID_DIMS[0] and 0 <= iy < GRID_DIMS[1] and 0 <= iz < GRID_DIMS[2]


def voxel_center(ix: int, iy: int, iz: int) -> tuple[float, float, float]:
    return (
        GRID_ORIGIN[0] + (ix + 0.5) * VOXEL_SIZE_M,
        GRID_ORIGIN[1] + (iy + 0.5) * VOXEL_SIZE_M,
        GRID_ORIGIN[2] + (iz + 0.5) * VOXEL_SIZE_M,
    )


def distance_to_line_segment(
    point: tuple[float, float, float],
    start: tuple[float, float, float],
    end: tuple[float, float, float],
) -> float:
    sx, sy, sz = start
    ex, ey, ez = end
    px, py, pz = point
    vx, vy, vz = ex - sx, ey - sy, ez - sz
    wx, wy, wz = px - sx, py - sy, pz - sz
    segment_len_sq = vx * vx + vy * vy + vz * vz
    if segment_len_sq <= 0.0:
        return distance_between(point, start)
    t = max(0.0, min(1.0, (wx * vx + wy * vy + wz * vz) / segment_len_sq))
    closest = (sx + t * vx, sy + t * vy, sz + t * vz)
    return distance_between(point, closest)


def distance_between(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


async def stream_demo_data(server: VizServer, camera_array: str, scenario: str) -> None:
    """Stream demo data to the visualizer."""
    logger.info("Starting demo data stream...")

    cameras = create_demo_cameras(camera_array)
    frame = 0
    total_frames = 1200

    while True:
        tracks = create_scenario_tracks(scenario, frame, total_frames, cameras)
        weavefield = create_combined_weavefield(frame, cameras, tracks)

        # Collect supporting camera IDs safely
        supporting_ids_set = set()
        for track in tracks:
            visible_ids = track.get("visible_camera_ids", [])
            if visible_ids:
                supporting_ids_set.update(visible_ids)
        supporting_ids = sorted(supporting_ids_set)

        # Keep last 30 frames of weavefield history
        weavefield_history = [weavefield]

        # Build and broadcast VizFrame
        viz_frame = build_viz_frame(
            tracks=tracks,
            cameras=cameras,
            weavefield_history=weavefield_history,
            measurements=[],
            stats={
                "fps": DEMO_FPS,
                "latency_p50_ms": 25.0,
                "n_tracks": len(tracks),
                "n_voxels": len(weavefield["voxels"]),
                "n_cameras": len(cameras),
                "n_visible_cameras": len(supporting_ids),
            },
            ts_ns=int(time.time() * 1_000_000_000),
        )

        await server.broadcast_viz_frame(viz_frame)

        # Advance frame
        frame = (frame + 1) % total_frames

        # Sleep to maintain ~30fps
        await asyncio.sleep(1.0 / DEMO_FPS)


async def main(args: argparse.Namespace) -> None:
    """Run the demo server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    viz_dir = Path(__file__).parent.parent.parent.parent / "viz_web"
    if not viz_dir.exists():
        logger.error(f"viz_web directory not found at {viz_dir}")
        return

    # Create and start server
    server = VizServer(viz_dir, host=args.host, port=args.port)
    await server.start()

    logger.info("=" * 60)
    logger.info("Demo Visualization Server Running")
    logger.info("=" * 60)
    logger.info("")
    logger.info("  Open http://localhost:%s in your browser", args.port)
    logger.info("")
    logger.info("  The visualizer will show:")
    logger.info("    - %s scenario", args.scenario)
    logger.info("    - %s camera array: %d cameras", args.camera_array, len(CAMERA_ARRAYS[args.camera_array]))
    logger.info("    - ray-intersection voxel evidence")
    logger.info("    - 12km x 22km x 2.5km surveillance volume")
    logger.info("")
    logger.info("=" * 60)
    logger.info("")

    # Start streaming demo data
    await stream_demo_data(server, args.camera_array, args.scenario)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Skyweave live visualizer demo.")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8080, help="HTTP/WebSocket port.")
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="orbit-drone",
        help="Synthetic movement scenario to stream.",
    )
    parser.add_argument(
        "--camera-array",
        choices=sorted(CAMERA_ARRAYS),
        default=None,
        help="Synthetic camera layout to stream.",
    )
    parser.add_argument("--list-camera-arrays", action="store_true", help="Print available camera arrays and exit.")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    if args.camera_array is None:
        args.camera_array = default_camera_array_for_scenario(args.scenario)
    if args.list_camera_arrays:
        for name, positions in sorted(CAMERA_ARRAYS.items()):
            print(f"{name}: {len(positions)} cameras")
        return

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        logger.info("Demo server stopped")


if __name__ == "__main__":
    run()
