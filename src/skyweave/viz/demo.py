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
}


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
        if distance > MAX_CAMERA_RANGE_M:
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
    positions = CAMERA_ARRAYS[array_name]
    return [
        {
            "id": camera_id,
            "position": [float(x), float(y), float(z)],
            "rotation_quat": look_at_quat((x, y, z)),
            "fov_h_deg": 72.0,
            "fov_v_deg": 52.0,
            "fps": 100.0,
            "online": True,
        }
        for camera_id, (x, y, z) in enumerate(positions)
    ]


def create_demo_track(
    track_id: int,
    frame: int,
    total_frames: int,
    supporting_camera_ids: list[int] | None = None,
) -> dict:
    """Create a demo track flying over downtown SF."""
    t = frame / max(total_frames - 1, 1)

    # Drone circling over downtown SF at ~300m altitude
    radius = 600.0
    angle = t * 2 * math.pi

    x = radius * math.cos(angle)
    y = radius * math.sin(angle)
    z = 300.0 + 50.0 * math.sin(t * 4 * math.pi)  # Slight altitude variation

    # Velocity (tangent to circle)
    speed = 25.0  # 25 m/s
    vx = -speed * math.sin(angle)
    vy = speed * math.cos(angle)
    vz = 50.0 * 4 * math.pi * math.cos(t * 4 * math.pi) / total_frames * 30

    # Build trail from previous positions
    trail = []
    for i in range(max(0, frame - 60), frame + 1):
        t_trail = i / max(total_frames - 1, 1)
        angle_trail = t_trail * 2 * math.pi
        x_trail = radius * math.cos(angle_trail)
        y_trail = radius * math.sin(angle_trail)
        z_trail = 300.0 + 50.0 * math.sin(t_trail * 4 * math.pi)
        ts_trail = int(i * (1.0 / 30) * 1_000_000_000)
        trail.append([x_trail, y_trail, z_trail, ts_trail])

    supporting_camera_ids = supporting_camera_ids or []
    has_support = len(supporting_camera_ids) >= MIN_SUPPORTING_CAMERAS
    status = "active" if frame > 5 and has_support else "candidate" if frame <= 5 else "coasting"
    classification = "drone" if track_id == 1 else "plane" if track_id == 2 else None
    classification_confidence = 0.85 if classification and has_support else 0.45 if classification else 0.0

    return {
        "id": track_id,
        "state": [x, y, z, vx, vy, vz],
        "covariance": [[0.1] * 6 for _ in range(6)],
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


def create_demo_voxels(frame: int, track_position: list[float]) -> list[dict]:
    """Create demo voxels around the track position and scattered in the volume."""
    voxels = []

    grid_origin = [-1000.0, -1000.0, 0.0]
    voxel_size = 10.0  # 10m voxels for this scale

    # Convert track position to voxel indices
    center_ix = int((track_position[0] - grid_origin[0]) / voxel_size)
    center_iy = int((track_position[1] - grid_origin[1]) / voxel_size)
    center_iz = int((track_position[2] - grid_origin[2]) / voxel_size)

    # Strong cluster around the track (detection evidence)
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            for dz in range(-2, 3):
                ix = center_ix + dx
                iy = center_iy + dy
                iz = center_iz + dz

                # Score falls off with distance from center
                distance = math.sqrt(dx**2 + dy**2 + dz**2)
                score = max(0.0, 6.0 - distance * 0.8)

                if score > 0.5:
                    voxels.append({
                        "ix": ix,
                        "iy": iy,
                        "iz": iz,
                        "score": score,
                    })

    # Add some scattered voxels (ambient detections, noise)
    import random
    random.seed(frame)
    for _ in range(50):
        # Random position in a large volume
        ix = center_ix + random.randint(-20, 20)
        iy = center_iy + random.randint(-20, 20)
        iz = random.randint(10, 50)  # 100m to 500m altitude

        score = random.uniform(0.5, 2.0)
        voxels.append({
            "ix": ix,
            "iy": iy,
            "iz": iz,
            "score": score,
        })

    return voxels


def create_demo_weavefield(frame: int, track_position: list[float], supporting_camera_ids: list[int]) -> dict:
    """Create a demo weavefield volume over SF."""
    voxels = []
    if len(supporting_camera_ids) >= MIN_SUPPORTING_CAMERAS:
        voxels = create_demo_voxels(frame, track_position)

    return {
        "ts_ns": int(time.time() * 1_000_000_000),
        "grid": {
            "frame_id": "world",
            "origin": [-1000.0, -1000.0, 0.0],
            "voxel_size_m": 10.0,  # 10m voxels
            "dims": [200, 200, 100],  # 2km x 2km x 1km volume
        },
        "voxels": voxels,
        "peaks": [],
        "decay_s": 1.0,
        "source_packet_ids": [f"cam{camera_id}" for camera_id in supporting_camera_ids],
    }


async def stream_demo_data(server: VizServer, camera_array: str) -> None:
    """Stream demo data to the visualizer."""
    logger.info("Starting demo data stream...")

    cameras = create_demo_cameras(camera_array)
    frame = 0
    total_frames = 600  # 20 seconds for full circle at 30fps

    while True:
        provisional_track = create_demo_track(1, frame, total_frames)
        supporting_ids = visible_camera_ids(cameras, provisional_track["state"][:3])

        # Create track(s)
        track1 = create_demo_track(1, frame, total_frames, supporting_ids)
        tracks = [track1]

        # Create weavefield with voxels around the track
        track_pos = track1["state"][:3]
        weavefield = create_demo_weavefield(frame, track_pos, supporting_ids)

        # Keep last 30 frames of weavefield history
        weavefield_history = [weavefield]

        # Build and broadcast VizFrame
        viz_frame = build_viz_frame(
            tracks=tracks,
            cameras=cameras,
            weavefield_history=weavefield_history,
            measurements=[],
            stats={
                "fps": 30.0,
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
        await asyncio.sleep(1.0 / 30)


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
    logger.info("    - %s camera array: %d cameras", args.camera_array, len(CAMERA_ARRAYS[args.camera_array]))
    logger.info("    - 1 drone circling at 300m altitude")
    logger.info("    - Voxel cloud showing detection evidence")
    logger.info("    - 2km x 2km surveillance volume")
    logger.info("")
    logger.info("=" * 60)
    logger.info("")

    # Start streaming demo data
    await stream_demo_data(server, args.camera_array)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Skyweave live visualizer demo.")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8080, help="HTTP/WebSocket port.")
    parser.add_argument(
        "--camera-array",
        choices=sorted(CAMERA_ARRAYS),
        default="perimeter-6",
        help="Synthetic camera layout to stream.",
    )
    parser.add_argument("--list-camera-arrays", action="store_true", help="Print available camera arrays and exit.")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
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
