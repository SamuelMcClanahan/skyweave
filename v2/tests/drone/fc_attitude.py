"""Test fixture: the FC's ATTITUDE stream into the production attitude source.

The world-angle tracker (V-D10) and guidance ([G1]) sample attitude at a
frame's ``t_cap`` through ``VehicleState.attitude_at``. Tests that drive them
need that source filled the way fc_link fills it: real pymavlink ``ATTITUDE``
frames (float32 angles), each ingested at its board-ms receive time ([F2]).
Nothing we own is mocked here (TESTING_DOCTRINE rule 2); only the FC's side
of the link is scripted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.types import Attitude
from skyweave2.drone.vehicle_state import LinkConfig, VehicleState, parse_frames

LINK = LinkConfig()  # attitude_bound_ms 100 ([F3], §9)
FC = mavlink2.MAVLink(None, srcSystem=LINK.fc_sysid, srcComponent=LINK.fc_compid)
ATTITUDE_PERIOD_MS = 20  # [F2]: 50 Hz
_SKIP_MS = 2_000  # a longer silence is skipped; the source keeps 1 s ([F2])

RollPitchYaw = tuple[float, float, float]


def level(_t_ms: int) -> RollPitchYaw:
    """A level vehicle facing north."""
    return (0.0, 0.0, 0.0)


def attitude_frame(t_ms: int, rpy: RollPitchYaw = (0.0, 0.0, 0.0)) -> bytes:
    """One ``ATTITUDE`` frame from the FC, as bytes on the link."""
    msg: Any = FC.attitude_encode(t_ms, rpy[0], rpy[1], rpy[2], 0.0, 0.0, 0.0)
    return bytes(msg.pack(FC))


class FcAttitude:
    """A real ``VehicleState`` fed 50 Hz ``ATTITUDE`` frames of a scripted
    attitude, on demand ([F2], [G1]).

    ``rpy(t_ms)`` is the vehicle's (roll, pitch, yaw) at board ms ``t_ms``;
    the frame sent at ``t`` carries ``rpy(t)`` and is received at ``t``.
    :meth:`attitude_at` first delivers every frame received up to one period
    after the asked time, as when a detection reaches the companion a frame
    period after its capture, so the asked time lies between two samples.
    ``silent`` lists ``[start, end)`` windows in which the FC sends nothing
    (a link stall).
    """

    def __init__(
        self,
        rpy: Callable[[int], RollPitchYaw] = level,
        *,
        link: LinkConfig = LINK,
        silent: tuple[tuple[int, int], ...] = (),
    ) -> None:
        self.vehicle = VehicleState(link)
        self._rpy = rpy
        self._silent = silent
        self._next: int | None = None

    def deliver(self, t_ms: int) -> VehicleState:
        """Ingest every frame received at or before ``t_ms``."""
        if self._next is None or t_ms - self._next > _SKIP_MS:
            first = max(0, t_ms - 1_000)
            self._next = first - first % ATTITUDE_PERIOD_MS
        while self._next <= t_ms:
            if not any(a <= self._next < b for a, b in self._silent):
                for msg in parse_frames(attitude_frame(self._next, self._rpy(self._next))):
                    self.vehicle.ingest(msg, self._next)
            self._next += ATTITUDE_PERIOD_MS
        return self.vehicle

    def attitude_at(self, t_ms: int) -> Attitude | None:
        return self.deliver(t_ms + ATTITUDE_PERIOD_MS).attitude_at(t_ms)

    def attitude_age_ms(self, t_ms: int) -> int | None:
        """E2a4-D1: the production answer, after the same delivery."""
        return self.deliver(t_ms + ATTITUDE_PERIOD_MS).attitude_age_ms(t_ms)
