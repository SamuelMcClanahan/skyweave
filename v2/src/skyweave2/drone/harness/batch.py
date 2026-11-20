"""Batch execution of closed-loop runs: seed plans, parallel SITL workers, aggregation.

The CLI (``__main__``) and the slow S-series tests both drive this module, so
the gate the tests check is the gate the command line runs. Each run gets its
own SITL process on its own ``--instance`` (ports ``5760 + 10 N``); every
run's clock is that SITL's boot time, so parallel runs do not share a clock.
Wall-clock reads here are process control and progress output only; nothing
scored carries them.
"""

from __future__ import annotations

import importlib.metadata
import multiprocessing
import os
import subprocess
import time
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from skyweave2.drone.harness.gate import RunEntry, aggregate
from skyweave2.drone.harness.seeds import SeedSet, check_seed, gate_seeds, probe_seeds
from skyweave2.drone.harness.sitl_loop import BACKEND, RunSpec, backend_obj, run_one
from skyweave2.drone.packets import canonical_json
from skyweave2.drone.sitl import PARM_SHA256, SITL_SHA256, SitlError, SitlPaths, free_instance

DEFAULT_PROBE_COUNT = 3
DEFAULT_INSTANCE_BASE = 20

DEFAULT_SPEEDUP = 4.0
"""SITL ``--speedup`` for the command line and the slow S tests alike (DT-3;
Provisional, E1 harness): the speed the recorded gate ran at. It is not
process control only: the harness's wall-clock processing time becomes sim
time multiplied by it, so every scorecard records it under ``backend``."""

DEFAULT_JOBS = 3
"""Parallel runs (one SITL each) for the command line and the slow S tests
(DT-3; Provisional, E1 harness). Recorded under ``backend`` like the speedup,
since parallel runs share the CPU."""

_INSTANCE: int | None = None  # this worker process's SITL instance
_RETRYABLE = ("error: SitlError", "error: StartupError")  # before any core input exists


def versions() -> dict[str, str]:
    """What every scorecard names as its versions (no wall clock)."""
    out = {
        "arducopter": "4.7.0",
        "arducopter_sitl_sha256": SITL_SHA256,
        "copter_parm_sha256": PARM_SHA256,
        "pymavlink": importlib.metadata.version("pymavlink"),
        "skyweave2": importlib.metadata.version("skyweave2"),
    }
    here = Path(__file__).resolve().parent
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=here, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=here, capture_output=True, text=True, check=True
        ).stdout.strip()
        out["git"] = rev + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        out["git"] = "unknown"
    return out


def plan_seeds(
    scenario: str, seed_set: SeedSet, explicit: Sequence[int] | None, probe_count: int
) -> list[int]:
    """The seeds one scenario runs, each checked against its label ([S7])."""
    if explicit:
        seeds = list(dict.fromkeys(explicit))
    elif seed_set is SeedSet.GATE:
        seeds = list(gate_seeds(scenario))
    else:
        seeds = list(probe_seeds(scenario, probe_count))
    for s in seeds:
        check_seed(seed_set, scenario, s)
    return seeds


@dataclass(frozen=True, kw_only=True)
class PlannedRun:
    repeat: int
    spec: RunSpec


def plan_runs(
    *,
    out: Path,
    scenarios: Sequence[str],
    seeds: dict[str, list[int]],
    seed_set: SeedSet,
    laws: Sequence[str],
    repeats: int,
    speedup: float,
    sitl_paths: SitlPaths,
    run_versions: dict[str, str],
    instance_base: int = DEFAULT_INSTANCE_BASE,
) -> list[PlannedRun]:
    """One run per (law, repeat, scenario, seed), each in its own directory."""
    plan: list[PlannedRun] = []
    for law in laws:
        for r in range(1, repeats + 1):
            for scenario in scenarios:
                for seed in seeds[scenario]:
                    spec = RunSpec(
                        scenario=scenario,
                        seed=seed,
                        seed_set=seed_set,
                        law=law,
                        speedup=speedup,
                        instance=instance_base,
                        out_dir=out / law / f"rep{r}" / scenario / str(seed),
                        sitl_paths=sitl_paths,
                        versions=run_versions,
                    )
                    plan.append(PlannedRun(repeat=r, spec=spec))
    return plan


def _instance_free(n: int) -> bool:
    try:
        return free_instance(start=n, stop=n + 1) == n
    except SitlError:
        return False


def _init_worker(counter: Any, base: int) -> None:
    global _INSTANCE
    with counter.get_lock():
        slot = counter.value
        counter.value += 1
    _INSTANCE = base + slot


