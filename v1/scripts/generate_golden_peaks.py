from __future__ import annotations

import argparse
import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from skyweave.config import load_config
from skyweave.fusion.aligner import TimeAligner
from skyweave.fusion.geom import point_distance
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.peaks import PeakExtractor
from skyweave.rayweave.scorer import RayweaveScorer
from skyweave.sim.check import _frame_packets
from skyweave.sim.generator import SyntheticPacketGenerator
from skyweave.sim.rendered import RenderedFrameGenerator
from skyweave.sim.scene import build_scene


BASELINES = (
    {
        "name": "legacy_3cam_packet",
        "config": "configs/sim.yaml",
        "source": "packet",
    },
    {
        "name": "room_7cam_packet",
        "config": "configs/sim_mvp_ov9281_100hz_07cam_perimeter_numba.yaml",
        "source": "packet",
    },
    {
        "name": "room_15cam_packet",
        "config": "configs/sim_mvp_ov9281_100hz_15cam_perimeter_numba.yaml",
        "source": "packet",
    },
    {
        "name": "room_7cam_rendered",
        "config": "configs/sim_mvp_ov9281_100hz_07cam_perimeter_numba.yaml",
        "source": "rendered",
    },
    {
        "name": "pixel_plane_7cam_rendered",
        "config": "configs/sim_pixel_plane_07cam_rendered_numba.yaml",
        "source": "rendered",
    },
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write deterministic golden peak baselines.")
    parser.add_argument("--output", default="data/golden/peak_baselines.json")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "description": "Golden Rayweave peak baselines generated from the current Skyweave implementation.",
        "baselines": [_run_case(case) for case in BASELINES],
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    for baseline in payload["baselines"]:
        summary = baseline["summary"]
        print(
            f"{baseline['name']} source={baseline['source']} frames={summary['frames']} "
            f"peaks={summary['peak_frames']} peak_rmse_m={summary['peak_rmse_m']:.6f} "
            f"max_peak_error_m={summary['max_peak_error_m']:.6f}"
        )
    return 0


def _run_case(case: dict[str, str]) -> dict[str, Any]:
    config = load_config(case["config"])
    config = deepcopy(config)
    config.rayweave.scorer.backend = "python_numpy"

    grid = VoxelGrid.from_config(config.rayweave.grid)
    scene = build_scene(config.simulation)
    frames = (
        SyntheticPacketGenerator(scene, config.simulation).frames()
        if case["source"] == "packet"
        else RenderedFrameGenerator(scene, config.simulation).frames()
    )
    aligner = TimeAligner(config.fusion.align_window_ns, config.fusion.min_cameras_per_frame)
    scorer = RayweaveScorer(grid, scene.cameras, config.rayweave.scorer)
    peak_extractor = PeakExtractor(grid, config.rayweave.peaks)
    rendered_builders: dict[int, Any] = {}
    rendered_previous_frames: dict[int, Any] = {}

    records = []
    errors = []
    aligned_count = 0
    for frame in frames:
        motion_packets, detection_packets, dropped, not_visible, not_visible_camera_ids, false_pos = _frame_packets(
            frame,
            case["source"],
            rendered_builders,
            rendered_previous_frames,
            None,
        )
        aligned = aligner.align_frame(motion_packets, detection_packets)
        record = {
            "frame_seq": int(frame.truth.frame_seq),
            "ts_ns": int(frame.truth.ts_ns),
            "truth_m": _round_vec(frame.truth.position),
            "aligned": aligned is not None,
            "packet_count": len(motion_packets),
            "dropped_packets": int(dropped),
            "not_visible_packets": int(not_visible),
            "not_visible_camera_ids": [int(x) for x in not_visible_camera_ids],
            "false_positive_packets": int(false_pos),
            "peak": None,
        }
        if aligned is not None:
            aligned_count += 1
            scored = scorer.score(aligned)
            peaks, _measurements = peak_extractor.extract(scored)
            if peaks:
                peak = peaks[0]
                error = point_distance(peak.position, frame.truth.position)
                errors.append(error)
                record["peak"] = {
                    "position_m": _round_vec(peak.position),
                    "score": _round_float(peak.score),
                    "error_m": _round_float(error),
                    "supporting_camera_ids": [int(x) for x in peak.supporting_camera_ids],
                    "n_voxels": int(peak.n_voxels),
                    "covariance_diag": _round_vec(np.diag(np.asarray(peak.covariance, dtype=np.float64))),
                }
        records.append(record)

    finite_errors = [error for error in errors if math.isfinite(error)]
    return {
        "name": case["name"],
        "config": case["config"],
        "source": case["source"],
        "scene": scene.name,
        "camera_count": len(scene.cameras),
        "grid": {
            "origin_m": _round_vec(grid.origin),
            "dims": [int(x) for x in grid.dims],
            "voxel_size_m": _round_float(grid.voxel_size),
        },
        "scorer": config.rayweave.scorer.model_dump(mode="json"),
        "peaks": config.rayweave.peaks.model_dump(mode="json"),
        "summary": {
            "frames": len(records),
            "aligned_frames": aligned_count,
            "peak_frames": len(finite_errors),
            "peak_rmse_m": _round_float(_rmse(finite_errors)),
            "max_peak_error_m": _round_float(max(finite_errors) if finite_errors else math.inf),
        },
        "frames": records,
    }


def _rmse(errors: list[float]) -> float:
    if not errors:
        return math.inf
    return math.sqrt(sum(error * error for error in errors) / len(errors))


def _round_vec(values: Any) -> list[float]:
    return [_round_float(float(value)) for value in values]


def _round_float(value: float) -> float:
    if not math.isfinite(value):
        return value
    return round(float(value), 9)


if __name__ == "__main__":
    raise SystemExit(main())
