from skyweave.config import load_config


def test_load_default_config() -> None:
    config = load_config("configs/sim.yaml")
    assert config.rayweave.grid.voxel_size_m == 0.10
    assert config.simulation.focal_length_px == 360.0


def test_load_resolution_profiles() -> None:
    assert load_config("configs/sim_075.yaml").rayweave.grid.voxel_size_m == 0.075
    assert load_config("configs/sim_05.yaml").rayweave.grid.voxel_size_m == 0.05

