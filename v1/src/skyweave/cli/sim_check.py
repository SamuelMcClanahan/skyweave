from __future__ import annotations

import argparse
import sys

from skyweave.config import load_config
from skyweave.log import JsonlLogger
from skyweave.recording.recorder import Recorder
from skyweave.sim.check import (
    DEFAULT_RECORD_DIR,
    DEFAULT_SIM_CHECK_CONFIG,
    SIM_SOURCE_CHOICES,
    print_sim_summary,
    run_sim_check,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the headless Skyweave synthetic packet check.")
    parser.add_argument("--config", default=DEFAULT_SIM_CHECK_CONFIG, help="Path to the simulation YAML config.")
    parser.add_argument("--record", action="store_true", help="Record packets and outputs for replay.")
    parser.add_argument("--record-dir", default=DEFAULT_RECORD_DIR, help="Directory for recorded sessions.")
    parser.add_argument("--log-stages", action="store_true", help="Write per-frame stage timings to JSONL logs.")
    parser.add_argument("--source", choices=SIM_SOURCE_CHOICES, default="packet", help="Synthetic source to validate.")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.log_stages:
        config.logging.log_stage_timings = True

    logger = JsonlLogger(config.logging.log_dir)
    recorder = Recorder.create(args.record_dir, config, args.config) if args.record else None
    try:
        summary = run_sim_check(config, logger, recorder=recorder, config_path=args.config, source=args.source)
    finally:
        if recorder:
            recorder.close()
        logger.close()

    print_sim_summary(summary, logger.path, recorder.session_dir if recorder else None)
    return 0 if summary.passed else 1


if __name__ == "__main__":
    sys.exit(main())
