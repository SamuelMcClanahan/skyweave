"""Drone stack JSON wire v1 (DRONE_CONTRACTS_D0.md §1-§2). FROZEN.

Every packet kind is a frozen dataclass plus a strict decoder. The decoder is
an adapter and nothing else: it never rounds a value into range, never fills a
missing field, and never coerces a type. A packet that the contract does not
allow exits as :class:`PacketError` ([C7]). Unknown fields are ignored ([C7]),
which is the whole of the additive-change rule on the receive side.

There is no in-band type tag ([C8]): the caller names the packet kind, which
in a live process is the UDP port the datagram arrived on and in a recording
is the record's ``stream``.

The canonical encoder ([C5]) is what golden fixtures pin byte for byte:
ASCII, sorted keys, compact separators, finite numbers only. Enum spellings
are pinned separately against the contract tables (P series).

Every rejection leaves ``decode`` as :class:`PacketError`, including input
that is not decodable JSON at all (bad UTF-8, deep nesting, an integer
literal past CPython's digit limit, a real that overflows to infinity), so a
receive loop that catches ``PacketError`` survives any datagram ([C7]).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

WIRE_VERSION = 1
FLIGHT_GRID_WIDTH = 1920
FLIGHT_GRID_HEIGHT = 1200
MAX_DATAGRAM_BYTES = 65507
MAX_EVENT_NAME_LEN = 128
MAX_JSON_DEPTH = 32  # [C5]: deeper nesting is refused before parsing
JSON_SAFE_INT = 2**53 - 1  # [C6]: integers a JSON peer (JavaScript) holds exactly
V_MAX_HARD_MPS = 5.0

_CMD_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_TOKEN_RE = re.compile(r"[!-~]{1,256}")


class PacketError(ValueError):
    """A datagram or object the frozen contract does not allow ([C7])."""


class UnsupportedVersion(PacketError):
    """``v`` is not 1. A breaking change bumps ``v``; it is never coerced."""


class InvalidParams(ValueError):
    """Prime params outside the contract ranges ([P5]); acked rejected_params."""


class PacketKind(str, Enum):
    DETECTION = "detection"
    TRACK = "track"
    MISSION_STATE = "mission_state"
    FC_LINK_HEALTH = "fc_link_health"
    COMMAND = "command"
    ACK = "ack"


class TrackState(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    COASTING = "coasting"


class MissionState(str, Enum):
    PRIMED = "PRIMED"
    LAUNCH = "LAUNCH"
    SEARCH = "SEARCH"
    ACQUIRING = "ACQUIRING"
    ENGAGED = "ENGAGED"
    COASTING = "COASTING"
    TOUCH = "TOUCH"
    COMPLETE = "COMPLETE"
    MISS = "MISS"
    LOST = "LOST"
    RETURN = "RETURN"
    LAND = "LAND"
    ABORT = "ABORT"


class TrialType(str, Enum):
    STANDOFF = "standoff"
    TOUCH = "touch"


class GateState(str, Enum):
    LOCKED = "locked"
    ENABLED = "enabled"


class CommandName(str, Enum):
    PRIME = "prime"
    APPROVE_ENGAGE = "approve_engage"
    MARK_COMPLETE = "mark_complete"
    ABORT = "abort"


class AckResult(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_STATE = "rejected_state"
    REJECTED_AUTH = "rejected_auth"
    REJECTED_PARAMS = "rejected_params"
    REJECTED_MALFORMED = "rejected_malformed"
    REJECTED_DUPLICATE_ID = "rejected_duplicate_id"


# ---------------------------------------------------------------------------
# Packet types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Box:
    """[P1] box: top-left origin on the flight grid ([C2])."""

    x: float
    y: float
    w: float
    h: float
    conf: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)


@dataclass(frozen=True, kw_only=True)
class DetectionPacket:
    """[P1] percepd -> tracker, one per processed frame."""

    t_cap: int
    frame_seq: int
    boxes: tuple[Box, ...]
    v: int = WIRE_VERSION


@dataclass(frozen=True, kw_only=True)
class TrackPacket:
    """[P2] tracker -> guidance and mission, one per live track per frame."""

    t_cap: int
    track_id: int
    state: TrackState
    u: float
    v_px: float
    du: float
    dv: float
    w: float
    h: float
    hits: int
    misses: int
    age_frames: int
    v: int = WIRE_VERSION


@dataclass(frozen=True, kw_only=True)
class TrialEcho:
    """[P3] trial parameter echo of the accepted prime."""

    trial_type: TrialType
    v_max: float
    alpha: float
    beta: float
    k: int
    pass_budget: int
    search_alt: float
    d_s: float


@dataclass(frozen=True, kw_only=True)
class Event:
    """[P3] one mission event; vocabulary in contract §4.6."""

    t: int
    name: str


@dataclass(frozen=True, kw_only=True)
class MissionStatePacket:
    """[P3] mission -> UI and recording."""

    t: int
    mission_state: MissionState
    engaged_track_id: int | None
    trial: TrialEcho
    events: tuple[Event, ...]
    v: int = WIRE_VERSION


@dataclass(frozen=True, kw_only=True)
class FcLinkHealthPacket:
    """[P4] fc_link -> mission, UI, recording, about 1 Hz."""

    t: int
    attitude_age_ms: int | None
    fc_link_up: bool
    rc_seen: bool
    gate_state: GateState
    last_setpoint_t: int | None
    v: int = WIRE_VERSION


@dataclass(frozen=True, kw_only=True)
class CommandPacket:
    """[P5] UI or radio -> mission. ``params`` is the raw object; only the
    mission validates it (``PrimeParams.from_obj``), so an invalid set can be
    acked ``rejected_params`` with this packet's ``cmd_id``."""

    cmd_id: str
    token: str
    command: CommandName
    params: Mapping[str, Any] | None = None
    v: int = WIRE_VERSION


