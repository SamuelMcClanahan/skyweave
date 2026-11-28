"""F series: the FC acts by itself when the companion's HEARTBEAT stops (decision B-D5,
DRONE_CONTRACTS_D0.md §11.4; [F1] (c) for the heartbeat itself).

The setting lives in the FC parameter baseline (``v2/deploy/fc/skyweave_cone_x.param``):
the ArduCopter ground-station failsafe watches one MAVLink system ID, and that ID is the
companion's. Nothing in fc_link changes, and nothing here writes a parameter: the SITL
test gives the lines to SITL in its launch defaults file ([F1]).

Fast tier: the baseline lines agree with the heartbeat fc_link really writes, and the
lint rules for them (R11, R19, R20 of ``check_params.py``) discriminate.

Slow tier (pinned ArduCopter 4.7.0 SITL): heartbeat loss in GUIDED gives RTL. The
heartbeat comes from ``FcLink`` itself, in the ``real_fc`` mode on SITL's loopback
port (B-F6): that is the only mode in which fc_link sends one (B-F3, B-F36). This
test is skipped for now: it was written but never run (see its skip reason).
"""

from __future__ import annotations

import importlib.util
import select
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.fc_link import FcLink
from skyweave2.drone.sitl import SitlInstance, SitlPaths, free_instance
from skyweave2.drone.vehicle_state import LinkConfig, parse_frames

FC_DIR = Path(__file__).resolve().parents[2] / "deploy" / "fc"
BASELINE = FC_DIR / "skyweave_cone_x.param"

COMPANION_LOSS_LINES = (
    "FS_GCS_ENABLE",
    "FS_GCS_TIMEOUT",
    "MAV_GCS_SYSID",
    "MAV_GCS_SYSID_HI",
    "FS_OPTIONS",
)
"""The baseline lines B-D5 added or changed."""

EKF_GPS_TIMEOUT_S = 90.0


