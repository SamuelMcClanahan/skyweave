"""Camera model, rotations, and the attitude-source interface for guidance
(contract [C2]-[C4], [G1]).

Guidance needs three geometric facts and nothing else: where a pixel points in
the camera frame (the lens model, [C2]), how the camera sits on the body
(``R_body_cam``, a configured mount, [C3], V-D1), and how the body sat in NED
at the frame's capture time (FC attitude, ``R_ned_body = Rz(yaw) Ry(pitch)
Rx(roll)``, [C3]). This module holds the first two, the rotation for the
third, and ``AttitudeSource``, the interface guidance samples attitude
through. The de-rotation itself and everything that consumes it live in
``guidance.py``.

Mount and lens are a named config choice (V-D1, E2-D2). :data:`MOUNTS` names
``c3_forward`` (the E1 forward camera) and ``cone_up`` (the cone airframe's
nose camera, looking up along the thrust axis). :data:`LENSES` names
``e1_f1000`` (the E1 placeholder) and ``hfov100`` (the 100 deg lens, V-F1).
A named mount or lens fixes its numbers; any other name is a measured or
custom one that carries its own.

Distortion (V-D11) is Brown-Conrady ``(k1, k2, p1, p2, k3)`` on normalized
image coordinates, the convention of ``contracts/camera.py`` (detection D0),
so one calibration serves both lines. Raw pixels are what the detector
reports; the pinhole image is what [G1] and [G2] use. Zero coefficients (the
default) make every distortion function the identity, value for value.
Outside the model's domain (past the radius where the radial term folds back,
:attr:`CameraModel.fold_radius`) the functions return NaN, which guidance
treats as unusable geometry ([G1a]) and the sim as no image.

The one [G1] sampler (linear interpolation of roll, pitch, and unwrapped yaw
between the samples that bracket ``t_cap``, the nearest sample held outside
the stored span, never extrapolation, ``None`` past ``attitude_bound_ms``) is
``VehicleState.attitude_at`` in ``vehicle_state.py``: fc_link feeds it live
and the recording feeds it in replay ([R3]).

Numbers here are Provisional (contract §9): the lenses are uncalibrated
(E1-F3), the distortion is zero until calibrated, and the mounts are nominal,
not measured (E1-F9).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol

import numpy as np

from skyweave2.drone.packets import FLIGHT_GRID_HEIGHT, FLIGHT_GRID_WIDTH
from skyweave2.drone.types import Attitude

Matrix3 = tuple[tuple[float, float, float], ...]
Dist5 = tuple[float, float, float, float, float]

# [C3] E1 forward mount: body x = camera Z, body y = camera X, body z = camera Y
# (upright, no uptilt).
R_BODY_CAM_C3_FORWARD: Matrix3 = (
    (0.0, 0.0, 1.0),
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
)
R_BODY_CAM_UPRIGHT = R_BODY_CAM_C3_FORWARD  # the E1 name

# E2-D2 cone nose mount: optical axis along body -z (up at hover), image u along
# body x, image v along body -y.
R_BODY_CAM_CONE_UP: Matrix3 = (
    (1.0, 0.0, 0.0),
    (0.0, -1.0, 0.0),
    (0.0, 0.0, -1.0),
)

MOUNTS: Mapping[str, Matrix3] = {
    "c3_forward": R_BODY_CAM_C3_FORWARD,
    "cone_up": R_BODY_CAM_CONE_UP,
}
"""Named mounts (V-D1, E2-D2). A named mount fixes ``R_body_cam``."""

LENSES: Mapping[str, tuple[float, float, float]] = {
    "e1_f1000": (1000.0, 959.5, 599.5),
    "hfov100": (806.0, 959.5, 599.5),
}
"""Named lenses as ``(f_px, cx, cy)`` (Provisional, uncalibrated, E1-F3).
``hfov100`` is the 100 deg lens: ``960 / tan(50 deg)`` = 805.5, rounded to 806
(V-F1, E2-D2). A named lens fixes ``f_px``, ``cx``, ``cy``; its distortion is
separate config (V-D11)."""

DEFAULT_MOUNT = "c3_forward"
DEFAULT_LENS = "e1_f1000"
NO_DISTORTION: Dist5 = (0.0, 0.0, 0.0, 0.0, 0.0)

_ROTATION_TOL = 1e-9
_NEWTON_MAX = 50
_NEWTON_TOL = 1e-15  # residual or step, normalized coordinates, relative to 1 + |x_d|
_ACCEPT_TOL = 1e-12
_BOX_TOL_PX = 1e-9
_NAN2 = (math.nan, math.nan)


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
    """Camera on the flight grid ([C2]): lens, distortion, and body mount ([C3]).

    Provisional (contract §9, E1-F3): ``f``, ``cx``, ``cy`` are placeholders
    until the lens is calibrated. ``cx``/``cy`` default to the grid center
    under the [C2] convention ((0, 0) is the center of the top-left pixel, so
    the center of a 1920-wide grid is 959.5). ``mount`` and ``lens`` name the
    config choice (V-D1, E2-D2); ``dist`` is Brown-Conrady ``(k1, k2, p1, p2,
    k3)`` (V-D11), zero by default. The default is the E1 camera:
    ``c3_forward``, ``e1_f1000``, no distortion. Use :meth:`named` to build a
    named one.
    """

    width: int = FLIGHT_GRID_WIDTH
    height: int = FLIGHT_GRID_HEIGHT
    f_px: float = 1000.0
    cx: float = 959.5
    cy: float = 599.5
    r_body_cam: Matrix3 = R_BODY_CAM_C3_FORWARD
    dist: Dist5 = NO_DISTORTION
    mount: str = DEFAULT_MOUNT
    lens: str = DEFAULT_LENS

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
        if len(self.dist) != 5 or not all(
            not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)
            for x in self.dist
        ):
            raise ValueError(f"dist must be 5 finite numbers (k1, k2, p1, p2, k3): {self.dist!r}")
        for key in ("mount", "lens"):
            if not (isinstance(getattr(self, key), str) and getattr(self, key)):
                raise ValueError(f"camera {key} must be a non-empty name")
        if self.mount in MOUNTS and self.r_body_cam != MOUNTS[self.mount]:
            raise ValueError(f"mount {self.mount!r} is named; its r_body_cam is fixed (V-D1)")
        if self.lens in LENSES and (self.f_px, self.cx, self.cy) != LENSES[self.lens]:
            raise ValueError(f"lens {self.lens!r} is named; its f_px, cx, cy are fixed")

    @classmethod
    def named(
        cls, mount: str = DEFAULT_MOUNT, lens: str = DEFAULT_LENS, dist: Dist5 = NO_DISTORTION
    ) -> CameraModel:
        """The camera for a named mount and lens (V-D1, E2-D2), with ``dist`` (V-D11)."""
        if mount not in MOUNTS:
            raise ValueError(f"unknown mount {mount!r}; named: {sorted(MOUNTS)}")
        if lens not in LENSES:
            raise ValueError(f"unknown lens {lens!r}; named: {sorted(LENSES)}")
        f_px, cx, cy = LENSES[lens]
        return cls(
            f_px=f_px,
            cx=cx,
            cy=cy,
            r_body_cam=MOUNTS[mount],
            dist=tuple(float(x) for x in dist),  # type: ignore[arg-type]
            mount=mount,
            lens=lens,
        )

    def rotation(self) -> np.ndarray:
        """``R_body_cam`` as a fresh 3x3 array."""
        return np.array(self.r_body_cam, dtype=float)

    # -- distortion (V-D11) ---------------------------------------------------

    @property
    def distorted(self) -> bool:
        """Any distortion coefficient nonzero. When False every distortion
        function below returns its input unchanged."""
        return any(x != 0.0 for x in self.dist)

    @cached_property
    def fold_radius(self) -> float:
        """The normalized pinhole radius where the radial term stops growing.

        ``r_d = r (1 + k1 r^2 + k2 r^4 + k3 r^6)`` grows while
        ``1 + 3 k1 s + 5 k2 s^2 + 7 k3 s^3 > 0`` (``s = r^2``); this is the
        square root of that polynomial's smallest positive root, or infinity.
        Past it the model folds back on itself (two pinhole points share one
        raw pixel), so the model's domain ends there. The tangential terms are
        left out of this bound; they are small for a real lens.
        """
        k1, k2, _, _, k3 = self.dist
        roots = np.roots([7.0 * k3, 5.0 * k2, 3.0 * k1, 1.0])
        real = [float(r.real) for r in roots if abs(r.imag) < 1e-12 and r.real > 0.0]
        return math.sqrt(min(real)) if real else math.inf

    def distort_normalized(self, x: float, y: float) -> tuple[float, float]:
        """Brown-Conrady on normalized pinhole coordinates, as ``contracts/camera.py``.
        NaN past :attr:`fold_radius`."""
        if not self.distorted:
            return x, y
        k1, k2, p1, p2, k3 = self.dist
        r2 = x * x + y * y
        if not r2 < self.fold_radius**2:
            return _NAN2
        radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        x_d = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        y_d = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        return x_d, y_d

    def undistort_normalized(self, x_d: float, y_d: float) -> tuple[float, float]:
        """Inverse of :meth:`distort_normalized` by Newton's method from ``(x_d, y_d)``.

        NaN when no inverse lies inside the model's domain (a raw point past
        the image of :attr:`fold_radius`), or when the iteration does not
        converge.
        """
        if not self.distorted:
            return x_d, y_d
        if not (math.isfinite(x_d) and math.isfinite(y_d)):
            return _NAN2
        k1, k2, p1, p2, k3 = self.dist
        scale = 1.0 + math.hypot(x_d, y_d)
        x, y = x_d, y_d
        for _ in range(_NEWTON_MAX):
            r2 = x * x + y * y
            radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
            fx = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x) - x_d
            fy = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y - y_d
            if math.hypot(fx, fy) <= _NEWTON_TOL * scale:
                break
            j11, j12, j22 = self._jacobian(x, y)
            det = j11 * j22 - j12 * j12
            if not (math.isfinite(det) and det > 0.0):
                return _NAN2
            dx = (j22 * fx - j12 * fy) / det
            dy = (j11 * fy - j12 * fx) / det
            x -= dx
            y -= dy
            if math.hypot(dx, dy) <= _NEWTON_TOL * scale:
                break
        if not (x * x + y * y < self.fold_radius**2):
            return _NAN2
        x_chk, y_chk = self.distort_normalized(x, y)
        if not math.hypot(x_chk - x_d, y_chk - y_d) <= _ACCEPT_TOL * scale:
            return _NAN2
        return x, y

    def distort_px(self, u: float, v: float) -> tuple[float, float]:
        """Pinhole pixel -> raw pixel (what the detector sees)."""
        if not self.distorted:
            return u, v
        x_d, y_d = self.distort_normalized((u - self.cx) / self.f_px, (v - self.cy) / self.f_px)
        return self.cx + self.f_px * x_d, self.cy + self.f_px * y_d

    def undistort_px(self, u: float, v: float) -> tuple[float, float]:
        """Raw pixel -> pinhole pixel."""
        if not self.distorted:
            return u, v
        x, y = self.undistort_normalized((u - self.cx) / self.f_px, (v - self.cy) / self.f_px)
        return self.cx + self.f_px * x, self.cy + self.f_px * y

    def _jacobian(self, x: float, y: float) -> tuple[float, float, float]:
        """``(j11, j12, j22)`` of :meth:`distort_normalized` at ``(x, y)``; the
        Jacobian is symmetric (``j21 = j12``). It is also the pixel Jacobian of
        :meth:`distort_px`, since both axes scale by the same ``f``."""
        k1, k2, p1, p2, k3 = self.dist
        r2 = x * x + y * y
        radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        dradial = k1 + 2.0 * k2 * r2 + 3.0 * k3 * r2 * r2  # d radial / d r2
        j11 = radial + 2.0 * x * x * dradial + 2.0 * p1 * y + 6.0 * p2 * x
        j22 = radial + 2.0 * y * y * dradial + 6.0 * p1 * y + 2.0 * p2 * x
        j12 = 2.0 * x * y * dradial + 2.0 * p1 * x + 2.0 * p2 * y
        return j11, j12, j22

    def _span(self, u: float, v: float, s: float, axis: int) -> tuple[float, float]:
        """Raw extent along ``axis`` (0: u, 1: v) of the pinhole segment of
        length ``s`` centered on pinhole ``(u, v)`` along that axis, and its
        slope ``d extent / d s`` (the mean of the two end points' Jacobian
        diagonal term)."""
        h = s / 2.0
        if axis == 0:
            lo, hi = (u - h, v), (u + h, v)
            k = 0
        else:
            lo, hi = (u, v - h), (u, v + h)
            k = 2
        extent = self.distort_px(*hi)[axis] - self.distort_px(*lo)[axis]
        j_lo = self._jacobian((lo[0] - self.cx) / self.f_px, (lo[1] - self.cy) / self.f_px)[k]
        j_hi = self._jacobian((hi[0] - self.cx) / self.f_px, (hi[1] - self.cy) / self.f_px)[k]
        return extent, (j_lo + j_hi) / 2.0

    def _unspan(self, u: float, v: float, extent: float, axis: int) -> float:
        """The pinhole length whose :meth:`_span` is ``extent`` (Newton; NaN if
        it does not converge inside the model's domain)."""
        j = self._jacobian((u - self.cx) / self.f_px, (v - self.cy) / self.f_px)
        s = extent / (j[0] if axis == 0 else j[2])
        for _ in range(_NEWTON_MAX):
            got, slope = self._span(u, v, s, axis)
            err = got - extent
            if abs(err) <= _BOX_TOL_PX:
                return s
            if not (math.isfinite(err) and slope > 0.0):
                return math.nan
            s -= err / slope
        return math.nan

    def distort_box(
        self, u: float, v: float, w: float, h: float
    ) -> tuple[float, float, float, float]:
        """A pinhole box (center, size) -> the raw box a detector would report.

        The center is distorted. The width is the raw u-distance between the
        distorted midpoints of the box's left and right edges; the height the
        raw v-distance between those of its top and bottom edges. This is the
        detector's box for a centered target of any size. NaN if the center
        or an edge midpoint is outside the model's domain.
        """
        if not self.distorted:
            return u, v, w, h
        u_d, v_d = self.distort_px(u, v)
        return u_d, v_d, self._span(u, v, w, 0)[0], self._span(u, v, h, 1)[0]

    def undistort_box(
        self, u: float, v: float, w: float, h: float
    ) -> tuple[float, float, float, float]:
        """A raw box (center, size) -> its pinhole box, the inverse of
        :meth:`distort_box` (to ``1e-9`` px): the center is undistorted, then
        each size is solved along its axis through that center. NaN outside
        the model's domain."""
        if not self.distorted:
            return u, v, w, h
        u_p, v_p = self.undistort_px(u, v)
        return u_p, v_p, self._unspan(u_p, v_p, w, 0), self._unspan(u_p, v_p, h, 1)

    # -- pinhole ----------------------------------------------------------------

    def ray(self, u: float, v: float) -> np.ndarray:
        """[G1] camera-frame ray ``((u - cx)/f, (v - cy)/f, 1)`` of a pinhole pixel;
        not normalized."""
        return np.array([(u - self.cx) / self.f_px, (v - self.cy) / self.f_px, 1.0])

    def project_cam(self, p_cam: Sequence[float] | np.ndarray) -> tuple[float, float]:
        """Pinhole projection of a camera-frame point to pixels ([C2]); no
        distortion (see :meth:`distort_px`).

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
        """Recording ``meta.config`` form ([R2], [R4]): the numbers replay needs
        and the mount and lens names (V-D1)."""
        return {
            "width": self.width,
            "height": self.height,
            "f_px": self.f_px,
            "cx": self.cx,
            "cy": self.cy,
            "r_body_cam": [list(row) for row in self.r_body_cam],
            "dist": [float(x) for x in self.dist],
            "mount": self.mount,
            "lens": self.lens,
        }

    @classmethod
    def from_obj(cls, obj: Mapping[str, Any]) -> CameraModel:
        """Inverse of :meth:`to_obj`; every key is required, unknown keys refused.

        An E1 recording's camera object (no ``dist``, ``mount``, ``lens``) reads
        as the E1 camera: no distortion, ``c3_forward``, ``e1_f1000``. Its
        numbers must then match those names, as for any named camera.
        """
        if not isinstance(obj, Mapping):
            raise ValueError("camera config must be an object")
        if set(obj) == _E1_KEYS:
            obj = {**obj, "dist": list(NO_DISTORTION), "mount": DEFAULT_MOUNT, "lens": DEFAULT_LENS}
        if set(obj) != _KEYS:
            raise ValueError(f"camera config keys must be exactly {sorted(_KEYS)}")
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
        dist = obj["dist"]
        if not isinstance(dist, (list, tuple)) or len(dist) != 5:
            raise ValueError("camera dist must be 5 numbers (k1, k2, p1, p2, k3)")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in dist):
            raise ValueError("camera dist entries must be numbers")
        return cls(
            width=obj["width"],
            height=obj["height"],
            f_px=float(obj["f_px"]),
            cx=float(obj["cx"]),
            cy=float(obj["cy"]),
            r_body_cam=tuple(matrix),
            dist=tuple(float(x) for x in dist),  # type: ignore[arg-type]
            mount=obj["mount"],
            lens=obj["lens"],
        )


_E1_KEYS = frozenset({"width", "height", "f_px", "cx", "cy", "r_body_cam"})
_KEYS = _E1_KEYS | {"dist", "mount", "lens"}

CONE_UP_HFOV100 = CameraModel.named("cone_up", "hfov100")
"""E2-D2: the cone nose camera looking up, with the 100 deg lens, no distortion
until calibrated (Provisional). Fast tests only in E2a; it flies in E2b."""


class AttitudeSource(Protocol):
    """Attitude at a board-ms time, per [G1]; ``None`` means attitude-degraded.

    Queried at a packet's ``t_cap`` for de-rotation and at a step's stamp for
    ``yaw_now`` ([G3]) and the [F3] term of [G1a]. Past the stored span the
    newest sample is held, so a query at "now" returns the newest sample, or
    ``None`` once it is older than the bound (the [F3] staleness test).
    """

    def attitude_at(self, t_ms: int) -> Attitude | None: ...
