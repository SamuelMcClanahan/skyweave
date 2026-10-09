"""The pinned ArduCopter 4.7.0 SITL and a pilot link for it (phase E1, SITL only).

The binary and its defaults file are pinned by URL and SHA-256. A copy is
used only after its hash matches; nothing here runs an unverified binary.

How SITL is run, and what was found by experiment (2026-10-08, this binary,
``copter.parm`` defaults, speedup 1):

* Ports, instance ``N``: SERIAL0 TCP ``5760 + 10N``, SERIAL1 ``5762 + 10N``,
  SERIAL2 ``5763 + 10N``; all MAVLink2. SERIAL1 is the companion port, as on
  the real FC (UART7 = SERIAL1). SITL listens on every interface; fc_link's
  [F1] (a) check keeps the companion side on ``127.0.0.1``.
* SITL blocks at boot ("Waiting for connection") until something connects to
  SERIAL0, and opens SERIAL1 only after that. :class:`SitlInstance` therefore
  opens SERIAL0 itself (the :class:`PilotLink`) and then waits for SITL to
  report SERIAL1 listening.
* Stream-rate parameters in 4.7 are ``MAVn_*`` (no ``SRn_*``), and
  ``MAV2_*`` is SERIAL1 (``MAV1_*`` is SERIAL0, ``MAV3_*`` SERIAL2). All are 0
  by default. With ``MAV1_EXTRA1`` set, a receive-only client on SERIAL1 sees
  only HEARTBEAT, TIMESYNC, GPS_GLOBAL_ORIGIN, HOME_POSITION (the earlier
  inconclusive probe). With ``MAV2_EXTRA1`` set, SERIAL1 streams to a client
  that has never sent a byte: ATTITUDE and SIMSTATE (164) arrive within about
  1.4 s of the connection, at the EXTRA1 rate. A companion HEARTBEAT is NOT
  needed; one HEARTBEAT on a port with all rates 0 starts no streams.
  SIMSTATE (164) is in EXTRA1. SIM_STATE (108) did not appear with EXTRA1,
  EXTRA3, POSITION, EXT_STAT, and RC_CHAN set; ``MAV_CMD_SET_MESSAGE_INTERVAL``
  for 108 on SERIAL0 is accepted and streams it (20 Hz checked), which is how
  the harness gets truth on its own connection ([S0]).
* STATUSTEXT reaches a port only once it is active (has received a frame) or
  streaming. SERIAL1 streams, so fc_link sees STATUSTEXT receive-only; the
  pilot link sends one GCS HEARTBEAT on SERIAL0 so the pilot sees them too.
* Selecting GUIDED during boot (about 2.5 s after start) was accepted and
  then replaced by STABILIZE about 0.2 s later (observed once; cause not
  investigated, plausibly the mode switch's first read). Arming in
  STABILIZE succeeds and the GUIDED takeoff is refused, so a pilot (and a
  test) moves the switch into GUIDED only after the EKF uses GPS.
* So [F1] (b) works with the defaults file below: fc_link stays receive-only
  until the first SIMSTATE, then requests its own rates with
  ``MAV_CMD_SET_MESSAGE_INTERVAL`` ([F2]). The defaults file sets only
  ``MAV2_EXTRA1``; everything else fc_link asks for after the proof.
* SITL writes ``eeprom.bin``, ``logs/``, and ``terrain/`` into its working
  directory, so every instance runs in its own temporary directory with
  ``-w`` (wipe).
* copter.parm: FLTMODE_CH 5, FLTMODE1..6 = CIRCLE, LAND, RTL, AUTO, LOITER,
  STABILIZE; GUID_TIMEOUT 3 s; FS_THR_ENABLE 1; FENCE_RADIUS 150. Arming in
  GUIDED is refused ("Need Position Estimate") until the EKF uses GPS
  (STATUSTEXT "EKF3 IMU0 is using GPS", about 40 s after start in these runs
  at speedup 1); a refused arm is retried. ATTITUDE at 50 Hz on SERIAL1 had a
  95th-percentile gap of 31 ms and a maximum of 96 ms over one 76 s run (wall
  clock, receive side), close to the 100 ms staleness bound.

The :class:`PilotLink` on SERIAL0 stands in for the pilot's radio and a GCS
(mode changes, message-interval requests, the harness's own SIM_STATE truth).
It is test and harness infrastructure, never the companion path: the
companion's only link is ``fc_link.FcLink`` on SERIAL1. Nothing in this module
writes a parameter. Wall-clock reads here are process-control timeouts only,
never scored output.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.fc_link import LOOPBACK_HOST, MavTransport
from skyweave2.drone.vehicle_state import ARDUCOPTER_MODES

SITL_URL = "https://firmware.ardupilot.org/Copter/stable-4.7.0/SITL_x86_64_linux_gnu/arducopter"
SITL_SHA256 = "c67d78a1dbbf0fcad233080b2144445fb93f8d9675a4388ace7eaff72451341a"
PARM_URL = (
    "https://raw.githubusercontent.com/ArduPilot/ardupilot/Copter-4.7.0/"
    "Tools/autotest/default_params/copter.parm"
)
PARM_SHA256 = "5e01345b45d1c6190b28bece5638bbdd4cf1cce35e05bbbf480ab24d2b51aa0e"
SITL_BINARY_NAME = "arducopter"
PARM_NAME = "copter.parm"

COMPANION_STREAM_PARAMS: dict[str, float] = {"MAV2_EXTRA1": 10.0}
"""SITL defaults for SERIAL1 (see the module docstring): EXTRA1 carries SIMSTATE,
the [F1] (b) proof, to a receive-only client. Provisional (E1)."""

DEFAULT_HOME = "37.0,-122.0,10.0,0"
"""lat, lng, alt (m), heading (deg). Fixed so runs start from the same place."""

PILOT_SYSID = 255  # MAV_GCS_SYSID default
PILOT_COMPID = 190  # MAV_COMP_ID_MISSIONPLANNER


class SitlError(RuntimeError):
    """The pinned SITL is missing, fails its hash, or did not start."""


@dataclass(frozen=True, kw_only=True)
class SitlPaths:
    binary: Path
    parm: Path


def default_cache_dir() -> Path:
    """``$SKYWEAVE_SITL_DIR``, else ``~/.cache/skyweave/sitl-copter-4.7.0``."""
    env = os.environ.get("SKYWEAVE_SITL_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "skyweave" / "sitl-copter-4.7.0"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _verified(path: Path, sha: str) -> bool:
    return path.is_file() and _sha256(path) == sha


def sitl_available(cache_dir: Path | None = None) -> bool:
    """True when the binary and copter.parm are present and hash-verified."""
    d = default_cache_dir() if cache_dir is None else Path(cache_dir)
    return _verified(d / SITL_BINARY_NAME, SITL_SHA256) and _verified(d / PARM_NAME, PARM_SHA256)


def ensure_sitl(cache_dir: Path | None = None) -> SitlPaths:
    """Download (if missing) and hash-verify the pinned SITL; refuse a mismatch."""
    d = default_cache_dir() if cache_dir is None else Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    for name, url, sha in (
        (SITL_BINARY_NAME, SITL_URL, SITL_SHA256),
        (PARM_NAME, PARM_URL, PARM_SHA256),
    ):
        path = d / name
        if _verified(path, sha):
            continue
        tmp = d / f".{name}.download"
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        got = _sha256(tmp)
        if got != sha:
            tmp.unlink()
            raise SitlError(f"{url}: sha256 {got} does not match the pin {sha}")
        os.replace(tmp, path)
    (d / SITL_BINARY_NAME).chmod(0o755)
    return SitlPaths(binary=d / SITL_BINARY_NAME, parm=d / PARM_NAME)


def instance_ports(instance: int) -> tuple[int, int, int]:
    """SERIAL0, SERIAL1, SERIAL2 TCP ports of SITL instance ``instance``."""
    base = 5760 + 10 * instance
    return base, base + 2, base + 3


def free_instance(start: int = 0, stop: int = 40) -> int:
    """The first instance whose TCP serial ports and RC-in UDP port are free."""
    for n in range(start, stop):
        try:
            for port in instance_ports(n):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("0.0.0.0", port))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(("0.0.0.0", 5501 + 10 * n))
        except OSError:
            continue
        return n
    raise SitlError(f"no free SITL instance in [{start}, {stop})")


class PilotLink:
    """SERIAL0: the pilot's radio and a GCS, for tests and the harness only.

    It keeps the newest message of each type from the FC autopilot in
    :attr:`latest`. Waiting is by wall-clock timeout (process control).
    """

    def __init__(self, transport: MavTransport, fc_sysid: int = 1, fc_compid: int = 1) -> None:
        self._transport = transport
        self._mav = mavlink2.MAVLink(None, srcSystem=PILOT_SYSID, srcComponent=PILOT_COMPID)
        self._parser = mavlink2.MAVLink(None)
        self._parser.robust_parsing = True
        self.fc_sysid = fc_sysid
        self.fc_compid = fc_compid
        self.latest: dict[str, Any] = {}
        self.statustext: list[str] = []

    def poll(self) -> list[Any]:
        msgs = []
        for msg in self._parser.parse_buffer(self._transport.recv()) or []:
            if msg.get_type() == "BAD_DATA":
                continue
            if msg.get_srcSystem() == self.fc_sysid and msg.get_srcComponent() == self.fc_compid:
                self.latest[msg.get_type()] = msg
                if msg.get_type() == "STATUSTEXT":
                    self.statustext.append(msg.text)
            msgs.append(msg)
        return msgs

    def send(self, msg: Any) -> None:
        self._transport.send(bytes(msg.pack(self._mav)))

    def heartbeat(self) -> None:
        """One GCS HEARTBEAT. SITL sends STATUSTEXT only to ports that are active
        (have received a frame) or streaming; SERIAL0 streams nothing by default."""
        self.send(
            self._mav.heartbeat_encode(
                mavlink2.MAV_TYPE_GCS,
                mavlink2.MAV_AUTOPILOT_INVALID,
                0,
                0,
                mavlink2.MAV_STATE_ACTIVE,
            )
        )

    def command(self, command: int, *params: float) -> None:
        p = list(params) + [0.0] * (7 - len(params))
        self.send(self._mav.command_long_encode(self.fc_sysid, self.fc_compid, command, 0, *p))

    def set_mode(self, name: str) -> None:
        """The pilot moves the mode switch (DO_SET_MODE from SERIAL0)."""
        number = {v: k for k, v in ARDUCOPTER_MODES.items()}[name]
        self.command(
            mavlink2.MAV_CMD_DO_SET_MODE,
            float(mavlink2.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
            float(number),
        )

    def request_interval(self, msg_id: int, hz: float) -> None:
        self.command(mavlink2.MAV_CMD_SET_MESSAGE_INTERVAL, float(msg_id), 1e6 / hz)

    @property
    def mode(self) -> str | None:
        hb = self.latest.get("HEARTBEAT")
        return None if hb is None else ARDUCOPTER_MODES.get(hb.custom_mode)

    @property
    def armed(self) -> bool | None:
        hb = self.latest.get("HEARTBEAT")
        return None if hb is None else bool(hb.base_mode & mavlink2.MAV_MODE_FLAG_SAFETY_ARMED)

    def wait_for(
        self,
        predicate: Callable[[], bool],
        timeout_s: float,
        every: Callable[[], None] | None = None,
        period_s: float = 0.01,
    ) -> bool:
        """Poll until ``predicate()`` holds or ``timeout_s`` (wall clock) passes.

        ``every`` runs on each iteration (for example, the test's ``FcLink.poll``).
        """
        deadline = time.monotonic() + timeout_s
        while True:
            self.poll()
            if every is not None:
                every()
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(period_s)

    def close(self) -> None:
        self._transport.close()


class SitlInstance:
    """One SITL process in its own temporary working directory (context manager).

    ``extra_params`` go into the extra defaults file after
    :data:`COMPANION_STREAM_PARAMS` (a defaults file, never a parameter write).
    """

    def __init__(
        self,
        paths: SitlPaths,
        *,
        instance: int = 0,
        speedup: float = 1.0,
        home: str = DEFAULT_HOME,
        model: str = "+",
        extra_params: Mapping[str, float] | None = None,
    ) -> None:
        self.paths = paths
        self.instance = instance
        self.speedup = speedup
        self.home = home
        self.model = model
        self.extra_params = dict(extra_params or {})
        s0, s1, _ = instance_ports(instance)
        self.gcs_endpoint = f"tcp:{LOOPBACK_HOST}:{s0}"
        self.companion_endpoint = f"tcp:{LOOPBACK_HOST}:{s1}"  # SERIAL1, as wired on the FC
        self.workdir: Path | None = None
        self.pilot: PilotLink | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._log: Any = None

    @property
    def log_path(self) -> Path | None:
        return None if self.workdir is None else self.workdir / "sitl.log"

    def log_text(self) -> str:
        p = self.log_path
        return "" if p is None or not p.exists() else p.read_text(errors="replace")

    def start(self, timeout_s: float = 30.0) -> PilotLink:
        """Launch SITL, connect the pilot link on SERIAL0, wait for SERIAL1."""
        self.workdir = Path(tempfile.mkdtemp(prefix="skyweave-sitl-"))
        extra = self.workdir / "skyweave_companion.parm"
        lines = [f"{k} {v:g}" for k, v in {**COMPANION_STREAM_PARAMS, **self.extra_params}.items()]
        extra.write_text("\n".join(lines) + "\n")
        self._log = open(self.workdir / "sitl.log", "wb")  # noqa: SIM115
        self._proc = subprocess.Popen(
            [
                str(self.paths.binary),
                "-w",
                "--model",
                self.model,
                "--speedup",
                f"{self.speedup:g}",
                "--home",
                self.home,
                "--defaults",
                f"{self.paths.parm},{extra}",
                "-I",
                str(self.instance),
            ],
            cwd=self.workdir,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout_s
        transport = None
        while transport is None:
            self._check_alive()
            try:
                transport = MavTransport(self.gcs_endpoint, timeout_s=1.0)
            except OSError:
                if time.monotonic() >= deadline:
                    self.stop()
                    raise SitlError(f"SERIAL0 never accepted; log:\n{self.log_text()}") from None
                time.sleep(0.05)
        self.pilot = PilotLink(transport)
        self.pilot.heartbeat()  # makes SERIAL0 an active port: STATUSTEXT then reaches it
        marker = f"SERIAL1 on TCP port {instance_ports(self.instance)[1]}"
        while marker not in self.log_text():
            self._check_alive()
            self.pilot.poll()
            if time.monotonic() >= deadline:
                self.stop()
                raise SitlError(f"SERIAL1 never opened; log:\n{self.log_text()}")
            time.sleep(0.05)
        return self.pilot

    def _check_alive(self) -> None:
        if self._proc is not None and self._proc.poll() is not None:
            text = self.log_text()
            self.stop()
            raise SitlError(f"SITL exited with {self._proc.returncode}; log:\n{text}")

    def stop(self) -> None:
        if self.pilot is not None:
            self.pilot.close()
            self.pilot = None
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
        if self._log is not None:
            self._log.close()
            self._log = None
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None

    def __enter__(self) -> SitlInstance:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
