"""Companion core: mission + guidance + vehicle state, driven by stamped inputs.

DRONE_CONTRACTS_D0.md [C10], [M2], [R2]-[R4]; brief work item 2 (replay gate).

The core is the pure part of the companion process ([C10]): one object that the
live process, the SITL harness, and replay all drive the same way, so that a
recording fed back through a fresh core gives the same outputs ([R3]). The
order of calls per input is part of that contract, so it lives here and
nowhere else:

- TRK (a track packet): ``mission.on_track``; then ``guidance.on_track`` with
  the mission view after that packet; then each guidance event goes to
  ``mission.on_guidance`` as its own GDE input ([M2]).
- CMD (a command with its authentication result): ``mission.on_command``.
- HB (a UI poll, [U6]): ``mission.on_ground_heartbeat``.
- TICK: the vehicle snapshot is taken (ticks only, [M2]); then
  ``mission.on_tick``; then, when a setpoint step is due ([G11]),
  ``guidance.step`` and its events as GDE inputs.

Radio approves ([F7]): each approve the vehicle state detected is its own
in-process ``approve_engage`` CMD input, stamped with the ``t_rx`` of its
``RC_CHANNELS`` sample (the stamp of its ``mavlink`` rx record, [M2]), so the
[M8] settle check and its ack and transition carry the sample's stamp. Every
input method above (TRK, CMD, HB, TICK) first drains the approves detected
since the previous input, in sample order, before it stamps and records its
own input. That point is the same live (fc_link ingests the frames) and in
replay (:meth:`CompanionCore.on_mavlink_rx` ingests them), because it depends
only on where the rx record falls among the input records.

After every input (GDE inputs and radio approves included) the core asks the
mission for a mission state packet and drains its FC requests, so a transition
is published by the input that caused it ([P3]).

Time ([M2], [R3], E1-D8): the core never reads a clock. "Now" is the stamp of
the input being processed, and every output record carries that stamp
([R2]). Stamps of the core's own inputs must not decrease.

What the core records, through :class:`recording.Recorder`: the ``meta`` line
(its configuration, never a token), each input record before the input is
processed (``track``, ``command`` with the token removed, ``tick``,
``ground_hb``), and each output (``ack``, ``mission_state``). fc_link records
``mavlink`` frames ([F10]); a radio approve is re-derived from its ``mavlink``
rx record and is never a ``command`` record ([F7]).

What the core does not do: it opens no socket and sends nothing. It returns
the FC requests and the setpoint, and the caller writes them through fc_link,
which owns the gate ([F5]), the hard limits ([F6]), and the SITL interlock
([F1]). Replay models only those two facts of the write path that the
recording carries: the gate setting (``meta.config``) and the SITL proof (an
rx frame), so it predicts which setpoints became ``mavlink`` tx records.

Ground heartbeats: an authenticated ground command already counts as a ground
heartbeat inside the mission, so the core writes a ``ground_hb`` record only
for UI polls; recording one per command as well would add a second input that
live and replay would both have to feed.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.camera import CameraModel
from skyweave2.drone.fc_link import (
    SITL_PROOF_MSG_IDS,
    TYPE_MASK_VELOCITY_YAW_RATE,
    clamp_velocity,
)
from skyweave2.drone.guidance import (
    LAWS,
    Guidance,
    GuidanceConfig,
    MissVector,
    make_law,
    miss_vector,
)
from skyweave2.drone.mission import Mission, MissionConfig, TransitionRecord
from skyweave2.drone.packets import (
    AckPacket,
    AckResult,
    CommandName,
    CommandPacket,
    GateState,
    MissionStatePacket,
    PrimeParams,
    TrackPacket,
    salvage_cmd_id,
)
from skyweave2.drone.recording import (
    REDACTED_TOKEN,
    Record,
    Recorder,
    RecordingError,
    Stream,
    read_records,
)
from skyweave2.drone.types import FcRequest, GuidanceEvent, VelocityCommand
from skyweave2.drone.vehicle_state import (
    LinkConfig,
    VehicleState,
    parse_frames,
    rc_approve_cmd_id,
)

DEFAULT_LAW = "pure_pursuit"

SetpointKey = tuple[int, int, float, float, float, float]
"""[R3] how setpoints compare: (coordinate frame, type_mask, vx, vy, vz,
yaw_rate), the four numbers at float32 precision."""

_CONFIG_KEYS = frozenset(
    {"mission", "guidance", "camera", "link", "law", "coast_cap", "track_timeout_ms", "gate"}
)
_SETPOINT_MSG = "SET_POSITION_TARGET_LOCAL_NED"


# ---------------------------------------------------------------------------
# Configuration ([R2] meta.config)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class CoreConfig:
    """Everything replay needs to rebuild the core ([R2]). Never a token ([P5c]).

    Numbers are the modules' Provisional defaults (contract §9) unless a
    caller passes others. ``coast_cap`` and ``track_timeout_ms`` live in the
    mission config; :meth:`to_obj` also names them at the top level, with the
    gate setting, because [R2] and [P2a] call them out (the tracker must run
    with the same ``coast_cap``).
    """

    mission: MissionConfig = field(default_factory=MissionConfig)
    guidance: GuidanceConfig = field(default_factory=GuidanceConfig)
    camera: CameraModel = field(default_factory=CameraModel)
    link: LinkConfig = field(default_factory=LinkConfig)
    law: str = DEFAULT_LAW

    def __post_init__(self) -> None:
        if self.law not in LAWS:
            raise ValueError(f"unknown guidance law {self.law!r}; known: {sorted(LAWS)}")

    @property
    def gate(self) -> GateState:
        """[F5]: ``enabled`` only for an explicit ``setpoints_enabled = True``."""
        return GateState.ENABLED if self.link.setpoints_enabled is True else GateState.LOCKED

    def to_obj(self) -> dict[str, Any]:
        """The recording's ``meta.config`` object ([R2])."""
        return {
            "mission": self.mission.to_obj(),
            "guidance": self.guidance.to_obj(),
            "camera": self.camera.to_obj(),
            "link": self.link.to_obj(),
            "law": self.law,
            "coast_cap": self.mission.coast_cap,
            "track_timeout_ms": self.mission.track_timeout_ms,
            "gate": self.gate.value,
        }

    @classmethod
    def from_obj(cls, obj: Any) -> CoreConfig:
        """Inverse of :meth:`to_obj`: exact keys, nothing coerced, the top-level
        copies must agree with the nested values. Raises ``ValueError``."""
        if not isinstance(obj, Mapping):
            raise ValueError("core config must be a JSON object")
        if set(obj) != _CONFIG_KEYS:
            missing = sorted(_CONFIG_KEYS - set(obj))
            unknown = sorted(set(obj) - _CONFIG_KEYS)
            raise ValueError(f"core config keys: missing {missing}, unknown {unknown}")
        law = obj["law"]
        if not isinstance(law, str):
            raise ValueError("core config law must be a string")
        config = cls(
            mission=MissionConfig.from_obj(obj["mission"]),
            guidance=GuidanceConfig.from_obj(obj["guidance"]),
            camera=CameraModel.from_obj(obj["camera"]),
            link=LinkConfig.from_obj(obj["link"]),
            law=law,
        )
        mirrors = (
            ("coast_cap", config.mission.coast_cap),
            ("track_timeout_ms", config.mission.track_timeout_ms),
            ("gate", config.gate.value),
        )
        for key, nested in mirrors:
            val = obj[key]
            if isinstance(val, bool) or val != nested or type(val) is not type(nested):
                raise ValueError(f"core config {key!r} = {val!r} disagrees with {nested!r}")
        return config


