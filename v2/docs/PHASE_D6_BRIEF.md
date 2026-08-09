# Phase D6 brief: fault injection campaign

**Status:** finalized 2026-08-08. Work order for phase D6. Reviewed and
approved by Samuel as written.
**Read first:** `/CLAUDE.md`, `v2/docs/DETECTION_CONTRACTS_D0.md` (all
closure entries), `v2/configs/d6_faults.yaml` (the campaign manifest),
`v2/docs/PHASE_D5_BRIEF.md` (the engine under test).

## Goal

Measure what every defense layer does when the data is bad. Inject known
faults one axis at a time, record which layer catches each injected item,
and prove the system degrades honestly. No threshold changes in this phase:
findings go to the log, changes are recorded decisions.

## Inputs

- Frozen golden U8 clips (gate + negative), untouched, scored once per
  frozen configuration.
- Kept radiance EXRs: Tier I regenerates faulted U8 variants locally
  through the sensor model.
- Seed-variant datasets for anything needing tuning.

## Fault tiers

All magnitudes live in `v2/configs/d6_faults.yaml`; the code reads them,
never restates them. Tiers:

- **I image** (from radiance, via sensor-model switches): read noise, shot
  noise, blur, exposure scale, mid-clip brightness step.
- **II detection** (observation stream): false blobs, deleted detections,
  split blobs, merged blobs, added centroid bias (constant + slow-varying).
- **III model** (perturbed estimator calibration; truth untouched):
  rotation (headline axis; confirm the D1 6.5 m curve on the real chain),
  position, focal, principal point, distortion mismatch, rolling shutter
  as per-row capture-time shifts.
- **IV time/stream** (aligner boundary): clock offset/drift/jitter,
  observation loss (random + burst), reorder, duplication, lateness, one
  mid-clip node restart (session UUID + frame_seq reset), full camera
  dropout for the second half of the clip.

Time-fault honesty rule: every clock-fault axis runs twice, once with
`time_sync_error_ms` declaring the injected error honestly and once with a
dishonest 0.5 ms claim. Both outcomes are reported and labeled.

## The layer table (core deliverable)

Per-layer accept/reject bookkeeping with injected-item labels across:

```text
1 mask cleanup + persistence   2 epipolar prefilter   3 consensus
4 residual/status gates        5 leave-one-out        6 NIS gate
7 lifecycle incl. suppression
```

Output per axis and magnitude: false-accept rate, false-reject rate, and
the terminating layer for every injected item.

## Honesty gates (hard, all faults)

1. No crash, no NaN.
2. Covariance grows when input quality drops; coverage within its declared
   band per regime.
3. Failures exit as labeled statuses, never silent absence.
4. Overconfidence events (published CONFIRMED state with true error > 5
   sigma of its own covariance): ZERO tolerance; any occurrence is a named
   finding with a mechanism.
5. Deterministic replay under every fault (byte-identical decisions).

## Campaign order

1. Injectors + per-layer bookkeeping, fixtures first, every injector with a
   can-fail fixture.
2. Single-axis campaigns: Tiers II and IV, then III, then I.
3. Sensitivity curves per axis (all Modeled; noise scales mid-bracket until
   the conversion-gain bench).
4. Freeze ONE combined held-out recipe (moderate values, chosen before any
   combined run), run once, score once.
5. `D6_FAULT_REPORT.md`: seeded command, byte-identical across two runs;
   layer table, curves, overconfidence count, honesty verdicts.

## Tests (N-series)

| ID | Test |
| --- | --- |
| N1 | Each injector produces exactly its declared fault |
| N2 | Layer bookkeeping attributes a planted item to the correct layer |
| N3 | Honest vs dishonest time_sync_error_ms produce different, correctly labeled outcomes |
| N4 | Mid-clip session restart: no corruption; new session accepted; track survives or re-births with audit |
| N5 | Camera dropout: 2-camera operation degrades covariance honestly; route-B confirmation still works |
| N6 | Overconfidence detector has a can-fail fixture |
| N7 | Determinism under fault replay |

All prior suites and ruff stay green. Fenced paths untouched: `v1/`,
`golden/`, `contracts/`, `geometry/`, the frozen manifests.

## Out of scope

Real transport/serialization (D7 re-runs stream faults over actual UDP),
threshold retuning, classifier work, viz.

## Done when

- All single-axis campaigns + the held-out combo have run; report
  reproduces byte-identically.
- Layer table filled for every axis; zero overconfidence events or each is
  a named finding.
- Hand-back: layer table summary, three worst sensitivity curves, honesty
  verdicts, surprises with shipped-consequence statements.
