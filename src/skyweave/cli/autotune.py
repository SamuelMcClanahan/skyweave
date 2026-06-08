from __future__ import annotations

import argparse
import sys
from pathlib import Path

from skyweave.sim.autotune import print_autotune_summary, run_autotune, write_operator_profile
from skyweave.sim.check import DEFAULT_SIM_CHECK_CONFIG, SIM_SOURCE_CHOICES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tune Skyweave synthetic tracking settings against known truth.")
    parser.add_argument("--config", default=DEFAULT_SIM_CHECK_CONFIG, help="Path to the simulation YAML config.")
    parser.add_argument("--source", choices=SIM_SOURCE_CHOICES, default="rendered", help="Synthetic source to tune against.")
    parser.add_argument("--passes", type=int, default=2, help="Coordinate-search passes.")
    parser.add_argument("--max-evals", type=int, default=72, help="Maximum candidate evaluations.")
    parser.add_argument("--frames-limit", type=int, help="Optional frame cap for faster exploratory tuning.")
    parser.add_argument("--profile-output", help="Optional operator profile YAML to write.")
    parser.add_argument("--profile-name", default="autotuned-rendered", help="Profile name stored in output YAML.")
    args = parser.parse_args(argv)

    result = run_autotune(
        args.config,
        source=args.source,
        passes=args.passes,
        max_evals=args.max_evals,
        frames_limit=args.frames_limit,
    )
    output_path: Path | None = None
    if args.profile_output:
        output_path = write_operator_profile(
            args.profile_output,
            result,
            requested_mode=args.source if args.source in {"stress", "rendered"} else "rendered",
            profile_name=args.profile_name,
        )
    print_autotune_summary(result, output_path)
    return 0 if result.best.summary.passed else 1


if __name__ == "__main__":
    sys.exit(main())