# ---------------------------------------------------------------------------
# The core
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class CoreOutput:
    """What one input produced, in production order.

    The live process sends ``acks`` to the command's sender: for a CMD input
    ``acks[0]`` is its ack, and other inputs have none. ``rc_acks`` are the acks
    of the radio approves drained before this input ([F7]), each with its stamp
    (its sample's ``t_rx``); they have no sender and are only recorded. The
    live process publishes ``mission_states`` (each carries its stamp in
    ``t``; a drained approve's come first) and writes ``requests`` and then
    ``setpoint`` through fc_link. ``setpoint`` is ``None`` unless this input
    was a TICK with a setpoint step whose state sends one ([G10], [G11]).
    """

    acks: tuple[AckPacket, ...] = ()
    rc_acks: tuple[tuple[int, AckPacket], ...] = ()
    mission_states: tuple[MissionStatePacket, ...] = ()
    requests: tuple[FcRequest, ...] = ()
    setpoint: VelocityCommand | None = None


@dataclass
class _Out:
    acks: list[AckPacket] = field(default_factory=list)
    rc_acks: list[tuple[int, AckPacket]] = field(default_factory=list)
    mission_states: list[MissionStatePacket] = field(default_factory=list)
    requests: list[FcRequest] = field(default_factory=list)
    setpoint: VelocityCommand | None = None

    def freeze(self) -> CoreOutput:
        return CoreOutput(
            acks=tuple(self.acks),
            rc_acks=tuple(self.rc_acks),
            mission_states=tuple(self.mission_states),
            requests=tuple(self.requests),
            setpoint=self.setpoint,
        )


