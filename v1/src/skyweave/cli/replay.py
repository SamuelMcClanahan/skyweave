from __future__ import annotations

import argparse
import sys

from skyweave.recording.replayer import replay_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a recorded Skyweave session.")
    parser.add_argument("--session", required=True, help="Path to a recorded session directory.")
    args = parser.parse_args(argv)

    summary = replay_session(args.session)
    print(
        "replay_run "
        f"scene={summary.scene} frames={summary.frames} voxel={summary.voxel_size_m:.3f}m "
        f"peak_rmse={summary.peak_rmse_m:.3f}m track_rmse={summary.track_rmse_m:.3f}m "
        f"max_track_err={summary.max_track_error_m:.3f}m latency_p50={summary.latency_p50_ms:.2f}ms "
        f"latency_p95={summary.latency_p95_ms:.2f}ms dropped={summary.dropped_packets} "
        f"not_visible={summary.not_visible_packets} false_pos={summary.false_positive_packets} "
        f"pass={str(summary.passed).lower()}"
    )
    return 0 if summary.passed else 1


if __name__ == "__main__":
    sys.exit(main())
