"""UDP measurement and evidence sockets (D7).

Two planes, two sockets, on purpose (working plan section 4): evidence can be
dropped under congestion without touching a measurement only if the two do not
share a queue. Measurement datagrams are fresh and never retransmitted; the
loss policy lives in the aligner, not here.

The receiver reads into a buffer far larger than the ceiling and rejects
oversized datagrams explicitly (see ``wire.RECV_BUFFER_BYTES``). Sizing the
buffer at the ceiling would make the kernel hand up a truncated body that
parses as a valid short packet — the exact silent-truncation failure this
phase exists to rule out.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

from skyweave2.transport.wire import RECV_BUFFER_BYTES


@dataclass
class SocketCounters:
    """Everything the socket layer knows. Labeled, never inferred."""

    datagrams: int = 0
    bytes_total: int = 0
    sizes: list[int] = field(default_factory=list)

    def record(self, size: int) -> None:
        self.datagrams += 1
        self.bytes_total += size
        self.sizes.append(size)


class UdpSender:
    """Fire-and-forget datagram sender. No retransmission, ever."""

    def __init__(self, host: str, port: int) -> None:
        self.address = (host, port)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.counters = SocketCounters()

    def send(self, datagram: bytes) -> int:
        """Send one pre-framed, pre-size-checked datagram.

        A short ``sendto`` is impossible for UDP (the datagram is atomic or it
        fails), but the assertion below makes that assumption explicit rather
        than assumed: if it ever stopped holding, the failure would look like
        silent truncation.
        """
        sent = self._socket.sendto(datagram, self.address)
        if sent != len(datagram):  # pragma: no cover - UDP sendto is atomic
            raise OSError(
                f"partial UDP send: {sent} of {len(datagram)} B — a datagram "
                "was truncated on the send path"
            )
        self.counters.record(len(datagram))
        return sent

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> UdpSender:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class UdpReceiver:
    """Bound datagram receiver.

    ``port=0`` binds an ephemeral port; read :attr:`port` afterwards. That is
    how the tests and the loopback rig avoid fixed ports (and the flakiness of
    a port another process already holds).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 rcvbuf_bytes: int = 4 * 1024 * 1024) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # A generous kernel receive buffer: three edge nodes bursting a frame
        # period apart must not lose datagrams to buffer pressure on loopback,
        # or the rig would measure the harness instead of the system.
        try:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf_bytes)
        except OSError:  # pragma: no cover - platform-dependent ceiling
            pass
        self._socket.bind((host, port))
        self.counters = SocketCounters()

    @property
    def port(self) -> int:
        return self._socket.getsockname()[1]

    @property
    def rcvbuf_bytes(self) -> int:
        return self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)

    def recv(self, timeout_s: float | None) -> bytes | None:
        """One datagram, or None on timeout.

        The read buffer is deliberately larger than any channel ceiling so an
        over-ceiling datagram arrives whole and can be REJECTED. It must never
        arrive silently shortened.
        """
        self._socket.settimeout(timeout_s)
        try:
            datagram = self._socket.recv(RECV_BUFFER_BYTES)
        except TimeoutError:
            return None
        except BlockingIOError:
            # settimeout(0.0) puts the socket in NON-BLOCKING mode, where an
            # empty queue raises BlockingIOError rather than timing out. Both
            # mean the same thing here: nothing was waiting.
            return None
        self.counters.record(len(datagram))
        return datagram

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> UdpReceiver:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
