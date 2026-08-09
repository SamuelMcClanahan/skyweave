"""Track contract (D0, frozen).

- States: tentative, confirmed, coast, reacquired, deleted.
- ``track_id`` is unique within one Jetson ``session_uuid`` and never reused.
- Every update records the observation IDs it consumed, which makes each
  track auditable end to end (D9 traceability gate).

Reserved wire field numbers (Track): 1 track_id, 2 session_uuid, 3 state,
4 covariance, 5 status, 6 created_ts_ns, 7 last_update_ts_ns,
8 update_count, 9 miss_count.
"""

from __future__ import annotations

from pydantic import Field

from skyweave2.contracts.base import ContractModel
from skyweave2.contracts.enums import ClockDomain, TrackState


class TrackUpdateRecord(ContractModel):
    """Audit record for one filter update."""

    track_id: int
    ts_ns: int
    clock_domain: ClockDomain
    obs_ids: list[str]
    accepted: bool
    nis: float | None = None


class Track(ContractModel):
    track_id: int = Field(ge=0)
    session_uuid: str
    state: tuple[float, float, float, float, float, float]  # x y z vx vy vz
    covariance: tuple[float, ...] = Field(description="6x6 row-major, 36 values")
    status: TrackState
    created_ts_ns: int
    last_update_ts_ns: int
    update_count: int = Field(ge=0)
    miss_count: int = Field(ge=0)

    def model_post_init(self, __context: object) -> None:
        if len(self.covariance) != 36:
            raise ValueError("track covariance must be 6x6 (36 values)")
