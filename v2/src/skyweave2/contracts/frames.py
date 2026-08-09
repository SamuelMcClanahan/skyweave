"""FrameEnvelope: the identity of one capture event (D0, frozen).

``capture_ts_ns`` names the EXPOSURE MIDPOINT of the frame (D0 decision).
The envelope is immutable: receive-side times live in separate records and
never overwrite capture fields.

Reserved wire field numbers: 1 camera_id, 2 session_uuid, 3 frame_seq,
4 capture_ts_ns, 5 clock_domain, 6 time_sync_error_ms, 7 exposure_us,
8 gain_db, 9 full_width, 10 full_height, 11 proc_width, 12 proc_height,
13 calibration_rev, 14 detector_rev, 15 line_readout_us.
"""

from __future__ import annotations

from pydantic import Field

from skyweave2.contracts.base import FrozenContractModel
from skyweave2.contracts.enums import ClockDomain


class FrameEnvelope(FrozenContractModel):
    camera_id: int
    session_uuid: str
    frame_seq: int = Field(ge=0)
    capture_ts_ns: int
    clock_domain: ClockDomain
    time_sync_error_ms: float = Field(ge=0.0)
    exposure_us: float = Field(gt=0.0)
    gain_db: float = 0.0
    full_width: int = Field(gt=0)
    full_height: int = Field(gt=0)
    proc_width: int = Field(gt=0)
    proc_height: int = Field(gt=0)
    calibration_rev: str
    detector_rev: str
    line_readout_us: float = Field(ge=0.0, default=0.0)

    @property
    def proc_scale_x(self) -> float:
        return self.full_width / self.proc_width

    @property
    def proc_scale_y(self) -> float:
        return self.full_height / self.proc_height
