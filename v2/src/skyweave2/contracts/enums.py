"""Frozen enumerations (D0).

Values are wire-stable strings. Do not rename a value; add a new one and
record the decision.
"""

from __future__ import annotations

from enum import Enum


class ClockDomain(str, Enum):
    """The clock a timestamp belongs to. Never mix domains without a mapping."""

    SYNTHETIC = "synthetic"  # truth/scene time from a dataset manifest
    NODE_MONO = "node_mono"  # edge node monotonic clock
    NODE_PTP = "node_ptp"  # edge node PTP/chrony-disciplined wall clock
    JETSON_RX = "jetson_rx"  # Jetson receive/dequeue time


class TrackState(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    COAST = "coast"
    REACQUIRED = "reacquired"
    DELETED = "deleted"


class ResultStatus(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    WEAK_GEOMETRY = "weak_geometry"
    LATE = "late"
    REJECTED = "rejected"


class SystematicSource(str, Enum):
    """Bias channels reported beside, never inside, the conditional covariance."""

    CALIBRATION = "calibration"
    CLOCK = "clock"
    MOUNT = "mount"
    TARGET_REFERENCE = "target_reference"
