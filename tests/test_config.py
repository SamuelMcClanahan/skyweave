from skyweave.config import load_config


def test_load_default_config() -> None:
    config = load_config("configs/sim.yaml")
    assert config.rayweave.grid.voxel_size_m == 0.10
    assert config.simulation.focal_length_px == 360.0


def test_load_resolution_profiles() -> None:
    assert load_config("configs/sim_075.yaml").rayweave.grid.voxel_size_m == 0.075
    assert load_config("configs/sim_05.yaml").rayweave.grid.voxel_size_m == 0.05


def test_load_room_perimeter_camera_count_profiles() -> None:
    for count in (3, 5, 7, 9, 11, 13, 15):
        config = load_config(f"configs/sim_mvp_ov9281_100hz_{count:02d}cam_perimeter_numba.yaml")
        assert config.simulation.camera_count == count
        assert config.simulation.camera_layout == "room_perimeter"
        assert config.rayweave.grid.voxel_size_m == 0.05
        assert config.rayweave.scorer.min_supporting_cameras == 2
        assert config.fusion.min_cameras_per_frame == 2


def test_load_pixel_plane_rendered_profile() -> None:
    config = load_config("configs/sim_pixel_plane_07cam_rendered_numba.yaml")
    assert config.simulation.scene == "pixel_plane_crossing"
    assert config.simulation.camera_count == 7
    assert config.simulation.camera_layout == "dispersed_perimeter"
    assert config.simulation.render_object_radius_m < 0.05
    assert config.rayweave.grid.voxel_size_m == 0.50
    assert config.rayweave.scorer.min_supporting_cameras == 3
    assert config.fusion.min_cameras_per_frame == 3
