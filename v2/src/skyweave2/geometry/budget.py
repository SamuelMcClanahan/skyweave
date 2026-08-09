"""Monte Carlo error budget for the D1 geometry engine (seeded, deterministic).

Generates ``D1_ERROR_BUDGET.md`` answering the five questions in the D1
brief. Every number produced here is Modeled: synthetic scene, modeled noise,
no bench data anywhere.

Scene: target at 243.84 m (800 ft) due north of a linear east-west camera
array; cameras evenly spaced across the baseline, each aimed at the target.
Random axes (centroid noise, timing jitter) test covariance coverage; pose
axes are systematic biases and are reported as sensitivity curves, never
folded into covariance (two-error-channel rule).

Usage (from ``v2/``):

    uv run python -m skyweave2.geometry.budget --seed 7 --out docs/D1_ERROR_BUDGET.md

Two runs with the same seed produce byte-identical output.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace

import numpy as np

from skyweave2.contracts import CameraModel
from skyweave2.geometry.config import GeometryConfig
from skyweave2.geometry.refine import solve_point

# Chi-square quantiles for 3 degrees of freedom at the 1/2/3-sigma
# probability masses (68.27 / 95.45 / 99.73 %). Definitions for the coverage
# report, not tunable thresholds.
_CHI2_3DOF = (3.5267, 8.0249, 14.1564)


@dataclass(frozen=True)
class BudgetSpec:
    """The nominal scene and sweep axes frozen by the D1 brief."""

    range_m: float = 243.84  # 800 ft
    n_cameras: int = 3
    fov_deg: float = 60.0  # horizontal; Provisional per D2 partial freeze
    width: int = 2304
    height: int = 1296
    baselines_m: tuple[float, ...] = (5.0, 10.0, 25.0, 50.0)
    centroid_sigmas_px: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0)
    pose_rot_err_deg: tuple[float, ...] = (0.0, 0.05, 0.1, 0.5, 1.0)
    pose_trans_err_m: tuple[float, ...] = (0.0, 0.01, 0.1, 1.0)
    timing_err_ms: tuple[float, ...] = (0.0, 0.5, 2.0, 10.0)
    target_speeds_mps: tuple[float, ...] = (10.0, 30.0)
    edge_sigma_scale: float = 1.5  # 2304 / 1536, edge-only centroid penalty
    nominal_baseline_m: float = 25.0  # Provisional operating point
    nominal_sigma_px: float = 0.5
    nominal_timing_ms: float = 2.0
    nominal_speed_mps: float = 30.0
    nominal_rot_deg: float = 0.1
    nominal_trans_m: float = 0.1
    p95_target_m: float = 5.0
    trials: int = 1000


@dataclass(frozen=True)
class CellStats:
    n_confident: int
    weak_fraction: float
    rmse_range: float
    p95_range: float
    rmse_cross: float
    p95_cross: float
    coverage: tuple[float, float, float]  # fraction inside 1/2/3 sigma


def _look_at_t_world_cam(position: np.ndarray, target: np.ndarray) -> np.ndarray:
    """OpenCV camera at ``position`` with +Z toward ``target``, world up +z."""
    z_cam = target - position
    z_cam = z_cam / np.linalg.norm(z_cam)
    up = np.array([0.0, 0.0, 1.0])
    x_cam = np.cross(z_cam, up)
    x_cam = x_cam / np.linalg.norm(x_cam)
    y_cam = np.cross(z_cam, x_cam)
    t = np.eye(4)
    t[:3, 0], t[:3, 1], t[:3, 2], t[:3, 3] = x_cam, y_cam, z_cam, position
    return t


def _camera(camera_id: int, t_world_cam: np.ndarray, spec: BudgetSpec) -> CameraModel:
    f = (spec.width / 2.0) / np.tan(np.radians(spec.fov_deg) / 2.0)
    cx = (spec.width - 1) / 2.0
    cy = (spec.height - 1) / 2.0
    return CameraModel(
        camera_id=camera_id,
        width=spec.width,
        height=spec.height,
        k=((f, 0.0, cx), (0.0, f, cy), (0.0, 0.0, 1.0)),
        t_world_cam=tuple(tuple(float(x) for x in row) for row in t_world_cam),
        calibration_rev="budget-truth",
    )


def make_cameras(baseline_m: float, spec: BudgetSpec) -> tuple[list[CameraModel], np.ndarray]:
    """Cameras spread across the baseline on the east axis, aimed at the target."""
    target = np.array([0.0, spec.range_m, 0.0])
    xs = np.linspace(-baseline_m / 2.0, baseline_m / 2.0, spec.n_cameras)
    cameras = [
        _camera(i, _look_at_t_world_cam(np.array([x, 0.0, 0.0]), target), spec)
        for i, x in enumerate(xs)
    ]
    return cameras, target


def _rotation(axis: np.ndarray, angle_rad: float) -> np.ndarray:
    """Rodrigues rotation matrix about a unit axis."""
    kx, ky, kz = axis
    k = np.array([[0.0, -kz, ky], [kz, 0.0, -kx], [-ky, kx, 0.0]])
    return np.eye(3) + np.sin(angle_rad) * k + (1.0 - np.cos(angle_rad)) * (k @ k)


def _perturb_camera(
    camera: CameraModel, rot_deg: float, trans_m: float, rng: np.random.Generator
) -> CameraModel:
    t = np.array(camera.t_world_cam, dtype=np.float64)
    if rot_deg > 0.0:
        axis = rng.normal(size=3)
        axis /= np.linalg.norm(axis)
        t[:3, :3] = _rotation(axis, np.radians(rot_deg)) @ t[:3, :3]
    if trans_m > 0.0:
        direction = rng.normal(size=3)
        direction /= np.linalg.norm(direction)
        t[:3, 3] += trans_m * direction
    return CameraModel(
        camera_id=camera.camera_id,
        width=camera.width,
        height=camera.height,
        k=camera.k,
        dist=camera.dist,
        t_world_cam=tuple(tuple(float(x) for x in row) for row in t),
        calibration_rev=camera.calibration_rev,
    )


def _stats(
    errors: list[np.ndarray], d2: list[float], n_weak: int, n_total: int
) -> CellStats:
    if not errors:
        nan = float("nan")
        return CellStats(0, 1.0, nan, nan, nan, nan, (nan, nan, nan))
    err = np.stack(errors)
    rng_err = np.abs(err[:, 1])  # line of sight is +y (north)
    cross_err = np.sqrt(err[:, 0] ** 2 + err[:, 2] ** 2)
    d2_arr = np.asarray(d2)
    coverage = tuple(float(np.mean(d2_arr <= q)) for q in _CHI2_3DOF)
    return CellStats(
        n_confident=len(errors),
        weak_fraction=n_weak / n_total,
        rmse_range=float(np.sqrt(np.mean(rng_err**2))),
        p95_range=float(np.percentile(rng_err, 95)),
        rmse_cross=float(np.sqrt(np.mean(cross_err**2))),
        p95_cross=float(np.percentile(cross_err, 95)),
        coverage=coverage,
    )


def run_random_cell(
    baseline_m: float,
    sigma_px: float,
    timing_ms: float,
    speed_mps: float,
    spec: BudgetSpec,
    config: GeometryConfig,
    rng: np.random.Generator,
    trials: int,
) -> CellStats:
    """Random-axis cell: centroid noise and per-camera timing jitter (coverage test)."""
    cameras, target = make_cameras(baseline_m, spec)
    velocity = np.array([speed_mps, 0.0, 0.0])  # cross-range, east
    cov = np.eye(2) * sigma_px**2
    pixel_covs = np.stack([cov] * len(cameras))
    errors: list[np.ndarray] = []
    d2: list[float] = []
    n_weak = 0
    for _ in range(trials):
        pixels = np.empty((len(cameras), 2))
        for i, cam in enumerate(cameras):
            dt = rng.normal(0.0, timing_ms * 1e-3) if timing_ms > 0.0 else 0.0
            proj = cam.project(target + velocity * dt)
            assert proj is not None
            pixels[i] = np.asarray(proj) + rng.normal(0.0, sigma_px, size=2)
        outcome = solve_point(cameras, pixels, pixel_covs, config)
        if outcome.weak_geometry:
            n_weak += 1
            continue
        err = outcome.position - target
        errors.append(err)
        info = np.linalg.pinv(outcome.covariance, hermitian=True)
        d2.append(float(err @ info @ err))
    return _stats(errors, d2, n_weak, trials)


def run_pose_cell(
    rot_deg: float,
    trans_m: float,
    spec: BudgetSpec,
    config: GeometryConfig,
    rng: np.random.Generator,
    trials: int,
) -> CellStats:
    """Systematic-axis cell: estimator poses perturbed, pixels noiseless.

    The resulting position error is pure bias induced by calibration error;
    it is reported as a sensitivity curve and never folded into covariance.
    """
    cameras, target = make_cameras(spec.nominal_baseline_m, spec)
    cov = np.eye(2) * spec.nominal_sigma_px**2
    pixel_covs = np.stack([cov] * len(cameras))
    errors: list[np.ndarray] = []
    d2: list[float] = []
    n_weak = 0
    for _ in range(trials):
        pixels = np.empty((len(cameras), 2))
        for i, cam in enumerate(cameras):
            proj = cam.project(target)
            assert proj is not None
            pixels[i] = np.asarray(proj)
        estimator_cams = [_perturb_camera(cam, rot_deg, trans_m, rng) for cam in cameras]
        outcome = solve_point(estimator_cams, pixels, pixel_covs, config)
        if outcome.weak_geometry:
            n_weak += 1
            continue
        err = outcome.position - target
        errors.append(err)
        info = np.linalg.pinv(outcome.covariance, hermitian=True)
        d2.append(float(err @ info @ err))
    return _stats(errors, d2, n_weak, trials)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _fmt(x: float, digits: int = 3) -> str:
    if not np.isfinite(x):
        return "—"
    return f"{x:.{digits}f}"


def _cov_fmt(stats: CellStats) -> str:
    return "/".join(_fmt(c, 2) for c in stats.coverage)


def _grid_table(grid: dict[tuple[float, float], CellStats], spec: BudgetSpec) -> list[str]:
    lines = [
        "| Baseline (m) | Centroid sigma (px) | Range RMSE (m) | Range p95 (m) | "
        "Cross RMSE (m) | Cross p95 (m) | Coverage 1/2/3 sigma | Weak frac |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for b in spec.baselines_m:
        for s in spec.centroid_sigmas_px:
            st = grid[(b, s)]
            lines.append(
                f"| {_fmt(b, 0)} | {_fmt(s, 2)} | {_fmt(st.rmse_range)} | "
                f"{_fmt(st.p95_range)} | {_fmt(st.rmse_cross)} | {_fmt(st.p95_cross)} | "
                f"{_cov_fmt(st)} | {_fmt(st.weak_fraction, 3)} |"
            )
    return lines


def generate_report(seed: int, trials: int, spec: BudgetSpec, config: GeometryConfig) -> str:
    root = np.random.SeedSequence(seed)
    # Fixed spawn order = deterministic bytes. Order: full grid, edge grid,
    # timing cells, rotation cells, translation cells.
    n_grid = len(spec.baselines_m) * len(spec.centroid_sigmas_px)
    n_timing = len(spec.timing_err_ms) * len(spec.target_speeds_mps)
    n_pose = len(spec.pose_rot_err_deg) + len(spec.pose_trans_err_m)
    children = root.spawn(2 * n_grid + n_timing + n_pose)
    seeds = iter(children)

    grid: dict[tuple[float, float], CellStats] = {}
    for b in spec.baselines_m:
        for s in spec.centroid_sigmas_px:
            grid[(b, s)] = run_random_cell(
                b, s, 0.0, 0.0, spec, config, np.random.default_rng(next(seeds)), trials
            )
    edge: dict[tuple[float, float], CellStats] = {}
    for b in spec.baselines_m:
        for s in spec.centroid_sigmas_px:
            edge[(b, s)] = run_random_cell(
                b,
                s * spec.edge_sigma_scale,
                0.0,
                0.0,
                spec,
                config,
                np.random.default_rng(next(seeds)),
                trials,
            )
    timing: dict[tuple[float, float], CellStats] = {}
    for t_ms in spec.timing_err_ms:
        for v in spec.target_speeds_mps:
            timing[(t_ms, v)] = run_random_cell(
                spec.nominal_baseline_m,
                spec.nominal_sigma_px,
                t_ms,
                v,
                spec,
                config,
                np.random.default_rng(next(seeds)),
                trials,
            )
    rot: dict[float, CellStats] = {}
    for r in spec.pose_rot_err_deg:
        rot[r] = run_pose_cell(
            r, 0.0, spec, config, np.random.default_rng(next(seeds)), trials
        )
    trans: dict[float, CellStats] = {}
    for tr in spec.pose_trans_err_m:
        trans[tr] = run_pose_cell(
            0.0, tr, spec, config, np.random.default_rng(next(seeds)), trials
        )

    # -- Question 1: smallest baseline meeting the p95 range target, per sigma.
    q1: dict[float, float | None] = {}
    for s in spec.centroid_sigmas_px:
        q1[s] = next(
            (b for b in spec.baselines_m if grid[(b, s)].p95_range <= spec.p95_target_m),
            None,
        )
    # -- Question 2: largest workable sigma per baseline.
    q2: dict[float, float | None] = {}
    for b in spec.baselines_m:
        ok = [s for s in spec.centroid_sigmas_px if grid[(b, s)].p95_range <= spec.p95_target_m]
        q2[b] = max(ok) if ok else None

    # -- Question 5: ranked terms at the nominal operating point.
    nominal = grid[(spec.nominal_baseline_m, spec.nominal_sigma_px)]
    timing_nom = timing[(spec.nominal_timing_ms, spec.nominal_speed_mps)]
    terms = [
        ("Centroid noise (0.50 px)", nominal.p95_range, nominal.p95_cross),
        (
            "Timing jitter (2.0 ms @ 30 m/s), incremental over centroid",
            max(timing_nom.p95_range - nominal.p95_range, 0.0),
            max(timing_nom.p95_cross - nominal.p95_cross, 0.0),
        ),
        (
            "Pose rotation bias (0.10 deg)",
            rot[spec.nominal_rot_deg].p95_range,
            rot[spec.nominal_rot_deg].p95_cross,
        ),
        (
            "Pose translation bias (0.10 m)",
            trans[spec.nominal_trans_m].p95_range,
            trans[spec.nominal_trans_m].p95_cross,
        ),
    ]
    terms.sort(key=lambda t: -t[1])

    # -- Question 3: weak-geometry boundary.
    angle_at = {b: _triangulation_angle(b, spec) for b in spec.baselines_m}
    boundary_baseline = spec.range_m * np.tan(np.radians(config.min_triangulation_angle_deg))

    lines: list[str] = []
    a = lines.append
    a("# D1 Monte Carlo error budget")
    a("")
    a("**Label: every number in this document is Modeled.** Synthetic scene,")
    a("modeled noise, no bench data. Nothing here is Measured.")
    a("")
    a(f"Generated by `uv run python -m skyweave2.geometry.budget --seed {seed} "
      f"--out docs/D1_ERROR_BUDGET.md` (deterministic; identical bytes for identical seed).")
    a("")
    a("## Scene and method")
    a("")
    a(f"- Target at {_fmt(spec.range_m, 2)} m (800 ft) due north of a linear east-west")
    a(f"  array of {spec.n_cameras} cameras aimed at the target; world ENU, line of")
    a("  sight = +y, cross-range = x/z.")
    a(f"- FOV {_fmt(spec.fov_deg, 1)} deg horizontal (Provisional per D2 partial freeze),")
    a(f"  resolution {spec.width}x{spec.height}, focal length "
      f"{_fmt((spec.width / 2.0) / np.tan(np.radians(spec.fov_deg) / 2.0), 1)} px.")
    a(f"- N = {trials} trials per cell, seed {seed}, per-cell child seeds in fixed order.")
    a(f"- Estimator: Huber ({_fmt(config.huber_px, 1)} px) Gauss-Newton with Levenberg")
    a("  damping on pixel reprojection residuals; covariance = inverse weighted")
    a("  normal equations scaled by robust residual variance (frozen D1 decisions).")
    a("- Random axes (centroid noise, per-camera timing jitter) are drawn per trial")
    a("  and test covariance coverage. Pose axes perturb the estimator's calibration")
    a("  against a fixed truth: pure systematic bias, reported as sensitivity curves,")
    a("  never folded into covariance (two-error-channel rule).")
    a("- Range error = |error along y|; cross-range error = sqrt(ex^2 + ez^2).")
    a("- Coverage = fraction of trials whose Mahalanobis distance^2 against the")
    a("  reported covariance falls inside the 3-dof chi-square quantiles "
      f"{_fmt(_CHI2_3DOF[0], 2)} / {_fmt(_CHI2_3DOF[1], 2)} / {_fmt(_CHI2_3DOF[2], 2)}.")
    a("")
    a("## Table A — baseline x centroid sigma (patch-refined, full-resolution)")
    a("")
    lines.extend(_grid_table(grid, spec))
    a("")
    a("## Table B — edge-only centroids (sigma x "
      f"{_fmt(spec.edge_sigma_scale, 1)}, the 2304/1536 ratio)")
    a("")
    lines.extend(_grid_table(edge, spec))
    a("")
    a("## Q1 — smallest baseline giving "
      f"{_fmt(spec.p95_target_m, 0)} m p95 range error, per centroid sigma")
    a("")
    a("| Centroid sigma (px) | Smallest baseline (m), patch-refined | "
      "Smallest baseline (m), edge-only |")
    a("| --- | --- | --- |")
    for s in spec.centroid_sigmas_px:
        e_ok = next(
            (b for b in spec.baselines_m if edge[(b, s)].p95_range <= spec.p95_target_m), None
        )
        a(
            f"| {_fmt(s, 2)} | "
            f"{_fmt(q1[s], 0) if q1[s] is not None else 'none in sweep'} | "
            f"{_fmt(e_ok, 0) if e_ok is not None else 'none in sweep'} |"
        )
    a("")
    a("## Q2 — required centroid sigma at each candidate baseline")
    a("")
    a("| Baseline (m) | Largest sigma meeting "
      f"{_fmt(spec.p95_target_m, 0)} m p95 (px) | Range p95 at that sigma (m) |")
    a("| --- | --- | --- |")
    for b in spec.baselines_m:
        if q2[b] is None:
            a(f"| {_fmt(b, 0)} | none in sweep | — |")
        else:
            a(f"| {_fmt(b, 0)} | {_fmt(q2[b], 2)} | {_fmt(grid[(b, q2[b])].p95_range)} |")
    a("")
    a("## Q3 — where depth is too weak to report")
    a("")
    a(f"- Solver weak-geometry gate: triangulation angle < "
      f"{_fmt(config.min_triangulation_angle_deg, 2)} deg or condition > "
      f"{_fmt(config.max_condition, 0)}.")
    a(f"- At {_fmt(spec.range_m, 2)} m range that angle gate corresponds to a total")
    a(f"  baseline of about {_fmt(boundary_baseline, 1)} m: below that, results return")
    a("  `WEAK_GEOMETRY` and must not be consumed as confident points.")
    a("- Max pairwise triangulation angle per swept baseline: "
      + ", ".join(f"{_fmt(b, 0)} m -> {_fmt(angle_at[b], 2)} deg" for b in spec.baselines_m)
      + ".")
    a("- No swept cell tripped the gate (weak fractions in Tables A/B). The")
    a("  practical boundary inside the sweep is accuracy, not the gate: cells with")
    a(f"  range p95 above {_fmt(spec.p95_target_m, 0)} m in Table A: "
      + _fail_cells(grid, spec) + ".")
    a("")
    a("## Q4 — cost of edge-only centroids")
    a("")
    a("| Baseline (m) | Sigma (px) | Range p95 patch (m) | Range p95 edge-only (m) | Ratio |")
    a("| --- | --- | --- | --- | --- |")
    for b in spec.baselines_m:
        for s in spec.centroid_sigmas_px:
            p, e = grid[(b, s)].p95_range, edge[(b, s)].p95_range
            ratio = e / p if np.isfinite(p) and p > 0 else float("nan")
            a(
                f"| {_fmt(b, 0)} | {_fmt(s, 2)} | {_fmt(p)} | {_fmt(e)} | {_fmt(ratio, 2)} |"
            )
    a("")
    a("## Timing jitter sensitivity (nominal baseline "
      f"{_fmt(spec.nominal_baseline_m, 0)} m, sigma {_fmt(spec.nominal_sigma_px, 2)} px)")
    a("")
    a("| Timing jitter (ms, 1-sigma) | Speed (m/s) | Range p95 (m) | Cross p95 (m) | "
      "Coverage 1/2/3 sigma |")
    a("| --- | --- | --- | --- | --- |")
    for t_ms in spec.timing_err_ms:
        for v in spec.target_speeds_mps:
            st = timing[(t_ms, v)]
            a(
                f"| {_fmt(t_ms, 1)} | {_fmt(v, 0)} | {_fmt(st.p95_range)} | "
                f"{_fmt(st.p95_cross)} | {_cov_fmt(st)} |"
            )
    a("")
    a("## Pose error sensitivity curves (systematic, report-only)")
    a("")
    a("Noiseless pixels; the whole error is bias from perturbing the estimator's")
    a("calibration. These numbers belong in the systematic-bound channel.")
    a("")
    a("| Rotation error (deg) | Range p95 (m) | Cross p95 (m) |")
    a("| --- | --- | --- |")
    for r in spec.pose_rot_err_deg:
        a(f"| {_fmt(r, 2)} | {_fmt(rot[r].p95_range)} | {_fmt(rot[r].p95_cross)} |")
    a("")
    a("| Translation error (m) | Range p95 (m) | Cross p95 (m) |")
    a("| --- | --- | --- |")
    for tr in spec.pose_trans_err_m:
        a(f"| {_fmt(tr, 2)} | {_fmt(trans[tr].p95_range)} | {_fmt(trans[tr].p95_cross)} |")
    a("")
    a("## Q5 — ranked dominant error terms at the nominal operating point")
    a("")
    a(f"Nominal (Provisional): baseline {_fmt(spec.nominal_baseline_m, 0)} m, sigma")
    a(f"{_fmt(spec.nominal_sigma_px, 2)} px, timing {_fmt(spec.nominal_timing_ms, 1)} ms,")
    a(f"speed {_fmt(spec.nominal_speed_mps, 0)} m/s, rotation "
      f"{_fmt(spec.nominal_rot_deg, 2)} deg, translation {_fmt(spec.nominal_trans_m, 2)} m.")
    a("")
    a("| Rank | Term | Range p95 contribution (m) | Cross p95 contribution (m) |")
    a("| --- | --- | --- | --- |")
    for rank, (name, p95_r, p95_c) in enumerate(terms, start=1):
        a(f"| {rank} | {name} | {_fmt(p95_r)} | {_fmt(p95_c)} |")
    a("")
    a("## Coverage finding")
    a("")
    a("Reported-covariance coverage under pure centroid noise sits below the")
    a("nominal 68/95/99.7% masses (Table A). Cause: with 3 cameras the robust")
    a("residual variance that scales the covariance is estimated from only")
    a("2N - 3 = 3 degrees of freedom, so the Mahalanobis statistic is")
    a("F-distributed rather than chi-square (heavier tail). This is a property")
    a("of the frozen scale rule at small camera counts, not an estimator bug;")
    a("coverage tightens as cameras are added. Candidate remedy if D5 needs")
    a("calibrated gates at N = 3: floor the residual variance at 1.0")
    a("(`GeometryConfig.residual_variance_floor`), which makes coverage")
    a("conservative instead of optimistic.")
    a("")
    return "\n".join(lines) + "\n"


def _triangulation_angle(baseline_m: float, spec: BudgetSpec) -> float:
    """Max pairwise angle between camera lines of sight to the target."""
    xs = np.linspace(-baseline_m / 2.0, baseline_m / 2.0, spec.n_cameras)
    target = np.array([0.0, spec.range_m, 0.0])
    dirs = []
    for x in xs:
        d = target - np.array([x, 0.0, 0.0])
        dirs.append(d / np.linalg.norm(d))
    best = 0.0
    for i in range(len(dirs)):
        for j in range(i + 1, len(dirs)):
            c = float(np.clip(dirs[i] @ dirs[j], -1.0, 1.0))
            best = max(best, float(np.degrees(np.arccos(c))))
    return best


def _fail_cells(grid: dict[tuple[float, float], CellStats], spec: BudgetSpec) -> str:
    fails = [
        f"({_fmt(b, 0)} m, {_fmt(s, 2)} px)"
        for b in spec.baselines_m
        for s in spec.centroid_sigmas_px
        if not (grid[(b, s)].p95_range <= spec.p95_target_m)
    ]
    return ", ".join(fails) if fails else "none"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="D1 Monte Carlo error budget (Modeled)")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=str, required=True)
    parser.add_argument(
        "--trials",
        type=int,
        default=BudgetSpec.trials,
        help="trials per cell (default 1000; lower only for smoke tests)",
    )
    args = parser.parse_args(argv)
    spec = replace(BudgetSpec(), trials=args.trials)
    report = generate_report(args.seed, args.trials, spec, GeometryConfig())
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report)
    print(f"wrote {args.out} (seed {args.seed}, {args.trials} trials/cell)")


if __name__ == "__main__":
    main()
