"""``python -m skyweave2.drone.harness``: run the closed-loop scenarios, write scorecards.

Usage::

    python -m skyweave2.drone.harness --seed-set {gate,probe} --out DIR
        [--scenarios S1 ...] [--law pure_pursuit ...] [--speedup N]
        [--repeats N] [--jobs N] [--probe-count N] [--seed N ...]

An explicit seed set or explicit seeds are required ([S7]); nothing defaults
to the gate set. ``--seed`` alone runs those seeds under the ``probe`` label
(a gate seed is refused there; name ``--seed-set gate`` to run gate seeds).
The gate set runs ``gate_repeats`` (2) times unless ``--repeats`` says
otherwise; the probe set runs once. A gate-set aggregate passes only when it
covers every scenario at least ``gate_repeats`` times (``gate_complete``).
``--speedup`` and ``--jobs`` default to ``batch.DEFAULT_SPEEDUP`` and
``batch.DEFAULT_JOBS``, the same values the slow S tests use; both are
recorded in every scorecard under ``backend``.

Output under ``--out``: one directory per run,
``<law>/rep<r>/<scenario>/<seed>/`` with ``recording.jsonl`` (every packet,
detections included, and the full MAVLink log), ``scorecard.json`` ([S8])
and ``run.json`` (world, script, counters, [R3] replay detail); plus the
aggregate ``scorecard.json`` at the root with every verdict and the law
ranking (``harness.gate``). No scored file carries a wall-clock value; the
wall time is printed to stderr only. Exit status 0 when the aggregate
passes, 1 when it does not, 2 on a usage error.

Each run starts its own SITL (``--instance N`` per worker), so ``--jobs``
runs in parallel; every run's clock is its own SITL's boot time.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from skyweave2.drone.guidance import LAWS
from skyweave2.drone.harness.batch import (
    DEFAULT_INSTANCE_BASE,
    DEFAULT_JOBS,
    DEFAULT_PROBE_COUNT,
    DEFAULT_SPEEDUP,
    plan_runs,
    plan_seeds,
    run_plan,
    versions,
    write_aggregate,
)
from skyweave2.drone.harness.seeds import GATE_REPEATS, SCENARIOS, SeedSet
from skyweave2.drone.harness.sitl_loop import core_config
from skyweave2.drone.sitl import ensure_sitl, sitl_available


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m skyweave2.drone.harness",
        description="Closed-loop SITL scenarios S1-S6 (DRONE_CONTRACTS_D0.md §8).",
    )
    ap.add_argument("--seed-set", choices=[s.value for s in SeedSet], help="[S7] seed set")
    ap.add_argument(
        "--seed", type=int, action="append", help="explicit seed (repeatable); probe label"
    )
    ap.add_argument("--out", required=True, type=Path, help="output directory")
    ap.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    ap.add_argument("--law", action="append", choices=sorted(LAWS), help="guidance law(s)")
    ap.add_argument(
        "--speedup",
        type=float,
        default=DEFAULT_SPEEDUP,
        help=f"SITL --speedup (default {DEFAULT_SPEEDUP:g}; recorded in each scorecard)",
    )
    ap.add_argument("--repeats", type=int, help=f"gate default {GATE_REPEATS}, probe 1")
    ap.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"parallel runs, one SITL each (default {DEFAULT_JOBS}; recorded in each scorecard)",
    )
    ap.add_argument("--probe-count", type=int, default=DEFAULT_PROBE_COUNT)
    ap.add_argument("--instance-base", type=int, default=DEFAULT_INSTANCE_BASE)
    args = ap.parse_args(argv)
    if args.seed_set is None and not args.seed:
        ap.error("an explicit --seed-set or --seed is required ([S7])")
    if not args.speedup > 0 or args.jobs < 1 or (args.repeats is not None and args.repeats < 1):
        ap.error("--speedup must be > 0, --jobs and --repeats >= 1")
    return args


def _progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    seed_set = SeedSet(args.seed_set) if args.seed_set else SeedSet.PROBE
    laws = args.law or [core_config("pure_pursuit").law]
    repeats = args.repeats or (GATE_REPEATS if seed_set is SeedSet.GATE else 1)
    try:
        seeds = {s: plan_seeds(s, seed_set, args.seed, args.probe_count) for s in args.scenarios}
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not sitl_available():
        print("error: the pinned SITL is not present (set SKYWEAVE_SITL_DIR)", file=sys.stderr)
        return 2
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    vers = versions()
    plan = plan_runs(
        out=out,
        scenarios=args.scenarios,
        seeds=seeds,
        seed_set=seed_set,
        laws=laws,
        repeats=repeats,
        speedup=args.speedup,
        sitl_paths=ensure_sitl(),
        run_versions=vers,
        instance_base=args.instance_base,
    )
    _progress(
        f"{len(plan)} runs: {len(laws)} law(s) x {repeats} repeat(s) x "
        + ", ".join(f"{s}:{len(seeds[s])}" for s in args.scenarios)
        + f"; {args.jobs} job(s), speedup {args.speedup:g}"
    )
    t_wall = time.monotonic()
    entries = run_plan(
        plan, out=out, jobs=args.jobs, instance_base=args.instance_base, progress=_progress
    )
    agg = write_aggregate(
        entries,
        out=out,
        seed_set=seed_set,
        scenarios=args.scenarios,
        laws=laws,
        repeats=repeats,
        run_versions=vers,
        speedup=args.speedup,
        jobs=args.jobs,
    )
    for res in agg["results"]:
        cells = " ".join(
            f"{s}={'green' if v['passed'] else 'RED'}" for s, v in res["scenarios"].items()
        )
        s2 = res["scenarios"].get("S2", {}).get("miss_p95")
        extra = f" (S2 p95 miss {s2['p95_m']} m)" if s2 else ""
        print(f"{res['law']} rep{res['repeat']}: {cells}{extra}")
    for row in agg["law_ranking"]:
        print(f"rank {row['rank']}: {row['law']} ({row['green_cells']}/{row['cells']} green)")
    if agg["gate_complete"] is False:
        print(f"gate incomplete: needs every scenario and >= {GATE_REPEATS} repeats")
    print(f"aggregate {'PASS' if agg['passed'] else 'FAIL'}: {out / 'scorecard.json'}")
    _progress(f"wall time {time.monotonic() - t_wall:.1f} s")
    return 0 if agg["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
