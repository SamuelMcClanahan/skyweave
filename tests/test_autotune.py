from __future__ import annotations

from pathlib import Path

import yaml

from skyweave.sim.autotune import run_autotune, write_operator_profile


def test_autotune_runs_and_writes_operator_profile(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "simulation": {
                    "frames": 5,
                    "image_width": 64,
                    "image_height": 48,
                    "focal_length_px": 36,
                    "camera_count": 3,
                    "camera_layout": "room_perimeter",
                    "render_object_radius_m": 0.10,
                },
                "rayweave": {
                    "grid": {"origin_m": [-2, -2, 0], "dims": [12, 12, 8], "voxel_size_m": 0.25},
                    "scorer": {"backend": "python_numpy", "min_supporting_cameras": 2, "top_k_voxels": 50},
                    "peaks": {"threshold_percentile": 99.5, "max_peaks": 1},
                },
                "logging": {"console_every": 1000},
            }
        ),
        encoding="utf-8",
    )

    result = run_autotune(config, source="rendered", passes=1, max_evals=8, frames_limit=3)
    profile_path = write_operator_profile(tmp_path / "profiles" / "autotuned.yaml", result)
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))

    assert len(result.evaluations) >= 2
    assert result.best.summary.frames == 3
    assert result.best.score <= result.baseline.score
    assert profile["settings"]["motion"]["threshold"] == result.best.candidate.motion_threshold
    assert profile["settings"]["rayweave"]["scorer"]["min_supporting_cameras"] >= 1
