"""Gate aggregation: per-run scorecards -> per-scenario verdicts, repeats, law ranking.

Contract §8: each scenario is green under its gate seeds ([S7]); S2 adds the
p95 commit-plane miss over the S2 gate seeds; the gate is run
``gate_repeats`` times and must be green every time ([S0]); the harness
ranks candidate guidance laws by scorecard ([G3], brief 3.6). A gate-set
aggregate passes only when it is the whole gate: every scenario, at least
``gate_repeats`` repeats (``gate_complete``, DT-4).

Pure: it reads scorecard objects (``scorecard.score_run`` output) and returns
JSON-ready objects. No clock, no I/O; the aggregate carries no wall-clock
value, so two aggregations of the same scorecards are byte-identical.

The law ranking key is a harness choice (Provisional, E1), made before any
run: most green (repeat, scenario) cells first; then the worst S2 p95
commit-plane miss over the repeats; then the worst S1 hold error p95; then
the lowest S6 lock retention. Lower miss and hold error rank higher; higher
retention ranks higher. A missing metric ranks last on that key.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from skyweave2.drone.harness.scorecard import Thresholds, miss_p95
from skyweave2.drone.harness.seeds import GATE_REPEATS, SCENARIOS, SeedSet, gate_seeds

FORMAT = "skyweave-drone-gate"
FORMAT_V = 2
"""v2: ``backend`` is an object (name, ``speedup``, ``jobs``; DT-3), the
``gate_complete`` field (DT-4), and ``attempts`` per run (DT-10)."""


@dataclass(frozen=True, kw_only=True)
class RunEntry:
    """One finished run: where it sits in the plan, and its scorecard."""

    law: str
    repeat: int
    scenario: str
    seed: int
    card: Mapping[str, Any]
    end_reason: str
    path: str  # the run directory, relative to the output root
    attempts: int = 1  # SITL starts the run took (a failed start is retried once; DT-10)


def _failed_checks(card: Mapping[str, Any]) -> list[str]:
    return [c["name"] for c in card["checks"] if not c["passed"]]


def scenario_verdict(
    scenario: str,
    entries: Sequence[RunEntry],
    *,
    seed_set: SeedSet,
    thresholds: Thresholds | None = None,
) -> dict[str, Any]:
    """One scenario over one seed set, one law, one repeat.

    Green when every run's scorecard passed, the seeds are the scenario's
    full gate set (gate runs only), and, for S2, the p95 commit-plane miss
    over those seeds is within ``miss_p95_max_m``.
    """
    runs = sorted(entries, key=lambda e: e.seed)
    seeds = [e.seed for e in runs]
    if len(set(seeds)) != len(seeds):
        raise ValueError(f"{scenario}: a seed ran twice in one repeat")
    for e in runs:
        if e.scenario != scenario or e.card["scenario"] != scenario:
            raise ValueError(f"run {e.path} is not {scenario}")
        if e.card["seed_set"] != seed_set.value:
            raise ValueError(f"run {e.path} is not from the {seed_set.value} set")
    complete = bool(runs) and (
        seed_set is not SeedSet.GATE or sorted(seeds) == sorted(gate_seeds(scenario))
    )
    out: dict[str, Any] = {
        "complete": complete,
        "runs": [
            {
                "seed": e.seed,
                "passed": bool(e.card["passed"]),
                "failed_checks": _failed_checks(e.card),
                "final_state": e.card["final_state"],
                "end_reason": e.end_reason,
                "attempts": e.attempts,
                "path": e.path,
            }
            for e in runs
        ],
    }
    passed = complete and all(e.card["passed"] for e in runs)
    if scenario == "S2" and runs:
        agg = miss_p95([e.card for e in runs], thresholds=thresholds)
        out["miss_p95"] = agg
        passed = passed and bool(agg["passed"])
    out["passed"] = passed
    return out


def _worst(values: Sequence[float | None], *, high_is_bad: bool) -> float | None:
    if not values:
        return None
    vals = [(math.inf if high_is_bad else -math.inf) if v is None else float(v) for v in values]
    w = max(vals) if high_is_bad else min(vals)
    return None if math.isinf(w) else w


def law_summary(
    law: str, repeats: Sequence[Mapping[str, Any]], entries: Sequence[RunEntry]
) -> dict:
    """The ranking inputs of one law over all its repeats."""
    cells = [v["passed"] for r in repeats for v in r["scenarios"].values()]
    s2 = [
        r["scenarios"]["S2"]["miss_p95"]["p95_m"]
        for r in repeats
        if "S2" in r["scenarios"] and "miss_p95" in r["scenarios"]["S2"]
    ]
    hold = []
    lock = []
    for e in entries:
        m = e.card["metrics"]
        if e.scenario == "S1":
            hold.append(None if m["hold_error"] is None else m["hold_error"]["p95_m"])
        if e.scenario == "S6":
            lock.append(m["lock_retention"]["fraction"])
    return {
        "law": law,
        "green_cells": sum(bool(c) for c in cells),
        "cells": len(cells),
        "passed": bool(cells) and all(cells),
        "s2_miss_p95_worst_m": _worst(s2, high_is_bad=True),
        "s1_hold_p95_worst_m": _worst(hold, high_is_bad=True),
        "s6_lock_retention_min": _worst(lock, high_is_bad=False),
    }


def rank_laws(summaries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Order laws by the module's ranking key; ties keep the name order."""

    def key(s: Mapping[str, Any]) -> tuple:
        def low(v: float | None) -> float:
            return math.inf if v is None else v

        def high(v: float | None) -> float:
            return math.inf if v is None else -v

        return (
            -s["green_cells"],
            low(s["s2_miss_p95_worst_m"]),
            low(s["s1_hold_p95_worst_m"]),
            high(s["s6_lock_retention_min"]),
            s["law"],
        )

    ordered = sorted(summaries, key=key)
    return [{"rank": i + 1, **s} for i, s in enumerate(ordered)]


