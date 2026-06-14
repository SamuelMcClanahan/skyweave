from __future__ import annotations

import numpy as np

from skyweave.camera.motion import MotionPacketConfig
from skyweave.config import SimCheckConfig
from skyweave.log import JsonlLogger
from skyweave.sim.check import run_sim_check
from skyweave.sim.rendered import RenderedFrameGenerator, rendered_motion_packets
from skyweave.sim.scene import GroundTruthSample, build_scene


def test_rendered_frames_have_camera_images_and_visibility_metadata() -> None:
    config = SimCheckConfig()
    config.simulation.frames = 3
    config.simulation.image_width = 96
    config.simulation.image_height = 72
    config.simulation.focal_length_px = 64
    scene = build_scene(config.simulation)

    frame = RenderedFrameGenerator(scene, config.simulation).frames()[1]

    assert len(frame.camera_frames) == config.simulation.camera_count
    assert set(frame.camera_meta) == set(scene.cameras)
    assert any(meta.visible for meta in frame.camera_meta.values())
    for camera_frame in frame.camera_frames:
        assert camera_frame.gray.shape == (72, 96)
        assert camera_frame.gray.dtype == np.uint8
        assert np.isfinite(camera_frame.gray).all()


def test_rendered_object_radius_scales_with_depth() -> None:
    config = SimCheckConfig()
    config.simulation.image_width = 96
    config.simulation.image_height = 72
    config.simulation.focal_length_px = 64
    config.simulation.render_object_radius_m = 0.10
    scene = build_scene(config.simulation)
    camera = scene.cameras[0]
    forward = camera.T_world_cam[:3, 2]
    generator = RenderedFrameGenerator(scene, config.simulation)

    near = GroundTruthSample(0, 0, camera.position + forward * 1.0, np.zeros(3))
    far = GroundTruthSample(1, 1, camera.position + forward * 2.0, np.zeros(3))
    _near_gray, near_meta = generator._render_camera(camera, near, None)
    _far_gray, far_meta = generator._render_camera(camera, far, None)

    assert near_meta.visible
    assert far_meta.visible
    assert near_meta.radius_px > far_meta.radius_px


def test_rendered_frames_feed_existing_frame_diff_thresholding() -> None:
    config = SimCheckConfig()
    config.simulation.frames = 3
    config.simulation.image_width = 96
    config.simulation.image_height = 72
    config.simulation.focal_length_px = 64
    config.simulation.render_object_radius_m = 0.08
    scene = build_scene(config.simulation)
    frames = RenderedFrameGenerator(scene, config.simulation).frames()

    builders = {}
    previous = {}
    motion_config = MotionPacketConfig(threshold=16, min_area_px=1, backend="python")
    first_packets = rendered_motion_packets(frames[0], builders, previous, config=motion_config)
    second_packets = rendered_motion_packets(frames[1], builders, previous, config=motion_config)

    assert sum(len(packet.blobs) for packet in first_packets) == 0
    assert sum(len(packet.blobs) for packet in second_packets) > 0

    builders = {}
    previous = {}
    high_threshold = MotionPacketConfig(threshold=255, min_area_px=1, backend="python")
    rendered_motion_packets(frames[0], builders, previous, config=high_threshold)
    suppressed = rendered_motion_packets(frames[1], builders, previous, config=high_threshold)

    assert sum(len(packet.blobs) for packet in suppressed) == 0


def test_rendered_sim_check_tracks_with_bounded_error(tmp_path) -> None:
    config = SimCheckConfig()
    config.simulation.frames = 12
    config.simulation.image_width = 96
    config.simulation.image_height = 72
    config.simulation.focal_length_px = 64
    config.simulation.camera_count = 3
    config.simulation.render_object_radius_m = 0.08
    config.rayweave.grid.origin_m = (-2.0, -2.0, 0.0)
    config.rayweave.grid.dims = (20, 20, 12)
    config.rayweave.grid.voxel_size_m = 0.20
    config.rayweave.scorer.backend = "python_numpy"
    config.rayweave.scorer.top_k_voxels = 250
    config.pass_peak_rmse_m = 0.45
    config.pass_track_rmse_m = 0.45
    config.logging.log_dir = str(tmp_path / "logs")

    logger = JsonlLogger(config.logging.log_dir)
    try:
        summary = run_sim_check(config, logger, source="rendered")
    finally:
        logger.close()

    assert summary.passed
    assert summary.track_rmse_m < 0.45