@dataclass(frozen=True, kw_only=True)
class AckPacket:
    """[P5] mission -> the command's sender."""

    cmd_id: str
    result: AckResult
    v: int = WIRE_VERSION


Packet = (
    DetectionPacket
    | TrackPacket
    | MissionStatePacket
    | FcLinkHealthPacket
    | CommandPacket
    | AckPacket
)


@dataclass(frozen=True, kw_only=True)
class PrimeParams:
    """[P5] prime params, validated against the contract ranges.

    Defaults are the Provisional prime-time values of contract §9 (brief §6
    plus the E1-added budgets); the UI form starts from them.
    """

    trial_type: TrialType = TrialType.STANDOFF
    v_max: float = 2.5
    alpha: float = 0.4
    beta: float = 0.1
    k: int = 5
    pass_budget: int = 1
    search_alt: float = 10.0
    d_s: float = 5.0
    target_width_m: float = 1.0
    engage_preauthorized: bool = False
    flight_time_cap_s: float = 180.0
    battery_floor_pct: float = 30.0
    geofence_radius_m: float = 60.0

    def __post_init__(self) -> None:
        _range_params(self)

    def echo(self) -> TrialEcho:
        return TrialEcho(
            trial_type=self.trial_type,
            v_max=self.v_max,
            alpha=self.alpha,
            beta=self.beta,
            k=self.k,
            pass_budget=self.pass_budget,
            search_alt=self.search_alt,
            d_s=self.d_s,
        )

    def to_obj(self) -> dict[str, Any]:
        return {
            "trial_type": self.trial_type.value,
            "v_max": self.v_max,
            "alpha": self.alpha,
            "beta": self.beta,
            "k": self.k,
            "pass_budget": self.pass_budget,
            "search_alt": self.search_alt,
            "d_s": self.d_s,
            "target_width_m": self.target_width_m,
            "engage_preauthorized": self.engage_preauthorized,
            "flight_time_cap_s": self.flight_time_cap_s,
            "battery_floor_pct": self.battery_floor_pct,
            "geofence_radius_m": self.geofence_radius_m,
        }

    @classmethod
    def from_obj(cls, obj: Any) -> PrimeParams:
        """Parse and range-check; raises :class:`InvalidParams`, never coerces."""
        if not isinstance(obj, Mapping):
            raise InvalidParams("prime params must be a JSON object")
        try:
            return cls(
                trial_type=_enum(obj, "trial_type", TrialType),
                v_max=_float(obj, "v_max"),
                alpha=_float(obj, "alpha"),
                beta=_float(obj, "beta"),
                k=_int(obj, "k"),
                pass_budget=_int(obj, "pass_budget"),
                search_alt=_float(obj, "search_alt"),
                d_s=_float(obj, "d_s"),
                target_width_m=_float(obj, "target_width_m"),
                engage_preauthorized=_bool(obj, "engage_preauthorized"),
                flight_time_cap_s=_float(obj, "flight_time_cap_s"),
                battery_floor_pct=_float(obj, "battery_floor_pct"),
                geofence_radius_m=_float(obj, "geofence_radius_m"),
            )
        except PacketError as exc:
            raise InvalidParams(str(exc)) from exc


