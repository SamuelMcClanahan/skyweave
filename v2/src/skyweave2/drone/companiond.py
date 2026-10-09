"""The live companion process (DRONE_CONTRACTS_D0.md [C10]; brief work item 6).

One process on the board holds mission, guidance, fc_link, the UI server, and
the recorder ([C10]). It receives track packets (from the tracker process)
and command packets on their UDP ports ([C8]), talks MAVLink2 to the FC
through :class:`fc_link.FcLink`, serves the ground UI over HTTP
(``ground_ui``), publishes ``mission_state`` and ``fc_link_health`` packets
for external listeners, and records the whole flight ([R1], [R2]).

The loop: wait (``select``) for a socket to be readable or the next tick to
fall due; then, holding the core lock, read the FC link, feed each track
packet and each command to the core, and run the mission tick at ``tick_hz``
(contract §9). fc_link publishes its health packet at about 1 Hz ([F4]).
Every core output is forwarded the same way: mission state packets to their
port, FC requests and then the setpoint through fc_link ([M12], [G11]).
The UI's request threads take the same lock, so the core is never called
concurrently and input stamps never decrease ([M2]).

Time ([C1]): :func:`live_clock` is the one wall-clock read in the drone stack,
board-monotonic ms from ``time.monotonic_ns`` (``CLOCK_MONOTONIC``, the
domain of percepd's ``t_cap``). Everything else gets that clock injected; the
loop's ``select`` timeout is process control, not a stamp.

Safety: :class:`fc_link.FcLink` applies the [F1] interlock (a literal
``127.0.0.1`` endpoint, receive-only until the SITL proof), checked here before
anything is opened. The setpoint gate is locked unless ``--enable-setpoints``
is given ([F5]). The process has no parameter-write path, no RC override, and
records no frames.

The UI token comes from the environment variable named by :data:`UI_ENV_VAR`
(not argv, which other users can read in the process table). It never enters
a configuration object, a recording, or a log ([P5c]).

Run (SITL only in E1)::

    SKYWEAVE_UI_TOKEN=... python -m skyweave2.drone.companiond \\
        --fc tcp:127.0.0.1:5762 --record flight.jsonl
"""

from __future__ import annotations

import argparse
import logging
import os
import select
import signal
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from skyweave2.drone.core import CompanionCore, CoreConfig, CoreOutput
from skyweave2.drone.fc_link import FcLink, check_endpoint
from skyweave2.drone.ground_ui import GroundUiServer, GroundUiService, RecordTap, ViewBuilder
from skyweave2.drone.packets import PacketKind, TrackPacket
from skyweave2.drone.recording import Recorder
from skyweave2.drone.types import Clock
from skyweave2.drone.udp import DEFAULT_PORTS, LOOPBACK, Datagram, UdpReceiver, UdpSender
from skyweave2.drone.vehicle_state import LinkConfig

UI_ENV_VAR = "SKYWEAVE_UI_TOKEN"
DEFAULT_UI_PORT = 8080  # E1 choice, not a contract value

log = logging.getLogger(__name__)

_Sock = TypeVar("_Sock", UdpReceiver, UdpSender)


def live_clock() -> Clock:
    """[C1] board ms on ``CLOCK_MONOTONIC``: the only wall-clock read in the stack."""
    return lambda: time.monotonic_ns() // 1_000_000


