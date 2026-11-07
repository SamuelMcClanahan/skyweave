"""Small non-blocking UDP helpers for the companion process (DRONE_CONTRACTS_D0.md [C8], [C10]).

Each packet kind travels on its own port ([C8]; defaults in contract §9,
Provisional, config not contract) and each port has exactly one bound
receiver, so sockets bind without ``SO_REUSEPORT`` ([C10]).

Receivers decode with :func:`packets.decode` and nothing else: a datagram the
frozen wire does not allow is counted and logged and goes no further ([C7]).
The datagram's bytes are never logged, because a command datagram carries the
shared token ([P5c]). The command path needs the raw bytes for the [P5b]
``cmd_id`` salvage, so it reads with :meth:`UdpReceiver.poll_raw` and decodes
in the command receiver (``ground_ui.CommandReceiver``) instead.

No clock here: the caller stamps what it receives ([C1]).
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

from skyweave2.drone.packets import Packet, PacketError, PacketKind, decode, encode

LOOPBACK = "127.0.0.1"

DEFAULT_PORTS: dict[PacketKind, int] = {
    PacketKind.DETECTION: 14601,
    PacketKind.TRACK: 14602,
    PacketKind.MISSION_STATE: 14603,
    PacketKind.FC_LINK_HEALTH: 14604,
    PacketKind.COMMAND: 14605,
}
"""Contract §9 UDP ports (E1, Provisional)."""

_RECV_BYTES = 65536  # above the 65507 B payload limit ([C9]): an oversize datagram is seen whole

Address = tuple[str, int]

log = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class Datagram:
    """One datagram as received: its bytes and its sender."""

    data: bytes
    addr: Address


@dataclass(frozen=True, kw_only=True)
class Received:
    """One datagram that decoded as a packet of the receiver's kind."""

    packet: Packet
    addr: Address


class UdpReceiver:
    """One bound, non-blocking UDP socket for one packet kind ([C8], [C10])."""

    def __init__(self, kind: PacketKind, host: str = LOOPBACK, port: int = 0) -> None:
        self.kind = kind
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((host, port))
            self.sock.setblocking(False)
        except OSError:
            self.sock.close()
            raise
        self.received = 0  # datagrams read
        self.rejected = 0  # datagrams that failed decoding ([C7]); poll() only

    @property
    def address(self) -> Address:
        host, port = self.sock.getsockname()[:2]
        return (host, port)

    def fileno(self) -> int:
        return self.sock.fileno()

    def poll_raw(self, limit: int = 256) -> list[Datagram]:
        """Every datagram readable now, up to ``limit``, undecoded."""
        out: list[Datagram] = []
        while len(out) < limit:
            try:
                data, addr = self.sock.recvfrom(_RECV_BYTES)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionRefusedError:
                continue  # an ICMP error left by an earlier reply; the socket is fine
            out.append(Datagram(data=data, addr=(addr[0], addr[1])))
        self.received += len(out)
        return out

    def poll(self, limit: int = 256) -> list[Received]:
        """Every readable datagram that decodes as this receiver's kind ([C7]).

        A datagram that fails decoding is counted in :attr:`rejected` and
        logged with its size and the rejection, never its bytes.
        """
        out: list[Received] = []
        for dg in self.poll_raw(limit):
            try:
                pkt = decode(self.kind, dg.data)
            except PacketError as exc:
                self.rejected += 1
                log.warning(
                    "%s datagram rejected (%d B from %s:%d): %s",
                    self.kind.value,
                    len(dg.data),
                    dg.addr[0],
                    dg.addr[1],
                    type(exc).__name__ if self.kind is PacketKind.COMMAND else exc,
                )
                continue
            out.append(Received(packet=pkt, addr=dg.addr))
        return out

    def reply(self, packet: Packet, addr: Address) -> bool:
        """Send ``packet`` from this socket to ``addr`` (an ack to the command's
        sender, [P5]); ``False`` if the send failed."""
        try:
            self.sock.sendto(encode(packet), addr)
        except OSError as exc:
            log.warning("reply to %s:%d failed: %s", addr[0], addr[1], exc)
            return False
        return True

    def close(self) -> None:
        self.sock.close()


class UdpSender:
    """Publishes one packet kind to its listener's port ([C10]); never blocks."""

    def __init__(self, host: str, port: int) -> None:
        self.target: Address = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sent = 0
        self.errors = 0

    def send(self, packet: Packet) -> bool:
        """Encode canonically ([C5]) and send; ``False`` (counted) if the send failed.

        No listener on the port is not an error: a publish is fire and forget.
        """
        try:
            data = encode(packet)
        except PacketError as exc:  # never fatal to the live loop
            self.errors += 1
            log.warning("unsendable %s packet dropped: %s", type(packet).__name__, exc)
            return False
        try:
            self.sock.sendto(data, self.target)
        except OSError:
            self.errors += 1
            return False
        self.sent += 1
        return True

    def close(self) -> None:
        self.sock.close()