def _range_params(p: PrimeParams) -> None:
    checks = (
        ("v_max", 0.0 < p.v_max <= V_MAX_HARD_MPS),
        ("alpha", 0.0 < p.alpha <= 1.0),
        ("beta", 0.0 < p.beta <= 0.5),
        ("k", 1 <= p.k <= 1000),
        # brief 3.4: autonomous retry is v2 (specced, not flown).
        ("pass_budget", p.pass_budget == 1),
        ("search_alt", 0.0 < p.search_alt <= 120.0),
        ("d_s", 0.0 < p.d_s <= 100.0),
        ("target_width_m", 0.0 < p.target_width_m <= 10.0),
        ("flight_time_cap_s", 0.0 < p.flight_time_cap_s <= 1800.0),
        ("battery_floor_pct", 0.0 <= p.battery_floor_pct <= 100.0),
        ("geofence_radius_m", 0.0 < p.geofence_radius_m <= 1000.0),
    )
    for name, ok in checks:
        if not ok:
            raise InvalidParams(f"prime param {name} out of range: {getattr(p, name)!r}")
    if not isinstance(p.trial_type, TrialType):
        raise InvalidParams("prime param trial_type must be a TrialType")
    if not isinstance(p.engage_preauthorized, bool):
        raise InvalidParams("prime param engage_preauthorized must be a bool")


# ---------------------------------------------------------------------------
# Field readers ([C6]): strict, never coercing
# ---------------------------------------------------------------------------


def _get(obj: Mapping[str, Any], key: str) -> Any:
    if key not in obj:
        raise PacketError(f"missing required field {key!r}")
    return obj[key]


