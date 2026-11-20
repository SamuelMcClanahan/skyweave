"""The closed loop ([S0]): world -> camera -> tracker -> core -> fc_link -> SITL -> world.

One run is one SITL process and one companion core::

    truth objects --camera_sim--> detection --Tracker--> track --CompanionCore-->
    requests + setpoints --FcLink (gate enabled)--> relay --> SITL SERIAL1
    SITL SERIAL0 (the harness's own link) --SIM_STATE--> vehicle truth --> camera

Clock ([S0], [C1]). The injected board clock is SITL boot time: the newest
``time_boot_ms`` seen in any FC frame, on SERIAL0 (the harness link) or on
SERIAL1 (read by the relay before fc_link gets the bytes, so a frame is never
stamped before SITL produced it). Every core input and every record carries
that clock, so a run's recording lives entirely on its own SITL time no
matter how many SITL instances run in parallel or at what speedup. Inputs are
processed in stamp order; a detection is delivered only once SERIAL0 truth
covers its capture time, so the camera never extrapolates.

Truth ([S0]). ``SIM_STATE`` (108) on SERIAL0, never on the companion's link.
ArduPilot sends it in the same stream bucket right after ``ATTITUDE`` (both
at the same interval), so each sample is stamped with that ``ATTITUDE``'s
``time_boot_ms`` (checked by experiment: strict alternation). Positions come
from ``lat_int``/``lon_int`` (1e-7 deg; the float fields carry too little
precision) with ArduPilot's own flat-earth scaling, relative to the SITL home.

The SERIAL0 link also plays the radio: RC override of the mode switch
(channel 5, ``FLTMODE_*`` set by the SITL defaults file to MANUAL / GUIDED /
RTL) and of the approve channel (held low). That is the test environment,
not companion code: the companion's only link is fc_link, which never sends
RC overrides or parameter writes.

The relay is loopback TCP between fc_link and SITL SERIAL1 (fc_link's [F1]
interlock still applies: the endpoint is ``127.0.0.1`` and nothing is
written before SITL's SIMSTATE proof). It lets S4 black out the FC link the
way a cut serial line does: fc_link keeps writing (its ``mavlink`` tx
records exist) and every byte in both directions is dropped. The connection
never closes, so the [F1] proof does not reset and [R3] replay stays exact.

Determinism: the world, the camera noise, the human's command ids and timing
rules come from the seed; closed-loop SITL is not bit-reproducible ([S0]), so
each run's own recording is checked by [R3] replay instead. Wall-clock reads
here are process-control timeouts only and never reach a scored output.
"""

from __future__ import annotations

import bisect
import json
import math
import select
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.camera import wrap_pi
from skyweave2.drone.core import CompanionCore, CoreConfig, CoreOutput, replay
from skyweave2.drone.fc_link import LOOPBACK_HOST, FcLink
from skyweave2.drone.ground_ui import CommandReceiver
from skyweave2.drone.harness.camera_sim import FrameTruth, Pose, SyntheticCamera
from skyweave2.drone.harness.scenarios import (
    APPROVE_IDLE_PWM,
    FLTMODE_PARAMS,
    SWITCH_PWM,
    DropoutWindow,
    Script,
    SendCommand,
    SetBlackout,
    SetSwitch,
    World,
    build_world,
)
from skyweave2.drone.harness.scorecard import (
    END_LANDED,
    RunTrace,
    Thresholds,
    TruthSample,
    score_run,
    scorecard_json,
)
from skyweave2.drone.harness.seeds import SeedSet, check_seed
from skyweave2.drone.packets import CommandName, CommandPacket, encode
from skyweave2.drone.recording import Recorder, read_records
from skyweave2.drone.sitl import DEFAULT_HOME, SitlInstance, SitlPaths, instance_ports
from skyweave2.drone.tracker import Tracker, TrackerConfig, id_base_from_start
from skyweave2.drone.vehicle_state import LinkConfig

BACKEND = "ardupilot-copter-4.7.0-sitl"