class CompanionCore:
    """Mission + guidance + vehicle state behind one input API ([C10], [R3]).

    ``vehicle`` is the :class:`VehicleState` the core reads: in a live process
    it is ``FcLink.state`` (fc_link ingests and records the frames); in replay
    and in tests without fc_link the core builds its own and frames come in
    through :meth:`on_mavlink_rx`. Use one route or the other, never both.

    With a ``recorder``, the constructor writes the ``meta`` line at
    ``t_start_ms``; build the core before anything else writes to that
    recorder, so ``meta`` is the first line ([R2]).
    """

    def __init__(
        self,
        config: CoreConfig,
        *,
        vehicle: VehicleState | None = None,
        recorder: Recorder | None = None,
        t_start_ms: int = 0,
    ) -> None:
        if vehicle is None:
            vehicle = VehicleState(config.link)
        elif vehicle.config != config.link:
            raise ValueError("the vehicle state's LinkConfig differs from the core config's")
        self.config = config
        self.vehicle = vehicle
        self.mission = Mission(config.mission)
        self.guidance = Guidance(
            config.camera, config.guidance, make_law(config.law, config.guidance)
        )
        self._recorder = recorder
        self._last_t: int | None = None
        if recorder is not None:
            recorder.meta(t_start_ms, config.to_obj())

    # -- inputs ([M2]) --------------------------------------------------------

    def on_track(self, pkt: TrackPacket, t_rx: int) -> CoreOutput:
        """TRK: mission first, then guidance on the view after it, then each
        guidance event (``commit``, ``hold_complete``) as its own GDE input.
        Pending radio approves are processed first ([F7])."""
        out = _Out()
        self._drain_approves(out)
        t = self._input_stamp(t_rx)
        if self._recorder is not None:
            self._recorder.packet(t, pkt)
        self.mission.on_track(pkt, t)
        self._after_input(t, out)
        for ev in self.guidance.on_track(pkt, t, self.mission.view(), self.vehicle):
            self._guidance_event(ev, out)
        return out.freeze()

    def on_command(self, cmd: CommandPacket, auth_ok: bool, t_rx: int) -> CoreOutput:
        """CMD from the ground (UI): the receiver authenticated it ([P5c]); the
        record keeps ``auth_ok`` and never the token. ``acks[0]`` is its ack.
        Pending radio approves are processed first ([F7])."""
        if not isinstance(auth_ok, bool):
            raise TypeError("auth_ok must be a bool")
        out = _Out()
        self._drain_approves(out)
        t = self._input_stamp(t_rx)
        if self._recorder is not None:
            self._recorder.command(t, cmd, auth_ok)
        out.acks.append(self._command(cmd, auth_ok, t, out, from_ground=True))
        return out.freeze()

    def on_ground_heartbeat(self, t_rx: int) -> CoreOutput:
        """HB: one UI state poll ([U6], [R2]). Pending radio approves are
        processed first ([F7])."""
        out = _Out()
        self._drain_approves(out)
        t = self._input_stamp(t_rx)
        if self._recorder is not None:
            self._recorder.ground_hb(t)
        self.mission.on_ground_heartbeat(t)
        self._after_input(t, out)
        return out.freeze()

    def on_tick(self, t_rx: int) -> CoreOutput:
        """TICK: snapshot, mission, then a setpoint step if due. Pending radio
        approves are processed first ([F7])."""
        out = _Out()
        self._drain_approves(out)
        t = self._input_stamp(t_rx)
        if self._recorder is not None:
            self._recorder.tick(t)
        snap = self.vehicle.snapshot(t)
        self.mission.on_tick(snap, t)
        self._after_input(t, out)
        if self.guidance.step_due(t):
            setpoint, events = self.guidance.step(t, self.mission.view(), self.vehicle, snap)
            out.setpoint = setpoint
            for ev in events:
                self._guidance_event(ev, out)
        return out.freeze()

    def on_mavlink_rx(self, raw: bytes, t_rx: int) -> list[Any]:
        """Frames from the FC, for replay and fc_link-free drivers: recorded
        (with a recorder) and ingested; returns the parsed messages. A live
        process with fc_link never calls this (fc_link records and ingests)."""
        msgs = parse_frames(raw)
        if self._recorder is not None:
            self._recorder.mavlink(t_rx, "rx", raw)
        for msg in msgs:
            self.vehicle.ingest(msg, t_rx)
        return msgs

    def on_malformed_command(self, data: bytes, t_rx: int) -> AckPacket | None:
        """[P5b]: a command datagram that failed decoding. It never reaches the
        mission and is not a heartbeat. When a ``cmd_id`` can be salvaged the
        ``rejected_malformed`` ack is recorded and returned; otherwise ``None``.
        The datagram itself is never recorded (it may carry the token)."""
        cmd_id = salvage_cmd_id(data)
        if cmd_id is None:
            return None
        ack = AckPacket(cmd_id=cmd_id, result=AckResult.REJECTED_MALFORMED)
        if self._recorder is not None:
            self._recorder.packet(t_rx, ack)
        return ack

    # -- internals ------------------------------------------------------------

    def _input_stamp(self, t_rx: int) -> int:
        if isinstance(t_rx, bool) or not isinstance(t_rx, int) or t_rx < 0:
            raise ValueError(f"an input stamp is board ms, an int >= 0 ([C1]); got {t_rx!r}")
        if self._last_t is not None and t_rx < self._last_t:
            raise ValueError(
                f"input stamps must not decrease ({t_rx} < {self._last_t}); the recording's "
                "file order is the input order ([M2], [R3])"
            )
        self._last_t = t_rx
        return t_rx

    def _drain_approves(self, out: _Out) -> None:
        """[F7]: each radio approve detected since the previous input, as its own
        CMD input, in sample order, before the input being processed."""
        for t_sample in self.vehicle.take_approvals():
            # [M2]: the approve's stamp is its RC_CHANNELS rx record's t_rx.
            t = self._input_stamp(t_sample)
            # An in-process approve_engage, authenticated by its source. Its ack
            # is recorded; it is not a command record (replay re-derives it from
            # the mavlink rx record).
            approve = CommandPacket(
                cmd_id=rc_approve_cmd_id(t_sample),
                token=REDACTED_TOKEN,
                command=CommandName.APPROVE_ENGAGE,
            )
            out.rc_acks.append((t, self._command(approve, True, t, out, from_ground=False)))

    def _command(
        self, cmd: CommandPacket, auth_ok: bool, t: int, out: _Out, *, from_ground: bool
    ) -> AckPacket:
        ack = self.mission.on_command(cmd, auth_ok, t, from_ground=from_ground)
        if self._recorder is not None:
            self._recorder.packet(t, ack)
        self._after_input(t, out)
        return ack

    def _guidance_event(self, ev: GuidanceEvent, out: _Out) -> None:
        self.mission.on_guidance(ev)  # GDE: its own input, stamped ev.t_ms [M2]
        self._after_input(ev.t_ms, out)

    def _after_input(self, t: int, out: _Out) -> None:
        pkt = self.mission.maybe_publish(t)
        if pkt is not None:
            if self._recorder is not None:
                self._recorder.packet(t, pkt)
            out.mission_states.append(pkt)
        out.requests.extend(self.mission.take_requests())


