"""fc_link v1: MAVLink2 between the companion and the FC (DRONE_CONTRACTS_D0.md §6).

Phase E1 is SITL only, and this module is built so that nothing in it can
command a real flight controller ([F1], E1-D7):

* (a) the endpoint must be a literal ``127.0.0.1`` TCP or UDP endpoint; a
  serial device, a hostname (even ``localhost``), or any other host is refused
  at construction, before a socket exists;
* (b) a loopback port can still bridge a real FC (mavlink-router, MAVProxy),
  so each connection is receive-only until the FC system id has sent
  ``SIMSTATE`` (164) or ``SIM_STATE`` (108), which ArduPilot emits only in
  SITL builds. Until then every write path writes zero bytes and counts the
  attempt in :attr:`FcLink.blocked_writes`. The proof resets on every
  (re)connect. ArduCopter 4.7.0 SITL streams on SERIAL1 receive-only when the
  SITL defaults file sets ``MAV2_*`` rates (``sitl.py`` documents the
  experiment), so the contract's one allowed pre-proof HEARTBEAT is never
  needed and fc_link sends no HEARTBEAT at all.

There is no parameter-write path (no ``PARAM_SET``) and no RC override path in
this module. The setpoint gate ([F5]) is ``locked`` unless the configuration
says ``setpoints_enabled = True``; while locked, a velocity send or an
arm-and-takeoff writes nothing and is counted in :attr:`FcLink.blocked_count`.
Exit mode requests ([F9]) are written only while the link is up and the
newest HEARTBEAT says ``GUIDED``; otherwise they are counted in
:attr:`FcLink.exit_blocked_count`.

Time comes only from the injected clock ([F11], [C1]): one read per received
chunk (every frame of that chunk shares the receive time) and one per write.
Every frame received or sent goes to the recorder as raw bytes ([F10]).
"""

from __future__ import annotations

import math
import re
import socket
from typing import Any

from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.packets import FcLinkHealthPacket, GateState
from skyweave2.drone.recording import Recorder
from skyweave2.drone.types import Clock, FcRequest, FcRequestKind, VelocityCommand
from skyweave2.drone.vehicle_state import GUIDED, LinkConfig, VehicleState

TYPE_MASK_VELOCITY_YAW_RATE = 1479
"""[F6] 0x05C7: velocity and yaw rate used; position, acceleration, yaw ignored."""

SITL_PROOF_MSG_IDS = frozenset(
    {mavlink2.MAVLINK_MSG_ID_SIMSTATE, mavlink2.MAVLINK_MSG_ID_SIM_STATE}
)
"""[F1] (b): messages ArduPilot emits only in SITL builds."""

LOOPBACK_HOST = "127.0.0.1"
ENDPOINT_SCHEMES = ("tcp", "udp", "udpin", "udpout")

ARDUCOPTER_MODE_NUMBERS = {"RTL": 6, "LAND": 9}

TONES: dict[str, str] = {
    "ACQUIRING": "MFT200L16O3ceg",
    "ENGAGED": "MFT200L8O4cc",
    "TOUCH": "MFT240L16O5cdefg",
    "LOST": "MFT200L8O3gec",
    "RETURN": "MFT150L4O3c",
    "ABORT": "MFT255L32O5cecece",
}
"""[F8] contract §9 tone table (E1): one distinct short MML tune per state name."""

_ENDPOINT_RE = re.compile(r"(?P<scheme>[a-z]+):(?P<host>[^:/]+):(?P<port>[0-9]{1,5})")


class EndpointRefused(ValueError):
    """[F1] (a): the endpoint is not a loopback MAVLink endpoint."""


class LinkClosed(ConnectionError):
    """The FC link's socket closed under us."""


def _parse_endpoint(endpoint: Any) -> tuple[str, str, int]:
    if not isinstance(endpoint, str):
        raise EndpointRefused(f"endpoint must be a string, got {type(endpoint).__name__}")
    m = _ENDPOINT_RE.fullmatch(endpoint)
    if m is None:
        raise EndpointRefused(
            f"{endpoint!r} is not '<tcp|udp|udpin|udpout>:127.0.0.1:<port>'; serial devices "
            "and other forms are refused in phase E1 ([F1])"
        )
    scheme, host, port = m["scheme"], m["host"], int(m["port"])
    if scheme not in ENDPOINT_SCHEMES:
        raise EndpointRefused(f"scheme {scheme!r} refused; only {ENDPOINT_SCHEMES} ([F1])")
    if host != LOOPBACK_HOST:
        raise EndpointRefused(f"host {host!r} refused; only the literal {LOOPBACK_HOST} ([F1])")
    if not 1 <= port <= 65535:
        raise EndpointRefused(f"port {port} out of range")
    return scheme, host, port


