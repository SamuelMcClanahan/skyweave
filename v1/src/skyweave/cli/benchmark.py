from __future__ import annotations

import argparse
import sys

from skyweave.sim.benchmark import (
    DEFAULT_BENCHMARK_CONFIG,
    DEFAULT_BENCHMARK_FRAMES,
    DEFAULT_BENCHMARK_WARMUP,
    STAGES,
    print_result,
    run_benchmark,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the headless Skyweave synthetic pipeline.")
    parser.add_argument("--config", default=DEFAULT_BENCHMARK_CONFIG, help="Path to the simulation YAML config.")
    parser.add_argument("--frames", type=int, default=DEFAULT_BENCHMARK_FRAMES, help="Measured frame count.")
    parser.add_argument("--warmup", type=int, default=DEFAULT_BENCHMARK_WARMUP, help="Warmup frame count excluded from timings.")
    args = parser.parse_args(argv)

    result = run_benchmark(args.config, args.frames, args.warmup)
    print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
