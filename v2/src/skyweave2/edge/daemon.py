"""Driving the C daemon from the host: locate it, configure it, run it.

The load-bearing function here is :func:`daemon_args`. The daemon's detector
knobs and ``DetectorConfig``'s are the same knobs, and the host detector is
the ORACLE for the D8 fixtures — so a config field the daemon does not
receive is a config field the two sides silently disagree about. The mapping
below is TOTAL and raises on any ``DetectorConfig`` field it does not know,
so adding a detector knob and forgetting the daemon fails a test instead of
producing a quiet divergence in the tolerance table.

Fields deliberately NOT passed are listed explicitly with the reason. Being
absent from both lists is what raises.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from skyweave2.detector.config import Backend, DetectorConfig

V2_ROOT = Path(__file__).resolve().parents[3]
FIRMWARE_ROOT = V2_ROOT / "firmware" / "rv1106"
DEFAULT_BUILD_DIR = FIRMWARE_ROOT / "build-host"


class DaemonUnavailable(RuntimeError):
    """The C daemon has not been built for this host."""


# DetectorConfig fields the daemon does NOT take, and why. A field that is in
# neither this set nor the mapping below is an ERROR.
_NOT_PASSED = {
    # Backend selection is not a daemon flag: the daemon has two detectors
    # (portable GMM2 and hardware IVE GMM2) and which one ran must be stated
    # by the caller, never inferred from a host backend name. `--detector` is
    # passed separately and explicitly.
    "backend",
    # Host-only backends. The daemon has no OpenCV MOG2 and no fixed_bg, by
    # design (RV1106_EDGE_NODE.md section 5: no OpenCV on the node).
    "mog2",
    "fixed_bg",
    # Carried by the injection stream's session header, which is
    # authoritative: the harness knows which clip it is replaying.
    "exposure_us",
    "fps",
    "calibration_rev",
    "detector_rev",
    "time_sync_error_ms",
}

# DetectorConfig field -> (daemon flag, formatter). proc_width/proc_height
# fold into one flag, so they are handled outside this table.
_SCALAR_FLAGS = {
    "warmup_frames": "--warmup",
    "max_components_per_frame": "--cap",
    # C-001 whitelist. These are passed even at their defaults: an artifact's
    # argv must say which side of an off/on morphology experiment ran, and the
    # board must never infer a campaign knob from a compiled-in value.
    "open_radius_px": "--morph-open",
    "min_area_px": "--min-area-px",
}

# ive_approx knobs that remain frozen in C-001. The two whitelisted GMM2
# controls are deliberately absent and travel through _GMM2_FLAGS below.
_GMM2_DEFAULTS = {
    "model_num": 3,
    "var_init": 225.0,
    "learn_rate": 0.005,
    "bg_ratio": 0.9,
    "weight_init": 0.05,
}

_GMM2_FLAGS = {
    "var_min": "--gmm2-var-min",
    "match_sigmas": "--gmm2-match-sigmas",
}

# Structural DetectorConfig fields the daemon implements with the shared
# defaults but which C-001 does not authorize as knobs. They are checked, not
# silently ignored: changing one on the oracle while the node keeps its
# compiled-in value would make the parity result about two configurations.
_STRUCTURAL_DEFAULTS = {
    "max_area_px": 10000,
    "persistence_frames": 2,
    "persistence_gate_px": 12.0,
    "centroid_cov_floor_px2": 0.25,
}


@dataclass(frozen=True)
class RamLoopDeclaration:
    """Everything the daemon needs to loop a SWIJ clip out of DDR, declared.

    This is the ONE place the RAM-loop flags are spelled. Three argv builders
    reach the daemon — this module for the tests, ``benchmark`` for the sweep
    and ``provision`` for a node — and three spellings of ``--ram-loop-frames``
    are three ways a board command line diverges from what a host test
    exercised.

    Deliberately NOT part of :func:`daemon_args`. That mapping is TOTAL over
    ``DetectorConfig`` and raises on any field it does not know, and a source
    is not a detector knob: putting it there would make every daemon-driving
    test fail on an unmapped field that does not exist.

    Every value is a DECLARATION. The daemon derives none of them — that is
    what keeps the looped capture timestamps the harness's arithmetic rather
    than the daemon's opinion.
    """

    #: Frames in the clip file itself.
    clip_frames: int
    #: Frames the run serves before it ends. A RAM loop has no trailer, so
    #: this is what terminates it — never a duration, because every counter in
    #: ``benchmark.EXACT_COUNTERS`` is compared with ``==``.
    total_frames: int
    #: What ``capture_ts_ns`` advances by on each pass through the clip.
    pts_stride_ns: int
    budget_mb: int = 160
    #: 0 = unpaced. Nanoseconds per frame, never an fps float: a rate rounded
    #: in two places is a rate that disagrees with itself.
    period_ns: int = 0

    def as_daemon_args(self, clip_path: str) -> list[str]:
        return [
            "--inject-ram", str(clip_path),
            "--ram-loop-frames", str(self.total_frames),
            "--ram-loop-pts-stride-ns", str(self.pts_stride_ns),
            "--ram-budget-mb", str(self.budget_mb),
            "--ram-loop-period-ns", str(self.period_ns),
        ]

    def as_dict(self) -> dict:
        return {
            "clip_frames": self.clip_frames,
            "total_frames": self.total_frames,
            "pts_stride_ns": self.pts_stride_ns,
            "budget_mb": self.budget_mb,
            "period_ns": self.period_ns,
        }


def daemon_args(config: DetectorConfig, detector: str = "soft") -> list[str]:
    """Daemon argv for a ``DetectorConfig``. Raises on anything unmapped."""
    known = set(_NOT_PASSED) | set(_SCALAR_FLAGS) | {
        "ive_approx",
        "proc_width",
        "proc_height",
        *_STRUCTURAL_DEFAULTS,
    }
    unmapped = set(DetectorConfig.model_fields) - known
    if unmapped:
        raise AssertionError(
            f"DetectorConfig fields the daemon knows nothing about: {sorted(unmapped)}. "
            "Add them to skyweave2/edge/daemon.py — either as a daemon flag or to "
            "_NOT_PASSED with the reason. An unmapped detector knob is a silent "
            "host/edge divergence, which is exactly what the D8 fixtures exist to "
            "make impossible."
        )
    if config.backend is not Backend.IVE_APPROX:
        raise AssertionError(
            f"the D8 oracle is ive_approx; this config says {config.backend.value}. "
            "Comparing the daemon against a different host backend would put a "
            "backend difference into the detector tolerance table."
        )
    for name, expected in _GMM2_DEFAULTS.items():
        actual = getattr(config.ive_approx, name)
        if actual != expected:
            raise AssertionError(
                f"ive_approx.{name} is {actual}, the daemon compiles in {expected}. "
                "The daemon takes no GMM2 flags, so a changed knob must be changed "
                "in firmware/rv1106/src/sw_config.c too, and this assertion is how "
                "you find out."
            )
    for name, expected in _STRUCTURAL_DEFAULTS.items():
        actual = getattr(config, name)
        if actual != expected:
            raise AssertionError(
                f"{name} is {actual}, the daemon compiles in {expected}. "
                "C-001 freezes this field, so it cannot be changed through the "
                "campaign runner."
            )
    args = ["--detector", detector, "--proc", f"{config.proc_width}x{config.proc_height}"]
    for field, flag in _SCALAR_FLAGS.items():
        args += [flag, str(getattr(config, field))]
    for field, flag in _GMM2_FLAGS.items():
        args += [flag, str(getattr(config.ive_approx, field))]
    return args


def daemon_path(build_dir: str | Path | None = None) -> Path:
    """Locate the built daemon, or say plainly that it is not there."""
    override = os.environ.get("SKYWEAVE_EDGE_BIN")
    if override:
        path = Path(override)
        if not path.exists():
            raise DaemonUnavailable(f"SKYWEAVE_EDGE_BIN points at {path}, which does not exist")
        return path
    path = Path(build_dir or DEFAULT_BUILD_DIR) / "skyweave-edge"
    if not path.exists():
        raise DaemonUnavailable(
            f"{path} is not built. Build it with:\n"
            f"  cmake -S {FIRMWARE_ROOT} -B {DEFAULT_BUILD_DIR} && "
            f"cmake --build {DEFAULT_BUILD_DIR}\n"
            "or set SKYWEAVE_EDGE_BIN."
        )
    return path


def fixture_tool_path(build_dir: str | Path | None = None) -> Path:
    override = os.environ.get("SKYWEAVE_FIXTURE_TOOL")
    if override:
        path = Path(override)
        if not path.exists():
            raise DaemonUnavailable(
                f"SKYWEAVE_FIXTURE_TOOL points at {path}, which does not exist"
            )
        return path
    path = Path(build_dir or DEFAULT_BUILD_DIR) / "sw-fixture-tool"
    if not path.exists():
        raise DaemonUnavailable(f"{path} is not built; see daemon_path() for the recipe")
    return path


def host_toolchain_available() -> bool:
    """Whether this machine could build the daemon at all."""
    return shutil.which("cmake") is not None and (
        shutil.which("cc") is not None or shutil.which("gcc") is not None
    )


@dataclass(frozen=True)
class DaemonRun:
    returncode: int
    stdout: str
    stderr: str
    packets: list[bytes]
    stats: dict


def run_daemon_on_stream(
    stream_path: str | Path,
    config: DetectorConfig,
    work_dir: str | Path,
    detector: str = "soft",
    build_dir: str | Path | None = None,
    timeout_s: float = 600.0,
    extra_args: list[str] | None = None,
    ram_loop: RamLoopDeclaration | None = None,
) -> DaemonRun:
    """Replay an injection stream through the daemon and collect what it sent.

    With ``ram_loop`` the same file is preloaded into DDR and looped instead of
    being read frame by frame. It is a PARAMETER rather than something to pass
    through ``extra_args``, because that hatch appends: ``--inject-file`` would
    stay in the argv, the daemon would count two sources and refuse
    (``sw_config.c``'s "choose exactly one source").

    The datagrams are read back from ``--packet-log`` rather than off the
    socket: the log is written by the same code path, immediately before the
    send, so it records exactly what went out without the test acquiring a
    dependence on a receiver's timing.

    A real socket is still bound and left unread. Without one, loopback
    replies ICMP port-unreachable and the NEXT send fails with ECONNREFUSED,
    which would show up as a permanent ``send_failures: 1`` in every run's
    stats — a fake defect in the artifact that reports real ones.
    """
    import socket

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    packet_log = work_dir / "packets.hex"
    stats_path = work_dir / "stats.json"
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    try:
        source_args = (
            ram_loop.as_daemon_args(str(stream_path))
            if ram_loop is not None
            else ["--inject-file", str(stream_path)]
        )
        argv = [
            str(daemon_path(build_dir)),
            *source_args,
            "--packet-log",
            str(packet_log),
            "--stats",
            str(stats_path),
            "--measurement-port",
            str(sink.getsockname()[1]),
            # A control port nobody dials is silent.
            "--control-port",
            "0",
        ] + daemon_args(config, detector=detector) + list(extra_args or [])
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    finally:
        sink.close()
    packets = []
    if packet_log.exists():
        packets = [
            bytes.fromhex(line.strip())
            for line in packet_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    return DaemonRun(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        packets=packets,
        stats=stats,
    )


__all__ = [
    "DaemonRun",
    "DaemonUnavailable",
    "RamLoopDeclaration",
    "daemon_args",
    "daemon_path",
    "fixture_tool_path",
    "host_toolchain_available",
    "run_daemon_on_stream",
]