TICK_MS = 50
"""Contract §9 ``tick_hz`` = 20 (E1): one mission-loop tick per 50 ms."""

UI_POLL_MS = 1_000
"""The ground UI's state poll period, each a ground heartbeat ([U6]);
Provisional (E1 harness), well inside ``ground_link_timeout_ms``."""

TRUTH_HZ = 50.0
"""SERIAL0 SIM_STATE and ATTITUDE rate (truth and its stamp; Provisional, E1)."""

TRUTH_STALL_MS = 1_000  # no SIM_STATE for this long: request the truth streams again
RC_REFRESH_MS = 200  # RC overrides lapse after RC_OVERRIDE_TIME (3 s) without a refresh
GCS_HEARTBEAT_MS = 1_000
SIM_TIMEOUT_MS = 300_000  # sim time after the core starts; covers a 180 s flight budget
LATLON_TO_M = 0.011131884502145034
"""ArduPilot ``LOCATION_SCALING_FACTOR``: metres per 1e-7 degree of latitude."""

UI_TOKEN = "harness-ui-token"
"""The shared UI token the harness's command receiver (``ground_ui.CommandReceiver``)
checks ([P5c]). Test environment only; the core records the authentication
result, never this."""

_PREARM_BIT = mavlink2.MAV_SYS_STATUS_PREARM_CHECK
_EKF_GPS_TEXTS = ("EKF3 IMU0 is using GPS", "EKF3 IMU1 is using GPS")


class StartupError(RuntimeError):
    """SITL started but never streamed truth: process control, retried once."""


# ---------------------------------------------------------------------------
# Truth from SIM_STATE
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Home:
    """The SITL home (``--home lat,lng,alt,heading``) as integers ArduPilot uses."""

    lat_e7: int
    lon_e7: int
    alt_m: float

    @classmethod
    def parse(cls, home: str) -> Home:
        lat, lon, alt, _heading = (float(x) for x in home.split(","))
        return cls(lat_e7=round(lat * 1e7), lon_e7=round(lon * 1e7), alt_m=alt)


def _longitude_scale(lat_e7: float) -> float:
    """ArduPilot ``Location::longitude_scale``: cos(lat), floored at 0.01."""
    return max(math.cos(math.radians(lat_e7 * 1e-7)), 0.01)


def ned_from_global(
    lat_e7: int, lon_e7: int, alt_m: float, home: Home
) -> tuple[float, float, float]:
    """NED metres of a global position relative to ``home`` (ArduPilot's
    ``get_distance_NE`` flat-earth scaling; ``D`` is minus height above home)."""
    d_lon = lon_e7 - home.lon_e7
    if d_lon > 1_800_000_000:
        d_lon -= 3_600_000_000
    elif d_lon < -1_800_000_000:
        d_lon += 3_600_000_000
    n = (lat_e7 - home.lat_e7) * LATLON_TO_M
    e = d_lon * LATLON_TO_M * _longitude_scale((lat_e7 + home.lat_e7) / 2.0)
    return (float(n), float(e), float(-(alt_m - home.alt_m)))


def truth_from_sim_state(msg: Any, home: Home) -> tuple[Pose, tuple[float, float, float]]:
    """Vehicle truth from one ``SIM_STATE``: pose (NED from home, true attitude)
    and true NED velocity."""
    pos = ned_from_global(int(msg.lat_int), int(msg.lon_int), float(msg.alt), home)
    pose = Pose(pos_ned=pos, roll=float(msg.roll), pitch=float(msg.pitch), yaw=float(msg.yaw))
    return pose, (float(msg.vn), float(msg.ve), float(msg.vd))


