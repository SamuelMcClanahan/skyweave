"""DetectorConfig: every knob for every backend; all config, no constants.

The ``ive_approx`` block mirrors the RK_MPI_IVE_GMM2 control set (model
count, variance init/floor, learning rates, background ratio) so the D8
board runs can be predicted by sweeping the same names. Values here are
Provisional defaults, not claims about the board.

The centroid covariance floor is a MEASURED-channel parameter: this phase's
repeatability harness produces the number; until it is recorded, the default
carries a Provisional label and the report must say so.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Backend(str, Enum):
    FIXED_BG = "fixed_bg"
    MOG2 = "mog2"
    IVE_APPROX = "ive_approx"


class Mog2Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history: int = Field(default=200, ge=1)
    var_threshold: float = Field(default=16.0, gt=0.0)
    detect_shadows: bool = False  # gray-world sky; shadows are a color feature
    learning_rate: float = Field(default=-1.0)  # -1: OpenCV auto (1/history)


class IveApproxParams(BaseModel):
    """Numpy GMM with RK_MPI_IVE_GMM2-shaped knobs (names per the pinned
    Luckfox header): u16 model count, variance init/floor, global learning
    rate, background weight ratio."""

    model_config = ConfigDict(extra="forbid")

    model_num: int = Field(default=3, ge=1, le=5)
    var_init: float = Field(default=225.0, gt=0.0)  # (15 DN)^2
    var_min: float = Field(default=25.0, gt=0.0)  # (5 DN)^2
    learn_rate: float = Field(default=0.005, gt=0.0, le=1.0)
    bg_ratio: float = Field(default=0.9, gt=0.0, le=1.0)
    match_sigmas: float = Field(default=2.5, gt=0.0)  # Mahalanobis match gate
    weight_init: float = Field(default=0.05, gt=0.0, le=1.0)


class FixedBgParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diff_threshold_dn: float = Field(default=12.0, gt=0.0)


class DetectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Backend = Backend.MOG2
    mog2: Mog2Params = Mog2Params()
    ive_approx: IveApproxParams = IveApproxParams()
    fixed_bg: FixedBgParams = FixedBgParams()

    # Processing resolution (the D0 scaling law maps centroids back to full).
    proc_width: int = Field(default=1536, gt=0)
    proc_height: int = Field(default=864, gt=0)

    # Warm-up schedule: background model learns, no observations emitted.
    warmup_frames: int = Field(default=90, ge=0)  # 3 s at 30 fps (manifest)

    # Mask post-processing.
    open_radius_px: int = Field(default=1, ge=0)  # morphological opening; 0 = off
    min_area_px: int = Field(default=2, ge=1)  # at proc resolution
    max_area_px: int = Field(default=10000, ge=1)

    # Persistence: a blob must be seen this many consecutive frames within
    # the gate radius before it becomes an Observation2D.
    persistence_frames: int = Field(default=2, ge=1)
    persistence_gate_px: float = Field(default=12.0, gt=0.0)  # at proc resolution

    # Per-frame component cap (D0 section 10, "D8 opening" — Chosen). At most
    # this many components become observations in one capture event; the rest
    # are DROPPED and COUNTED (never silent, see DetectorStats).
    #
    # It exists because one capture event is one datagram that never splits:
    # without a cap a cluttered frame is a loud encode failure and a lost
    # measurement. The cap lives here, in the SHARED config, so the host
    # detector stays the oracle for the D8 frame->packet fixtures and the edge
    # byte governor (RV1106_EDGE_NODE.md section 8) reads the same number.
    #
    # Invariant, pinned by test E1: `WireLimits.observations_max_count >=
    # max_components_per_frame`. The wire bound is what the nanopb daemon
    # ALLOCATES from, so a cap above it would emit events the board cannot
    # decode. Raising the cap is a ceiling decision, not a detector decision.
    max_components_per_frame: int = Field(default=7, ge=1)

    # Centroid covariance floor, FULL-RESOLUTION px^2. Provisional default;
    # replaced by the Measured repeatability number this phase produces.
    centroid_cov_floor_px2: float = Field(default=0.25, gt=0.0)

    # Envelope bookkeeping for emitted observations.
    exposure_us: float = Field(default=3000.0, gt=0.0)
    fps: float = Field(default=30.0, gt=0.0)
    detector_rev: str = "d4-detector/1"
    calibration_rev: str = "uncalibrated"
    time_sync_error_ms: float = Field(default=0.0, ge=0.0)
