"""The SITL side of a world: the airframe SITL flies and the wind it flies in.

Contract §8 [S10] (wind, V-D15) and [S12] (airframe, V-F2). Both reach SITL
only through its launch: ``--model`` and the extra defaults file
(``sitl.SitlInstance``). Nothing here writes a parameter to a running FC,
and fc_link has no parameter-write path ([F1]).

Airframes:

- :data:`PLUS` is what the E1 gate flies: SITL model ``+`` with no frame
  file and no extra parameters. Gate scenes S1-S6 always use it.
- :data:`CONE_X` is the cone-tower X quad of V-F1 and V-F6, from the frame
  file ``frames/cone_x.json`` (numbers and their sources in
  ``frames/README.md``, all Provisional). The SITL ``quad`` template gives
  the motor numbers and spin directions; the file's motor positions make it
  an X. ArduCopter's mixer must match, so the defaults set ``FRAME_TYPE`` 1
  (X). The other defaults are what an operator sets on this vehicle (V-D7):
  the hover throttle found in SITL on 2026-11-20 and the 6S battery limits.

Wind (SITL 4.7.0 behavior, read in ``SIMState.cpp`` and ``SIM_Aircraft.cpp``
at tag Copter-4.7.0 on 2026-11-20):

- ``SIM_WIND_SPD`` (m/s) and ``SIM_WIND_DIR`` (deg true, the direction the
  wind comes FROM) give a steady wind. SITL starts it 5 s after boot and
  slews to it with time constant ``SIM_WIND_TC`` (default 5 s).
- ``SIM_WIND_T`` sets the height profile. The default (0) scales speed by
  ``sqrt(h / SIM_WIND_T_ALT)`` below ``SIM_WIND_T_ALT`` (60 m), so at a 10 m
  search altitude the vehicle would feel 41 % of the set speed. The harness
  sets 1 (no height scaling), so the vehicle and the synthetic targets see
  the same wind.
- ``SIM_WIND_TURB`` (m/s) adds turbulence when airborne: a first-order IIR
  (coefficient 0.98 per physics step, about 42 ms at 1200 Hz) on a normal
  draw scaled so the standard deviation is about ``SIM_WIND_TURB`` per
  component. Its horizontal direction is re-drawn every step (a float
  azimuth advanced by ``2 * rand()`` degrees), so it is fast buffeting, not
  slow gusts. It uses libc ``rand()`` with no seed in the SITL HAL, so it is
  not tied to the run seed. Slow gusts are not modelled (finding).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FRAMES_DIR = Path(__file__).resolve().parent / "frames"


@dataclass(frozen=True, kw_only=True)
class Airframe:
    """What SITL flies: ``--model``, an optional JSON frame file, and the
    airframe's own defaults-file entries."""

    name: str
    model: str
    frame_file: Path | None = None
    params: Mapping[str, float] = field(default_factory=dict)

    def to_obj(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "frame_file": None if self.frame_file is None else self.frame_file.name,
            "params": {k: float(v) for k, v in sorted(self.params.items())},
        }


PLUS = Airframe(name="plus", model="+")
"""The E1 gate airframe (SITL's default 3 kg plus-frame model)."""

CONE_X = Airframe(
    name="cone_x",
    model="quad",
    frame_file=FRAMES_DIR / "cone_x.json",
    params={
        "FRAME_TYPE": 1.0,  # X mixer, to match the frame file's motor positions
        "MOT_THST_HOVER": 0.21,  # learned in calm air in SITL, 2026-11-20 (frames/README.md)
        "MOT_BAT_VOLT_MAX": 25.2,  # 6S full (4.2 V/cell)
        "MOT_BAT_VOLT_MIN": 19.8,  # 6S at 3.3 V/cell
    },
)
"""The cone-tower X quad (V-F1, V-F6). Every number is Provisional."""

AIRFRAMES: Mapping[str, Airframe] = {a.name: a for a in (PLUS, CONE_X)}


@dataclass(frozen=True, kw_only=True)
class Wind:
    """Steady wind plus SITL turbulence ([S10], V-D15). Provisional inputs.

    ``from_deg`` is where the wind comes from, degrees true (SITL's
    ``SIM_WIND_DIR`` convention). The air moves toward ``from_deg + 180``.
    """

    speed_mps: float
    from_deg: float
    turbulence_mps: float = 0.0
    change_tc_s: float = 5.0

    def __post_init__(self) -> None:
        for name in ("speed_mps", "turbulence_mps"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v < 30.0:
                raise ValueError(f"wind {name} must be in [0, 30) m/s, got {v!r}")
        d = self.from_deg
        if isinstance(d, bool) or not isinstance(d, (int, float)) or not 0.0 <= d < 360.0:
            raise ValueError(f"wind from_deg must be in [0, 360), got {d!r}")
        tc = self.change_tc_s
        if isinstance(tc, bool) or not isinstance(tc, (int, float)) or not 0.1 <= tc <= 60.0:
            raise ValueError(f"wind change_tc_s must be in [0.1, 60] s, got {tc!r}")

    def downwind_ned(self) -> tuple[float, float, float]:
        """Unit vector the air moves along (NED, horizontal)."""
        a = math.radians(self.from_deg + 180.0)
        return (math.cos(a), math.sin(a), 0.0)

    def sitl_params(self) -> dict[str, float]:
        """The SITL defaults-file entries (names checked on 4.7.0)."""
        return {
            "SIM_WIND_SPD": float(self.speed_mps),
            "SIM_WIND_DIR": float(self.from_deg),
            "SIM_WIND_TURB": float(self.turbulence_mps),
            "SIM_WIND_TC": float(self.change_tc_s),
            "SIM_WIND_T": 1.0,  # no height scaling (module docstring)
        }

    def to_obj(self) -> dict[str, float]:
        return {
            "speed_mps": float(self.speed_mps),
            "from_deg": float(self.from_deg),
            "turbulence_mps": float(self.turbulence_mps),
            "change_tc_s": float(self.change_tc_s),
        }


def sitl_params(airframe: Airframe, wind: Wind | None) -> dict[str, float]:
    """Defaults-file entries for one world: the airframe's, then the wind's."""
    out = {k: float(v) for k, v in airframe.params.items()}
    if wind is not None:
        out.update(wind.sitl_params())
    return out