@dataclass(frozen=True, kw_only=True)
class CompanionConfig:
    """Process wiring. ``core`` is what the recording's ``meta.config`` holds;
    the rest is local plumbing (ports per contract §9, Provisional). Never a
    token."""

    fc_endpoint: str
    recording: str | Path
    core: CoreConfig = field(default_factory=CoreConfig)
    tick_hz: float = 20.0  # contract §9 tick_hz (E1, Provisional)
    bind_host: str = LOOPBACK  # track and command receivers ([C10]: one board)
    track_port: int = DEFAULT_PORTS[PacketKind.TRACK]
    command_port: int = DEFAULT_PORTS[PacketKind.COMMAND]
    publish_host: str = LOOPBACK
    mission_state_port: int = DEFAULT_PORTS[PacketKind.MISSION_STATE]
    fc_link_health_port: int = DEFAULT_PORTS[PacketKind.FC_LINK_HEALTH]
    ui_host: str = LOOPBACK  # 0.0.0.0 serves the field laptop over the AP (E1-F14)
    ui_port: int = DEFAULT_UI_PORT
    fc_connect_timeout_s: float = 1.0
    fc_retry_ms: int = 1000

    def __post_init__(self) -> None:
        if not self.tick_hz > 0:
            raise ValueError("tick_hz must be > 0")

    @property
    def tick_period_ms(self) -> int:
        return max(1, round(1000.0 / self.tick_hz))