# ---------------------------------------------------------------------------
# Replay ([R3])
# ---------------------------------------------------------------------------


def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def setpoint_key(cmd: VelocityCommand, link: LinkConfig) -> SetpointKey:
    """[R3] key of the setpoint fc_link writes for ``cmd``: the [F6] limits
    applied (fc_link's own function), frame ``LOCAL_NED``, mask 1479, float32."""
    c = clamp_velocity(cmd, link)
    return (
        mavlink2.MAV_FRAME_LOCAL_NED,
        TYPE_MASK_VELOCITY_YAW_RATE,
        _f32(c.vn),
        _f32(c.ve),
        _f32(c.vd),
        _f32(c.yaw_rate),
    )


def recorded_setpoint_keys(raw: bytes) -> list[SetpointKey]:
    """[R3] keys of the velocity setpoints in one ``mavlink`` tx record."""
    return [
        (
            int(m.coordinate_frame),
            int(m.type_mask),
            _f32(m.vx),
            _f32(m.vy),
            _f32(m.vz),
            _f32(m.yaw_rate),
        )
        for m in parse_frames(raw)
        if m.get_type() == _SETPOINT_MSG
    ]


@dataclass(frozen=True, kw_only=True)
class ReplayResult:
    """What a fresh core produced from a recording's inputs, beside what the
    recording says was produced ([R3]). Pairs are ``(t_rx, packet)``.

    ``recorded_acks`` leaves out ``rejected_malformed`` acks (they have no
    input record, [P5b]); ``skipped_malformed`` counts them.
    """

    config: CoreConfig
    mission_states: tuple[tuple[int, MissionStatePacket], ...]
    acks: tuple[tuple[int, AckPacket], ...]
    setpoints: tuple[SetpointKey, ...]
    recorded_mission_states: tuple[tuple[int, MissionStatePacket], ...]
    recorded_acks: tuple[tuple[int, AckPacket], ...]
    recorded_setpoints: tuple[SetpointKey, ...]
    skipped_malformed: int
    transitions: tuple[TransitionRecord, ...]

    @property
    def matches(self) -> bool:
        """[R3]: mission states, acks, and setpoints all reproduced exactly."""
        return self.mismatch() is None

    def mismatch(self) -> str | None:
        """The first difference, described; ``None`` when replay matches."""
        for name, got, want in (
            ("mission_state", self.mission_states, self.recorded_mission_states),
            ("ack", self.acks, self.recorded_acks),
            ("setpoint", self.setpoints, self.recorded_setpoints),
        ):
            for i, (g, w) in enumerate(zip(got, want, strict=False)):  # lengths next
                if g != w:
                    return f"{name} #{i}: replay {g!r} != recorded {w!r}"
            if len(got) != len(want):
                return f"{name}: replay produced {len(got)}, the recording has {len(want)}"
        return None