class TruthBuffer:
    """Vehicle truth samples by SITL ms; linear interpolation, hold at the ends."""

    def __init__(self) -> None:
        self.t: list[int] = []
        self.poses: list[Pose] = []
        self.vels: list[tuple[float, float, float]] = []

    def add(self, t_ms: int, pose: Pose, vel: tuple[float, float, float]) -> None:
        if self.t and t_ms < self.t[-1]:
            raise ValueError(f"truth time went back ({t_ms} < {self.t[-1]})")
        if self.t and t_ms == self.t[-1]:
            self.poses[-1], self.vels[-1] = pose, vel
            return
        self.t.append(t_ms)
        self.poses.append(pose)
        self.vels.append(vel)

    @property
    def newest_t(self) -> int | None:
        return self.t[-1] if self.t else None

    def pose_at(self, t_ms: int) -> Pose:
        if not self.t:
            raise ValueError("no truth yet")
        i = bisect.bisect_right(self.t, t_ms)
        if i == 0:
            return self.poses[0]
        if i == len(self.t):
            return self.poses[-1]
        a, b = self.poses[i - 1], self.poses[i]
        f = (t_ms - self.t[i - 1]) / (self.t[i] - self.t[i - 1])
        pos = tuple(pa + f * (pb - pa) for pa, pb in zip(a.pos_ned, b.pos_ned, strict=True))
        return Pose(
            pos_ned=(pos[0], pos[1], pos[2]),
            roll=a.roll + f * (b.roll - a.roll),
            pitch=a.pitch + f * (b.pitch - a.pitch),
            yaw=wrap_pi(a.yaw + f * wrap_pi(b.yaw - a.yaw)),
        )


# ---------------------------------------------------------------------------
# Relay between fc_link and SITL SERIAL1
# ---------------------------------------------------------------------------


