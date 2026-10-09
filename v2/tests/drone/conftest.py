"""Shared drone-test fixtures: the pinned SITL ([F1]-[F11] slow tier, S series).

SITL tests skip, with the reason, when the pinned ArduCopter 4.7.0 SITL is not
present and hash-verified (``skyweave2.drone.sitl.sitl_available``); when it
is present they must pass. Point ``SKYWEAVE_SITL_DIR`` at a directory holding
``arducopter`` and ``copter.parm``, or run ``skyweave2.drone.sitl.ensure_sitl()``
once to download and verify them.

The SITL fixtures read the wall clock: SITL at speedup 1 runs in real time, so
the injected board clock for a live SITL test is monotonic wall time. That is
process control in a test fixture, never a scored output ([C1]).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import pytest

from skyweave2.drone.sitl import (
    SitlInstance,
    SitlPaths,
    default_cache_dir,
    ensure_sitl,
    free_instance,
    sitl_available,
)


@pytest.fixture(scope="session")
def sitl_paths() -> SitlPaths:
    """The verified SITL binary and copter.parm, or a skip that says why."""
    if not sitl_available():
        pytest.skip(
            f"pinned ArduCopter 4.7.0 SITL not present or not hash-verified in "
            f"{default_cache_dir()} (set SKYWEAVE_SITL_DIR, or run "
            "skyweave2.drone.sitl.ensure_sitl())"
        )
    return ensure_sitl()  # present and verified: no download happens


@pytest.fixture()
def sitl(sitl_paths: SitlPaths) -> Iterator[SitlInstance]:
    """A fresh SITL process (own temp workdir, free instance), pilot link connected."""
    inst = SitlInstance(sitl_paths, instance=free_instance())
    inst.start()
    try:
        yield inst
    finally:
        inst.stop()


@pytest.fixture()
def wall_clock() -> Callable[[], int]:
    """Board-ms stand-in for live SITL at speedup 1 (monotonic, starts near 0)."""
    t0 = time.monotonic_ns()
    return lambda: (time.monotonic_ns() - t0) // 1_000_000
