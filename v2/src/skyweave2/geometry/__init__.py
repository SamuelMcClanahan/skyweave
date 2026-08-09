"""D1 geometry engine: bearings, deterministic init, robust refinement, budget.

Inputs and outputs are the frozen D0 contracts (`Observation2D`,
`CameraModel`, `LocalizationResult`). See `v2/docs/PHASE_D1_BRIEF.md`.
"""

from skyweave2.geometry.bearings import (
    Bearing,
    bearing_from_observation,
    bearing_from_pixel,
    triangulation_angle_deg,
)
from skyweave2.geometry.config import GeometryConfig
from skyweave2.geometry.initializer import InitializerResult, initialize
from skyweave2.geometry.refine import (
    LooEntry,
    SolveOutcome,
    leave_one_out,
    localize,
    solve_point,
)

__all__ = [
    "Bearing",
    "GeometryConfig",
    "InitializerResult",
    "LooEntry",
    "SolveOutcome",
    "bearing_from_observation",
    "bearing_from_pixel",
    "initialize",
    "leave_one_out",
    "localize",
    "solve_point",
    "triangulation_angle_deg",
]
