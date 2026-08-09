"""TCP control plane: same protobuf, length-prefixed (D0 section 10, Provisional).

Frame layout on the stream:

    uint32 big-endian body length | 4-byte wire header | protobuf ControlMessage

The length prefix is what makes a stream protocol safe: protobuf is not
self-delimiting, so a reader without a prefix cannot tell where one message
ends and the next begins. Every read is exact-length — a short read is looped
on, never accepted as a complete message, because a partially-read protobuf
usually still parses and would deliver a message with missing fields.

Nothing on this plane carries a measurement. It carries calibration revision,
node config, start/stop, and acknowledgements.
"""

from __future__ import annotations

import socket
import struct

from skyweave2.transport.codec import decode_control, encode_control
from skyweave2.transport.proto import skyweave_pb2 as pb
from skyweave2.transport.wire import (
    CONTROL_FRAME_MAX_BYTES,
    CONTROL_LENGTH_PREFIX_FORMAT,
    CONTROL_LENGTH_PREFIX_LEN,
    WIRE_LIMITS,
    WireFormatError,
)


class ControlChannelClosed(Exception):
    """The peer closed the stream cleanly between frames."""


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    """Read exactly ``count`` bytes or fail. Never returns a short buffer."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            if chunks:
                raise WireFormatError(
                    f"control stream ended mid-frame: wanted {count} B, got "
                    f"{count - remaining} B — a truncated control frame is "
                    "never parsed"
                )
            raise ControlChannelClosed("peer closed the control stream")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_control(sock: socket.socket, message: pb.ControlMessage) -> int:
    """Length-prefix and send one control message; returns bytes written."""
    body = encode_control(message)
    if len(body) > CONTROL_FRAME_MAX_BYTES:
        raise WireFormatError(
            f"control frame is {len(body)} B, over the "
            f"{CONTROL_FRAME_MAX_BYTES} B bound"
        )
    payload = struct.pack(CONTROL_LENGTH_PREFIX_FORMAT, len(body)) + body
    sock.sendall(payload)
    return len(payload)


def recv_control(sock: socket.socket) -> pb.ControlMessage:
    """Read exactly one control message. Raises ControlChannelClosed at EOF."""
    prefix = _recv_exact(sock, CONTROL_LENGTH_PREFIX_LEN)
    (length,) = struct.unpack(CONTROL_LENGTH_PREFIX_FORMAT, prefix)
    if length == 0:
        raise WireFormatError("control frame declares zero length")
    if length > CONTROL_FRAME_MAX_BYTES:
        # Bounded BEFORE allocating: an absurd length prefix must not be able
        # to make the host reserve memory on a peer's say-so.
        raise WireFormatError(
            f"control frame declares {length} B, over the "
            f"{CONTROL_FRAME_MAX_BYTES} B bound — refusing to allocate"
        )
    datagram = _recv_exact(sock, length)
    from skyweave2.transport.wire import PayloadType, unframe

    payload_type, body = unframe(datagram)
    if payload_type is not PayloadType.CONTROL:
        raise WireFormatError(
            f"control stream carried a {payload_type.name} payload"
        )
    return decode_control(body)


class ControlServer:
    """Listening side of the control plane (the Jetson)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, backlog: int = 8) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((host, port))
        self._socket.listen(backlog)

    @property
    def port(self) -> int:
        return self._socket.getsockname()[1]

    def accept(self, timeout_s: float | None = None) -> socket.socket | None:
        self._socket.settimeout(timeout_s)
        try:
            connection, _ = self._socket.accept()
        except TimeoutError:
            return None
        connection.settimeout(timeout_s)
        return connection

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> ControlServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def connect(host: str, port: int, timeout_s: float = 5.0) -> socket.socket:
    """Connecting side of the control plane (an edge node)."""
    sock = socket.create_connection((host, port), timeout=timeout_s)
    sock.settimeout(timeout_s)
    return sock


def start_message(camera_id: int, session_uuid: str, control_seq: int = 0
                  ) -> pb.ControlMessage:
    return pb.ControlMessage(
        type=pb.CONTROL_TYPE_START,
        control_seq=control_seq,
        session_uuid=session_uuid,
        camera_id=camera_id,
    )


def stop_message(camera_id: int, session_uuid: str, control_seq: int = 0
                 ) -> pb.ControlMessage:
    return pb.ControlMessage(
        type=pb.CONTROL_TYPE_STOP,
        control_seq=control_seq,
        session_uuid=session_uuid,
        camera_id=camera_id,
    )


def calibration_message(camera_id: int, session_uuid: str, calibration_rev: str,
                        detector_rev: str, control_seq: int = 0) -> pb.ControlMessage:
    message = pb.ControlMessage(
        type=pb.CONTROL_TYPE_CALIBRATION_REVISION,
        control_seq=control_seq,
        session_uuid=session_uuid,
        camera_id=camera_id,
    )
    message.calibration.calibration_rev = calibration_rev
    message.calibration.detector_rev = detector_rev
    return message


def config_message(camera_id: int, session_uuid: str, proc_width: int,
                   proc_height: int, target_fps: float,
                   max_observations_per_packet: int, evidence_enabled: bool,
                   control_seq: int = 0) -> pb.ControlMessage:
    message = pb.ControlMessage(
        type=pb.CONTROL_TYPE_SET_CONFIG,
        control_seq=control_seq,
        session_uuid=session_uuid,
        camera_id=camera_id,
    )
    message.config.proc_width = proc_width
    message.config.proc_height = proc_height
    message.config.target_fps = target_fps
    message.config.max_observations_per_packet = max_observations_per_packet
    message.config.evidence_enabled = evidence_enabled
    return message


def ack_message(control_seq: int, accepted: bool, detail: str = "") -> pb.ControlMessage:
    """Acknowledge (or refuse) one control message.

    ``detail`` is a bounded LABEL, not a log line: nanopb allocates
    ``Ack.detail`` at the declared ``max_size``, so a long diagnostic is a
    datagram the D8 board cannot decode. The full reason belongs in the host
    log; the wire carries the short form. Over-long details are refused here,
    at construction, rather than at send time — a failure while reporting a
    failure is the worst place to discover a bound.
    """
    from skyweave2.transport.codec import _check_text

    _check_text("ack.detail", detail, WIRE_LIMITS.ack_detail_max_size)
    message = pb.ControlMessage(type=pb.CONTROL_TYPE_ACK, control_seq=control_seq)
    message.ack.control_seq = control_seq
    message.ack.accepted = accepted
    message.ack.detail = detail
    return message
