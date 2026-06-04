#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Rubik Pi camera and voxel performance sweep.")
    parser.add_argument("--devices", default="/dev/video0,/dev/video2,/dev/video4")
    parser.add_argument("--config", default="configs/sim_mvp_ov9281_100hz_numba.yaml")
    parser.add_argument("--frames", type=int, default=6000)
    parser.add_argument("--warmup-frames", type=int, default=100)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--fps", type=float, default=100.0)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--motion-backend", default="opencv_contours")
    parser.add_argument("--camera-min-fps", type=float, default=90.0)
    parser.add_argument("--voxel-max-p99-ms", type=float, default=16.67)
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    output_path = Path(args.output) if args.output else _default_output_path()
    commands = _build_commands(args)
    if args.dry_run:
        for name, command in commands:
            print(name, " ".join(command))
        return 0

    results: dict[str, object] = {
        "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "thresholds": {
            "camera_min_fps": args.camera_min_fps,
            "voxel_max_p99_ms": args.voxel_max_p99_ms,
        },
        "commands": {},
    }

    passed = True
    for name, command in commands:
        print(f"running {name}: {' '.join(command)}", flush=True)
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        parsed = _parse_output(completed.stdout)
        parsed["returncode"] = completed.returncode
        parsed["command"] = command
        results["commands"][name] = parsed
        passed = passed and completed.returncode == 0

    camera_passed = _camera_passed(results["commands"]["camera"], args.camera_min_fps)
    voxel_passed = _voxel_passed(results["commands"]["voxel"], args.frames, args.voxel_max_p99_ms)
    passed = passed and camera_passed and voxel_passed
    results["passed"] = passed
    results["camera_passed"] = camera_passed
    results["voxel_passed"] = voxel_passed

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"summary_json={output_path}")
    print(f"passed={passed} camera_passed={camera_passed} voxel_passed={voxel_passed}")
    return 0 if passed else 1


def _build_commands(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    common = [
        "--devices",
        args.devices,
        "--frames",
        str(args.frames),
        "--warmup-frames",
        str(args.warmup_frames),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--fps",
        f"{args.fps:g}",
        "--fourcc",
        args.fourcc,
        "--motion-backend",
        args.motion_backend,
        "--console-every",
        "0",
    ]
    camera = [
        sys.executable,
        "-m",
        "skyweave.cli.camera_check",
        *common,
        "--parallel-cameras",
        "--profile-stages",
    ]
    voxel = [
        sys.executable,
        "-m",
        "skyweave.cli.live_benchmark",
        "--config",
        args.config,
        *common,
        "--rayweave-input",
        "stress-patches",
    ]
    return [("camera", camera), ("voxel", voxel)]


def _parse_output(output: str) -> dict[str, object]:
    parsed: dict[str, object] = {"lines": output.splitlines()}
    camera_summaries = []
    stage_summaries = []
    live_summary: dict[str, object] | None = None
    for line in output.splitlines():
        if line.startswith("camera_check_live "):
            camera_summaries.append(_parse_key_values(line))
        elif line.startswith("camera_check_live_stage "):
            stage_summaries.append(_parse_key_values(line))
        elif line.startswith("camera_check_live_loop "):
            parsed["camera_loop"] = _parse_key_values(line)
        elif line.startswith("live_benchmark_run "):
            live_summary = _parse_key_values(line)
        elif line.startswith("stage,"):
            parsed["stage_header"] = line
        elif "," in line and line.split(",", 1)[0] in {"camera_packets", "alignment", "scoring", "peaks", "kalman", "total"}:
            stage = _parse_stage_csv(line)
            parsed.setdefault("voxel_stages", {})[stage.pop("stage")] = stage
    if camera_summaries:
        parsed["cameras"] = camera_summaries
    if stage_summaries:
        parsed["camera_stages"] = stage_summaries
    if live_summary is not None:
        parsed["live"] = live_summary
    return parsed


def _parse_key_values(line: str) -> dict[str, object]:
    fields = line.split()[1:]
    result: dict[str, object] = {}
    for field in fields:
        if "=" not in field:
            continue
        key, value = field.split("=", 1)
        result[key] = _coerce_value(value)
    return result


def _parse_stage_csv(line: str) -> dict[str, object]:
    stage, p50, p95, p99, total, share = line.split(",")
    return {
        "stage": stage,
        "p50_ms": float(p50),
        "p95_ms": float(p95),
        "p99_ms": float(p99),
        "total_ms": float(total),
        "share_pct": float(share),
    }


def _coerce_value(value: str) -> object:
    if value in {"True", "False"}:
        return value == "True"
    numeric = value
    for suffix in ("ms",):
        if numeric.endswith(suffix):
            numeric = numeric[: -len(suffix)]
            break
    try:
        if any(char in numeric for char in ".eE"):
            return float(numeric)
        return int(numeric)
    except ValueError:
        return value


def _camera_passed(camera: object, min_fps: float) -> bool:
    if not isinstance(camera, dict):
        return False
    cameras = camera.get("cameras", [])
    if not isinstance(cameras, list) or not cameras:
        return False
    for item in cameras:
        if not isinstance(item, dict):
            return False
        if item.get("read_failures") != 0:
            return False
        if float(item.get("effective_fps", 0.0)) < min_fps:
            return False
    return True


def _voxel_passed(voxel: object, frames: int, max_p99_ms: float) -> bool:
    if not isinstance(voxel, dict):
        return False
    live = voxel.get("live")
    stages = voxel.get("voxel_stages")
    if not isinstance(live, dict) or not isinstance(stages, dict):
        return False
    total = stages.get("total")
    if not isinstance(total, dict):
        return False
    return (
        live.get("read_failures") == 0
        and live.get("aligned") == frames
        and live.get("measurements") == frames
        and float(total.get("p99_ms", 1.0e9)) <= max_p99_ms
    )


def _default_output_path() -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    return Path("data/logs") / f"rubik_perf_sweep_{stamp}.json"


if __name__ == "__main__":
    raise SystemExit(main())
