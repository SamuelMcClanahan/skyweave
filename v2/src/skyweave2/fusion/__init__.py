"""D5 central fusion: Observation2D streams in, auditable tracks out.

Pipeline: event-time alignment -> cross-camera association (direct and
voxel-proposed candidate groups) -> D1 ``localize()`` unchanged -> EKF track
lifecycle with suppression -> metrics/report.

Every stage is deterministic: exact arithmetic and sorted iteration, no
seeds needed at runtime; the only seeded component is fixture generation.
Replay of the same observation stream reproduces identical track decisions
byte-for-byte (test F5).
"""

from skyweave2.fusion.config import FusionConfig

FUSION_VERSION = "d5-fusion/1"

__all__ = ["FUSION_VERSION", "FusionConfig"]