def check_endpoint(endpoint: str) -> None:
    """[F1] (a): raise :class:`EndpointRefused` unless ``endpoint`` is loopback."""
    _parse_endpoint(endpoint)


def clamp_velocity(cmd: VelocityCommand, cfg: LinkConfig) -> VelocityCommand:
    """[F6] hard limits, independent of guidance (pure).

    The horizontal vector is scaled down to ``v_xy_hard`` with its direction
    kept; ``vd`` and the yaw rate are clamped. A non-finite component is a
    defect upstream and is refused, never sent.
    """
    vals = (cmd.vn, cmd.ve, cmd.vd, cmd.yaw_rate)
    if not all(math.isfinite(v) for v in vals):
        raise ValueError(f"non-finite setpoint refused: {cmd}")
    h = math.hypot(cmd.vn, cmd.ve)
    scale = cfg.v_xy_hard / h if h > cfg.v_xy_hard else 1.0
    return VelocityCommand(
        vn=cmd.vn * scale,
        ve=cmd.ve * scale,
        vd=min(max(cmd.vd, -cfg.v_z_hard), cfg.v_z_hard),
        yaw_rate=min(max(cmd.yaw_rate, -cfg.yaw_rate_hard), cfg.yaw_rate_hard),
    )


class MavTransport:
    """Raw byte transport to one loopback MAVLink endpoint (non-blocking reads).

    ``udpin``/``udp`` binds and replies to the first peer that sends; ``udpout``
    sends to the endpoint. Both only ever touch ``127.0.0.1``.
    """

    def __init__(self, endpoint: str, timeout_s: float) -> None:
        scheme, host, port = _parse_endpoint(endpoint)
        self.scheme = scheme
        self._peer: tuple[str, int] | None = None
        if scheme == "tcp":
            self.sock = socket.create_connection((host, port), timeout=timeout_s)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        elif scheme == "udpout":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.connect((host, port))
            self._peer = (host, port)
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((host, port))
        self.sock.setblocking(False)

    def fileno(self) -> int:
        return self.sock.fileno()

    def recv(self) -> bytes:
        """Everything readable now; ``b""`` when nothing is. Raises :class:`LinkClosed`."""
        chunks: list[bytes] = []
        while True:
            try:
                if self.scheme in ("udp", "udpin"):
                    data, addr = self.sock.recvfrom(65536)
                    if self._peer is None:
                        self._peer = addr
                elif self.scheme == "udpout":
                    data = self.sock.recv(65536)
                else:
                    data = self.sock.recv(65536)
                    if not data:
                        raise LinkClosed("FC link closed by peer")
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionRefusedError:
                if self.scheme == "tcp":
                    raise LinkClosed("FC link refused") from None
                break  # UDP: an ICMP port-unreachable from a peer that is not up yet
            except ConnectionResetError as exc:
                raise LinkClosed(str(exc)) from exc
            chunks.append(data)
        return b"".join(chunks)

    def send(self, data: bytes) -> bool:
        """Write one frame. ``False`` when there is nowhere to send it yet (udpin)."""
        if self.scheme in ("udp", "udpin"):
            if self._peer is None:
                return False
            self.sock.sendto(data, self._peer)
            return True
        self.sock.sendall(data)
        return True

    def close(self) -> None:
        self.sock.close()


