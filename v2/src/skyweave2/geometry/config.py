"""GeometryConfig: every threshold the D1 geometry engine uses.

The D1 brief forbids constants in code: any value that gates a decision
(robust threshold, iteration budget, degeneracy limits, diagnostic flags)
lives here. Numerical step sizes for finite differences are included so a
future analytic-Jacobian swap is a config-visible change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GeometryConfig:
    # Robust loss (Huber), threshold on the raw pixel residual norm.
    huber_px: float = 1.5

    # Gauss-Newton with Levenberg damping.
    max_iterations: int = 50
    step_tol_m: float = 1e-10
    cost_tol: float = 1e-14
    damping_init: float = 1e-6
    damping_up: float = 10.0
    damping_down: float = 0.1
    damping_max: float = 1e10

    # Degeneracy gates.
    min_cameras: int = 2
    min_triangulation_angle_deg: float = 0.5
    max_condition: float = 1e8

    # Covariance: sigma^2 = max(robust residual variance, floor) scales the
    # inverse normal-equations matrix. Floor 0.0 keeps the frozen D1 rule
    # exactly; raising it is a recorded-config change, not a code change.
    # D6-F2 (2026-08-08): floored from D4's MEASURED centroid repeatability
    # (0.128 px combined, full-res equivalent; D4_DETECTOR_REPORT.md). The
    # solver's residual variance is in units of the whitened pixel residual,
    # so a detector whose true centroid sigma is s_px cannot produce a
    # residual variance below (s_px / sigma_declared)^2 in expectation; with
    # the D4 sigma and the D0 covariance floor (0.5 px declared) that is
    # (0.128 / 0.5)^2 = 0.066. Evidence-derived from a Measured bench
    # number, NOT tuned against any gate scene.
    residual_variance_floor: float = 0.066

    # Leave-one-camera-out diagnostic: a camera is flagged when the solve
    # WITHOUT it is internally consistent (rms below loo_consistent_rms_px)
    # while the held-out camera's own residual exceeds loo_flag_residual_px.
    # Both conditions are required: a large held-out residual against an
    # inconsistent reduced solve indicts the solve, not the camera.
    loo_flag_residual_px: float = 5.0
    loo_consistent_rms_px: float = 3.0

    # Finite-difference step sizes.
    fd_step_m: float = 1e-4
    fd_step_px: float = 1e-3

    # Numerical guards.
    eps_direction_norm: float = 1e-12
    eps_singular: float = 1e-14