class Companion:
    """The companion process's parts and its loop ([C10])."""

    def __init__(
        self, config: CompanionConfig, ui_token: str, *, clock: Clock | None = None
    ) -> None:
        check_endpoint(config.fc_endpoint)  # [F1] (a): refused before anything opens
        self.config = config
        self.clock: Clock = clock if clock is not None else live_clock()
        self.lock = threading.Lock()
        self.builder = ViewBuilder()
        self._stack = ExitStack()
        try:
            # A flight recording is never overwritten ("x").
            fh = self._stack.enter_context(
                open(config.recording, "x", encoding="ascii", newline="\n")
            )
            self.recorder = Recorder(RecordTap(self.builder, fh))
            self.fc_link = FcLink(
                config.fc_endpoint, self.clock, config.core.link, recorder=self.recorder
            )
            self._stack.callback(self.fc_link.close)
            # The core writes meta first; fc_link writes nothing before connect.
            self.core = CompanionCore(
                config.core,
                vehicle=self.fc_link.state,
                recorder=self.recorder,
                t_start_ms=self.clock(),
            )
            self.ui = GroundUiService(
                self.core, self.builder, self.clock, ui_token, forward=self._forward, lock=self.lock
            )
            self.tracks = self._open(
                UdpReceiver(PacketKind.TRACK, config.bind_host, config.track_port)
            )
            self.commands = self._open(
                UdpReceiver(PacketKind.COMMAND, config.bind_host, config.command_port)
            )
            self.mission_states = self._open(
                UdpSender(config.publish_host, config.mission_state_port)
            )
            self.health = self._open(UdpSender(config.publish_host, config.fc_link_health_port))
            self.server = GroundUiServer(self.ui, config.ui_host, config.ui_port)
            self._stack.callback(self.server.close)
        except BaseException:
            self._stack.close()
            raise
        self._next_tick: int | None = None
        self._next_connect: int | None = None
        self.ticks = 0
        self.input_errors = 0  # inputs whose processing raised; logged, loop kept alive

    def _open(self, sock: _Sock) -> _Sock:
        self._stack.callback(sock.close)
        return sock

    # -- lifecycle -------------------------------------------------------------

    @property
    def ui_url(self) -> str:
        return self.server.url

    def start(self) -> None:
        """Serve the UI and open the FC link (receive-only until the SITL proof)."""
        self.server.start()
        with self.lock:
            self._connect(self.clock())

    def run(self, stop: threading.Event) -> None:
        """Loop until ``stop`` is set."""
        while not stop.is_set():
            self.run_once(self.wait_s())

    def close(self) -> None:
        self._stack.close()

    def __enter__(self) -> Companion:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the loop ----------------------------------------------------------------

    def wait_s(self) -> float:
        """Seconds until the next tick is due (0 when due now)."""
        if self._next_tick is None:
            return 0.0
        return max(0.0, (self._next_tick - self.clock()) / 1000.0)

    def run_once(self, timeout_s: float = 0.0) -> None:
        """One iteration: wait up to ``timeout_s`` for input, then serve it all."""
        if timeout_s > 0:
            fds = [self.tracks.fileno(), self.commands.fileno()]
            fc = self.fc_link.fileno()
            if fc is not None:
                fds.append(fc)
            select.select(fds, [], [], timeout_s)
        with self.lock:
            self._step()

    def _step(self) -> None:
        now = self.clock()
        if not self.fc_link.connected and (self._next_connect is None or now >= self._next_connect):
            self._connect(now)
        self.fc_link.poll()  # records and ingests every FC frame ([F10])
        for r in self.tracks.poll():
            assert isinstance(r.packet, TrackPacket)
            self._guarded(
                "track", lambda p=r.packet: self._forward(self.core.on_track(p, self.clock()))
            )
        for dg in self.commands.poll_raw():
            self._guarded("command", lambda d=dg: self._command(d))
        t = self.clock()
        if self._next_tick is None or t >= self._next_tick:
            self._guarded("tick", lambda: self._forward(self.core.on_tick(t)))
            self.ticks += 1
            period = self.config.tick_period_ms
            nxt = (t if self._next_tick is None else self._next_tick) + period
            self._next_tick = nxt if nxt > t else t + period  # late: skip, never burst
        pkt = self.fc_link.maybe_health()  # [F4] about 1 Hz; fc_link records it
        if pkt is not None:
            self.health.send(pkt)

    def _command(self, dg: Datagram) -> None:
        ack = self.ui.receiver.receive(dg.data, self.clock())
        if ack is not None:
            self.commands.reply(ack, dg.addr)  # [P5]: to the command's sender

    def _guarded(self, what: str, fn: Callable[[], None]) -> None:
        """Process one input; an exception is logged and counted, never fatal.

        One bad datagram must not end the companion process in flight: that
        would leave the mission unprimed after a restart (E1-F6).
        """
        try:
            fn()
        except Exception:  # noqa: BLE001 - the live loop outlives any one input
            self.input_errors += 1
            log.exception("companion %s input failed; loop continues", what)

    def _connect(self, now: int) -> None:
        try:
            self.fc_link.connect(self.config.fc_connect_timeout_s)
        except OSError as exc:
            self._next_connect = now + self.config.fc_retry_ms
            log.warning("FC link %s not open (%s); retrying", self.config.fc_endpoint, exc)
            return
        log.info("FC link %s open (receive-only until the SITL proof)", self.config.fc_endpoint)

    def _forward(self, out: CoreOutput) -> None:
        """What every core output does: publish, then requests, then the setpoint."""
        for pkt in out.mission_states:
            self.mission_states.send(pkt)
        for req in out.requests:
            self.fc_link.request(req)
        if out.setpoint is not None:
            self.fc_link.send_velocity(out.setpoint)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m skyweave2.drone.companiond",
        description="SkyWeave companion process (phase E1: SITL only). "
        f"The UI token is read from ${UI_ENV_VAR}.",
    )
    parser.add_argument("--fc", required=True, help="FC endpoint, tcp:127.0.0.1:<port> ([F1])")
    parser.add_argument("--record", required=True, type=Path, help="new recording file (.jsonl)")
    parser.add_argument("--ui-host", default=LOOPBACK)
    parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT)
    parser.add_argument("--tick-hz", type=float, default=20.0)
    parser.add_argument(
        "--enable-setpoints",
        action="store_true",
        help="unlock the setpoint gate ([F5]); locked by default",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ui_token = os.environ.get(UI_ENV_VAR, "")
    if not ui_token:
        log.error("set %s to the shared UI token", UI_ENV_VAR)
        return 2
    config = CompanionConfig(
        fc_endpoint=args.fc,
        recording=args.record,
        core=CoreConfig(link=LinkConfig(setpoints_enabled=bool(args.enable_setpoints))),
        tick_hz=args.tick_hz,
        ui_host=args.ui_host,
        ui_port=args.ui_port,
    )
    try:
        companion = Companion(config, ui_token)
    except (OSError, ValueError) as exc:  # messages never carry the token
        log.error("companion not started: %s", exc)
        return 2
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        companion.start()
        log.info("ground UI at %s; gate %s", companion.ui_url, config.core.gate.value)
        companion.run(stop)
    except KeyboardInterrupt:
        pass
    finally:
        companion.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
