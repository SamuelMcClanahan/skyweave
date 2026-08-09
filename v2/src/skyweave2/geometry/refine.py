"""Robust refinement: Huber Gauss-Newton on pixel reprojection residuals.

Frozen D1 decisions implemented here:

- Residual space: pixel reprojection (raw distorted pixels, full resolution).
- Robust loss: Huber, threshold in pixels from ``GeometryConfig.huber_px``.
- Solver: Gauss-Newton with Levenberg damping, numpy only.
- Covariance: inverse of the weighted normal-equations matrix at the
  solution, scaled by the robust residual variance. Full 3x3, anisotropic.
- Outliers: Huber reweighting plus the leave-one-camera-out diagnostic.
  Membership selection (consensus) is out of scope (D5).
- Degeneracy: triangulation angle, conditioning, cheirality. Weak geometry
  is reported as ``ResultStatus.WEAK_GEOMETRY``, never a confident point.

The numeric core (:func:`solve_point`, :func:`leave_one_out`) works on raw
arrays so the Monte Carlo budget can drive it without pydantic overhead.
:func:`localize` is the contract-level wrapper producing a
``LocalizationResult`` from ``Observation2D`` + ``CameraModel``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from skyweave2.contracts import (
    CameraModel,
    LocalizationResult,
    Observation2D,
    ResultStatus,
)
from skyweave2.geometry.bearings import Bearing, bearing_from_pixel
from skyweave2.geometry.config import GeometryConfig
from skyweave2.geometry.initializer import initialize


@dataclass(frozen=True)
class SolveOutcome:
    """Numeric result of one localization solve."""

    position: np.ndarray  # (3,)
    covariance: np.ndarray  # (3, 3) conditional, meters^2
    residual_px_rms: float  # sqrt(mean over used cameras of ||residual||^2), raw px
    condition: float  # normal-equations conditioning at the solution
    triangulation_angle_deg: float
    used_indices: list[int]  # indices into the input arrays that support the solve
    excluded_indices: list[int]  # dropped by cheirality
    weak_geometry: bool
    converged: bool
    iterations: int
    robust_weights: np.ndarray = field(default_factory=lambda: np.empty(0))
    residual_variance: float = 0.0  # the s^2 that scaled the covariance


def _residuals_jacobians(
    cameras: list[CameraModel],
    pixels: np.ndarray,
    point: np.ndarray,
    fd_step_m: float,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Per-camera reprojection residuals (z - proj) and Jacobians d proj / d point.

    Returns None when the point projects behind any camera (step rejected).
    """
    n = len(cameras)
    residuals = np.empty((n, 2), dtype=np.float64)
    jacobians = np.empty((n, 2, 3), dtype=np.float64)
    for i, cam in enumerate(cameras):
        proj = cam.project(point)
        if proj is None:
            return None
        residuals[i] = pixels[i] - np.asarray(proj)
        for axis in range(3):
            pert = point.copy()
            pert[axis] += fd_step_m
            proj_pert = cam.project(pert)
            if proj_pert is None:
                return None
            jacobians[i, :, axis] = (np.asarray(proj_pert) - np.asarray(proj)) / fd_step_m
    return residuals, jacobians


def _huber_weights(residuals: np.ndarray, huber_px: float, robust: bool) -> np.ndarray:
    """IRLS weight per camera from the raw pixel residual norm."""
    norms = np.linalg.norm(residuals, axis=1)
    if not robust:
        return np.ones_like(norms)
    weights = np.ones_like(norms)
    over = norms > huber_px
    weights[over] = huber_px / norms[over]
    return weights


def _robust_cost(residuals: np.ndarray, info: np.ndarray, huber_px: float, robust: bool) -> float:
    """Sum of per-camera Huber-weighted whitened quadratic residuals."""
    weights = _huber_weights(residuals, huber_px, robust)
    cost = 0.0
    for i in range(len(residuals)):
        cost += float(weights[i] * residuals[i] @ info[i] @ residuals[i])
    return cost


