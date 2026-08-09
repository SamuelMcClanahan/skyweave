"""FusionConfig: every D5 threshold a field; no constants in code.

All values are Provisional and tuned ONLY on seed-variant datasets
(anti-tuning rule: the gate clip is scored once per frozen config; a miss
there is a finding, never a tuning target).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AlignerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Event-time batches of one frame period (manifest fps 30).
    batch_period_ns: int = Field(default=33_333_333, gt=0)
    # Observations older than this behind the newest seen capture time are
    # late: recorded with a reason, never solved.
    lateness_ns: int = Field(default=100_000_000, gt=0)  # 3 frame periods
    # Batches are emitted once the newest capture time passes batch end by
    # this margin (reorder tolerance inside the stream).
    emit_horizon_ns: int = Field(default=66_666_666, ge=0)  # 2 frame periods


class AssociationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Epipolar prefilter: max point-to-epipolar-line distance, full-res px.
    epipolar_gate_px: float = Field(default=6.0, gt=0.0)
    # Consensus: a remaining camera supports a pair hypothesis when its
    # observation reprojects within this distance of the pair's solve.
    consensus_gate_px: float = Field(default=6.0, gt=0.0)
    # Post-solve residual bound for a group to survive.
    residual_px_max: float = Field(default=3.0, gt=0.0)
    # A better group must beat an overlapping one by this residual margin
    # before stealing its observations (deterministic tie-break otherwise).
    consensus_margin_px: float = Field(default=0.5, ge=0.0)
    # Track seeding: predicted track position gates candidate groups.
    track_gate_m: float = Field(default=12.0, gt=0.0)
    # Minimum cameras for any hypothesis (pairs) — confirmation needs more.
    min_cameras: int = Field(default=2, ge=2)


class VoxelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False  # default recorded AFTER the F10 comparison
    cell_m: float = Field(default=4.0, gt=0.0)
    # Bounded local grid half-extent around each seed (candidate region or
    # track prediction), in cells.
    half_extent_cells: int = Field(default=8, ge=1)
    # A cell is a proposal only with support from at least this many cameras
    # (bitmask popcount) and only as a local peak.
    min_camera_support: int = Field(default=2, ge=2)
    # Ray-to-cell distance for a camera to vote for a cell.
    vote_radius_cells: float = Field(default=0.75, gt=0.0)
    max_peaks: int = Field(default=8, ge=1)
    # Observation-gathering gate around a peak's reprojection. Deliberately
    # looser than the association consensus gate: a voting oracle is
    # proposal-grade (cell-quantized, shallow convergence bundles), and the
    # D1 solver + residual gate downstream are the accuracy mechanism.
    gather_gate_px: float = Field(default=40.0, gt=0.0)


class TrackerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Six-state CV process noise: white-acceleration PSD, (m/s^2)^2/Hz-ish
    # discrete form; swept on seed variants.
    accel_psd: float = Field(default=4.0, gt=0.0)
    # Initial velocity variance for new tracks, (m/s)^2.
    initial_velocity_var: float = Field(default=400.0, gt=0.0)
    # NIS gate: chi-square 3-dof upper quantile (0.99 -> 11.34).
    nis_gate: float = Field(default=11.34, gt=0.0)
    # Floor added to measurement covariance before the update: keeps NIS
    # finite on near-exact fixtures where the D1 residual-variance scaling
    # collapses the reported covariance.
    measurement_var_floor_m2: float = Field(default=1e-4, gt=0.0)
    # Confirmation routes (frozen decision; numbers Provisional/config):
    confirm_min_cameras: int = Field(default=3, ge=2)  # route A: N-cam in one batch
    confirm_two_cam_batches: int = Field(default=3, ge=1)  # route B: 2-cam x N batches
    # Coast/delete/reacquire timers, in event-time batches.
    coast_after_misses: int = Field(default=3, ge=1)
    delete_after_coast: int = Field(default=15, ge=1)
    # Suppression: mechanism only in D5 (clutter rules wait for real sky).
    # A suppressed track keeps consuming its gated observations for this
    # many batches (blocking re-birth) and publishes nothing.
    suppress_lifetime_batches: int = Field(default=30, ge=1)


class SystematicConfig(BaseModel):
    """DECLARED uncertainty feeding the systematic channel (D6-F1).

    These are declarations, not measurements of truth: the bound the system
    publishes describes what it was TOLD about its own calibration. An
    undeclared error is invisible here by construction — that asymmetry is
    the point, and the honest/dishonest campaign pair measures it.
    """

    model_config = ConfigDict(extra="forbid")

    # Per-camera calibration uncertainty as declared by the deployment.
    calibration_rotation_sigma_deg: float = Field(default=0.0, ge=0.0)
    calibration_position_sigma_m: float = Field(default=0.0, ge=0.0)
    # Declared target extent: the centroid may sit anywhere inside the
    # volume relative to the D0 geometric-center truth reference.
    target_width_m: float = Field(default=0.0, ge=0.0)
    # Estimated target speed used with declared time_sync_error to bound the
    # clock term. Declared, not inferred from truth.
    estimated_speed_mps: float = Field(default=0.0, ge=0.0)


class FusionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aligner: AlignerConfig = AlignerConfig()
    association: AssociationConfig = AssociationConfig()
    voxel: VoxelConfig = VoxelConfig()
    tracker: TrackerConfig = TrackerConfig()
    systematic: SystematicConfig = SystematicConfig()
