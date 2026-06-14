from __future__ import annotations

import time


def monotonic_ns() -> int:
    return time.monotonic_ns()


def wall_ns() -> int:
    return time.time_ns()


def ns_to_seconds(ts_ns: int) -> float:
    return ts_ns / 1_000_000_000.0

