from __future__ import annotations

import argparse
from pathlib import Path

from skyweave.calibration.extrinsics import (
    load_fixed_board_dataset,
    solve_fixed_board_extrinsics,
    write_extrinsics,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Solve fixed-board camera poses from ChArUco observations and calibrated intrinsics."
    )
    parser.add_argument("capture_dir", help="Directory containing manifest.yaml and observations.jsonl.")
    parser.add_argument(
        "--intrinsics",
        action="append",
        required=True,
        help="Camera intrinsics YAML. Repeat once per camera label present in the capture.",
    )
    parser.add_argument("--output", default="configs/extrinsics.yaml")
    parser.add_argument("--min-corners", type=int, default=24)
    parser.add_argument("--world-frame", default="charuco_board")
    args = parser.parse_args(argv)
    if args.min_corners <= 0:
        parser.error("--min-corners must be positive")

    dataset = load_fixed_board_dataset(
        Path(args.capture_dir),
        [Path(path) for path in args.intrinsics],
        min_corners=args.min_corners,
    )
    calibration = solve_fixed_board_extrinsics(dataset, world_frame=args.world_frame)
    output = Path(args.output)
    write_extrinsics(calibration, output)
    print(
        "charuco_extrinsics "
        f"cameras={len(calibration.cameras)} rms_px={calibration.rms_px:.4f} "
        f"pattern={calibration.board_pattern} output={output}"
    )
    for camera in calibration.cameras:
        tx, ty, tz = camera.t_world_cam_m
        print(
            "charuco_extrinsics_camera "
            f"camera_id={camera.camera_id} label={camera.label} observations={camera.observation_count} "
            f"corners={camera.corner_count} rms_px={camera.rms_px:.4f} "
            f"t_world_cam_m={tx:.4f},{ty:.4f},{tz:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