def _normal_equations(
    residuals: np.ndarray, jacobians: np.ndarray, info: np.ndarray, weights: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    h = np.zeros((3, 3), dtype=np.float64)
    g = np.zeros(3, dtype=np.float64)
    for i in range(len(residuals)):
        jtw = weights[i] * jacobians[i].T @ info[i]
        h += jtw @ jacobians[i]
        g += jtw @ residuals[i]
    return h, g


def _condition(h: np.ndarray, eps_singular: float) -> float:
    eigvals = np.linalg.eigvalsh(h)
    if eigvals[0] <= eps_singular * max(eigvals[-1], 1.0):
        return float(np.inf)
    return float(eigvals[-1] / eigvals[0])


def _covariance(
    h: np.ndarray,
    residuals: np.ndarray,
    info: np.ndarray,
    weights: np.ndarray,
    config: GeometryConfig,
) -> tuple[np.ndarray, float]:
    """Inverse weighted normal equations scaled by robust residual variance."""
    dof = 2 * len(residuals) - 3
    if dof > 0:
        s2 = (
            sum(
                float(weights[i] * residuals[i] @ info[i] @ residuals[i])
                for i in range(len(residuals))
            )
            / dof
        )
    else:
        s2 = 0.0
    s2 = max(s2, config.residual_variance_floor)
    h_inv = np.linalg.pinv(h, hermitian=True)
    cov = s2 * h_inv
    cov = 0.5 * (cov + cov.T)
    # Clip tiny negative eigenvalues from pinv round-off so the contract's
    # PSD validator never rejects an honest result.
    eigvals, eigvecs = np.linalg.eigh(cov)
    cov = (eigvecs * np.clip(eigvals, 0.0, None)) @ eigvecs.T
    return cov, s2


def solve_point(
    cameras: list[CameraModel],
    pixels: np.ndarray,
    pixel_covs: np.ndarray,
    config: GeometryConfig,
    robust: bool = True,
    initial_position: np.ndarray | None = None,
) -> SolveOutcome:
    """Initialize, resolve cheirality, then run Huber Gauss-Newton with damping.

    ``pixels`` is (N, 2) raw full-resolution coordinates; ``pixel_covs`` is
    (N, 2, 2). Entry ``i`` belongs to ``cameras[i]``. ``initial_position``
    warm-starts the refinement (used by the leave-one-out diagnostic, which
    asks a local sensitivity question about an existing solution); degeneracy
    gates still apply.
    """
    pixels = np.asarray(pixels, dtype=np.float64)
    pixel_covs = np.asarray(pixel_covs, dtype=np.float64)
    if len(cameras) < config.min_cameras:
        raise ValueError(f"need at least {config.min_cameras} cameras, got {len(cameras)}")

    all_indices = list(range(len(cameras)))
    bearings: dict[int, Bearing] = {
        i: bearing_from_pixel(cameras[i], pixels[i, 0], pixels[i, 1], pixel_covs[i], config)
        for i in all_indices
    }
    info = np.stack([np.linalg.inv(pixel_covs[i]) for i in all_indices])

    # Cheirality: drop cameras the initial point sits behind, re-initialize,
    # repeat until stable or too few cameras remain.
    used = list(all_indices)
    init = initialize([bearings[i] for i in used], config)
    for _ in range(len(cameras)):
        behind = {
            bearings[i].camera_id: i
            for i in used
            if bearings[i].camera_id in init.cheirality_violations
        }
        if not behind:
            break
        remaining = [i for i in used if i not in behind.values()]
        if len(remaining) < config.min_cameras:
            break
        used = remaining
        init = initialize([bearings[i] for i in used], config)
    excluded = [i for i in all_indices if i not in used]

    angle = init.triangulation_angle_deg
    weak = not init.ok
    point = init.position.copy()
    if initial_position is not None and np.all(np.isfinite(initial_position)):
        point = np.asarray(initial_position, dtype=np.float64).copy()
    if not np.all(np.isfinite(point)):
        point = np.zeros(3, dtype=np.float64)

    cams_used = [cameras[i] for i in used]
    pix_used = pixels[used]
    info_used = info[used]

    converged = False
    iterations = 0
    if not weak:
        evaluated = _residuals_jacobians(cams_used, pix_used, point, config.fd_step_m)
        if evaluated is None:
            weak = True
        else:
            residuals, jacobians = evaluated
            cost = _robust_cost(residuals, info_used, config.huber_px, robust)
            damping = config.damping_init
            while iterations < config.max_iterations:
                iterations += 1
                weights = _huber_weights(residuals, config.huber_px, robust)
                h, g = _normal_equations(residuals, jacobians, info_used, weights)
                try:
                    step = np.linalg.solve(h + damping * np.diag(np.diag(h)), g)
                except np.linalg.LinAlgError:
                    weak = True
                    break
                candidate = point + step
                evaluated = _residuals_jacobians(
                    cams_used, pix_used, candidate, config.fd_step_m
                )
                if evaluated is None:
                    damping = min(damping * config.damping_up, config.damping_max)
                    continue
                new_residuals, new_jacobians = evaluated
                new_cost = _robust_cost(new_residuals, info_used, config.huber_px, robust)
                if new_cost <= cost:
                    point = candidate
                    improvement = cost - new_cost
                    residuals, jacobians, cost = new_residuals, new_jacobians, new_cost
                    damping = max(damping * config.damping_down, config.damping_init)
                    if (
                        float(np.linalg.norm(step)) < config.step_tol_m
                        or improvement < config.cost_tol
                    ):
                        converged = True
                        break
                else:
                    damping = min(damping * config.damping_up, config.damping_max)
                    if damping >= config.damping_max:
                        converged = True
                        break

    # Metrics at the final point (refined or initializer-only for weak cases).
    evaluated = _residuals_jacobians(cams_used, pix_used, point, config.fd_step_m)
    if evaluated is None:
        n_used = len(cams_used)
        return SolveOutcome(
            position=point,
            covariance=np.zeros((3, 3), dtype=np.float64),
            residual_px_rms=0.0,
            condition=float(np.inf),
            triangulation_angle_deg=angle,
            used_indices=used,
            excluded_indices=excluded,
            weak_geometry=True,
            converged=False,
            iterations=iterations,
            robust_weights=np.ones(n_used),
        )
    residuals, jacobians = evaluated
    weights = _huber_weights(residuals, config.huber_px, robust)
    h, _ = _normal_equations(residuals, jacobians, info_used, weights)
    condition = _condition(h, config.eps_singular)
    covariance, s2 = _covariance(h, residuals, info_used, weights, config)
    residual_px_rms = float(np.sqrt(np.mean(np.sum(residuals**2, axis=1))))
    weak = weak or condition > config.max_condition

    return SolveOutcome(
        position=point,
        covariance=covariance,
        residual_px_rms=residual_px_rms,
        condition=condition,
        triangulation_angle_deg=angle,
        used_indices=used,
        excluded_indices=excluded,
        weak_geometry=bool(weak),
        converged=converged,
        iterations=iterations,
        robust_weights=weights,
        residual_variance=s2,
    )


@dataclass(frozen=True)
class LooEntry:
    """Leave-one-camera-out diagnostic for one camera."""

    index: int
    camera_id: int
    residual_px: float  # this camera's residual against the solve WITHOUT it
    reduced_rms_px: float  # internal residual rms of the solve without this camera
    position_shift_m: float  # how far the solution moves when this camera is dropped
    flagged: bool


def leave_one_out(
    cameras: list[CameraModel],
    pixels: np.ndarray,
    pixel_covs: np.ndarray,
    config: GeometryConfig,
    base: SolveOutcome | None = None,
    robust: bool = True,
) -> list[LooEntry]:
    """Re-solve without each camera, warm-started from the full solution.

    A camera is flagged when the remaining cameras agree with each other
    (small internal rms) but disagree with it (large held-out residual).
    A large held-out residual against an internally inconsistent reduced
    solve indicts the solve, not the camera, and does not flag.
    """
    pixels = np.asarray(pixels, dtype=np.float64)
    pixel_covs = np.asarray(pixel_covs, dtype=np.float64)
    if base is None:
        base = solve_point(cameras, pixels, pixel_covs, config, robust=robust)
    if len(cameras) - 1 < config.min_cameras:
        raise ValueError("leave-one-out needs at least min_cameras + 1 cameras")

    entries: list[LooEntry] = []
    for i in range(len(cameras)):
        keep = [j for j in range(len(cameras)) if j != i]
        outcome = solve_point(
            [cameras[j] for j in keep],
            pixels[keep],
            pixel_covs[keep],
            config,
            robust=robust,
            initial_position=base.position,
        )
        proj = cameras[i].project(outcome.position)
        if proj is None or outcome.weak_geometry:
            residual = float(np.inf)
        else:
            residual = float(np.linalg.norm(pixels[i] - np.asarray(proj)))
        shift = float(np.linalg.norm(outcome.position - base.position))
        entries.append(
            LooEntry(
                index=i,
                camera_id=cameras[i].camera_id,
                residual_px=residual,
                reduced_rms_px=outcome.residual_px_rms,
                position_shift_m=shift,
                flagged=(
                    outcome.residual_px_rms < config.loo_consistent_rms_px
                    and residual > config.loo_flag_residual_px
                ),
            )
        )
    return entries


def localize(
    observations: list[Observation2D],
    cameras: dict[int, CameraModel],
    config: GeometryConfig,
    robust: bool = True,
) -> LocalizationResult:
    """Contract-level solve: frozen D0 inputs in, ``LocalizationResult`` out.

    Observations are sorted internally by (camera_id, obs_id) so camera input
    order never changes the result (G2).
    """
    if not observations:
        raise ValueError("no observations")
    ordered = sorted(observations, key=lambda o: (o.envelope.camera_id, o.obs_id))
    domains = {o.envelope.clock_domain for o in ordered}
    if len(domains) > 1:
        raise ValueError(f"mixed clock domains {sorted(d.value for d in domains)}; map first")
    for obs in ordered:
        if obs.envelope.camera_id not in cameras:
            raise ValueError(f"no camera model for camera_id {obs.envelope.camera_id}")
        cam = cameras[obs.envelope.camera_id]
        if obs.envelope.calibration_rev != cam.calibration_rev:
            raise ValueError(
                f"calibration_rev mismatch for camera {cam.camera_id}: "
                f"{obs.envelope.calibration_rev!r} vs {cam.calibration_rev!r}"
            )

    cams = [cameras[o.envelope.camera_id] for o in ordered]
    pixels = np.array([[o.u, o.v] for o in ordered], dtype=np.float64)
    pixel_covs = np.array(
        [[[o.cov_uu, o.cov_uv], [o.cov_uv, o.cov_vv]] for o in ordered], dtype=np.float64
    )
    outcome = solve_point(cams, pixels, pixel_covs, config, robust=robust)

    times = sorted(o.envelope.capture_ts_ns for o in ordered)
    ts_ns = times[(len(times) - 1) // 2]  # lower median: deterministic, stays an int
    used_obs = [ordered[i] for i in outcome.used_indices]
    status = ResultStatus.WEAK_GEOMETRY if outcome.weak_geometry else ResultStatus.CONFIRMED

    return LocalizationResult(
        ts_ns=ts_ns,
        clock_domain=ordered[0].envelope.clock_domain,
        position=tuple(float(x) for x in outcome.position),
        covariance=tuple(float(x) for x in outcome.covariance.reshape(-1)),
        residual_px_rms=outcome.residual_px_rms,
        supporting_camera_ids=[o.envelope.camera_id for o in used_obs],
        triangulation_angle_deg=outcome.triangulation_angle_deg,
        condition=outcome.condition,
        status=status,
        obs_ids=[
            f"{o.envelope.camera_id}:{o.envelope.session_uuid}:{o.envelope.frame_seq}:{o.obs_id}"
            for o in used_obs
        ],
    )