def _drain(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            data = sock.recv(65536, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            break
        if not data:
            raise ConnectionError("relay peer closed")
        chunks.append(data)
    return b"".join(chunks)


class LinkRelay:
    """Loopback TCP relay, fc_link <-> SITL SERIAL1, with a blackout switch.

    It also reads the FC's ``time_boot_ms`` from the SITL-side bytes so the
    harness clock is never behind a frame it hands to fc_link.
    """

    def __init__(self, upstream_port: int, *, fc_ids: tuple[int, int], timeout_s: float) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind((LOOPBACK_HOST, 0))
        self._listener.listen(1)
        self.port = int(self._listener.getsockname()[1])
        self._up = socket.create_connection((LOOPBACK_HOST, upstream_port), timeout=timeout_s)
        self._up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._up.settimeout(None)  # blocking: recv uses MSG_DONTWAIT, sendall may wait
        self._down: socket.socket | None = None
        self._fc_ids = fc_ids
        self._parser = mavlink2.MAVLink(None)
        self._parser.robust_parsing = True
        self._timeout_s = timeout_s
        self.blackout = False
        self.newest_boot_ms: int | None = None
        self.dropped_bytes_to_fc = 0
        self.dropped_bytes_to_companion = 0

    @property
    def endpoint(self) -> str:
        return f"tcp:{LOOPBACK_HOST}:{self.port}"

    def upstream_fileno(self) -> int:
        return self._up.fileno()

    def accept(self) -> None:
        self._listener.settimeout(self._timeout_s)
        down, _ = self._listener.accept()
        down.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        down.settimeout(None)
        self._down = down

    def pump(self) -> None:
        """Move every pending byte both ways (or drop it during a blackout)."""
        data = _drain(self._up)
        if data:
            for msg in self._parser.parse_buffer(data) or []:
                if msg.get_type() == "BAD_DATA":
                    continue
                if (msg.get_srcSystem(), msg.get_srcComponent()) != self._fc_ids:
                    continue
                boot = getattr(msg, "time_boot_ms", None)
                if boot is not None and (self.newest_boot_ms is None or boot > self.newest_boot_ms):
                    self.newest_boot_ms = int(boot)
            if self.blackout or self._down is None:
                self.dropped_bytes_to_companion += len(data)
            else:
                self._down.sendall(data)
        if self._down is None:
            return
        data = _drain(self._down)
        if data:
            if self.blackout:
                self.dropped_bytes_to_fc += len(data)
            else:
                self._up.sendall(data)

    def close(self) -> None:
        for s in (self._down, self._up, self._listener):
            if s is not None:
                s.close()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RunSpec:
    """One closed-loop run: a scenario, a seed of a named set, a law."""

    scenario: str
    seed: int
    seed_set: SeedSet
    law: str = "pure_pursuit"
    speedup: float = 1.0
    jobs: int = 1  # runs in parallel with this one (``batch.run_plan`` stamps it; DT-3)
    instance: int = 0
    out_dir: Path
    sitl_paths: SitlPaths
    versions: Mapping[str, str] = field(default_factory=dict)
    sim_timeout_ms: int = SIM_TIMEOUT_MS
    home: str = DEFAULT_HOME


@dataclass(frozen=True, kw_only=True)
class RunResult:
    spec: RunSpec
    scorecard: dict[str, Any]
    replay_ok: bool
    replay_mismatch: str | None
    end_reason: str
    recording: Path
    scorecard_path: Path


def core_config(law: str) -> CoreConfig:
    """The companion configuration under test: contract §9 defaults, the law
    named on the command line, and the setpoint gate enabled (SITL only, [F5])."""
    return CoreConfig(link=LinkConfig(setpoints_enabled=True), law=law)


def backend_obj(spec: RunSpec) -> dict[str, Any]:
    """The scorecard's ``backend``: the SITL, and how it ran (DT-3). The speedup
    turns the harness's wall-clock processing time into sim time, and the
    parallel jobs share the CPU, so both shape the scored timing."""
    return {"name": BACKEND, "speedup": float(spec.speedup), "jobs": int(spec.jobs)}


class ClosedLoop:
    """One run of the [S0] loop against a fresh SITL instance."""

    def __init__(self, spec: RunSpec) -> None:
        check_seed(spec.seed_set, spec.scenario, spec.seed)
        self.spec = spec
        self.config = core_config(spec.law)
        self.world: World = build_world(spec.scenario, spec.seed)
        self.script = Script(
            self.world,
            approve_settle_ms=self.config.mission.approve_settle_ms,
            camera_latency_ms=math.ceil(self.world.camera.latency_ms),
        )
        self.home = Home.parse(spec.home)
        self.fc_ids = (self.config.link.fc_sysid, self.config.link.fc_compid)
        self.now = 0  # the injected board clock (SITL ms)
        self.truth = TruthBuffer()
        self.truth_samples: list[TruthSample] = []
        self.frame_truths: list[FrameTruth] = []
        self.camera: SyntheticCamera | None = None
        self.tracker: Tracker | None = None
        self._frame_k = 0
        self._dropouts_applied = 0
        self._switch = self.script.initial_switch
        self._s0_boot: int | None = None  # newest time_boot_ms on SERIAL0
        self._ekf_gps: set[str] = set()
        self._prearm_ok = False
        self._ready_seen = False
        self._next_tick = 0
        self._next_poll = 0
        self._next_rc = 0
        self._next_gcs_hb = 0
        self.end_reason = "running"
        self.counters: dict[str, int] = {
            "frames": 0,
            "commands": 0,
            "degraded_ticks_flying": 0,  # [F3] degraded at a tick in SEARCH..LOST
            "truth_rerequests": 0,
        }
        self._tick_lag: list[int] = []  # sim ms between a tick's stamp and its processing
        self._ui_out: list[CoreOutput] = []  # what the command receiver forwarded

    # -- clock ----------------------------------------------------------------

    def clock(self) -> int:
        return self.now

    def _sim_now(self) -> int | None:
        times = [t for t in (self._s0_boot, self.relay.newest_boot_ms) if t is not None]
        return max(times) if times else None

    # -- SERIAL0 (pilot / radio / truth) --------------------------------------

    def _poll_pilot(self) -> None:
        for msg in self.pilot.poll():
            if (msg.get_srcSystem(), msg.get_srcComponent()) != self.fc_ids:
                continue
            kind = msg.get_type()
            boot = getattr(msg, "time_boot_ms", None)
            if boot is not None and (self._s0_boot is None or boot > self._s0_boot):
                self._s0_boot = int(boot)
            if kind == "SIM_STATE" and self._s0_boot is not None:
                pose, vel = truth_from_sim_state(msg, self.home)
                self.truth.add(self._s0_boot, pose, vel)
                if self.camera is not None:
                    self._truth_sample(self._s0_boot, pose, vel)
            elif kind == "STATUSTEXT":
                for text in _EKF_GPS_TEXTS:
                    if text in msg.text:
                        self._ekf_gps.add(text)
            elif kind == "SYS_STATUS":
                self._prearm_ok = bool(msg.onboard_control_sensors_health & _PREARM_BIT)

    def _truth_sample(self, t: int, pose: Pose, vel: tuple[float, float, float]) -> None:
        objects = {}
        for obj in self.world.scene:
            if obj.present(t) or obj.name == self.world.target:
                p = obj.position(t)
                objects[obj.name] = (float(p[0]), float(p[1]), float(p[2]))
        self.truth_samples.append(TruthSample(t_ms=t, pose=pose, vel_ned=vel, objects=objects))

    def _send_rc(self) -> None:
        vals = [65535] * 8 + [0] * 10  # ch1-8: 65535 = no override; ch9-18: 0 = no override
        vals[4] = SWITCH_PWM[self._switch]  # channel 5: FLTMODE_CH
        vals[self.config.link.approve_channel - 1] = APPROVE_IDLE_PWM
        self.pilot.send(
            mavlink2.MAVLink_rc_channels_override_message(self.fc_ids[0], self.fc_ids[1], *vals)
        )

    def _pilot_periodic(self, sim_now: int) -> None:
        newest = self.truth.newest_t
        if newest is not None and sim_now - newest > TRUTH_STALL_MS and sim_now >= self._next_rc:
            self._request_truth()  # SIM_STATE stopped: frames wait on truth, so ask again
            self.counters["truth_rerequests"] += 1
        if sim_now >= self._next_rc:
            self._send_rc()
            self._next_rc = sim_now + RC_REFRESH_MS
        if sim_now >= self._next_gcs_hb:
            self.pilot.heartbeat()
            self._next_gcs_hb = sim_now + GCS_HEARTBEAT_MS

    def _check_ready(self, sim_now: int) -> None:
        if self._ready_seen:
            return
        snap = self.fc.state.snapshot(sim_now)
        if (
            self.fc.sitl_proven
            and len(self._ekf_gps) == len(_EKF_GPS_TEXTS)
            and self._prearm_ok
            and snap.armed is False
            and snap.on_ground
        ):
            self._ready_seen = True
            self.script.on_ready(sim_now)

    # -- core inputs ----------------------------------------------------------

    def _apply(self, out: CoreOutput, t: int) -> None:
        """Hand one input's outputs on: the UI sees packets and acks; fc_link
        writes requests, then the setpoint (CoreOutput's documented order)."""
        self.script.on_output(t, out.mission_states, out.acks)
        for req in out.requests:
            self.fc.request(req)
        if out.setpoint is not None:
            self.fc.send_velocity(out.setpoint)

    def _start_camera(self, t: int) -> None:
        self.camera = SyntheticCamera(self.world.camera, seed=self.spec.seed, t0_ms=t)
        self.tracker = Tracker(
            TrackerConfig(id_base=id_base_from_start(t), coast_cap=self.config.mission.coast_cap)
        )
        self._frame_k = 0

    def _apply_dropouts(self) -> None:
        if len(self.script.dropouts) == self._dropouts_applied or self.camera is None:
            return
        windows = tuple((w.start_ms, w.end_ms) for w in self.script.dropouts)
        new_cfg = replace(self.camera.config, dropout_windows_ms=windows)
        self.camera = SyntheticCamera(new_cfg, seed=self.spec.seed, t0_ms=self.camera.t0_ms)
        self._dropouts_applied = len(self.script.dropouts)

    def _next_frame(self) -> tuple[int, int] | None:
        """(delivery time, capture time) of the next frame."""
        if self.camera is None:
            return None
        t_cap = self.camera.capture_time(self._frame_k)
        return self.camera.deliver_time(t_cap), t_cap

    def _deliver_frame(self, t: int) -> None:
        assert self.camera is not None and self.tracker is not None
        self._apply_dropouts()
        k = self._frame_k
        t_cap = self.camera.capture_time(k)
        frame = self.camera.capture(k, self.truth.pose_at(t_cap), self.world.scene)
        self._frame_k += 1
        self.frame_truths.append(frame.truth)
        self.recorder.packet(t, frame.packet)  # E1-F10: the harness records detections
        self.counters["frames"] += 1
        for pkt in self.tracker.update(frame.packet):
            self._apply(self.core.on_track(pkt, t), t)

    def _start_core(self) -> None:
        """The companion core on fc_link's vehicle state, and the UI's command
        receiver in front of it: the one path from a command's bytes to the core
        ([P5b], [P5c]; contract §8 "through the command path")."""
        self.core = CompanionCore(
            self.config, vehicle=self.fc.state, recorder=self.recorder, t_start_ms=self.now
        )
        self.ui = CommandReceiver(self.core, UI_TOKEN, self._ui_out.append)

    def _send_command(self, action: SendCommand, t: int) -> None:
        """CC-5: the scripted human's command goes over the wire form, encoded and
        then decoded, prefix-checked, and authenticated by the receiver; the UI
        shows the receiver's ack."""
        cmd = CommandPacket(
            cmd_id=action.cmd_id, token=UI_TOKEN, command=action.command, params=action.params
        )
        ack = self.ui.receive(encode(cmd), t)
        if ack is None:  # pragma: no cover - encode() output always carries its cmd_id
            raise RuntimeError(f"scripted command {action.cmd_id} was not acked")
        forwarded = list(self._ui_out)
        self._ui_out.clear()  # in place: the receiver holds this list's append
        # A rejected_malformed command never reaches the core, so nothing is forwarded.
        out = forwarded[0] if forwarded else CoreOutput()
        self.counters["commands"] += 1
        if action.command is CommandName.PRIME and self.camera is None:
            self._start_camera(t)  # the trial starts: percepd's output is scored from here
        self._apply(replace(out, acks=(ack,)), t)

    def _do_action(self, action: Any, t: int) -> None:
        if isinstance(action, SendCommand):
            self._send_command(action, t)
        elif isinstance(action, SetSwitch):
            self._switch = action.position
            self._send_rc()
        elif isinstance(action, SetBlackout):
            self.relay.blackout = action.on
        else:  # pragma: no cover - the Action union is closed
            raise TypeError(f"unknown action {action!r}")

    def _run_due(self, horizon: int) -> bool:
        """Process every input stamped <= ``horizon`` in stamp order.

        Returns ``False`` when a frame is due but SERIAL0 truth does not yet
        cover its capture time (the loop waits for more truth).
        """
        while True:
            cands: list[tuple[int, int]] = [(self._next_tick, 3), (self._next_poll, 2)]
            nf = self._next_frame()
            if nf is not None:
                cands.append((nf[0], 0))
            t_act = self.script.next_time()
            if t_act is not None:
                cands.append((t_act, 1))
            t, kind = min(cands)
            if t > horizon:
                return True
            if kind == 0:
                newest = self.truth.newest_t
                assert nf is not None
                if newest is None or newest < nf[1]:
                    return False
            self.now = t
            if kind == 0:
                self._deliver_frame(t)
            elif kind == 1:
                for action in self.script.pop_due(t):
                    self._do_action(action, t)
            elif kind == 2:
                self._apply(self.core.on_ground_heartbeat(t), t)
                self._next_poll = t + UI_POLL_MS
            else:
                self._tick_lag.append(horizon - t)
                if self.script.flying() and self.fc.state.attitude_degraded(t):
                    self.counters["degraded_ticks_flying"] += 1
                self._apply(self.core.on_tick(t), t)
                self._next_tick = t + TICK_MS

    # -- the run --------------------------------------------------------------

    def run(self) -> RunResult:
        spec = self.spec
        run_dir = spec.out_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        rec_path = run_dir / "recording.jsonl"
        extra = dict(FLTMODE_PARAMS)
        sitl = SitlInstance(
            spec.sitl_paths, instance=spec.instance, speedup=spec.speedup, extra_params=extra
        )
        wall_limit = spec.sim_timeout_ms / 1000.0 / spec.speedup * 2.0 + 120.0
        self.relay = None  # type: ignore[assignment]
        self.recorder = Recorder(rec_path)
        try:
            self.pilot = sitl.start(timeout_s=60.0)
            self._wait_for_truth()
            self.relay = LinkRelay(
                instance_ports(spec.instance)[1], fc_ids=self.fc_ids, timeout_s=10.0
            )
            self.now = self._s0_boot
            self.fc = FcLink(self.relay.endpoint, self.clock, self.config.link, self.recorder)
            self._start_core()
            self.fc.connect(timeout_s=10.0)
            self.relay.accept()
            self._next_tick = self._next_poll = self.now
            self._loop(wall_limit)
        except Exception as exc:  # noqa: BLE001 - the run's verdict carries it
            self.end_reason = f"error: {type(exc).__name__}: {exc}"
        finally:
            try:
                if getattr(self, "fc", None) is not None:
                    self.fc.close()
                if self.relay is not None:
                    self.relay.close()
            finally:
                sitl.stop()
                self.recorder.close()
        return self._score(rec_path)

    def _request_truth(self) -> None:
        self._send_rc()
        for msg_id in (mavlink2.MAVLINK_MSG_ID_ATTITUDE, mavlink2.MAVLINK_MSG_ID_SIM_STATE):
            self.pilot.request_interval(msg_id, TRUTH_HZ)
        self.pilot.request_interval(mavlink2.MAVLINK_MSG_ID_SYS_STATUS, 2.0)

    def _wait_for_truth(self) -> None:
        """Stream truth on SERIAL0 and wait for the first SITL time (the meta
        stamp). SITL can drop commands that arrive while it is still booting,
        so the requests repeat until SIM_STATE flows."""
        deadline = time.monotonic() + 30.0
        next_request = 0.0
        while self._s0_boot is None or self.truth.newest_t is None:
            now = time.monotonic()
            if now >= next_request:
                self._request_truth()
                next_request = now + 1.0
            if now > deadline:
                raise StartupError("no ATTITUDE / SIM_STATE on SERIAL0 within 30 s")
            self._poll_pilot()
            time.sleep(0.002)

    def _loop(self, wall_limit_s: float) -> None:
        wall_deadline = time.monotonic() + wall_limit_s
        t_core0 = self.now
        up_fd = self.relay.upstream_fileno()
        while True:
            select.select([up_fd], [], [], 0.002)
            self.relay.pump()
            self._poll_pilot()
            sim_now = self._sim_now()
            assert sim_now is not None
            caught_up = self._run_due(sim_now)
            if caught_up:
                self.now = sim_now
                self.fc.poll()
                self.fc.maybe_health()
                self._check_ready(sim_now)
            self.relay.pump()
            self._pilot_periodic(sim_now)
            if self.script.end_at is not None and self.now >= self.script.end_at:
                self.end_reason = END_LANDED
                return
            reason = self.script.stuck(self.now)
            if reason is not None:
                self.end_reason = reason
                return
            if sim_now - t_core0 > self.spec.sim_timeout_ms:
                self.end_reason = "sim_timeout"
                return
            if time.monotonic() > wall_deadline:
                self.end_reason = "wall_timeout"
                return

    # -- scoring --------------------------------------------------------------

    def _score(self, rec_path: Path) -> RunResult:
        spec = self.spec
        try:
            result = replay(rec_path)
            replay_ok = result.matches
            mismatch = result.mismatch()
        except Exception as exc:  # noqa: BLE001 - a recording replay cannot read is a failure
            replay_ok, mismatch = False, f"{type(exc).__name__}: {exc}"
        records = tuple(read_records(rec_path))
        trace = RunTrace(
            records=records,
            frames=tuple(self.frame_truths),
            truth=tuple(self.truth_samples),
            target=self.world.target,
        )
        g = self.config.guidance
        backend = backend_obj(spec)
        card = score_run(
            trace,
            scenario=spec.scenario,
            seed=spec.seed,
            seed_set=spec.seed_set,
            backend=backend,
            versions=dict(spec.versions),
            law=spec.law,
            replay_ok=replay_ok,
            end_reason=self.end_reason,
            thresholds=Thresholds(hold_tol_m=g.hold_tol_m, hold_time_s=g.hold_time_s),
            extra_checks=self.script.extra_checks(trace),
            fc_ids=self.fc_ids,
        )
        card_path = spec.out_dir / "scorecard.json"
        card_path.write_bytes(scorecard_json(card) + b"\n")
        run_log = {
            "scenario": spec.scenario,
            "seed": spec.seed,
            "seed_set": spec.seed_set.value,
            "law": spec.law,
            "backend": backend,
            "end_reason": self.end_reason,
            "replay": {"ok": replay_ok, "mismatch": mismatch},
            "world": self.world.to_obj(),
            "dropouts": [_window_obj(w) for w in self.script.dropouts],
            "script": [[t, note] for t, note in self.script.log.notes],
            "counters": {**self.counters, **_lag_stats(self._tick_lag)},
            "command_receiver": _receiver_counters(getattr(self, "ui", None)),
            "fc_link": _fc_counters(getattr(self, "fc", None)),
            "relay": _relay_counters(self.relay),
        }
        (spec.out_dir / "run.json").write_text(json.dumps(run_log, sort_keys=True) + "\n")
        return RunResult(
            spec=spec,
            scorecard=card,
            replay_ok=replay_ok,
            replay_mismatch=mismatch,
            end_reason=self.end_reason,
            recording=rec_path,
            scorecard_path=card_path,
        )


def _lag_stats(lags: list[int]) -> dict[str, int]:
    """Loop fidelity: how far behind SITL time ticks were processed (sim ms)."""
    if not lags:
        return {"tick_lag_p95_ms": 0, "tick_lag_max_ms": 0}
    ordered = sorted(lags)
    return {
        "tick_lag_p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "tick_lag_max_ms": ordered[-1],
    }


def _window_obj(w: DropoutWindow) -> dict[str, Any]:
    return {"kind": w.kind, "start_ms": w.start_ms, "end_ms": w.end_ms}


def _fc_counters(fc: FcLink | None) -> dict[str, int]:
    if fc is None:
        return {}
    return {
        "blocked_count": fc.blocked_count,
        "blocked_writes": fc.blocked_writes,
        "exit_blocked_count": fc.exit_blocked_count,
        "unknown_tones": fc.unknown_tones,
        "rx_frames": fc.rx_frames,
        "rx_bad": fc.rx_bad,
        "tx_frames": fc.tx_frames,
    }


def _receiver_counters(ui: CommandReceiver | None) -> dict[str, int]:
    if ui is None:
        return {}
    return {"received": ui.received, "malformed": ui.malformed, "auth_failed": ui.auth_failed}


def _relay_counters(relay: LinkRelay | None) -> dict[str, int]:
    if relay is None:
        return {}
    return {
        "dropped_bytes_to_fc": relay.dropped_bytes_to_fc,
        "dropped_bytes_to_companion": relay.dropped_bytes_to_companion,
    }


def run_one(spec: RunSpec) -> RunResult:
    """Run one scenario seed closed-loop against its own SITL and score it."""
    return ClosedLoop(spec).run()


__all__ = [
    "BACKEND",
    "TICK_MS",
    "UI_POLL_MS",
    "ClosedLoop",
    "Home",
    "LinkRelay",
    "RunResult",
    "RunSpec",
    "TruthBuffer",
    "backend_obj",
    "core_config",
    "ned_from_global",
    "run_one",
    "truth_from_sim_state",
]
