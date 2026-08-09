"""Systematic-bound estimation (D6-F1): the second error channel, populated.

D0 section 5 requires every LocalizationResult to carry a per-axis
``systematic_bound`` with source labels beside the conditional covariance.
D5 shipped it as (0,0,0) with no sources; D6 measured the consequence
(calibration faults published confidently-wrong states with nothing
flagging them). This module fills it.

The bound is computed from what the system was TOLD, never from truth:

- ``calibration``: declared per-camera rotation/position sigma propagated
  through the actual solve geometry. A rotation sigma is an angular error,
  so its world-space effect grows with range (sigma_rad x range); a
  position sigma enters directly. Both are combined across the supporting
  cameras in the conservative (worst-camera) sense, because a systematic
  bias does not average down the way random noise does.
- ``target_reference``: half the declared target width — the centroid of
  the visible foreground can sit anywhere within the target volume
  relative to its geometric center (the D0 truth reference).
- ``clock``: declared ``time_sync_error_ms`` times the estimated speed;
  a timing error moves a moving target along its own velocity.

Honesty semantics (D0 §10, D6.1): the bound describes the DECLARED
uncertainty. An undeclared calibration error is by construction invisible
here — the system's obligation is honesty about what it was told, which is
exactly what the honest/dishonest campaign pair measures.
"""

from __future__ import annotations

import numpy as np

from skyweave2.contracts import CameraModel, Observation2D, SystematicSource


def _solve_closest_point(origins: np.ndarray, directions: np.ndarray) -> np.ndarray:
    """Unweighted closest point to a set of rays (working plan §7.4 form)."""
    a = np.zeros((3, 3))
    b = np.zeros(3)
    for origin, direction in zip(origins, directions, strict=True):
        p = np.eye(3) - np.outer(direction, direction)
        a += p
        b += p @ origin
    eigvals = np.linalg.eigvalsh(a)
    if eigvals[0] <= 1e-12 * max(eigvals[-1], 1.0):
        return np.linalg.lstsq(a, b, rcond=None)[0]
    return np.linalg.solve(a, b)


def _perpendicular_axes(direction: np.ndarray) -> np.ndarray:
    ref = np.array([0.0, 0.0, 1.0])
    if abs(float(direction @ ref)) > 1.0 - 1e-6:
        ref = np.array([1.0, 0.0, 0.0])
    e1 = np.cross(direction, ref)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(direction, e1)
    e2 /= np.linalg.norm(e2)
    return np.stack([e1, e2])


def calibration_bound(
    position: np.ndarray,
    cameras: list[CameraModel],
    rotation_sigma_deg: float,
    position_sigma_m: float,
) -> np.ndarray:
    """Per-axis world bound from declared calibration uncertainty,
    PROPAGATED THROUGH THE SOLVE GEOMETRY.

    A per-ray displacement is not the bound: triangulation amplifies an
    angular error along the depth direction by roughly range/baseline (the
    same factor D1's error budget measured). So each camera's declared
    uncertainty is pushed through the actual closest-point solve by finite
    difference — rotate that camera's bearing by the declared sigma about
    each of two perpendicular axes, re-solve, and take the largest per-axis
    displacement. Per-camera worst cases are SUMMED: a bound must hold when
    every camera is wrong at once, and biases do not average down.

    Deterministic: fixed perturbation axes, no sampling.
    """
    if rotation_sigma_deg <= 0.0 and position_sigma_m <= 0.0:
        return np.zeros(3)
    if len(cameras) < 2:
        return np.zeros(3)
    position = np.asarray(position, dtype=np.float64)
    sigma_rad = np.radians(max(rotation_sigma_deg, 0.0))

    origins = np.array([c.position_world() for c in cameras])
    directions = []
    for origin in origins:
        offset = position - origin
        directions.append(offset / np.linalg.norm(offset))
    directions = np.array(directions)
    baseline_solution = _solve_closest_point(origins, directions)

    total = np.zeros(3)
    for index in range(len(cameras)):
        worst = np.zeros(3)
        axes = _perpendicular_axes(directions[index])
        if sigma_rad > 0.0:
            for axis in axes:
                for sign in (+1.0, -1.0):
                    perturbed = directions.copy()
                    tilted = (directions[index] * np.cos(sigma_rad)
                              + sign * axis * np.sin(sigma_rad))
                    perturbed[index] = tilted / np.linalg.norm(tilted)
                    shifted = _solve_closest_point(origins, perturbed)
                    worst = np.maximum(worst, np.abs(shifted - baseline_solution))
        if position_sigma_m > 0.0:
            for axis_index in range(3):
                for sign in (+1.0, -1.0):
                    moved = origins.copy()
                    moved[index, axis_index] += sign * position_sigma_m
                    # The bearing is attached to the camera: moving the
                    # centre moves the ray origin, not its direction.
                    shifted = _solve_closest_point(moved, directions)
                    worst = np.maximum(worst, np.abs(shifted - baseline_solution))
        total = total + worst
    return total


def target_reference_bound(target_width_m: float) -> np.ndarray:
    """Centroid-vs-geometric-center bound: half the declared target width."""
    half = max(target_width_m, 0.0) / 2.0
    return np.full(3, half)


def clock_bound(
    observations: list[Observation2D], speed_mps: float
) -> np.ndarray:
    """Declared timing error x estimated speed, along all axes.

    The worst declared ``time_sync_error_ms`` across the consumed
    observations sets the bound: one badly-synced camera is enough to
    displace the fused point.
    """
    if speed_mps <= 0.0 or not observations:
        return np.zeros(3)
    worst_ms = max(o.envelope.time_sync_error_ms for o in observations)
    return np.full(3, worst_ms * 1e-3 * max(speed_mps, 0.0))


def estimate_systematic(
    position: np.ndarray,
    observations: list[Observation2D],
    cameras: list[CameraModel],
    rotation_sigma_deg: float,
    position_sigma_m: float,
    target_width_m: float,
    speed_mps: float,
) -> tuple[tuple[float, float, float], list[SystematicSource]]:
    """Total per-axis systematic bound and the sources that contributed.

    Terms are summed (not combined in quadrature): each is a BOUND, and a
    bound on a sum of biases is the sum of the bounds. Only sources that
    actually contribute are labeled — an empty declaration yields an empty
    source list, so a consumer can tell "no bias" from "no declaration".
    """
    sources: list[SystematicSource] = []
    total = np.zeros(3)

    calibration = calibration_bound(position, cameras, rotation_sigma_deg,
                                    position_sigma_m)
    if float(np.max(calibration)) > 0.0:
        total = total + calibration
        sources.append(SystematicSource.CALIBRATION)

    target = target_reference_bound(target_width_m)
    if float(np.max(target)) > 0.0:
        total = total + target
        sources.append(SystematicSource.TARGET_REFERENCE)

    clock = clock_bound(observations, speed_mps)
    if float(np.max(clock)) > 0.0:
        total = total + clock
        sources.append(SystematicSource.CLOCK)

    return tuple(float(v) for v in total), sources
