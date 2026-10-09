"""Shared drone-test fixtures: the pinned SITL ([F1]-[F11] slow tier, S series),
and the fast-tier speed budget.

SITL tests skip, with the reason, when the pinned ArduCopter 4.7.0 SITL is not
present and hash-verified (``skyweave2.drone.sitl.sitl_available``); when it
is present they must pass. Point ``SKYWEAVE_SITL_DIR`` at a directory holding
``arducopter`` and ``copter.parm``, or run ``skyweave2.drone.sitl.ensure_sitl()``
once to download and verify them.

The SITL fixtures read the wall clock: SITL at speedup 1 runs in real time, so
the injected board clock for a live SITL test is monotonic wall time. That is
process control in a test fixture, never a scored output ([C1]).

Speed budget (TESTING_DOCTRINE rule 6, "the budget is a test"; finding DT-5):
the hooks below sum the call-phase duration of every drone test not marked
``slow`` and fail the session when the sum exceeds ``FAST_TIER_BUDGET_S``.
Slow-marked tests (SITL, S scenarios) never count. The hooks live in this
conftest, so pytest hands them only the reports of tests under ``tests/drone``.
Durations are process timing for the verdict only; no scored output sees them.
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


# -- fast-tier speed budget (TESTING_DOCTRINE rule 6, finding DT-5) ----------

FAST_TIER_BUDGET_S = 30.0
"""Rule 6 fast-tier cap: summed call time of the non-slow drone tests, seconds."""

_fast_call_s: dict[str, float] = {}  # nodeid -> call-phase duration, non-slow drone tests


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" and "slow" not in report.keywords:
        _fast_call_s[report.nodeid] = report.duration


def _fast_tier_s() -> float:
    return sum(_fast_call_s.values())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Rule 6: a fast tier over budget fails an otherwise green session."""
    if _fast_tier_s() > FAST_TIER_BUDGET_S and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    total = _fast_tier_s()
    if total > FAST_TIER_BUDGET_S:
        terminalreporter.write_sep("=", "drone fast-tier speed budget exceeded", red=True)
        terminalreporter.write_line(
            f"{len(_fast_call_s)} non-slow drone tests took {total:.1f} s of call time, over "
            f"the {FAST_TIER_BUDGET_S:g} s budget (TESTING_DOCTRINE rule 6): speed them up "
            "or move the heavy ones to the slow tier"
        )