class FcLink:
    """The companion's one MAVLink link to the FC ([F1]-[F11])."""

    def __init__(
        self,
        endpoint: str,
        clock: Clock,
        config: LinkConfig,
        recorder: Recorder | None = None,
    ) -> None:
        check_endpoint(endpoint)  # [F1] (a): refused before any socket exists
        self.endpoint = endpoint
        self.config = config
        self.state = VehicleState(config)
        self._clock = clock
        self._recorder = recorder
        self._mav = mavlink2.MAVLink(
            None, srcSystem=config.companion_sysid, srcComponent=config.companion_compid
        )
        self._parser: Any = None
        self._transport: MavTransport | None = None
        self.sitl_proven = False  # [F1] (b), per connection
        self.streams_requested = False  # [F2], per connection
        self.blocked_count = 0  # [F5] gate-blocked velocity sends and arm-and-takeoffs
        self.blocked_writes = 0  # [F1] (b) writes refused before the SITL proof
        self.exit_blocked_count = 0  # [F9] RTL / LAND requests refused
        self.unknown_tones = 0  # [F8] tone names with no tune (nothing sent)
        self.rx_frames = 0
        self.rx_bad = 0
        self.tx_frames = 0
        self.last_setpoint_t: int | None = None
        self._last_health_t: int | None = None

    # -- connection -----------------------------------------------------------

    @property
    def gate_state(self) -> GateState:
        """[F5]: ``locked`` unless the configuration explicitly enables setpoints."""
        return GateState.ENABLED if self.config.setpoints_enabled is True else GateState.LOCKED

    @property
    def connected(self) -> bool:
        return self._transport is not None

    def fileno(self) -> int | None:
        """The socket's descriptor, for a caller's ``select``; ``None`` when closed."""
        return None if self._transport is None else self._transport.fileno()

    def connect(self, timeout_s: float) -> None:
        """Open the endpoint. Receive-only until the SITL proof ([F1] (b)); the
        [F2] stream requests go out from :meth:`poll` right after the proof."""
        self.close()
        self._transport = MavTransport(self.endpoint, timeout_s)
        self._parser = mavlink2.MAVLink(None)
        self._parser.robust_parsing = True

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._parser = None
        self.sitl_proven = False  # the proof resets on reconnect
        self.streams_requested = False

    # -- receive --------------------------------------------------------------

    def poll(self) -> int:
        """Read every pending frame, record it, ingest it; returns frames read."""
        if self._transport is None:
            return 0
        try:
            data = self._transport.recv()
        except LinkClosed:
            self.close()
            return 0
        if not data:
            return 0
        t = self._clock()
        cfg = self.config
        n = 0
        for msg in self._parser.parse_buffer(data) or []:
            if msg.get_type() == "BAD_DATA":
                self.rx_bad += 1
                continue
            if self._recorder is not None:
                self._recorder.mavlink(t, "rx", bytes(msg.get_msgbuf()))  # [F10]
            self.state.ingest(msg, t)
            n += 1
            if (
                msg.get_msgId() in SITL_PROOF_MSG_IDS
                and msg.get_srcSystem() == cfg.fc_sysid
                and msg.get_srcComponent() == cfg.fc_compid
            ):
                self.sitl_proven = True
        self.rx_frames += n
        if self.sitl_proven and not self.streams_requested:
            self._request_streams(t)
        return n

    def _request_streams(self, t: int) -> None:
        """[F2]: ATTITUDE at ``attitude_hz`` plus the [M2a] telemetry."""
        cfg = self.config
        rates = (
            (mavlink2.MAVLINK_MSG_ID_ATTITUDE, cfg.attitude_hz),
            (mavlink2.MAVLINK_MSG_ID_HEARTBEAT, cfg.heartbeat_hz),
            (mavlink2.MAVLINK_MSG_ID_EXTENDED_SYS_STATE, cfg.telemetry_hz),
            (mavlink2.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, cfg.telemetry_hz),
            (mavlink2.MAVLINK_MSG_ID_LOCAL_POSITION_NED, cfg.telemetry_hz),
            (mavlink2.MAVLINK_MSG_ID_SYS_STATUS, cfg.telemetry_hz),
            (mavlink2.MAVLINK_MSG_ID_RC_CHANNELS, cfg.telemetry_hz),
        )
        for msg_id, hz in rates:
            self._command(t, mavlink2.MAV_CMD_SET_MESSAGE_INTERVAL, msg_id, 1e6 / hz)
        self.streams_requested = True

    # -- send -----------------------------------------------------------------

    def _write(self, msg: Any, t: int) -> bool:
        """The one write path. Zero bytes before the SITL proof ([F1] (b))."""
        if not self.sitl_proven or self._transport is None:
            self.blocked_writes += 1
            return False
        raw = bytes(msg.pack(self._mav))
        try:
            sent = self._transport.send(raw)
        except OSError:
            self.close()
            self.blocked_writes += 1
            return False
        if not sent:
            self.blocked_writes += 1
            return False
        self.tx_frames += 1
        if self._recorder is not None:
            self._recorder.mavlink(t, "tx", raw)  # [F10]
        return True

    def _command(self, t: int, command: int, *params: float) -> bool:
        p = list(params) + [0.0] * (7 - len(params))
        cfg = self.config
        msg = self._mav.command_long_encode(cfg.fc_sysid, cfg.fc_compid, command, 0, *p)
        return self._write(msg, t)

    def send_velocity(self, cmd: VelocityCommand) -> bool:
        """[F5] gated, [F6] limited, ``type_mask`` 1479, frame ``LOCAL_NED``."""
        t = self._clock()
        if self.gate_state is not GateState.ENABLED:
            self.blocked_count += 1
            return False
        c = clamp_velocity(cmd, self.config)
        cfg = self.config
        msg = self._mav.set_position_target_local_ned_encode(
            t & 0xFFFFFFFF,
            cfg.fc_sysid,
            cfg.fc_compid,
            mavlink2.MAV_FRAME_LOCAL_NED,
            TYPE_MASK_VELOCITY_YAW_RATE,
            0.0,
            0.0,
            0.0,
            c.vn,
            c.ve,
            c.vd,
            0.0,
            0.0,
            0.0,
            0.0,
            c.yaw_rate,
        )
        if not self._write(msg, t):
            return False
        self.last_setpoint_t = t
        return True

    def request(self, req: FcRequest) -> bool:
        """One mission request ([M12]); ``True`` when its bytes were written."""
        t = self._clock()
        if req.kind is FcRequestKind.ARM_AND_TAKEOFF:
            if self.gate_state is not GateState.ENABLED:  # [F5]
                self.blocked_count += 1
                return False
            alt = req.value
            if isinstance(alt, bool) or not isinstance(alt, (int, float)) or not alt > 0:
                raise ValueError(f"takeoff altitude must be a number > 0, got {alt!r}")
            if not math.isfinite(alt):
                raise ValueError(f"takeoff altitude must be finite, got {alt!r}")
            # Arm then takeoff, back to back: ArduCopter handles one port's frames
            # in order, so a refused arm makes the takeoff refused too (E1-F11).
            return self._command(t, mavlink2.MAV_CMD_COMPONENT_ARM_DISARM, 1.0) and self._command(
                t, mavlink2.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, float(alt)
            )
        if req.kind in (FcRequestKind.MODE_RTL, FcRequestKind.MODE_LAND):
            # [F9]: only while the link is up and the newest HEARTBEAT is GUIDED;
            # a mode seen before a link loss does not count (VehicleState forgets it).
            if self.state.mode(t) != GUIDED:
                self.exit_blocked_count += 1
                return False
            name = "RTL" if req.kind is FcRequestKind.MODE_RTL else "LAND"
            return self._command(
                t,
                mavlink2.MAV_CMD_DO_SET_MODE,
                float(mavlink2.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
                float(ARDUCOPTER_MODE_NUMBERS[name]),
            )
        if req.kind is FcRequestKind.TONE:
            tune = TONES.get(req.value) if isinstance(req.value, str) else None
            if tune is None:  # [F8]: an unknown name sends nothing
                self.unknown_tones += 1
                return False
            cfg = self.config
            msg = self._mav.play_tune_encode(cfg.fc_sysid, cfg.fc_compid, tune.encode("ascii"))
            return self._write(msg, t)
        raise ValueError(f"unknown FC request kind {req.kind!r}")

    # -- health ---------------------------------------------------------------

    def health_packet(self) -> FcLinkHealthPacket:
        """[P4] / [F4] at the clock's now (not recorded; see :meth:`maybe_health`)."""
        t = self._clock()
        return FcLinkHealthPacket(
            t=t,
            attitude_age_ms=self.state.attitude_age_ms(t),
            fc_link_up=self.state.link_up(t),
            rc_seen=self.state.rc_seen(t),
            gate_state=self.gate_state,
            last_setpoint_t=self.last_setpoint_t,
        )

    def maybe_health(self) -> FcLinkHealthPacket | None:
        """[F4]: a health packet once per ``health_period_ms`` (recorded when due)."""
        pkt = self.health_packet()
        if self._last_health_t is not None and pkt.t - self._last_health_t < (
            self.config.health_period_ms
        ):
            return None
        self._last_health_t = pkt.t
        if self._recorder is not None:
            self._recorder.packet(pkt.t, pkt)
        return pkt