class _WriteModel:
    """The two facts of fc_link's setpoint write path a recording carries:
    the gate setting ([F5], ``meta.config``) and the SITL proof ([F1] (b), an
    rx frame from the FC). A link drop and reconnect is not in the recording."""

    def __init__(self, link: LinkConfig) -> None:
        self._link = link
        self._proven = False

    def observe(self, msgs: Iterable[Any]) -> None:
        for m in msgs:
            if (
                m.get_msgId() in SITL_PROOF_MSG_IDS
                and m.get_srcSystem() == self._link.fc_sysid
                and m.get_srcComponent() == self._link.fc_compid
            ):
                self._proven = True

    def writes(self) -> bool:
        return self._link.setpoints_enabled is True and self._proven


def _open(source: str | Path | Iterable[str]) -> tuple[CoreConfig, Iterator[Record]]:
    records = iter(read_records(source))
    first = next(records, None)
    if first is None or first.stream is not Stream.META or first.config is None:
        raise RecordingError("a recording starts with its meta record ([R2])")
    try:
        config = CoreConfig.from_obj(first.config)
    except ValueError as exc:
        raise RecordingError(f"meta.config: {exc}") from exc
    return config, records


def replay(source: str | Path | Iterable[str]) -> ReplayResult:
    """[R3]: feed a recording's input records, in file order, into a fresh core
    built from its ``meta.config``; collect what it produces.

    Inputs are ``track``, ``command``, ``mavlink`` rx, ``tick`` and
    ``ground_hb``. ``mission_state``, ``ack`` and ``mavlink`` tx records are
    what the run produced and are collected for comparison; ``detection`` and
    ``fc_link_health`` records are not core inputs and are skipped.
    """
    config, records = _open(source)
    core = CompanionCore(config)
    writes = _WriteModel(config.link)
    states: list[tuple[int, MissionStatePacket]] = []
    acks: list[tuple[int, AckPacket]] = []
    setpoints: list[SetpointKey] = []
    rec_states: list[tuple[int, MissionStatePacket]] = []
    rec_acks: list[tuple[int, AckPacket]] = []
    rec_setpoints: list[SetpointKey] = []
    skipped = 0
    for r in records:
        stream, t = r.stream, r.t_rx
        if stream is Stream.MAVLINK:
            assert r.raw is not None
            if r.direction == "rx":
                writes.observe(core.on_mavlink_rx(r.raw, t))
            else:
                rec_setpoints.extend(recorded_setpoint_keys(r.raw))
            continue
        if stream is Stream.MISSION_STATE:
            assert isinstance(r.packet, MissionStatePacket)
            rec_states.append((t, r.packet))
            continue
        if stream is Stream.ACK:
            assert isinstance(r.packet, AckPacket)
            if r.packet.result is AckResult.REJECTED_MALFORMED:
                skipped += 1
            else:
                rec_acks.append((t, r.packet))
            continue
        if stream is Stream.TRACK:
            assert isinstance(r.packet, TrackPacket)
            out = core.on_track(r.packet, t)
        elif stream is Stream.COMMAND:
            assert isinstance(r.packet, CommandPacket) and r.auth_ok is not None
            out = core.on_command(r.packet, r.auth_ok, t)
        elif stream is Stream.TICK:
            out = core.on_tick(t)
        elif stream is Stream.GROUND_HB:
            out = core.on_ground_heartbeat(t)
        elif stream is Stream.META:
            raise RecordingError("a recording has exactly one meta record ([R2])")
        else:
            continue  # detection, fc_link_health: not core inputs
        # Each output carries the stamp of the input that caused it ([R2]): a
        # drained radio approve's is its sample's, not this record's.
        states.extend((pkt.t, pkt) for pkt in out.mission_states)
        acks.extend(out.rc_acks)
        acks.extend((t, ack) for ack in out.acks)
        if out.setpoint is not None and writes.writes():
            setpoints.append(setpoint_key(out.setpoint, config.link))
    return ReplayResult(
        config=config,
        mission_states=tuple(states),
        acks=tuple(acks),
        setpoints=tuple(setpoints),
        recorded_mission_states=tuple(rec_states),
        recorded_acks=tuple(rec_acks),
        recorded_setpoints=tuple(rec_setpoints),
        skipped_malformed=skipped,
        transitions=tuple(core.mission.transition_log),
    )


