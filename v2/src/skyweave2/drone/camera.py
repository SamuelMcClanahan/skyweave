"""Camera model, attitude history, and rotations for guidance (contract [C2]-[C4], [G1]).

Guidance needs three geometric facts and nothing else: where a pixel points in
the camera frame (the pinhole model, [C2]), how the camera sits on the body
(``R_body_cam``, upright with no uptilt, [C3]), and how the body sat in NED at
the frame's capture time (FC attitude, ``R_ned_body = Rz(yaw) Ry(pitch)
Rx(roll)``, [C3]). This module holds those three. The de-rotation itself and
everything that consumes it live in ``guidance.py``.

The attitude history is here because [G1] defines how a sample is chosen for a
capture time: linear interpolation of roll, pitch, and unwrapped yaw between
the two samples that bracket ``t_cap``; the nearest sample held outside the
stored span; never extrapolation; and "degraded" (``None``) when the sample
used lies more than ``attitude_bound_ms`` from the query. Samples are indexed
by their board-ms receive time ([F2]); ``time_boot_ms`` is carried, not mapped
(E1-F8). The class is pure: it never reads a clock ([C1]).

Numbers here are Provisional (contract §9): the AR0234 lens is uncalibrated
(E1-F3) and the mount roll and uptilt are assumed zero (E1-F9).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from skyweave2.drone.packets import FLIGHT_GRID_HEIGHT, FLIGHT_GRID_WIDTH
from skyweave2.drone.types import Attitude

# [C3]: body x = camera Z, body y = camera X, body z = camera Y (upright, no uptilt).
R_BODY_CAM_UPRIGHT: tuple[tuple[float, float, float], ...] = (
    (0.0, 0.0, 1.0),
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
)

_ROTATION_TOL = 1e-9


def wrap_pi(angle: float) -> float:
    """Wrap an angle in radians into [-pi, pi]."""
    return math.remainder(angle, 2.0 * math.pi)


def rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def r_ned_body(att: Attitude) -> np.ndarray:
    """[C3] ``R_ned_body = Rz(yaw) Ry(pitch) Rx(roll)`` (ArduPilot ``ATTITUDE``)."""
    return rot_z(att.yaw) @ rot_y(att.pitch) @ rot_x(att.roll)


@dataclass(frozen=True, kw_only=True)
class CameraModel:
    """Pinhole camera on the flight grid ([C2]) with its body mount ([C3]).

    Provisional (contract §9, E1-F3): ``f``, ``cx``, ``cy`` are placeholders
    until the AR0234 lens is calibrated. ``cx``/``cy`` default to the grid
    center under the [C2] convention ((0, 0) is the center of the top-left
    pixel, so the center of a 1920-wide grid is 959.5).
    """

    width: int = FLIGHT_GRID_WIDTH
    height: int = FLIGHT_GRID_HEIGHT
    f_px: float = 1000.0
    cx: float = 959.5
    cy: float = 599.5
    r_body_cam: tuple[tuple[float, float, float], ...] = R_BODY_CAM_UPRIGHT

    def __post_init__(self) -> None:
        if (self.width, self.height) != (FLIGHT_GRID_WIDTH, FLIGHT_GRID_HEIGHT):
            raise ValueError(
                f"[C2]: v1 represents only the {FLIGHT_GRID_WIDTH}x{FLIGHT_GRID_HEIGHT} "
                f"flight grid, got {self.width}x{self.height}"
            )
        if not (math.isfinite(self.f_px) and self.f_px > 0.0):
            raise ValueError(f"f_px must be finite and > 0, got {self.f_px!r}")
        if not (math.isfinite(self.cx) and math.isfinite(self.cy)):
            raise ValueError("cx and cy must be finite")
        m = np.asarray(self.r_body_cam, dtype=float)
        if m.shape != (3, 3) or not np.all(np.isfinite(m)):
            raise ValueError("r_body_cam must be a finite 3x3 matrix")
        if not (
            np.allclose(m @ m.T, np.eye(3), atol=_ROTATION_TOL)
            and abs(np.linalg.det(m) - 1.0) < _ROTATION_TOL
        ):
            raise ValueError("r_body_cam must be a proper rotation (orthonormal, det +1)")

    def rotation(self) -> np.ndarray:
        """``R_body_cam`` as a fresh 3x3 array."""
        return np.array(self.r_body_cam, dtype=float)

    def ray(self, u: float, v: float) -> np.ndarray:
        """[G1] camera-frame ray ``((u - cx)/f, (v - cy)/f, 1)``; not normalized."""
        return np.array([(u - self.cx) / self.f_px, (v - self.cy) / self.f_px, 1.0])

    def project_cam(self, p_cam: Sequence[float] | np.ndarray) -> tuple[float, float]:
        """Pinhole projection of a camera-frame point to pixels ([C2]).

        Raises ``ValueError`` for a point not in front of the camera.
        """
        x, y, z = (float(c) for c in p_cam)
        if not z > 0.0:
            raise ValueError(f"point is not in front of the camera (Z = {z})")
        return (self.cx + self.f_px * x / z, self.cy + self.f_px * y / z)

    def project_body(self, p_body: Sequence[float] | np.ndarray) -> tuple[float, float]:
        """Project a body-frame (FRD) point: ``p_cam = R_body_cam^T p_body``."""
        return self.project_cam(self.rotation().T @ np.asarray(p_body, dtype=float))

    def project_ned(
        self, p_rel_ned: Sequence[float] | np.ndarray, att: Attitude
    ) -> tuple[float, float]:
        """Project a point given relative to the vehicle in NED, at attitude ``att``.

        The inverse of guidance's de-rotation; the harness uses it to render
        synthetic detections.
        """
        p_body = r_ned_body(att).T @ np.asarray(p_rel_ned, dtype=float)
        return self.project_body(p_body)

    def to_obj(self) -> dict[str, Any]:
        """Recording ``meta.config`` form ([R2], [R4])."""
        return {
            "width": self.width,
            "height": self.height,
            "f_px": self.f_px,
            "cx": self.cx,
            "cy": self.cy,
            "r_body_cam": [list(row) for row in self.r_body_cam],
        }

    @classmethod
    def from_obj(cls, obj: Mapping[str, Any]) -> CameraModel:
        """Inverse of :meth:`to_obj`; every key is required, unknown keys refused."""
        if not isinstance(obj, Mapping):
            raise ValueError("camera config must be an object")
        expected = {"width", "height", "f_px", "cx", "cy", "r_body_cam"}
        if set(obj) != expected:
            raise ValueError(f"camera config keys must be exactly {sorted(expected)}")
        for key in ("width", "height"):
            if isinstance(obj[key], bool) or not isinstance(obj[key], int):
                raise ValueError(f"camera {key} must be an integer")
        for key in ("f_px", "cx", "cy"):
            if isinstance(obj[key], bool) or not isinstance(obj[key], (int, float)):
                raise ValueError(f"camera {key} must be a number")
        rows = obj["r_body_cam"]
        if not isinstance(rows, (list, tuple)) or len(rows) != 3:
            raise ValueError("camera r_body_cam must be 3 rows")
        matrix: list[tuple[float, float, float]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                raise ValueError("camera r_body_cam rows must have 3 numbers")
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in row):
                raise ValueError("camera r_body_cam entries must be numbers")
            matrix.append((float(row[0]), float(row[1]), float(row[2])))
        return cls(
            width=obj["width"],
            height=obj["height"],
            f_px=float(obj["f_px"]),
            cx=float(obj["cx"]),
            cy=float(obj["cy"]),
            r_body_cam=tuple(matrix),
        )


class AttitudeSource(Protocol):
    """Attitude at a board-ms time, per [G1]; ``None`` means attitude-degraded.

    Queried at a packet's ``t_cap`` for de-rotation and at a step's stamp for
    ``yaw_now`` ([G3]) and the [F3] term of [G1a]. Past the stored span the
    newest sample is held, so a query at "now" returns the newest sample, or
    ``None`` once it is older than the bound (the [F3] staleness test).
    """

    def attitude_at(self, t_ms: int) -> Attitude | None: ...


class AttitudeHistory:
    """[G1] / [F2] / [F3] attitude store and sampler, pure (no clock).

    ``add`` takes samples in non-decreasing receive-time order; a sample with
    the same receive time as the newest replaces it. At least ``keep_ms`` of
    samples are kept ([F2]: at least 1 s), including one sample at or before
    the cut so any query inside the kept window still interpolates.
    """

    def __init__(self, bound_ms: int = 100, keep_ms: int = 1000) -> None:
        if bound_ms < 0 or keep_ms < bound_ms:
            raise ValueError("need 0 <= bound_ms <= keep_ms")
        self.bound_ms = bound_ms
        self.keep_ms = keep_ms
        self._t: list[int] = []
        self._s: list[Attitude] = []

    def __len__(self) -> int:
        return len(self._s)

    def add(self, att: Attitude) -> None:
        if self._t and att.t_ms < self._t[-1]:
            raise ValueError(
                f"attitude samples must arrive in receive-time order ({att.t_ms} < {self._t[-1]})"
            )
        if self._t and att.t_ms == self._t[-1]:
            self._s[-1] = att
            return
        self._t.append(att.t_ms)
        self._s.append(att)
        cut = att.t_ms - self.keep_ms
        k = bisect.bisect_right(self._t, cut) - 1  # last sample at or before the cut
        if k > 0:
            del self._t[:k]
            del self._s[:k]

    def newest(self) -> Attitude | None:
        return self._s[-1] if self._s else None

    def age_ms(self, t_ms: int) -> int | None:
        """[F3] ``attitude_age_ms``: ``t_ms`` minus the newest receive time."""
        return None if not self._t else t_ms - self._t[-1]

    def degraded(self, t_ms: int) -> bool:
        """[F3]: no sample, or the newest is older than ``bound_ms``."""
        age = self.age_ms(t_ms)
        return age is None or age > self.bound_ms

    def attitude_at(self, t_ms: int) -> Attitude | None:
        """[G1] sample for ``t_ms``: interpolated, held at the span ends, or ``None``.

        Inside the span the result is a new :class:`Attitude` stamped ``t_ms``
        whose roll and pitch are linear and whose yaw follows the shorter way
        round between the bracketing samples. The "sample used" for the bound
        check is the nearer bracketing sample.
        """
        if not self._t:
            return None
        i = bisect.bisect_right(self._t, t_ms)
        if i == 0:
            held = self._s[0]
            return held if held.t_ms - t_ms <= self.bound_ms else None
        if i == len(self._t):
            held = self._s[-1]
            return held if t_ms - held.t_ms <= self.bound_ms else None
        a, b = self._s[i - 1], self._s[i]
        if min(t_ms - a.t_ms, b.t_ms - t_ms) > self.bound_ms:
            return None
        frac = (t_ms - a.t_ms) / (b.t_ms - a.t_ms)
        return Attitude(
            t_ms=t_ms,
            time_boot_ms=a.time_boot_ms + round(frac * (b.time_boot_ms - a.time_boot_ms)),
            roll=a.roll + frac * (b.roll - a.roll),
            pitch=a.pitch + frac * (b.pitch - a.pitch),
            yaw=wrap_pi(a.yaw + frac * wrap_pi(b.yaw - a.yaw)),
        )
