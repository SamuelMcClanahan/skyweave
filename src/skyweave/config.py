from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class GridConfig(BaseModel):
    frame_id: str = "world"
    origin_m: tuple[float, float, float] = (-2.0, -2.0, 0.0)
    dims: tuple[int, int, int] = (48, 48, 32)
    voxel_size_m: float = 0.10


class ScorerConfig(BaseModel):
    min_supporting_cameras: int = 2
    top_k_voxels: int = 5000
    backend: str = "python_numpy"


class PeakConfig(BaseModel):
    threshold_percentile: float = 99.5
    max_peaks: int = 1


class RayweaveConfig(BaseModel):
    grid: GridConfig = Field(default_factory=GridConfig)
    scorer: ScorerConfig = Field(default_factory=ScorerConfig)
    peaks: PeakConfig = Field(default_factory=PeakConfig)


class SimulationConfig(BaseModel):
    scene: str = "paper_airplane_arc"
    frames: int = 90
    timestep_hz: float = 30.0
    image_width: int = 640
    image_height: int = 480
    focal_length_px: float = 360.0
    patch_size_px: int = 5
    pixel_noise_std_px: float = 0.0
    dropout_probability: float = 0.0
    timestamp_jitter_ms: float = 0.0
    false_positive_probability: float = 0.0
    seed: int = 7


class FusionConfig(BaseModel):
    align_window_ns: int = 33_000_000
    min_cameras_per_frame: int = 2
    pixel_noise_px: float = 1.0


class KalmanConfig(BaseModel):
    sigma_accel_mps2: float = 6.0
    initial_position_var: float = 1.0
    initial_velocity_var: float = 4.0
    measurement_var_scale: float = 1.0
    coast_seconds: float = 2.0


class LoggingConfig(BaseModel):
    log_dir: str = "data/logs"
    console_every: int = 1


class SimCheckConfig(BaseModel):
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)
    rayweave: RayweaveConfig = Field(default_factory=RayweaveConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    kalman: KalmanConfig = Field(default_factory=KalmanConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    pass_peak_rmse_m: float = 0.20
    pass_track_rmse_m: float = 0.20


def load_config(path: str | Path) -> SimCheckConfig:
    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return SimCheckConfig.model_validate(data)
