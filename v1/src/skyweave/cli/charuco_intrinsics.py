from __future__ import annotations

import argparse
from pathlib import Path

from skyweave.calibration.intrinsics import calibrate_intrinsics, load_intrinsic_dataset, write_intrinsics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Solve camera intrinsics from ChArUco capture observations.")
    parser.add_argument("capture_dir", help="Directory containing manifest.yaml and observations.jsonl.")
    parser.add_argument("--output", default=None, help="Output YAML path. Defaults to configs/intrinsics_<label>.yaml.")
    parser.add_argument("--min-corners", type=int, default=24)
    args = parser.parse_args(argv)
    if args.min_corners <= 0:
        parser.error("--min-corners must be positive")

    dataset = load_intrinsic_dataset(Path(args.capture_dir), min_corners=args.min_corners)
    calibration = calibrate_intrinsics(dataset)
    output = Path(args.output) if args.output else Path("configs") / f"intrinsics_{dataset.label}.yaml"
    write_intrinsics(calibration, output)
    fx, fy = calibration.focal_lengths_px
    cx, cy = calibration.principal_point_px
    print(
        "charuco_intrinsics "
        f"label={calibration.label} views={calibration.accepted_views} "
        f"rms_px={calibration.rms_px:.4f} pattern={calibration.board_pattern} "
        f"fx={fx:.2f} fy={fy:.2f} cx={cx:.2f} cy={cy:.2f} output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