# ---------------------------------------------------------------------------
# Miss vector offline ([R4])
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class OfflineMiss:
    """One commit's [G6] miss vector recomputed from a recording ([R4]).

    ``logged_m`` is the ``miss:<x>:<y>:<z>`` event that followed the commit
    (``%.3f``), or ``None`` if the recording has none.
    """

    t: int  # stamp of the commit event
    track_id: int
    t_cap: int
    target_width_m: float
    vector: MissVector
    logged_m: tuple[float, float, float] | None


def _event_fields(name: str, prefix: str, n: int) -> list[str]:
    parts = name.split(":")
    if parts[0] != prefix or len(parts) != n + 1:
        raise RecordingError(f"malformed {prefix}: event {name!r}")
    return parts[1:]


def miss_vectors_from_recording(source: str | Path | Iterable[str]) -> list[OfflineMiss]:
    """[R4]: for each ``commit:<track_id>:<t_cap>`` event, the miss vector from
    the matching ``track`` record, ``target_width_m`` of the last accepted
    ``prime`` command record before the event, and the camera in
    ``meta.config``. The guidance config there drives the [G2] fallback.

    A command record executes only if it is the first authenticated record of
    its ``cmd_id`` ([P5a]): a later authenticated record with that id is a true
    retry (acked with the stored ``accepted``) or a duplicate id, and executes
    nothing, so an older prime re-sent after a re-prime never becomes the trial
    in force. Unauthenticated records are not stored and do not count.
    """
    config, records = _open(source)
    tracks: dict[tuple[int, int], TrackPacket] = {}
    seen: set[str] = set()  # [P5a]: authenticated cmd_ids already received
    prime: CommandPacket | None = None  # the newest prime command record that can execute
    width: float | None = None
    out: list[OfflineMiss] = []
    for r in records:
        pkt = r.packet
        if isinstance(pkt, TrackPacket):
            tracks[(pkt.track_id, pkt.t_cap)] = pkt
        elif isinstance(pkt, CommandPacket):
            authenticated = r.auth_ok is True
            retry = authenticated and pkt.cmd_id in seen  # executes nothing
            if authenticated:
                seen.add(pkt.cmd_id)
            if pkt.command is CommandName.PRIME:
                prime = None if retry else pkt
        elif isinstance(pkt, AckPacket):
            # The core writes a command's ack right after its command record.
            if prime is not None and pkt.cmd_id == prime.cmd_id:
                if pkt.result is AckResult.ACCEPTED:
                    width = PrimeParams.from_obj(prime.params).target_width_m
        elif isinstance(pkt, MissionStatePacket):
            events = pkt.events
            for i, ev in enumerate(events):
                if not ev.name.startswith("commit:"):
                    continue
                track_id, t_cap = (int(x) for x in _event_fields(ev.name, "commit", 2))
                track = tracks.get((track_id, t_cap))
                if track is None:
                    raise RecordingError(f"no track record for {ev.name!r} ([R4])")
                if width is None:
                    raise RecordingError(f"no accepted prime before {ev.name!r} ([R4])")
                logged = None
                if i + 1 < len(events) and events[i + 1].name.startswith("miss:"):
                    x, y, z = (float(v) for v in _event_fields(events[i + 1].name, "miss", 3))
                    logged = (x, y, z)
                out.append(
                    OfflineMiss(
                        t=ev.t,
                        track_id=track_id,
                        t_cap=t_cap,
                        target_width_m=width,
                        vector=miss_vector(track, config.camera, width, config.guidance),
                        logged_m=logged,
                    )
                )
    return out
