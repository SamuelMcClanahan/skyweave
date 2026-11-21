"""Synthetic camera: truth -> detection packets ([S0]; contract [C2], [C3], [P1]).

The camera renders what a detector would report. Each truth object is
projected through the real camera model (``camera.CameraModel`` and
``camera.r_ned_body``). The inputs are the vehicle's truth position and
attitude and the true camera mount: the [C3] upright mount with an injected
boresight error, which guidance does not know about. The box is the pinhole
image of a fronto-parallel object of the target's size at the target's
camera depth ``Z``: ``w = f W / Z``, ``h = f H / Z``. This is exactly the
model [G2] inverts, so in a clean world the size range is exact, and the
only range error is the noise this module injects.

Per object and frame, in this order:

1. Not present, or behind the camera (``Z <= 0``): no box.
2. Seeded detector noise: the center moves by ``N(0, center_sigma_px)`` per
   axis, and the size is multiplied by ``exp(N(0, size_sigma_frac))`` per
   axis. Size noise is log-normal, so a box can never reach zero size.
3. Dropout: no box inside a scripted window (``[start, end)`` on ``t_cap``)
   or with probability ``dropout_p``. The frame still produces a packet,
   with fewer boxes ([P1]: one packet per processed frame), so the tracker
   sees misses and coasts.
4. Clipping to the 1920 x 1200 grid. Under [C2], ``(0, 0)`` is the center of
   the top-left pixel, so the image spans ``[-0.5, 1919.5] x [-0.5,
   1199.5]``. A box with no overlap is outside the frame and gives no box.

Randomness ([C1], CLAUDE.md determinism): frame ``k``'s draws come from
``numpy.random.default_rng([seed, k])``. Every object slot always draws the
same numbers, whether or not it is visible. A frame's noise therefore
depends only on the seed, the frame index, and the scene size, not on which
frames were rendered before it. Closed-loop SITL renders a different number
of frames run to run ([S0]), and frame ``k`` still gets the same noise.

Timing: frame ``k`` is captured at ``t0 + floor(k * 1000 / fps + 0.5)``
board ms. Its truth (pose and targets) is taken at that instant: the sim's
``t_cap`` is exact, unlike percepd v0's unverified one (E1-F1). The packet
is delivered ``latency_ms`` later, rounded up to the next integer ms. A
packet cannot be used before it exists.

Bad frames ([S11], V-D16; probe scenarios only, all off by default):

- Clutter: per frame, a Poisson number of false boxes (mean
  ``clutter_per_frame``) at uniform image positions, width log-uniform in
  ``clutter_w_px``, aspect ratio log-uniform in [0.5, 2], confidence uniform in
  [0.3, 0.9]. They come after the true boxes, so true box indices never move.
  A persistent false track is a scene object instead (``targets.WanderingBird``).
- Frame drops: a dropped frame produces no packet at all (percepd skipped it;
  ``frame_seq`` jumps). Random drops with ``frame_drop_p`` per frame, and
  bursts: start times a Poisson process at ``burst_per_s`` from ``t0``,
  durations uniform in ``burst_ms``. A detector miss (``dropout_p``, scripted
  windows) is different: the frame still produces a packet.
- Latency jitter: frame ``k`` is delivered ``latency_ms + j_k`` after capture,
  rounded up, with ``j_k = min(latency_jitter_ms * E_k, latency_jitter_max_ms)``
  and ``E_k`` a unit exponential draw. Delivery never goes back: a frame is
  never delivered before the one captured ahead of it (percepd is one pipeline).

Each bad-frame feature draws from its own generator,
``SeedSequence([seed, k], spawn_key=(0xCA, n))`` per frame and
``SeedSequence([seed], spawn_key=(0xCA, 3))`` for the burst schedule, so the
frame noise above is the same draw for draw with or without them, and with
every feature off a frame is identical to the E1 camera's.

All numbers are Provisional (E1) model inputs, never tuned on gate seeds.
The 27.3 ms latency and 60 fps are brief §2's percepd v0 pipelined figures.
They are Measured there for percepd v0 only and used here as model inputs.
:data:`PROVISIONAL_NOISE` is a starting world chosen on physical grounds
before any run. It is not tuned.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from skyweave2.drone.camera import CameraModel, r_ned_body, rot_x, rot_y, rot_z
from skyweave2.drone.harness.targets import Trajectory, Vec3
from skyweave2.drone.packets import Box, DetectionPacket
from skyweave2.drone.types import Attitude

PERCEPD_V0_LATENCY_MS = 27.3  # brief §2, pipelined keep-latest @60 FPS; model input here
PERCEPD_V0_FPS = 60.0
_DRAWS_PER_OBJECT = 4  # du, dv, size w, size h (standard normal); plus one uniform for dropout
_STREAM_CLUTTER = (0xCA, 1)
_STREAM_FRAME = (0xCA, 2)  # frame drop and latency jitter
_STREAM_BURST = (0xCA, 3)
CLUTTER_ASPECT = (0.5, 2.0)
CLUTTER_CONF = (0.3, 0.9)


def _is_finite_number(x: Any) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)


@dataclass(frozen=True, kw_only=True)
class Pose:
    """Vehicle truth at one instant: NED position relative to home, and the
    ArduPilot attitude ([C3]: ``R_ned_body = Rz(yaw) Ry(pitch) Rx(roll)``)."""

    pos_ned: Vec3
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0

    def __post_init__(self) -> None:
        p = np.asarray(self.pos_ned, dtype=float)
        if p.shape != (3,) or not np.all(np.isfinite(p)):
            raise ValueError(f"pose pos_ned must be 3 finite numbers, got {self.pos_ned!r}")
        for name in ("roll", "pitch", "yaw"):
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"pose {name} must be a number")
            if not math.isfinite(val):
                raise ValueError(f"pose {name} must be finite")

    def attitude(self, t_ms: int) -> Attitude:
        return Attitude(
            t_ms=t_ms, time_boot_ms=t_ms, roll=self.roll, pitch=self.pitch, yaw=self.yaw
        )


@dataclass(frozen=True, kw_only=True)
class CameraSimConfig:
    """The synthetic camera's world parameters (all Provisional, E1).

    ``boresight_error_rad`` is ``(roll, pitch, yaw)`` of the true mount
    relative to the [C3] nominal, applied in the body frame:
    ``R_body_cam_true = Rz(yaw) Ry(pitch) Rx(roll) R_body_cam``. A positive yaw
    error turns the boresight right, so a target dead ahead appears left of
    center. A positive pitch error tilts it up (FRD), so the target appears
    below center.
    """

    boresight_error_rad: tuple[float, float, float] = (0.0, 0.0, 0.0)
    center_sigma_px: float = 0.0
    size_sigma_frac: float = 0.0
    dropout_p: float = 0.0
    dropout_windows_ms: tuple[tuple[int, int], ...] = ()
    latency_ms: float = PERCEPD_V0_LATENCY_MS
    fps: float = PERCEPD_V0_FPS
    conf: float = 0.9
    # Bad frames ([S11], V-D16): all off by default (module docstring).
    clutter_per_frame: float = 0.0
    clutter_w_px: tuple[float, float] = (8.0, 80.0)
    frame_drop_p: float = 0.0
    burst_per_s: float = 0.0
    burst_ms: tuple[int, int] = (100, 600)
    latency_jitter_ms: float = 0.0
    latency_jitter_max_ms: float = 100.0

    def __post_init__(self) -> None:
        err = self.boresight_error_rad
        if len(err) != 3 or not all(_is_finite_number(x) for x in err):
            raise ValueError("boresight_error_rad must be 3 finite numbers (roll, pitch, yaw)")
        for name in ("center_sigma_px", "size_sigma_frac", "latency_ms"):
            val = getattr(self, name)
            if not (_is_finite_number(val) and val >= 0.0):
                raise ValueError(f"{name} must be a finite number >= 0, got {val!r}")
        if not (_is_finite_number(self.fps) and self.fps > 0.0):
            raise ValueError(f"fps must be a finite number > 0, got {self.fps!r}")
        for name in ("dropout_p", "conf"):
            val = getattr(self, name)
            if not (_is_finite_number(val) and 0.0 <= val <= 1.0):
                raise ValueError(f"{name} must be in [0, 1], got {val!r}")
        for window in self.dropout_windows_ms:
            if (
                len(window) != 2
                or any(isinstance(x, bool) or not isinstance(x, int) for x in window)
                or not window[0] < window[1]
            ):
                raise ValueError(
                    f"dropout window must be (start_ms, end_ms) ints, start < end: {window!r}"
                )
        for name in ("clutter_per_frame", "burst_per_s", "latency_jitter_ms"):
            val = getattr(self, name)
            if not (_is_finite_number(val) and 0.0 <= val <= 1000.0):
                raise ValueError(f"{name} must be in [0, 1000], got {val!r}")
        if not (_is_finite_number(self.frame_drop_p) and 0.0 <= self.frame_drop_p <= 1.0):
            raise ValueError(f"frame_drop_p must be in [0, 1], got {self.frame_drop_p!r}")
        lo, hi = self.clutter_w_px
        if not (_is_finite_number(lo) and _is_finite_number(hi) and 0.0 < lo <= hi):
            raise ValueError(f"clutter_w_px must be (min, max) with 0 < min <= max: {(lo, hi)!r}")
        b0, b1 = self.burst_ms
        if any(isinstance(x, bool) or not isinstance(x, int) for x in (b0, b1)) or not (
            0 < b0 <= b1
        ):
            raise ValueError(f"burst_ms must be (min, max) ints, 0 < min <= max: {(b0, b1)!r}")
        cap = self.latency_jitter_max_ms
        if not (_is_finite_number(cap) and cap >= 0.0):
            raise ValueError(f"latency_jitter_max_ms must be a finite number >= 0, got {cap!r}")

    @property
    def bad_frames(self) -> bool:
        """Any [S11] bad-frame feature on."""
        return (
            self.clutter_per_frame > 0.0
            or self.frame_drop_p > 0.0
            or self.burst_per_s > 0.0
            or self.latency_jitter_ms > 0.0
        )

    def to_obj(self) -> dict[str, Any]:
        """Plain JSON object, so a scorecard or run log can name its world. The
        bad-frame fields appear only when one is on, so an E1 world's object is
        unchanged."""
        out = self._base_obj()
        if self.bad_frames:
            out.update(
                {
                    "clutter_per_frame": float(self.clutter_per_frame),
                    "clutter_w_px": [float(x) for x in self.clutter_w_px],
                    "frame_drop_p": float(self.frame_drop_p),
                    "burst_per_s": float(self.burst_per_s),
                    "burst_ms": [int(x) for x in self.burst_ms],
                    "latency_jitter_ms": float(self.latency_jitter_ms),
                    "latency_jitter_max_ms": float(self.latency_jitter_max_ms),
                }
            )
        return out

    def _base_obj(self) -> dict[str, Any]:
        return {
            "boresight_error_rad": [float(x) for x in self.boresight_error_rad],
            "center_sigma_px": float(self.center_sigma_px),
            "size_sigma_frac": float(self.size_sigma_frac),
            "dropout_p": float(self.dropout_p),
            "dropout_windows_ms": [[int(a), int(b)] for a, b in self.dropout_windows_ms],
            "latency_ms": float(self.latency_ms),
            "fps": float(self.fps),
            "conf": float(self.conf),
        }


PROVISIONAL_NOISE = CameraSimConfig(
    boresight_error_rad=(0.0, math.radians(0.5), math.radians(0.5)),
    center_sigma_px=1.0,
    size_sigma_frac=0.03,
    dropout_p=0.02,
)
"""A starting world (Provisional, E1, chosen before any run): 1 px center jitter,
3 % size jitter, 2 % per-frame misses, and a 0.5 deg pitch and yaw mount error
(hobby-build mount tolerance). Scenarios may script more; this is not tuned."""


@dataclass(frozen=True, kw_only=True)
class ObjectTruth:
    """One object's noise-free image truth in one frame, through the TRUE camera.

    ``center_px`` is the projected center (``None`` when the object is not
    present or is behind the camera). It is kept even when it lies outside
    the grid, because [S8] attribution tests it against track boxes, and a
    coasting box can extend past the frame. ``in_fov`` means in front of the
    camera with the center inside the grid.
    """

    name: str
    center_px: tuple[float, float] | None
    depth_m: float | None
    in_fov: bool


@dataclass(frozen=True, kw_only=True)
class FrameTruth:
    """Image truth for one detection frame, keyed by its ``t_cap``."""

    t_cap: int
    frame_seq: int
    objects: tuple[ObjectTruth, ...]

    def get(self, name: str) -> ObjectTruth:
        for obj in self.objects:
            if obj.name == name:
                return obj
        raise KeyError(f"no truth object {name!r} in frame t_cap={self.t_cap}")


@dataclass(frozen=True, kw_only=True)
class Frame:
    """One rendered frame: the packet, when it is delivered, and its truth.

    ``dropped`` is a whole-frame drop ([S11]): the packet (with no boxes) is
    never delivered, so the tracker and the recording never see the frame."""

    packet: DetectionPacket
    t_deliver_ms: int
    truth: FrameTruth
    dropped: bool = False


def mount_with_error(nominal: CameraModel, error_rad: tuple[float, float, float]) -> CameraModel:
    """The true camera: ``nominal`` with its mount rotated by the boresight error."""
    roll, pitch, yaw = (float(x) for x in error_rad)
    r_true = rot_z(yaw) @ rot_y(pitch) @ rot_x(roll) @ nominal.rotation()
    return CameraModel(
        width=nominal.width,
        height=nominal.height,
        f_px=nominal.f_px,
        cx=nominal.cx,
        cy=nominal.cy,
        r_body_cam=tuple((float(row[0]), float(row[1]), float(row[2])) for row in r_true.tolist()),
    )


class SyntheticCamera:
    """Seeded detection source. Pure: every time is an argument ([C1]).

    ``nominal`` is the camera model guidance believes in. The sim renders
    through :attr:`true_cam`, which is ``nominal`` plus the boresight error.
    """

    def __init__(
        self,
        config: CameraSimConfig,
        *,
        seed: int,
        nominal: CameraModel | None = None,
        t0_ms: int = 0,
    ) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError(f"seed must be an int >= 0, got {seed!r}")
        if isinstance(t0_ms, bool) or not isinstance(t0_ms, int) or t0_ms < 0:
            raise ValueError(f"t0_ms must be an int >= 0, got {t0_ms!r}")
        self.config = config
        self.seed = seed
        self.t0_ms = t0_ms
        self.nominal = nominal if nominal is not None else CameraModel()
        self.true_cam = mount_with_error(self.nominal, config.boresight_error_rad)
        self._u_max = self.true_cam.width - 0.5
        self._v_max = self.true_cam.height - 0.5
        self._deliver: list[int] = []  # delivery time per frame index, monotone ([S11])
        self._bursts: list[tuple[int, int]] = []  # [start, end) on t_cap, generated lazily
        self._burst_rng = np.random.default_rng(
            np.random.SeedSequence([seed], spawn_key=_STREAM_BURST)
        )
        self._burst_horizon = float(t0_ms)  # the next burst starts after this time

    # -- timing ---------------------------------------------------------------

    def capture_time(self, k: int) -> int:
        """Board-ms capture time of frame ``k`` (``k >= 0``)."""
        if isinstance(k, bool) or not isinstance(k, int) or k < 0:
            raise ValueError(f"frame index must be an int >= 0, got {k!r}")
        return self.t0_ms + math.floor(k * 1000.0 / self.config.fps + 0.5)

    def deliver_time(self, t_cap: int) -> int:
        """``t_cap`` plus the base capture-to-delivery latency, rounded up to whole
        ms (no jitter)."""
        return t_cap + math.ceil(self.config.latency_ms)

    def _frame_rng(self, k: int, stream: tuple[int, int]) -> np.random.Generator:
        return np.random.default_rng(np.random.SeedSequence([self.seed, k], spawn_key=stream))

    def _frame_draws(self, k: int) -> tuple[float, float]:
        """Frame ``k``'s drop uniform and unit-exponential jitter draw ([S11])."""
        rng = self._frame_rng(k, _STREAM_FRAME)
        return float(rng.random()), float(rng.standard_exponential())

    def jitter_ms(self, k: int) -> float:
        """Frame ``k``'s extra latency ([S11]); 0 when jitter is off."""
        cfg = self.config
        if cfg.latency_jitter_ms <= 0.0:
            return 0.0
        return min(cfg.latency_jitter_ms * self._frame_draws(k)[1], cfg.latency_jitter_max_ms)

    def deliver_time_of(self, k: int) -> int:
        """Delivery time of frame ``k``: capture plus latency plus jitter, rounded
        up, and never before the previous frame's delivery. With jitter off it is
        :meth:`deliver_time` of the capture time."""
        if isinstance(k, bool) or not isinstance(k, int) or k < 0:
            raise ValueError(f"frame index must be an int >= 0, got {k!r}")
        while len(self._deliver) <= k:
            j = len(self._deliver)
            t = self.capture_time(j) + math.ceil(self.config.latency_ms + self.jitter_ms(j))
            if self._deliver:
                t = max(t, self._deliver[-1])
            self._deliver.append(t)
        return self._deliver[k]

    def in_dropout_window(self, t_cap: int) -> bool:
        return any(a <= t_cap < b for a, b in self.config.dropout_windows_ms)

    def bursts_until(self, t_ms: int) -> list[tuple[int, int]]:
        """The burst-drop windows ``[start, end)`` that start at or before ``t_ms``
        ([S11]); a pure function of the seed and ``t0``."""
        cfg = self.config
        if cfg.burst_per_s <= 0.0:
            return []
        while self._burst_horizon <= t_ms:
            gap_ms = 1000.0 * float(self._burst_rng.standard_exponential()) / cfg.burst_per_s
            start = math.ceil(self._burst_horizon + gap_ms)
            dur = int(self._burst_rng.integers(cfg.burst_ms[0], cfg.burst_ms[1] + 1))
            self._bursts.append((start, start + dur))
            self._burst_horizon = float(start + dur)
        return [b for b in self._bursts if b[0] <= t_ms]

    def frame_dropped(self, k: int) -> bool:
        """Whole frame ``k`` dropped: a random drop or inside a burst ([S11])."""
        cfg = self.config
        if cfg.frame_drop_p > 0.0 and self._frame_draws(k)[0] < cfg.frame_drop_p:
            return True
        if cfg.burst_per_s > 0.0:
            t_cap = self.capture_time(k)
            return any(a <= t_cap < b for a, b in self.bursts_until(t_cap))
        return False

    def _clutter(self, k: int) -> list[Box]:
        """Frame ``k``'s false boxes ([S11]); none when clutter is off."""
        cfg = self.config
        if cfg.clutter_per_frame <= 0.0:
            return []
        rng = self._frame_rng(k, _STREAM_CLUTTER)
        n = int(rng.poisson(cfg.clutter_per_frame))
        draws = rng.random((n, 5))
        lw0, lw1 = (math.log(x) for x in cfg.clutter_w_px)
        la0, la1 = (math.log(x) for x in CLUTTER_ASPECT)
        out: list[Box] = []
        for du, dv, dw, da, dc in draws.tolist():
            w = math.exp(lw0 + (lw1 - lw0) * dw)
            h = w * math.exp(la0 + (la1 - la0) * da)
            u = -0.5 + du * self.true_cam.width
            v = -0.5 + dv * self.true_cam.height
            box = self._clip(u, v, w, h)
            if box is not None:
                conf = CLUTTER_CONF[0] + (CLUTTER_CONF[1] - CLUTTER_CONF[0]) * dc
                out.append(replace(box, conf=conf))
        return out

    # -- geometry -------------------------------------------------------------

    def project(
        self, pose: Pose, p_ned: Sequence[float] | np.ndarray, t_ms: int
    ) -> tuple[tuple[float, float], float] | None:
        """Noise-free image of a NED point through the true camera.

        Returns ``((u, v), Z)``, or ``None`` for a point behind the camera.
        """
        rel = np.asarray(p_ned, dtype=float) - np.asarray(pose.pos_ned, dtype=float)
        p_body = r_ned_body(pose.attitude(t_ms)).T @ rel
        p_cam = self.true_cam.rotation().T @ p_body
        z = float(p_cam[2])
        if not z > 0.0:
            return None
        u, v = self.true_cam.project_cam(p_cam)
        if not (math.isfinite(u) and math.isfinite(v)):
            return None
        return (u, v), z

    def _in_grid(self, u: float, v: float) -> bool:
        return -0.5 <= u <= self._u_max and -0.5 <= v <= self._v_max

    def _clip(self, u: float, v: float, w: float, h: float) -> Box | None:
        x0 = max(u - w / 2.0, -0.5)
        x1 = min(u + w / 2.0, self._u_max)
        y0 = max(v - h / 2.0, -0.5)
        y1 = min(v + h / 2.0, self._v_max)
        if not (x1 - x0 > 0.0 and y1 - y0 > 0.0):
            return None
        return Box(x=x0, y=y0, w=x1 - x0, h=y1 - y0, conf=float(self.config.conf))

    # -- rendering ------------------------------------------------------------

    def capture(self, k: int, pose: Pose, scene: Sequence[Trajectory]) -> Frame:
        """Render frame ``k``. ``pose`` is the vehicle truth at ``capture_time(k)``."""
        names = [obj.name for obj in scene]
        if len(set(names)) != len(names):
            raise ValueError(f"truth object names must be unique, got {names}")
        cfg = self.config
        t_cap = self.capture_time(k)
        rng = np.random.default_rng([self.seed, k])
        normals = rng.standard_normal((len(scene), _DRAWS_PER_OBJECT))
        uniforms = rng.random(len(scene))
        dropped_window = self.in_dropout_window(t_cap)
        boxes: list[Box] = []
        truths: list[ObjectTruth] = []
        f = self.true_cam.f_px
        for j, obj in enumerate(scene):
            image = self.project(pose, obj.position(t_cap), t_cap) if obj.present(t_cap) else None
            if image is None:
                truths.append(
                    ObjectTruth(name=obj.name, center_px=None, depth_m=None, in_fov=False)
                )
                continue
            (u, v), z = image
            truths.append(
                ObjectTruth(name=obj.name, center_px=(u, v), depth_m=z, in_fov=self._in_grid(u, v))
            )
            if dropped_window or uniforms[j] < cfg.dropout_p:
                continue
            n = normals[j]
            u_d = u + cfg.center_sigma_px * float(n[0])
            v_d = v + cfg.center_sigma_px * float(n[1])
            w_d = f * obj.width_m / z * math.exp(cfg.size_sigma_frac * float(n[2]))
            h_d = f * obj.height_m / z * math.exp(cfg.size_sigma_frac * float(n[3]))
            if not all(math.isfinite(x) for x in (u_d, v_d, w_d, h_d)):
                continue
            box = self._clip(u_d, v_d, w_d, h_d)
            if box is not None:
                boxes.append(box)
        boxes.extend(self._clutter(k))
        dropped = self.frame_dropped(k)
        packet = DetectionPacket(t_cap=t_cap, frame_seq=k, boxes=() if dropped else tuple(boxes))
        return Frame(
            packet=packet,
            t_deliver_ms=self.deliver_time_of(k),
            truth=FrameTruth(t_cap=t_cap, frame_seq=k, objects=tuple(truths)),
            dropped=dropped,
        )
