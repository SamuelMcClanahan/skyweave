from __future__ import annotations

import argparse
import sys

from skyweave.camera.live_benchmark import (
    DEFAULT_LIVE_BENCHMARK_CONFIG,
    DEFAULT_LIVE_BENCHMARK_FOURCC,
    DEFAULT_LIVE_BENCHMARK_FRAMES,
    DEFAULT_LIVE_BENCHMARK_WARMUP_FRAMES,
    STAGES,
    LiveBenchmarkOptions,
    print_result,
    run_live_benchmark,
    _with_stress_evidence,
)
from skyweave.camera.motion import DEFAULT_OPTIMIZED_MOTION_BACKEND, MOTION_BACKEND_CHOICES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark live camera packet generation plus Rayweave scoring.")
    parser.add_argument("--config", default=DEFAULT_LIVE_BENCHMARK_CONFIG)
    parser.add_argument("--devices", required=True, help="Comma-separated live camera devices.")
    parser.add_argument("--frames", type=int, default=DEFAULT_LIVE_BENCHMARK_FRAMES)
    parser.add_argument("--warmup-frames", type=int, default=DEFAULT_LIVE_BENCHMARK_WARMUP_FRAMES)
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--fps", type=float, default=None)
    parser.add_argument("--fourcc", default=DEFAULT_LIVE_BENCHMARK_FOURCC)
    parser.add_argument("--motion-backend", choices=MOTION_BACKEND_CHOICES, default=DEFAULT_OPTIMIZED_MOTION_BACKEND)
    parser.add_argument(
        "--align-window-ms",
        type=float,
        default=None,
        help="Override the packet alignment window for live timing diagnostics.",
    )
    parser.add_argument(
        "--rayweave-input",
        choices=("live", "stress-patches"),
        default="stress-patches",
        help="Use live motion packets as-is or replace them with deterministic patch workload.",
    )
    parser.add_argument("--stress-patch-size", type=int, default=None)
    parser.add_argument("--console-every", type=int, default=0)
    parser.add_argument("--enable-gc", action="store_true", help="Keep Python cyclic GC enabled during the hot loop.")
    args = parser.parse_args(argv)

    devices = [item.strip() for item in args.devices.split(",") if item.strip()]
    if not devices:
        parser.error("--devices must include at least one device")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc must be exactly 4 characters")

    result = run_live_benchmark(
        LiveBenchmarkOptions(
            config_path=args.config,
            devices=devices,
            frames=args.frames,
            warmup_frames=args.warmup_frames,
            width=args.width,
            height=args.height,
            fps=args.fps,
            fourcc=args.fourcc,
            motion_backend=args.motion_backend,
            align_window_ms=args.align_window_ms,
            rayweave_input=args.rayweave_input,
            stress_patch_size=args.stress_patch_size,
            console_every=args.console_every,
            enable_gc=args.enable_gc,
        )
    )
    print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
