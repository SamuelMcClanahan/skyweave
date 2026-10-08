"""Whole-flight packet recording (DRONE_CONTRACTS_D0.md §3, brief 3.8).

Packets only: wire packets, the full MAVLink log, and the mission loop's time
inputs (ticks, ground heartbeats). There is no frame or pixel record type, and
this module offers no way to write one ([R1]).

The command token is removed before a command is written; the record keeps the
authentication result instead ([P5c], [R2]). Replay therefore never needs, and
never sees, the secret.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Any

from skyweave2.drone.packets import (
    CommandPacket,
    Packet,
    PacketError,
    PacketKind,
    canonical_json,
    from_obj,
    kind_of,
    to_obj,
)

FORMAT = "skyweave-drone-rec"
FORMAT_V = 1
REDACTED_TOKEN = "redacted"


class Stream(str, Enum):
    META = "meta"
    DETECTION = "detection"
    TRACK = "track"
    MISSION_STATE = "mission_state"
    FC_LINK_HEALTH = "fc_link_health"
    COMMAND = "command"
    ACK = "ack"
    MAVLINK = "mavlink"
    TICK = "tick"
    GROUND_HB = "ground_hb"


class RecordingError(ValueError):
    """A recording line the §3 format does not allow."""


@dataclass(frozen=True, kw_only=True)
class Record:
    t_rx: int
    stream: Stream
    packet: Packet | None = None  # packet streams; commands carry REDACTED_TOKEN
    auth_ok: bool | None = None  # command
    direction: str | None = None  # mavlink: "rx" or "tx"
    raw: bytes | None = None  # mavlink frame bytes
    config: Mapping[str, Any] | None = None  # meta


class Recorder:
    """Append-only JSONL writer, one canonical object per line, flushed per line."""

    def __init__(self, sink: IO[str] | str | Path) -> None:
        if isinstance(sink, (str, Path)):
            self._fh: IO[str] = open(sink, "w", encoding="ascii", newline="\n")  # noqa: SIM115
            self._owns = True
        else:
            self._fh = sink
            self._owns = False

    def _write(self, obj: dict[str, Any]) -> None:
        self._fh.write(canonical_json(obj).decode("ascii") + "\n")
        self._fh.flush()

    def meta(self, t_rx: int, config: Mapping[str, Any]) -> None:
        self._write(
            {
                "t_rx": t_rx,
                "stream": Stream.META.value,
                "format": FORMAT,
                "format_v": FORMAT_V,
                "config": dict(config),
            }
        )

    def packet(self, t_rx: int, packet: Packet) -> None:
        if isinstance(packet, CommandPacket):
            raise TypeError("commands are recorded with Recorder.command (token removed)")
        self._write({"t_rx": t_rx, "stream": kind_of(packet).value, "pkt": to_obj(packet)})

    def command(self, t_rx: int, packet: CommandPacket, auth_ok: bool) -> None:
        pkt = to_obj(packet)
        del pkt["token"]
        self._write(
            {"t_rx": t_rx, "stream": Stream.COMMAND.value, "pkt": pkt, "auth_ok": bool(auth_ok)}
        )

    def mavlink(self, t_rx: int, direction: str, raw: bytes) -> None:
        if direction not in ("rx", "tx"):
            raise ValueError("direction must be 'rx' or 'tx'")
        self._write(
            {
                "t_rx": t_rx,
                "stream": Stream.MAVLINK.value,
                "dir": direction,
                "raw": base64.b64encode(raw).decode("ascii"),
            }
        )

    def tick(self, t_rx: int) -> None:
        self._write({"t_rx": t_rx, "stream": Stream.TICK.value})

    def ground_hb(self, t_rx: int) -> None:
        self._write({"t_rx": t_rx, "stream": Stream.GROUND_HB.value})

    def close(self) -> None:
        if self._owns:
            self._fh.close()


def parse_record(line: str) -> Record:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RecordingError(f"not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RecordingError("record must be a JSON object")
    t_rx = obj.get("t_rx")
    if isinstance(t_rx, bool) or not isinstance(t_rx, int) or t_rx < 0:
        raise RecordingError("record t_rx must be an int >= 0")
    try:
        stream = Stream(obj.get("stream"))
    except ValueError:
        raise RecordingError(f"unknown stream {obj.get('stream')!r}") from None
    try:
        if stream is Stream.META:
            if obj.get("format") != FORMAT or obj.get("format_v") != FORMAT_V:
                raise RecordingError("meta record has an unknown format")
            config = obj.get("config")
            if not isinstance(config, dict):
                raise RecordingError("meta config must be an object")
            return Record(t_rx=t_rx, stream=stream, config=config)
        if stream is Stream.COMMAND:
            pkt = dict(obj["pkt"])
            pkt["token"] = REDACTED_TOKEN
            auth_ok = obj["auth_ok"]
            if not isinstance(auth_ok, bool):
                raise RecordingError("command auth_ok must be a bool")
            return Record(
                t_rx=t_rx,
                stream=stream,
                packet=from_obj(PacketKind.COMMAND, pkt),
                auth_ok=auth_ok,
            )
        if stream is Stream.MAVLINK:
            direction = obj["dir"]
            if direction not in ("rx", "tx"):
                raise RecordingError("mavlink dir must be 'rx' or 'tx'")
            return Record(
                t_rx=t_rx,
                stream=stream,
                direction=direction,
                raw=base64.b64decode(obj["raw"], validate=True),
            )
        if stream in (Stream.TICK, Stream.GROUND_HB):
            return Record(t_rx=t_rx, stream=stream)
        return Record(
            t_rx=t_rx, stream=stream, packet=from_obj(PacketKind(stream.value), obj["pkt"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, (RecordingError, PacketError)):
            raise
        raise RecordingError(f"malformed {stream.value} record: {exc}") from exc


def read_records(source: str | Path | Iterable[str]) -> Iterator[Record]:
    """Yield records in file order. ``source`` is a path or an iterable of lines."""
    if isinstance(source, (str, Path)):
        with open(source, encoding="ascii") as fh:
            yield from _parse_lines(fh)
    else:
        yield from _parse_lines(source)


def _parse_lines(lines: Iterable[str]) -> Iterator[Record]:
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            yield parse_record(line)
        except (RecordingError, PacketError) as exc:
            raise RecordingError(f"line {n}: {exc}") from exc
