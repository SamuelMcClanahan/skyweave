"""Observation2D: one 2D measurement from one frame (D0, frozen).

Rules:
- ``u``/``v`` are FULL-RESOLUTION raw pixel coordinates (pixel-center
  convention), regardless of the detector's processing resolution.
- The 2x2 centroid covariance is in full-resolution pixels squared and is
  never zero; the floor comes from measured repeatability (D4).
- ``evidence_ref`` (patch/mask) is OPTIONAL and droppable: losing it must
  never lose the measurement (test T7).

Reserved wire field numbers: 1 envelope, 2 obs_id, 3 u, 4 v, 5 cov_uu,
6 cov_uv, 7 cov_vv, 8 bbox_x/y/w/h, 9 area_px, 10 persistence_count,
11 confidence, 12 local_blob_id, 13 evidence_ref.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from skyweave2.contracts.base import ContractModel
from skyweave2.contracts.frames import FrameEnvelope


class Observation2D(ContractModel):
    envelope: FrameEnvelope
    obs_id: int = Field(ge=0)
    u: float
    v: float
    cov_uu: float = Field(gt=0.0)
    cov_uv: float = 0.0
    cov_vv: float = Field(gt=0.0)
    bbox_x: int
    bbox_y: int
    bbox_w: int = Field(gt=0)
    bbox_h: int = Field(gt=0)
    area_px: int = Field(gt=0, description="component area at PROC resolution")
    persistence_count: int = Field(ge=1, default=1)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    local_blob_id: int | None = None
    evidence_ref: str | None = None

    @model_validator(mode="after")
    def _covariance_is_positive_definite(self) -> Observation2D:
        det = self.cov_uu * self.cov_vv - self.cov_uv * self.cov_uv
        if det <= 0.0:
            raise ValueError("centroid covariance must be positive definite")
        return self

    def drop_evidence(self) -> Observation2D:
        """Return the same measurement without optional evidence (T7)."""
        return self.model_copy(update={"evidence_ref": None})