def _int(obj: Mapping[str, Any], key: str, minimum: int | None = None) -> int:
    val = _get(obj, key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise PacketError(f"field {key!r} must be a JSON integer, got {val!r}")
    if not -JSON_SAFE_INT <= val <= JSON_SAFE_INT:
        raise PacketError(f"field {key!r} is outside the JSON-safe integer range")
    if minimum is not None and val < minimum:
        raise PacketError(f"field {key!r} must be >= {minimum}, got {val}")
    return val


def _int_or_null(obj: Mapping[str, Any], key: str, minimum: int | None = None) -> int | None:
    if _get(obj, key) is None:
        return None
    return _int(obj, key, minimum)


def _float(obj: Mapping[str, Any], key: str) -> float:
    val = _get(obj, key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise PacketError(f"field {key!r} must be a JSON number, got {val!r}")
    try:
        out = float(val)
    except OverflowError:
        raise PacketError(f"field {key!r} is outside the float range") from None
    if not math.isfinite(out):
        raise PacketError(f"field {key!r} must be finite, got {val!r}")
    return out


def _bool(obj: Mapping[str, Any], key: str) -> bool:
    val = _get(obj, key)
    if not isinstance(val, bool):
        raise PacketError(f"field {key!r} must be true or false, got {val!r}")
    return val


def _str(obj: Mapping[str, Any], key: str, max_len: int) -> str:
    val = _get(obj, key)
    if not isinstance(val, str) or not 1 <= len(val) <= max_len:
        raise PacketError(f"field {key!r} must be a string of 1-{max_len} chars")
    return val


def _enum(obj: Mapping[str, Any], key: str, enum_cls: type[Enum]) -> Any:
    val = _get(obj, key)
    try:
        return enum_cls(val)
    except ValueError:
        allowed = ", ".join(str(m.value) for m in enum_cls)
        raise PacketError(f"field {key!r} must be one of {allowed}; got {val!r}") from None


def _list(obj: Mapping[str, Any], key: str) -> list[Any]:
    val = _get(obj, key)
    if not isinstance(val, list):
        raise PacketError(f"field {key!r} must be an array")
    return val


def _obj(val: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(val, Mapping):
        raise PacketError(f"{what} must be a JSON object")
    return val


def _version(obj: Mapping[str, Any]) -> int:
    v = _int(obj, "v")
    if v != WIRE_VERSION:
        raise UnsupportedVersion(f"packet v={v}; this receiver speaks v={WIRE_VERSION}")
    return v


def _cmd_id(obj: Mapping[str, Any]) -> str:
    val = _get(obj, "cmd_id")
    if not isinstance(val, str) or not _CMD_ID_RE.fullmatch(val):
        raise PacketError("field 'cmd_id' must be 1-64 chars of [A-Za-z0-9._:-]")
    return val


def _token(obj: Mapping[str, Any]) -> str:
    val = _get(obj, "token")
    if not isinstance(val, str) or not _TOKEN_RE.fullmatch(val):
        raise PacketError("field 'token' must be 1-256 chars of printable ASCII")
    return val


# ---------------------------------------------------------------------------
# Object <-> packet
# ---------------------------------------------------------------------------


def _box_from(obj: Any) -> Box:
    o = _obj(obj, "box")
    box = Box(
        x=_float(o, "x"),
        y=_float(o, "y"),
        w=_float(o, "w"),
        h=_float(o, "h"),
        conf=_float(o, "conf"),
    )
    if not (box.w > 0.0 and box.h > 0.0):
        raise PacketError("box w and h must be > 0")
    if not 0.0 <= box.conf <= 1.0:
        raise PacketError("box conf must be in [0, 1]")
    return box


def _trial_from(obj: Any) -> TrialEcho:
    o = _obj(obj, "trial")
    trial = TrialEcho(
        trial_type=_enum(o, "trial_type", TrialType),
        v_max=_float(o, "v_max"),
        alpha=_float(o, "alpha"),
        beta=_float(o, "beta"),
        k=_int(o, "k", 1),
        pass_budget=_int(o, "pass_budget", 1),
        search_alt=_float(o, "search_alt"),
        d_s=_float(o, "d_s"),
    )
    if not (trial.v_max > 0.0 and trial.search_alt > 0.0 and trial.d_s > 0.0):
        raise PacketError("trial v_max, search_alt and d_s must be > 0")
    if not (0.0 < trial.alpha <= 1.0 and 0.0 < trial.beta <= 0.5):
        raise PacketError("trial alpha must be in (0, 1] and beta in (0, 0.5]")
    return trial


def _event_from(obj: Any) -> Event:
    o = _obj(obj, "event")
    return Event(t=_int(o, "t", 0), name=_str(o, "name", MAX_EVENT_NAME_LEN))


def from_obj(kind: PacketKind, obj: Any) -> Packet:
    """Decode one packet object of a known kind ([C7], [C8])."""
    o = _obj(obj, "packet")
    _version(o)
    if kind is PacketKind.DETECTION:
        return DetectionPacket(
            t_cap=_int(o, "t_cap", 0),
            frame_seq=_int(o, "frame_seq", 0),
            boxes=tuple(_box_from(b) for b in _list(o, "boxes")),
        )
    if kind is PacketKind.TRACK:
        pkt = TrackPacket(
            t_cap=_int(o, "t_cap", 0),
            track_id=_int(o, "track_id", 1),
            state=_enum(o, "state", TrackState),
            u=_float(o, "u"),
            v_px=_float(o, "v_px"),
            du=_float(o, "du"),
            dv=_float(o, "dv"),
            w=_float(o, "w"),
            h=_float(o, "h"),
            hits=_int(o, "hits", 0),
            misses=_int(o, "misses", 0),
            age_frames=_int(o, "age_frames", 1),
        )
        if not (pkt.w > 0.0 and pkt.h > 0.0):
            raise PacketError("track w and h must be > 0")
        return pkt
    if kind is PacketKind.MISSION_STATE:
        return MissionStatePacket(
            t=_int(o, "t", 0),
            mission_state=_enum(o, "mission_state", MissionState),
            engaged_track_id=_int_or_null(o, "engaged_track_id", 1),
            trial=_trial_from(_get(o, "trial")),
            events=tuple(_event_from(e) for e in _list(o, "events")),
        )
    if kind is PacketKind.FC_LINK_HEALTH:
        return FcLinkHealthPacket(
            t=_int(o, "t", 0),
            attitude_age_ms=_int_or_null(o, "attitude_age_ms", 0),
            fc_link_up=_bool(o, "fc_link_up"),
            rc_seen=_bool(o, "rc_seen"),
            gate_state=_enum(o, "gate_state", GateState),
            last_setpoint_t=_int_or_null(o, "last_setpoint_t", 0),
        )
    if kind is PacketKind.COMMAND:
        command = _enum(o, "command", CommandName)
        params: Mapping[str, Any] | None = None
        if command is CommandName.PRIME:
            params = dict(_obj(_get(o, "params"), "prime params"))
        return CommandPacket(
            cmd_id=_cmd_id(o),
            token=_token(o),
            command=command,
            params=params,
        )
    if kind is PacketKind.ACK:
        return AckPacket(cmd_id=_cmd_id(o), result=_enum(o, "result", AckResult))
    raise PacketError(f"unknown packet kind {kind!r}")


def to_obj(packet: Packet) -> dict[str, Any]:
    """Packet -> plain JSON object, field names exactly as in contract §2."""
    if isinstance(packet, DetectionPacket):
        return {
            "v": packet.v,
            "t_cap": packet.t_cap,
            "frame_seq": packet.frame_seq,
            "boxes": [
                {"x": b.x, "y": b.y, "w": b.w, "h": b.h, "conf": b.conf} for b in packet.boxes
            ],
        }
    if isinstance(packet, TrackPacket):
        return {
            "v": packet.v,
            "t_cap": packet.t_cap,
            "track_id": packet.track_id,
            "state": packet.state.value,
            "u": packet.u,
            "v_px": packet.v_px,
            "du": packet.du,
            "dv": packet.dv,
            "w": packet.w,
            "h": packet.h,
            "hits": packet.hits,
            "misses": packet.misses,
            "age_frames": packet.age_frames,
        }
    if isinstance(packet, MissionStatePacket):
        trial = packet.trial
        return {
            "v": packet.v,
            "t": packet.t,
            "mission_state": packet.mission_state.value,
            "engaged_track_id": packet.engaged_track_id,
            "trial": {
                "trial_type": trial.trial_type.value,
                "v_max": trial.v_max,
                "alpha": trial.alpha,
                "beta": trial.beta,
                "k": trial.k,
                "pass_budget": trial.pass_budget,
                "search_alt": trial.search_alt,
                "d_s": trial.d_s,
            },
            "events": [{"t": e.t, "name": e.name} for e in packet.events],
        }
    if isinstance(packet, FcLinkHealthPacket):
        return {
            "v": packet.v,
            "t": packet.t,
            "attitude_age_ms": packet.attitude_age_ms,
            "fc_link_up": packet.fc_link_up,
            "rc_seen": packet.rc_seen,
            "gate_state": packet.gate_state.value,
            "last_setpoint_t": packet.last_setpoint_t,
        }
    if isinstance(packet, CommandPacket):
        out: dict[str, Any] = {
            "v": packet.v,
            "cmd_id": packet.cmd_id,
            "token": packet.token,
            "command": packet.command.value,
        }
        if packet.params is not None:
            out["params"] = dict(packet.params)
        return out
    if isinstance(packet, AckPacket):
        return {"v": packet.v, "cmd_id": packet.cmd_id, "result": packet.result.value}
    raise TypeError(f"not a drone packet: {type(packet).__name__}")


def kind_of(packet: Packet) -> PacketKind:
    for cls, kind in _KIND_BY_CLASS:
        if isinstance(packet, cls):
            return kind
    raise TypeError(f"not a drone packet: {type(packet).__name__}")


_KIND_BY_CLASS: tuple[tuple[type, PacketKind], ...] = (
    (DetectionPacket, PacketKind.DETECTION),
    (TrackPacket, PacketKind.TRACK),
    (MissionStatePacket, PacketKind.MISSION_STATE),
    (FcLinkHealthPacket, PacketKind.FC_LINK_HEALTH),
    (CommandPacket, PacketKind.COMMAND),
    (AckPacket, PacketKind.ACK),
)


# ---------------------------------------------------------------------------
# Bytes
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> bytes:
    """[C5] canonical JSON bytes: ASCII, sorted keys, compact, finite only."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def encode(packet: Packet) -> bytes:
    """Packet -> one canonical datagram ([C5], [C9]).

    The encoded object is decoded again before it is returned, so this repo
    can never send a packet its own receivers would reject.
    """
    obj = to_obj(packet)
    try:
        data = canonical_json(obj)
    except ValueError as exc:  # NaN or Infinity
        raise PacketError(f"packet not encodable: {exc}") from exc
    if len(data) > MAX_DATAGRAM_BYTES:
        raise PacketError(f"packet encodes to {len(data)} B > {MAX_DATAGRAM_BYTES} B")
    from_obj(kind_of(packet), obj)
    return data


def strict_json_loads(text: str) -> Any:
    """[C5], [C7] JSON text -> object, refusing non-finite numbers and
    nesting deeper than :data:`MAX_JSON_DEPTH`.

    Raises :class:`PacketError` for anything that is not decodable, finite
    JSON, never another exception type. The depth cap keeps every accepted
    packet re-parseable from a recording line at any stack depth (bug-hunt
    finding: a 984-deep params object decoded, then crashed the recorder's
    re-parse).
    """
    _check_depth(text)
    try:
        return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except PacketError:
        raise
    except (ValueError, RecursionError) as exc:  # JSONDecodeError, int digit limit
        raise PacketError(f"not decodable JSON: {exc}") from None


def _check_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise PacketError(f"JSON nested deeper than {MAX_JSON_DEPTH} levels")
        elif ch in "]}":
            depth -= 1


def decode(kind: PacketKind, data: bytes) -> Packet:
    """One datagram -> packet, or :class:`PacketError` ([C5], [C7])."""
    if len(data) > MAX_DATAGRAM_BYTES:
        raise PacketError(f"datagram {len(data)} B > {MAX_DATAGRAM_BYTES} B")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PacketError(f"not UTF-8: {exc}") from None
    return from_obj(kind, strict_json_loads(text))


def _reject_constant(name: str) -> Any:
    raise PacketError(f"non-finite JSON constant {name} is not allowed")


def _finite_float(text: str) -> float:
    out = float(text)
    if not math.isfinite(out):
        raise PacketError(f"non-finite JSON number {text} is not allowed")
    return out


def salvage_cmd_id(data: bytes) -> str | None:
    """[P5b] the ``cmd_id`` of an undecodable command datagram, if readable.

    Parses leniently (a non-finite number elsewhere in the datagram does not
    hide its ``cmd_id``) and never raises.
    """
    try:
        obj = json.loads(data.decode("utf-8"))
        if isinstance(obj, Mapping):
            return _cmd_id(obj)
    except (ValueError, RecursionError):  # UnicodeDecodeError, JSON errors, PacketError
        return None
    return None