def aggregate(
    entries: Sequence[RunEntry],
    *,
    seed_set: SeedSet,
    scenarios: Sequence[str],
    laws: Sequence[str],
    repeats: int,
    backend: Mapping[str, Any],
    versions: Mapping[str, str],
    thresholds: Thresholds | None = None,
) -> dict[str, Any]:
    """The aggregate scorecard: verdict per (law, repeat, scenario), the
    per-repeat gate verdict, the law ranking, and the overall verdict (every
    law green in every repeat).

    For the gate set, ``gate_complete`` is true only when the aggregate covers
    every scenario at least ``gate_repeats`` times ([S0], [S7]), and the
    overall verdict needs it: a one-repeat or one-scenario "gate" is not the
    gate (DT-4). For the probe set ``gate_complete`` is ``null``.
    """
    for s in scenarios:
        if s not in SCENARIOS:
            raise ValueError(f"unknown scenario {s!r}")
    th = thresholds or Thresholds()
    results: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for law in laws:
        law_repeats: list[dict[str, Any]] = []
        for r in range(1, repeats + 1):
            cells = {
                s: scenario_verdict(
                    s,
                    [e for e in entries if e.law == law and e.repeat == r and e.scenario == s],
                    seed_set=seed_set,
                    thresholds=th,
                )
                for s in scenarios
            }
            law_repeats.append(
                {
                    "law": law,
                    "repeat": r,
                    "scenarios": cells,
                    "passed": all(c["passed"] for c in cells.values()),
                }
            )
        results.extend(law_repeats)
        summaries.append(law_summary(law, law_repeats, [e for e in entries if e.law == law]))
    gate_complete = (
        None
        if seed_set is not SeedSet.GATE
        else repeats >= GATE_REPEATS and set(scenarios) == set(SCENARIOS)
    )
    return {
        "format": FORMAT,
        "format_v": FORMAT_V,
        "seed_set": seed_set.value,
        "scenarios": list(scenarios),
        "laws": list(laws),
        "repeats": repeats,
        "gate_complete": gate_complete,
        "backend": dict(backend),
        "versions": dict(versions),
        "thresholds": th.to_obj(),
        "results": results,
        "law_ranking": rank_laws(summaries),
        "passed": gate_complete is not False
        and bool(results)
        and all(r["passed"] for r in results),
    }
