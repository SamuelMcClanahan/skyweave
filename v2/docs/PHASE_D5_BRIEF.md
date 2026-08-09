# Phase D5 brief: association, fusion engine, EKF track lifecycle

**Status:** finalized 2026-08-07. Work order for phase D5.
**Read first:** `/CLAUDE.md`, `v2/docs/DETECTION_CONTRACTS_D0.md`,
`v2/docs/DETECTION_ARCHITECTURE_WORKING_PLAN.md` sections 6, 7, 9, 10,
`v2/docs/PHASE_D1_BRIEF.md` (the solver this phase consumes as-is).

## Goal

The central fusion engine: Observation2D streams in, auditable tracks out.
Ends with the first full-chain scored run of the gate clip against the
manifest acceptance numbers, with the voxel path evaluated on and off.

## Scope

In: event-time alignment, cross-camera association, direct + voxel
hypothesis generation, refinement wiring (D1 solver unchanged), EKF track
lifecycle with suppression state, full-chain metrics and report.

Out: viz adapter (DEFERRED past D9 by decision), transport/UDP (D7),
IMM/JPDA/MHT/smoothers (post-D9, evidence-gated), classifier, any change to
`contracts/`, `geometry/`, `v1/`, or `golden/`.

## Frozen decisions

| Item | Value |
| --- | --- |
| Batch | Event-time batches of one frame period; observations grouped by capture time, never arrival order |
| Lateness | Late observations are recorded for replay/diagnostics; they never enter a current solve. Policy and window in config |
| Association conditions | ALL of: compatible event time, epipolar/ray-geometry gate, bounded post-solve residual, motion plausibility against existing tracks, persistence for birth. Camera count alone is never sufficient |
| Association mechanics | Deterministic pair enumeration + consensus against remaining cameras (exact, seedless). Track-seeded gating once a track exists. Interfaces carry MULTIPLE candidate groups always |
| Voxel path | Small deterministic CPU oracle, seeded by candidate regions or track prediction. Evaluated BOTH enabled and disabled on the gate clip and on an injected multi-blob seed-variant; the report carries the comparison table; the shipping default is chosen from it and recorded afterwards |
| Measurement | D1 `localize()` unchanged; one result per candidate group; voxel center never enters the filter |
| Filter | Six-state constant-velocity, refined-XYZ update through the EKF-capable interface; innovation/NIS gating before any state pull |
| Confirmation | 3-camera support in one batch OR consistent 2-camera support across 3 consecutive batches (numbers Provisional, config) |
| Lifecycle | tentative, confirmed, coast, reacquired, deleted + SUPPRESSED: a clutter-judged track consumes its gated observations (blocking re-birth) but publishes nothing. Mechanism now; clutter rules wait for real-sky data |
| Audit | Every track update records consumed obs_ids (contract); replay of the same observation stream reproduces identical track decisions byte-for-byte |
| Anti-tuning | All thresholds tuned on seed-variant datasets only; gate clip scored once per frozen config |

## Package layout

```text
v2/src/skyweave2/fusion/
  config.py        # FusionConfig: windows, gates, lifecycle thresholds
  aligner.py       # event-time batching, reorder, lateness
  association.py   # candidate groups: pair enumeration, consensus, track seeding
  voxel_oracle.py  # bounded deterministic proposal grid
  engine.py        # batch -> candidate groups -> localize() -> results
  tracker.py       # EKF, lifecycle incl. suppressed, audit records
  metrics.py       # acceptance stats vs truth; acquisition, velocity, duplicates
  report.py        # D5_TRACKING_REPORT.md generator (seeded, deterministic)
v2/tests/fusion/
```

## The multi-blob variant

One seed-variant dataset with injected extra blobs per camera (independent
positions, plausible sizes) to stress correspondence. Used for: the voxel
on/off comparison, ghost-rejection tests, and association tuning. Generated
with the existing sensor-model/fixture tooling; no new render required.

## Tests (F-series)

| ID | Test |
| --- | --- |
| F1 | Aligner: batching by capture time, reorder handled, late observation excluded from solve but recorded |
| F2 | Association single target: correct group on clean fixtures at 2 and 3 cameras |
| F3 | Ghost rejection: plausible-but-wrong cross-pairing on the multi-blob fixture is rejected by consensus + residual, never confirmed |
| F4 | Voxel oracle: proposals match brute-force voting on small fixtures; deterministic |
| F5 | Engine determinism: identical observation stream replayed twice produces identical track decisions |
| F6 | Innovation gating: an implausible measurement does not pull the state; recorded as rejected with obs_ids |
| F7 | Lifecycle: every transition reachable on fixtures, including suppressed blocking re-birth |
| F8 | Confirmation routes: both the 3-camera and the 2-camera x 3-batch route confirm; neither confirms below threshold |
| F9 | Full chain on the gate clip: manifest acceptance table produced; duplicate confirmed tracks <= 1 |
| F10 | Voxel on/off: comparison table produced for both scenes; the report states candidate recall, false candidates, runtime for each mode |

All prior suites (D0/D1/D3/D4) still pass; ruff clean.

## Report: D5_TRACKING_REPORT.md (seeded command, byte-deterministic)

1. Gate-clip acceptance table against `exp001_scene.yaml` numbers: range
   p95, cross-range p95, velocity RMSE, acquisition time, duplicates,
   detection recall carried from D4. Every value labeled (Measured on the
   golden clips; the noise inside them remains Modeled mid-bracket).
2. Voxel on/off comparison (both scenes).
3. Covariance coverage of the track filter (with the D1 F-distribution
   caveat at 3 cameras).
4. Rejected-measurement and lifecycle statistics with obs_id audit samples.

## Done when

- F1-F10 pass; full suite green; report exists and reproduces byte-identically.
- The acceptance table is filled, pass or fail per line. A miss is a
  finding to report, not something to tune away on the gate clip.
- Hand-back: the acceptance table, the voxel verdict with numbers, and any
  surprise.