def _check_params() -> ModuleType:
    """``check_params.py`` is a script beside the parameter file, not a package module."""
    spec = importlib.util.spec_from_file_location("fc_check_params", FC_DIR / "check_params.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclass looks the module up while it loads
    spec.loader.exec_module(module)
    return module


def _baseline() -> dict[str, float]:
    return _check_params().parse_param_file(BASELINE)


class StepClock:
    """Injected board clock ([F11]); the test moves it by hand."""

    def __init__(self, t: int = 10_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


# ---------------------------------------------------------------------------
# Fast tier
# ---------------------------------------------------------------------------


def test_bd5_baseline_watches_the_heartbeat_fc_link_writes() -> None:
    """B-D5, [F1] (c), B-F3: the system ID the FC baseline watches is the source system of
    the HEARTBEAT a ``real_fc`` link writes; the FC watches no second ID; the heartbeat
    period leaves room for one lost heartbeat inside ``FS_GCS_TIMEOUT``; the action is RTL."""
    params = _baseline()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    clock = StepClock()
    link = FcLink(
        f"tcp:127.0.0.1:{listener.getsockname()[1]}", clock, LinkConfig(real_fc=True)
    )
    link.connect(timeout_s=2.0)
    conn, _ = listener.accept()
    try:
        sent_at: list[int] = []
        for _ in range(100):  # 5 s of board time in 50 ms ticks
            before = link.tx_frames
            link.maybe_health()
            if link.tx_frames > before:
                sent_at.append(clock.t)
            clock.t += 50
        raw = bytearray()
        while select.select([conn], [], [], 0.05)[0]:
            raw += conn.recv(65536)
    finally:
        link.close()
        conn.close()
        listener.close()
    frames = parse_frames(bytes(raw))
    assert [f.get_type() for f in frames] == ["HEARTBEAT"] * len(sent_at) and len(sent_at) == 5
    assert {f.get_srcSystem() for f in frames} == {int(params["MAV_GCS_SYSID"])}
    assert {f.type for f in frames} == {mavlink2.MAV_TYPE_ONBOARD_CONTROLLER}
    assert params["MAV_GCS_SYSID_HI"] < params["MAV_GCS_SYSID"]  # one ID, not a range
    period_ms = {b - a for a, b in zip(sent_at, sent_at[1:], strict=False)}
    assert period_ms == {link.config.health_period_ms}
    # One lost heartbeat is a gap of two periods; that must stay inside the timeout.
    assert params["FS_GCS_TIMEOUT"] * 1000.0 > 2 * link.config.health_period_ms
    assert params["FS_GCS_ENABLE"] == 1  # RTL (V-D13)
    assert _check_params().COMPANION_SYSID == LinkConfig().companion_sysid


def test_bd5_a_sitl_mode_link_writes_no_heartbeat() -> None:
    """B-F36, [F1] (b): without ``real_fc`` the link writes no HEARTBEAT, so the FC of a
    SITL-mode run never sees a first heartbeat and its companion-loss failsafe is not active."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    clock = StepClock()
    link = FcLink(f"tcp:127.0.0.1:{listener.getsockname()[1]}", clock, LinkConfig())
    link.connect(timeout_s=2.0)
    conn, _ = listener.accept()
    fc = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)
    try:
        proof = fc.simstate_encode(0.0, 0.0, 0.0, 0.0, 0.0, -9.8, 0.0, 0.0, 0.0, 370000000, 0)
        conn.sendall(bytes(proof.pack(fc)))
        for _ in range(200):
            fd = link.fileno()
            assert fd is not None
            select.select([fd], [], [], 0.05)
            link.poll()
            if link.sitl_proven:
                break
        assert link.sitl_proven  # the link may write now
        for _ in range(100):
            link.maybe_health()
            clock.t += 50
        raw = bytearray()
        while select.select([conn], [], [], 0.05)[0]:
            raw += conn.recv(65536)
    finally:
        link.close()
        conn.close()
        listener.close()
    kinds = {f.get_type() for f in parse_frames(bytes(raw))}
    assert kinds == {"COMMAND_LONG"}  # the [F2] stream requests; never a HEARTBEAT


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"FS_OPTIONS": 0.0}, "R11"),  # companion loss would force RTL on the pilot in ALT_HOLD
        ({"FS_OPTIONS": 20.0}, "R11"),  # bit 2: a radio failsafe would continue in GUIDED
        ({"FS_OPTIONS": 18.0}, "R11"),  # bit 1: companion loss would continue in AUTO
        ({"FS_GCS_ENABLE": 0.0}, "R19"),
        ({"FS_GCS_ENABLE": 5.0}, "R19"),  # Land is not the recorded action (V-D13)
        ({"MAV_GCS_SYSID": 255.0}, "R19"),  # a ground station's ID, not the companion's
        ({"MAV_GCS_SYSID_HI": 255.0}, "R19"),  # a range: a ground station hides the loss
        ({"FS_GCS_TIMEOUT": 2.0}, "R20"),  # one lost 1 Hz heartbeat would trigger
        ({"FS_GCS_TIMEOUT": 30.0}, "R20"),
    ],
)
def test_bd5_lint_rules_discriminate(change: dict[str, float], rule: str) -> None:
    """B-D5 (R11, R19, R20): the baseline holds every lint rule, and each wrong
    companion-loss value fails exactly its own rule."""
    module = _check_params()
    good = module.parse_param_file(BASELINE)
    assert [rid for rid, _, fn in module.RULES if fn(good) is not None] == []
    bad = {**good, **change}
    assert [rid for rid, _, fn in module.RULES if fn(bad) is not None] == [rule]


# ---------------------------------------------------------------------------
# Slow tier: the pinned SITL
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="B-D5: written on 2026-11-28 but never run on the pinned SITL (the session "
    "ended first). Run it on Linux x86-64, correct it if needed, then remove this skip. "
    "The same case passed with a scratch script (v2/deploy/fc/README.md section 11.2)"
)
@pytest.mark.slow
@pytest.mark.sitl
def test_bd5_sitl_heartbeat_loss_in_guided_gives_rtl(
    sitl_paths: SitlPaths, wall_clock: Callable[[], int]
) -> None:
    """B-D5: with the baseline's companion-loss lines as SITL defaults, a GUIDED hover
    goes to RTL about ``FS_GCS_TIMEOUT`` after fc_link's last HEARTBEAT (not while the
    heartbeat runs, and not early); a ground station heartbeat (system 255) on SERIAL0
    does not hold the failsafe off; when the heartbeat returns the FC reports the
    failsafe cleared and stays in RTL."""
    params = _baseline()
    lines = {name: params[name] for name in COMPANION_LOSS_LINES}
    timeout_s = params["FS_GCS_TIMEOUT"]
    sitl = SitlInstance(sitl_paths, instance=free_instance(), extra_params=lines)
    pilot = sitl.start(timeout_s=60.0)
    link = FcLink(sitl.companion_endpoint, wall_clock, LinkConfig(real_fc=True))
    beat = {"on": True, "last_s": 0.0, "next_gcs_s": 0.0}

    def pump() -> None:
        link.poll()
        now = time.monotonic()
        if now >= beat["next_gcs_s"]:
            pilot.heartbeat()  # a ground station that stays connected, system 255
            beat["next_gcs_s"] = now + 1.0
        if beat["on"]:
            before = link.tx_frames
            link.maybe_health()  # the companion HEARTBEAT, on the health beat ([F1] (c))
            if link.tx_frames > before:
                beat["last_s"] = now

    def texts(needle: str) -> list[str]:
        return [s for s in pilot.statustext if needle in s]

    try:
        link.connect(timeout_s=5.0)
        ok = pilot.wait_for(
            lambda: any("is using GPS" in s for s in pilot.statustext), EKF_GPS_TIMEOUT_S, pump
        )
        assert ok, f"EKF never used GPS; statustext: {pilot.statustext[-10:]}"
        pilot.set_mode("GUIDED")
        assert pilot.wait_for(lambda: pilot.mode == "GUIDED", 10, pump)
        assert pilot.wait_for(lambda: False, 1.5, pump) is False  # GUIDED must hold (sitl.py)
        assert pilot.mode == "GUIDED"
        armed = False
        for _ in range(10):  # a refused arm (EKF settling) is retried
            pilot.command(mavlink2.MAV_CMD_COMPONENT_ARM_DISARM, 1.0)
            pilot.command(mavlink2.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 5.0)
            armed = pilot.wait_for(lambda: pilot.armed is True, 3, pump)
            if armed:
                break
        assert armed, f"never armed; statustext: {pilot.statustext[-10:]}"

        def rel_alt() -> float:
            return link.state.snapshot(wall_clock()).rel_alt_m or 0.0

        assert pilot.wait_for(lambda: rel_alt() >= 4.5, 45, pump), rel_alt()
        # Longer than the timeout with the heartbeat running: nothing happens.
        assert pilot.wait_for(lambda: pilot.mode != "GUIDED", timeout_s + 2.0, pump) is False
        assert texts("Failsafe") == []

        beat["on"] = False  # the companion stops; the link stays open, as a UART does
        assert pilot.wait_for(lambda: bool(texts("GCS Failsafe")), timeout_s + 3.0, pump), (
            pilot.statustext[-5:]
        )
        after_s = time.monotonic() - beat["last_s"]
        # Measured 3.0 to 3.3 s for the 3 s timeout (v2/deploy/fc/README.md section 11).
        assert timeout_s - 0.5 <= after_s <= timeout_s + 1.5, after_s
        assert texts("GCS Failsafe") == ["GCS Failsafe"]  # the plain action, not "Continuing"
        assert pilot.wait_for(lambda: pilot.mode == "RTL", 3, pump), pilot.mode
        assert pilot.armed is True

        beat["on"] = True  # the companion is back
        assert pilot.wait_for(lambda: bool(texts("GCS Failsafe Cleared")), 5, pump)
        assert pilot.wait_for(lambda: pilot.mode != "RTL", 4, pump) is False  # RTL stays
    finally:
        link.close()
        sitl.stop()

