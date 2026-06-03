"""
Demo script to test the visualizer with simulated data.

This creates synthetic tracks, cameras, and voxels and streams them to the viz server.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from pathlib import Path

from skyweave.viz.server import VizServer, build_viz_frame

logger = logging.getLogger(__name__)


def create_demo_cameras() -> list[dict]:
    """Create a set of demo cameras over downtown San Francisco."""
    # Downtown SF coordinates: ~37.7749° N, 122.4194° W
    # Cameras positioned around the Financial District
    return [
        {
            "id": 0,
            "position": [0.0, -500.0, 150.0],  # South camera, 150m up
            "rotation_quat": [0.0, 0.0, 0.0, 1.0],
            "fov_h_deg": 60.0,
            "fov_v_deg": 45.0,
            "fps": 30.0,
            "online": True,
        },
        {
            "id": 1,
            "position": [-400.0, 300.0, 120.0],  # Northwest camera
            "rotation_quat": [0.0, 0.0, 0.0, 1.0],
            "fov_h_deg": 60.0,
            "fov_v_deg": 45.0,
            "fps": 30.0,
            "online": True,
        },
        {
            "id": 2,
            "position": [400.0, 300.0, 120.0],  # Northeast camera
            "rotation_quat": [0.0, 0.0, 0.0, 1.0],
            "fov_h_deg": 60.0,
            "fov_v_deg": 45.0,
            "fps": 30.0,
            "online": True,
        },
    ]


def create_demo_track(track_id: int, frame: int, total_frames: int) -> dict:
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

    status = "active" if frame > 5 else "candidate"
    classification = "drone" if track_id == 1 else "plane" if track_id == 2 else None

    return {
        "id": track_id,
        "state": [x, y, z, vx, vy, vz],
        "covariance": [[0.1] * 6 for _ in range(6)],
        "status": status,
        "classification": classification,
        "classification_confidence": 0.85 if classification else 0.0,
        "created_ts_ns": 0,
        "last_update_ts_ns": int(time.time() * 1_000_000_000),
        "update_count": frame,
        "miss_count": 0,
        "trail": trail,
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


def create_demo_weavefield(frame: int, track_position: list[float]) -> dict:
    """Create a demo weavefield volume over SF."""
    return {
        "ts_ns": int(time.time() * 1_000_000_000),
        "grid": {
            "frame_id": "world",
            "origin": [-1000.0, -1000.0, 0.0],
            "voxel_size_m": 10.0,  # 10m voxels
            "dims": [200, 200, 100],  # 2km x 2km x 1km volume
        },
        "voxels": create_demo_voxels(frame, track_position),
        "peaks": [],
        "decay_s": 1.0,
        "source_packet_ids": ["cam0", "cam1", "cam2"],
    }


async def stream_demo_data(server: VizServer) -> None:
    """Stream demo data to the visualizer."""
    logger.info("Starting demo data stream...")

    cameras = create_demo_cameras()
    frame = 0
    total_frames = 600  # 20 seconds for full circle at 30fps

    while True:
        # Create track(s)
        track1 = create_demo_track(1, frame, total_frames)
        tracks = [track1]

        # Create weavefield with voxels around the track
        track_pos = track1["state"][:3]
        weavefield = create_demo_weavefield(frame, track_pos)

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
            },
            ts_ns=int(time.time() * 1_000_000_000),
        )

        await server.broadcast_viz_frame(viz_frame)

        # Advance frame
        frame = (frame + 1) % total_frames

        # Sleep to maintain ~30fps
        await asyncio.sleep(1.0 / 30)


async def main() -> None:
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
    server = VizServer(viz_dir, host="0.0.0.0", port=8080)
    await server.start()

    logger.info("=" * 60)
    logger.info("Demo Visualization Server Running")
    logger.info("=" * 60)
    logger.info("")
    logger.info("  Open http://localhost:8080 in your browser")
    logger.info("")
    logger.info("  The visualizer will show:")
    logger.info("    - 3 cameras positioned around downtown SF")
    logger.info("    - 1 drone circling at 300m altitude")
    logger.info("    - Voxel cloud showing detection evidence")
    logger.info("    - 2km x 2km surveillance volume")
    logger.info("")
    logger.info("=" * 60)
    logger.info("")

    # Start streaming demo data
    await stream_demo_data(server)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Demo server stopped")
