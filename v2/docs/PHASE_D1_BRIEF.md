# Phase D1 brief: geometry engine and error budget

**Status:** finalized 2026-08-05. This is the work order for phase D1.
**Read first:** `/CLAUDE.md`, `v2/docs/DETECTION_CONTRACTS_D0.md`,
`v2/docs/DETECTION_ARCHITECTURE_WORKING_PLAN.md` sections 7 and 9.

## Goal

Prove the 3D geometry with exact numbers before any images exist. Two
deliverables: the geometry package, and a Monte Carlo error budget document
whose numbers close the parked D2 items (baseline, centroid precision
requirement, edge-only versus patch-refined centroid cost).

## Scope

In: bearing construction, deterministic initializer, robust refinement,
anisotropic covariance, degeneracy handling, leave-one-out diagnostic,
Monte Carlo budget, tests.

Out (do not build): detectors, voxel code, association, RANSAC/consensus
membership, EKF, Blender, transport, anything that touches `v1/`.

## Frozen technical decisions

| Decision | Value |
| --- | --- |
| Residual space | Pixel reprojection (primary). One test cross-checks against angular residuals |
| Robust loss | Huber, threshold in px, config default 1.5, never hardcoded |
| Solver | Gauss-Newton with Levenberg damping, numpy only, no scipy |
| Initializer | Weighted least-squares closest point over bearing lines (working plan §7.4) |
| Covariance | inverse of the weighted normal-equations matrix at the solution, scaled by robust residual variance. Full 3x3, anisotropic. This replaces v1's isotropic form (finding F-D0-2) |
| Outlier handling in D1 | Huber reweighting plus leave-one-camera-out diagnostic. Membership selection (consensus) is D5 |
| Degeneracy | triangulation angle, condition number, cheirality. Weak geometry returns `ResultStatus.WEAK_GEOMETRY`; never a confident bad point |
| Thresholds | all in a config dataclass; no constants in code |

## Package layout

```text
v2/src/skyweave2/geometry/
  __init__.py
  bearings.py      # Observation2D + CameraModel -> world ray + angular covariance
  initializer.py   # closest-point solve, conditioning, cheirality
  refine.py        # Huber Gauss-Newton, covariance, leave-one-out, LocalizationResult
  config.py        # GeometryConfig: huber_px, max_iterations, angle/condition thresholds
  budget.py        # Monte Carlo error budget runner (seeded, deterministic)
v2/tests/geometry/ # test files mirror modules
```

Inputs and outputs are the frozen D0 contracts only: `Observation2D`,
`CameraModel`, `LocalizationResult`. If a contract seems insufficient, stop
and report; do not edit `contracts/`.

## Monte Carlo error budget (budget.py)

Nominal scene: target at 243.84 m (800 ft), 3 cameras, FOV parameterized
60 to 70 degrees (60 default, Provisional), resolution 2304x1296.

Sweep axes (all seeded, N = 1000 trials per cell):

```text
baseline_m:        [5, 10, 25, 50]
centroid_sigma_px: [0.25, 0.5, 1.0, 2.0]      # full-resolution pixels
pose_rot_err_deg:  [0, 0.05, 0.1, 0.5, 1.0]
pose_trans_err_m:  [0, 0.01, 0.1, 1.0]
timing_err_ms:     [0, 0.5, 2.0, 10.0]        # converted to centroid shift
target_speed_mps:  [10, 30]                    # used by the timing axis
```

Report per cell: range error and cross-range error, RMSE and p95, reported-
covariance coverage (fraction of trials inside 1/2/3 sigma). Random axes
(centroid, timing jitter) test coverage; pose axes are systematic biases and
are reported as sensitivity curves, not folded into covariance.

The output document `v2/docs/D1_ERROR_BUDGET.md` must answer, with numbers:

1. Smallest baseline giving 5 m p95 range error, per centroid sigma.
2. Required centroid sigma at each candidate baseline.
3. Where depth is too weak to report (the weak-geometry boundary).
4. Cost of edge-only centroids: same sweep with sigma scaled by 1.5
   (the 2304/1536 ratio), reported side by side with patch-refined.
5. Ranked dominant error terms at the nominal operating point.

Generation command must be of the form
`uv run python -m skyweave2.geometry.budget --seed 7 --out docs/D1_ERROR_BUDGET.md`
and two runs with the same seed must produce identical bytes.

## Tests (all must pass; D0's 27 tests must still pass)

| ID | Test |
| --- | --- |
| G1 | Exact recovery: 3 to 6 cameras, exact pixels, error < 1e-6 m |
| G2 | Camera order invariance: shuffled input, identical result |
| G3 | Degenerate layouts (collinear cameras, near-parallel rays) return WEAK_GEOMETRY, never a confident point |
| G4 | Cheirality: target behind a camera excludes that camera or fails honestly |
| G5 | Huber: one camera off by 30 px among 4; solution error stays bounded (document the bound); non-robust solve demonstrably worse |
| G6 | Leave-one-out flags the perturbed camera from G5 |
| G7 | Covariance anisotropy: at 5 m baseline / 244 m range, depth sigma exceeds cross-range sigma by roughly range/baseline; direction of largest eigenvector points along depth |
| G8 | Coverage: empirical scatter matches reported covariance (chi-square within tolerance band) under pure centroid noise |
| G9 | Angular-vs-reprojection cross-check on clean cases |
| G10 | Determinism: budget run with fixed seed is byte-identical across two runs |

## Done when

- All G-tests and all D0 tests pass; `uv run ruff check src tests` is clean.
- `D1_ERROR_BUDGET.md` exists, generated by the seeded command, and answers
  the five questions above.
- No file under `v1/`, `golden/`, or `v2/src/skyweave2/contracts/` changed.
- A short hand-back note lists the dominant error terms and any surprise.

## Reminders

Determinism: explicit seeds, no wall clock in outputs. Labels: every number
in the budget document is Modeled; do not write Measured anywhere.
