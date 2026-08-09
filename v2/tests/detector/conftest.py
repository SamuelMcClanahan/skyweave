from __future__ import annotations

import numpy as np
import pytest

from skyweave2.detector.config import Backend, DetectorConfig
from skyweave2.eval.clips import write_clip

FULL_W, FULL_H = 384, 216
BG_DN = 60


def gaussian_blob_frame(
    center_uv: tuple[float, float], peak_dn: float = 90.0, fwhm_px: float = 5.0,
    width: int = FULL_W, height: int = FULL_H, background: int = BG_DN,
) -> np.ndarray:
    sigma = fwhm_px / 2.354820045
    v, u = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    cu, cv = center_uv
    blob = peak_dn * np.exp(-0.5 * (((u - cu) / sigma) ** 2 + ((v - cv) / sigma) ** 2))
    return np.clip(background + blob, 0, 255).astype(np.uint8)


def flat_frame(width: int = FULL_W, height: int = FULL_H, background: int = BG_DN) -> np.ndarray:
    return np.full((height, width), background, dtype=np.uint8)


def moving_blob_clip(
    tmp_path, n_frames: int = 60, appear_at: int = 15,
    start_uv: tuple[float, float] = (80.25, 120.5), du: float = 2.0, dv: float = -0.5,
):
    """Blob appears after ``appear_at`` and moves linearly; returns (dir, truths)."""
    frames = []
    truths: dict[int, tuple[float, float]] = {}
    for seq in range(n_frames):
        if seq >= appear_at:
            step = seq - appear_at
            center = (start_uv[0] + du * step, start_uv[1] + dv * step)
            truths[seq] = center
            frames.append(gaussian_blob_frame(center))
        else:
            frames.append(flat_frame())
    clip_dir = tmp_path / "clip"
    write_clip(frames, clip_dir, fps=30.0, source="detector-fixture")
    return clip_dir, truths


@pytest.fixture()
def base_config() -> DetectorConfig:
    """Small, fast defaults for fixtures: full res == proc res, short warmup."""
    return DetectorConfig(
        backend=Backend.MOG2,
        proc_width=FULL_W,
        proc_height=FULL_H,
        warmup_frames=10,
        persistence_frames=2,
        min_area_px=2,
    )