def _run_task(spec: RunSpec, fallback_start: int) -> dict[str, Any]:
    """One run in a worker. A SITL that fails to start is retried once
    (process control: a port race with another SITL user, not a verdict)."""
    attempts = 0
    while True:
        attempts += 1
        instance = _INSTANCE if _INSTANCE is not None else spec.instance
        if not _instance_free(instance):
            instance = free_instance(start=fallback_start, stop=fallback_start + 40)
        res = run_one(replace(spec, instance=instance))
        if attempts < 2 and res.end_reason.startswith(_RETRYABLE):
            continue
        return {
            "card": res.scorecard,
            "end_reason": res.end_reason,
            "replay_ok": res.replay_ok,
            "replay_mismatch": res.replay_mismatch,
            "attempts": attempts,
        }


def _error_result(spec: RunSpec, exc: BaseException) -> dict[str, Any]:
    """A red entry for a run whose worker raised (the traceback goes to ``error.txt``).
    ``attempts`` is 1: an attempt before the one that raised is not known here."""
    spec.out_dir.mkdir(parents=True, exist_ok=True)
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    (spec.out_dir / "error.txt").write_text(text)
    reason = f"error: {type(exc).__name__}: {exc}"
    card = {
        "scenario": spec.scenario,
        "seed": spec.seed,
        "seed_set": spec.seed_set.value,
        "backend": backend_obj(spec),
        "law": spec.law,
        "final_state": None,
        "metrics": {
            "hold_error": None,
            "lock_retention": {"fraction": None},
            "commit_plane_miss": None,
        },
        "checks": [{"name": "harness_error", "passed": False, "value": reason, "limit": None}],
        "passed": False,
    }
    return {
        "card": card,
        "end_reason": reason,
        "replay_ok": False,
        "replay_mismatch": None,
        "attempts": 1,
    }


def run_plan(
    plan: Sequence[PlannedRun],
    *,
    out: Path,
    jobs: int,
    instance_base: int = DEFAULT_INSTANCE_BASE,
    progress: Callable[[str], None] | None = None,
) -> list[RunEntry]:
    """Run every planned run, ``jobs`` at a time, each on its own SITL instance.

    Each run's spec is stamped with ``jobs``, so its scorecard records the
    parallelism it actually ran under (DT-3)."""
    ctx = multiprocessing.get_context("spawn")
    counter = ctx.Value("i", 0)
    fallback = instance_base + jobs + 10
    entries: list[RunEntry] = []
    t0 = time.monotonic()
    with ProcessPoolExecutor(
        max_workers=jobs,
        mp_context=ctx,
        initializer=_init_worker,
        initargs=(counter, instance_base),
    ) as pool:
        futures = {}
        for p in plan:
            spec = replace(p.spec, jobs=jobs)
            futures[pool.submit(_run_task, spec, fallback)] = (p.repeat, spec)
        for n, fut in enumerate(as_completed(futures), start=1):
            repeat, spec = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 - a harness defect is a red run, not a crash
                res = _error_result(spec, exc)
            card = res["card"]
            if progress is not None:
                failed = [c["name"] for c in card["checks"] if not c["passed"]]
                progress(
                    f"[{n}/{len(plan)}] {spec.law} rep{repeat} {spec.scenario} "
                    f"{spec.seed}: {'PASS' if card['passed'] else 'FAIL'} "
                    f"final={card['final_state']} end={res['end_reason']} "
                    f"replay={res['replay_ok']}"
                    + (f" attempts={res['attempts']}" if res["attempts"] > 1 else "")
                    + (f" failed={failed}" if failed else "")
                    + (f" mismatch={res['replay_mismatch']}" if not res["replay_ok"] else "")
                    + f" ({time.monotonic() - t0:.0f} s wall)"
                )
            entries.append(
                RunEntry(
                    law=spec.law,
                    repeat=repeat,
                    scenario=spec.scenario,
                    seed=spec.seed,
                    card=card,
                    end_reason=res["end_reason"],
                    attempts=res["attempts"],
                    path=os.path.relpath(spec.out_dir, out),
                )
            )
    return entries


def write_aggregate(
    entries: Sequence[RunEntry],
    *,
    out: Path,
    seed_set: SeedSet,
    scenarios: Sequence[str],
    laws: Sequence[str],
    repeats: int,
    run_versions: dict[str, str],
    speedup: float,
    jobs: int,
) -> dict[str, Any]:
    """Aggregate (``gate.aggregate``) and write ``<out>/scorecard.json``. The
    aggregate's ``backend`` records the SITL ``speedup`` and the ``jobs`` the
    runs used (DT-3)."""
    agg = aggregate(
        sorted(entries, key=lambda e: (e.law, e.repeat, e.scenario, e.seed)),
        seed_set=seed_set,
        scenarios=scenarios,
        laws=laws,
        repeats=repeats,
        backend={"name": BACKEND, "speedup": float(speedup), "jobs": int(jobs)},
        versions=run_versions,
    )
    (out / "scorecard.json").write_bytes(canonical_json(agg) + b"\n")
    return agg
