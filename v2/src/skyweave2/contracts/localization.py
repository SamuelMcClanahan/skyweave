"""LocalizationResult: one fused 3D measurement (D0, frozen).

Two separate error channels (D0 decision):
- ``covariance``: full 3x3 conditional covariance, row-major, meters^2.
  Random error under the current calibration. The ONLY channel a track
  filter may consume.
- ``systematic_bound`` + ``systematic_sources``: per-axis bias bound in
  meters with labeled sources. Reported beside every result. Never fed to
  the filter, never averaged down.

More than one result per capture event is legal (data association).

Reserved wire field numbers: 1 ts_ns, 2 clock_domain, 3 position,
4 covariance, 5 systematic_bound, 6 systematic_sources, 7 residual_px_rms,
8 supporting_camera_ids, 9 triangulation_angle_deg, 10 condition,
11 status, 12 obs_ids.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field, model_validator

from skyweave2.contracts.base import ContractModel
from skyweave2.contracts.enums import ClockDomain, ResultStatus, SystematicSource


class LocalizationResult(ContractModel):
    ts_ns: int
    clock_domain: ClockDomain
    position: tuple[float, float, float]
    covariance: tuple[float, float, float, float, float, float, float, float, float]
    systematic_bound: tuple[float, float, float] = (0.0, 0.0, 0.0)
    systematic_sources: list[SystematicSource] = Field(default_factory=list)
    residual_px_rms: float = Field(ge=0.0)
    supporting_camera_ids: list[int]
    triangulation_angle_deg: float = Field(ge=0.0)
    condition: float = Field(gt=0.0)
    status: ResultStatus
    obs_ids: list[str] = Field(
        default_factory=list,
        description="'{camera_id}:{session_uuid}:{frame_seq}:{obs_id}' per consumed observation",
    )

    @model_validator(mode="after")
    def _covariance_is_symmetric_psd(self) -> LocalizationResult:
        c = np.asarray(self.covariance, dtype=np.float64).reshape(3, 3)
        if not np.allclose(c, c.T, atol=1e-9):
            raise ValueError("covariance must be symmetric")
        if np.any(np.linalg.eigvalsh(c) < -1e-12):
            raise ValueError("covariance must be positive semidefinite")
        return self

    def covariance_matrix(self) -> np.ndarray:
        return np.asarray(self.covariance, dtype=np.float64).reshape(3, 3)
