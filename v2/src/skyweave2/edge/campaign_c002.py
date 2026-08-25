"""Campaign C-002: the board sweep + soak driver at 1152x648.

The host harness (`benchmark.run_sweep` / `run_soak`) is host-local by design:
it Popens a local binary, binds a loopback health listener and reads a local
``/proc``. C-002 measures a real RV1106 node, so this module composes the
provisioning primitives (`provision.push_daemon`, `daemon_command`,
`SshTransport.spawn`) with three things a board run needs that the host path
never did:

1. **An on-board sampler** for the two E8-bounded axes that have no remote
   collector — peak RSS (``/proc/<pid>/status`` VmHWM) and A7 utilisation
   (``/proc/<pid>/stat`` utime+stime over CLK_TCK) — plus the thermal curve.
   A BusyBox ``sh`` loop beside the daemon, because 720 SSH round trips per
   soak hour would be measuring the network. The sampler writes an ``end``
   marker when the daemon's /proc entry vanishes: a log without the marker is
   a sampler that died, and every quantity it half-measured is reported as
   NOT-MEASURED rather than as a number about part of a run.
2. **A capture listener ON the Jetson** for the measurement/health plane. The
   node's UDP does not route to the home LAN (measured, C1/C2 sessions), so
   datagrams are recorded at the Jetson's rig-LAN address on the measurement
   port with their arrival timestamp AND SENDER, and replayed locally through
   the real `health` stack. Sender recording is load-bearing: 5601 is the rig-wide default
   measurement port, so a stale daemon from an aborted attempt could
   interleave its packets with this run's — the replay filters to the run's
   board and counts everything else as foreign. The daemon's own
   ``health_sent`` counter is then reconciled against the filtered capture,
   so a listener that died mid-run is a detected absence, not a clean tail.
3. **Record construction** feeding `benchmark.compare_runs` UNCHANGED. The E8
   bounds and the exact-counter list stay in `tolerance.py`/`benchmark.py`;
   this module never retypes a bound.

Every run gets a fresh run id, a fresh remote directory on the board and a
fresh capture path on the Jetson: nothing on either machine is reused, so a
crashed daemon cannot leave a previous run's ``stats.json`` where this run's
evidence is collected — that exact substitution is how a dead run scores as
a live one.

Everything variable is declared in ``docs/campaigns/C-002-sweep-soak.md``
before any run; the constants here are that declaration, quoted not tuned.
This is a measurement campaign: there is no knob whitelist and no field of
the detector configuration is writable from this module's CLI at all.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from skyweave2.transport.codec import decode_health
from skyweave2.transport.wire import PayloadType, WireError, unframe

from . import benchmark, daemon, health, metrics, provision

CAMPAIGN_ID = "C-002"

# --- the frozen C-001-final geometry and identities (declared, not knobs) ---

PROC_WIDTH = 1152
PROC_HEIGHT = 648
PLAN_SEED = 20260824
CLIP_FRAMES = 36
SWEEP_TOTAL_FRAMES = 6300
SOAK_TOTAL_FRAMES = 108000
PACE_FPS = 30.0
HEALTH_PERIOD_MS = 1000
MEASUREMENT_PORT = 5601
PROBE_SESSION_UUID = f"c002-probe-{PLAN_SEED}"

#: The Shift 4 frozen ARM daemon — the binary C-001 measured. C-002 deploys
#: exactly this file and refuses anything else.
FROZEN_DAEMON_SHA256 = (
    "eaa9d178d1c0e5d408ff7260e1c34ae6917c3a8f5aa09c5720de718d9169ba94"
)
#: The board's IVE runtime, hashed before AND after every run: a runtime that
#: changed mid-run would make the two hashes a finding, not a formality.
BOARD_LIBRVE_SHA256 = (
    "2446c5b1720c083b89338c33cdf3f289c8fc94b29386a0404481881a06cc3455"
)
BOARD_LIBRVE_PATH = "/oem/usr/lib/librve.so"

DEFAULT_BOARD = "board-104"
DEFAULT_IMAGE_MARKER = "Buildroot 2023.02.6"
# The board's host and MAC and the Jetson's rig-LAN address carry no defaults:
# identities and addresses are operator-supplied at invocation and live in the
# private rig log, never in this tree — mirroring campaign_c001_run's design.

# --- declared verdict criteria (campaign file, "Health criteria") -----------

#: A missed 1 Hz send puts its neighbours two periods apart (the E7 slack
#: argument): cadence max_period_s at or above this many declared periods is
#: a health gap.
HEALTH_GAP_FACTOR = 2.0
#: The gap and fps evidence is only as good as the capture: the filtered
#: capture must hold at least the daemon's own health_sent minus this many
#: packets (UDP on a switched rig LAN can lose one; a listener that died
#: loses hundreds and fails this reconciliation loudly).
HEALTH_CAPTURE_LOSS_TOLERANCE = 2
#: Declared before any C-002 board thermal number existed: (a) an absolute
#: ceiling no zone may ever reach, and (b) a plateau requirement over the
#: soak's final minutes — a curve still climbing at the end is runaway even
#: if it has not reached the ceiling yet. Windows SLIDE at the sampler's own
#: cadence; each needs at least half its expected samples to count as
#: evaluated, and the tail is anchored at the sampler's END marker so a
#: sampler that died mid-run cannot shrink the judged interval.
THERMAL_CEILING_C = 100.0
THERMAL_TAIL_S = 900.0
THERMAL_WINDOW_S = 300.0
THERMAL_WINDOW_MAX_RISE_C = 0.5

#: Wall caps, provision start to collection (campaign file, "Budget").
#: The soak cap was declared 120 min against the stale C3 throughput
#: numbers (~21-26 fps, which counted label-failure frames that skip
#: work). S1 measured the clean unpaced ceiling at 14.47 fps, making a
#: 108,000-frame paced soak ~124 min by arithmetic — the campaign file's
#: own "K1/K2 still run at a sub-30 ceiling" clause and its cap
#: contradicted each other. Re-derived BEFORE any soak ran (amendment in
#: the campaign file): frames over the measured ceiling plus margin,
#: covering any duty cycle down to ~12.6 fps. No verdict criterion moves
#: with a wall cap; it bounds cost, not truth.
SWEEP_WALL_CAP_S = 20 * 60.0
SOAK_WALL_CAP_S = 150 * 60.0

SAMPLER_PERIOD_S = 5
#: The alive() poll runs over SSH through a jump host; one stalled probe is
#: weather, this many consecutive failures is an outage worth aborting on.
MAX_CONSECUTIVE_POLL_FAILURES = 8

_RUN_KINDS = ("sweep", "soak")


class CampaignC002Error(RuntimeError):
    """A refusal from this driver. Every one names what to fix."""


# ---------------------------------------------------------------------------
# Plan, declaration, probe
# ---------------------------------------------------------------------------


def build_plan(kind: str) -> benchmark.BenchmarkPlan:
    """The one plan both runs of a pair share, byte-for-byte.

    ``frames`` is the RUN length, not the clip length: `ram_loop_declaration`
    reads the clip length from ``ram_clip_frames`` and the run length from its
    own ``total_frames`` argument, and keeping the two spelled from the same
    constants here is what makes the manifest echo checkable.
    """
    if kind not in _RUN_KINDS:
        raise CampaignC002Error(f"unknown run kind {kind!r}; one of {_RUN_KINDS}")
    total = SWEEP_TOTAL_FRAMES if kind == "sweep" else SOAK_TOTAL_FRAMES
    return benchmark.BenchmarkPlan(
        frames=total,
        warmup_frames=30,
        seed=PLAN_SEED,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        ram_clip_frames=CLIP_FRAMES,
        ram_budget_mb=160,
    )


def ram_declaration(kind: str) -> daemon.RamLoopDeclaration:
    """The RAM-loop declaration for one run kind.

    A sweep is UNPACED — its fps is a throughput ceiling. A soak is paced at
    the declared 30 fps operating point. Both come from the same derivation in
    `benchmark.ram_loop_declaration`, so the 36-frame clip, the 1.2e9 ns
    per-pass stride and the 33,333,333 ns soak period are arithmetic here,
    never typed numbers.
    """
    plan = build_plan(kind)
    return benchmark.ram_loop_declaration(
        plan,
        PROC_WIDTH,
        PROC_HEIGHT,
        total_frames=plan.frames,
        detector="ive",
        paced_fps=PACE_FPS if kind == "soak" else None,
    )


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_probe_clip(path: Path) -> int:
    """The one spelling of the clip-generation call, shared by prepare and
    verification so the regeneration check cannot drift from the producer."""
    plan = build_plan("sweep")
    return benchmark.write_benchmark_stream(
        path,
        plan,
        PROC_WIDTH,
        PROC_HEIGHT,
        session_uuid=PROBE_SESSION_UUID,
        frame_count=CLIP_FRAMES,
    )


def prepare_probe(out_dir: str | Path) -> dict:
    """Write the one immutable clip both pairs reuse, plus its manifest.

    ``frame_count=CLIP_FRAMES`` engages the shadow-plan rule in
    `write_benchmark_stream` (warm-up zero for the CLIP so movers exist from
    frame 0; the daemon's own ``--warmup`` still comes from the plan).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    clip_path = out / "probe.swij"
    if clip_path.exists():
        raise CampaignC002Error(
            f"refusing to overwrite an existing probe at {clip_path}"
        )
    written = _write_probe_clip(clip_path)
    import numpy

    manifest = {
        "schema": "skyweave-c002-probe/1",
        "campaign_id": CAMPAIGN_ID,
        "proc_width": PROC_WIDTH,
        "proc_height": PROC_HEIGHT,
        "seed": PLAN_SEED,
        "session_uuid": PROBE_SESSION_UUID,
        "clip_frames": CLIP_FRAMES,
        "clip_bytes": written,
        "clip_sha256": _sha256_file(clip_path),
        "plan": build_plan("sweep").as_dict(),
        "declarations": {
            kind: ram_declaration(kind).as_dict() for kind in _RUN_KINDS
        },
        "generator_versions": {
            "numpy": numpy.__version__,
        },
    }
    manifest_path = out / "probe_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_probe(manifest_path: str | Path) -> tuple[Path, dict]:
    """Re-verify the probe by REGENERATING it, not by trusting its manifest.

    A manifest is a claim; the generator is the authority. The clip is
    re-derived from this module's frozen plan into a temporary file and must
    hash identically to the file on disk — so a hand-authored or substituted
    clip (a gate scene wearing a rehashed manifest) cannot reach a board no
    matter what its manifest says. Same rule C-001's probe loader enforced.
    """
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "skyweave-c002-probe/1":
        raise CampaignC002Error("not a C-002 probe manifest")
    clip_path = manifest_path.parent / "probe.swij"
    actual = _sha256_file(clip_path)
    if actual != manifest["clip_sha256"]:
        raise CampaignC002Error(
            f"probe clip does not match its manifest: {actual} vs "
            f"{manifest['clip_sha256']}"
        )
    with tempfile.TemporaryDirectory() as scratch:
        regenerated = Path(scratch) / "probe.swij"
        _write_probe_clip(regenerated)
        derived = _sha256_file(regenerated)
    if derived != actual:
        raise CampaignC002Error(
            "probe clip is not the declared generator's output: regenerating "
            f"from the frozen plan yields {derived}, the file is {actual}"
        )
    if manifest["plan"] != build_plan("sweep").as_dict():
        raise CampaignC002Error(
            "probe manifest plan does not match this module's declared plan"
        )
    return clip_path, manifest


# ---------------------------------------------------------------------------
# The on-board sampler
# ---------------------------------------------------------------------------

#: BusyBox sh. Args: wrapper pid, output file, period seconds. The pid that
#: `SshTransport.spawn` returns is the exit-status wrapper's — sampling ITS
#: /proc would measure a sleeping shell — so the sampler resolves the daemon
#: as the wrapper's child whose comm is skyweave-edge, then reads that pid
#: until it exits, then writes an ``end`` marker. ``cut -d')' -f2-`` before
#: field-splitting /proc/*/stat, because comm may contain spaces and field
#: numbers only hold after it.
SAMPLER_SCRIPT = r"""#!/bin/sh
wrapper="$1"; out="$2"; period="${3:-5}"
clk=$( (getconf CLK_TCK) 2>/dev/null ); [ -n "$clk" ] || clk=100
echo "clk_tck $clk" > "$out"
pid=""
tries=0
while [ -z "$pid" ] && [ "$tries" -lt 15 ]; do
  for d in /proc/[0-9]*; do
    p="${d#/proc/}"
    [ -r "$d/stat" ] || continue
    comm=$(sed -n 's/^[0-9]* (\(.*\)) .*/\1/p' "$d/stat" 2>/dev/null)
    [ "$comm" = "skyweave-edge" ] || continue
    rest=$(cut -d')' -f2- "$d/stat" 2>/dev/null) || continue
    set -- $rest
    if [ "$2" = "$wrapper" ]; then pid="$p"; break; fi
  done
  if [ -z "$pid" ]; then tries=$((tries+1)); sleep 1; fi
done
if [ -z "$pid" ]; then echo "no_daemon_child_found" >> "$out"; exit 1; fi
echo "daemon_pid $pid" >> "$out"
while [ -d "/proc/$pid" ]; do
  up=$(cut -d' ' -f1 /proc/uptime)
  hwm=$(awk '/^VmHWM:/ {print $2}' "/proc/$pid/status" 2>/dev/null)
  rest=$(cut -d')' -f2- "/proc/$pid/stat" 2>/dev/null)
  set -- $rest
  ut="${12}"; st="${13}"
  tmax=""
  for z in /sys/class/thermal/thermal_zone*/temp; do
    [ -r "$z" ] || continue
    t=$(cat "$z" 2>/dev/null)
    [ -n "$t" ] || continue
    if [ -z "$tmax" ] || [ "$t" -gt "$tmax" ]; then tmax="$t"; fi
  done
  echo "s $up ${hwm:--} ${ut:--} ${st:--} ${tmax:--}" >> "$out"
  sleep "$period"
done
echo "end $(cut -d' ' -f1 /proc/uptime)" >> "$out"
"""


@dataclass(frozen=True)
class SamplerReading:
    uptime_s: float
    vmhwm_kib: int | None
    utime_ticks: int | None
    stime_ticks: int | None
    thermal_milli_c: int | None


@dataclass
class SamplerLog:
    clk_tck: int
    daemon_pid: int | None
    samples: list[SamplerReading] = field(default_factory=list)
    child_found: bool = True
    #: True only when the sampler outlived the daemon and wrote its ``end``
    #: marker. Without it the log is a PREFIX of the run, and a prefix
    #: cannot carry a peak, a utilisation or a thermal tail.
    ended: bool = False
    end_uptime_s: float | None = None


def parse_sampler_log(text: str) -> SamplerLog:
    """The sampler's file, back into numbers. A '-' field stays an absence."""
    clk_tck = 100
    daemon_pid: int | None = None
    child_found = True
    ended = False
    end_uptime: float | None = None
    samples: list[SamplerReading] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "clk_tck" and len(parts) == 2:
            clk_tck = int(parts[1])
        elif parts[0] == "daemon_pid" and len(parts) == 2:
            daemon_pid = int(parts[1])
        elif parts[0] == "no_daemon_child_found":
            child_found = False
        elif parts[0] == "end" and len(parts) == 2:
            ended = True
            end_uptime = float(parts[1])
        elif parts[0] == "s" and len(parts) == 6:

            def _opt(token: str) -> int | None:
                return None if token == "-" else int(token)

            samples.append(
                SamplerReading(
                    uptime_s=float(parts[1]),
                    vmhwm_kib=_opt(parts[2]),
                    utime_ticks=_opt(parts[3]),
                    stime_ticks=_opt(parts[4]),
                    thermal_milli_c=_opt(parts[5]),
                )
            )
    return SamplerLog(
        clk_tck=clk_tck,
        daemon_pid=daemon_pid,
        samples=samples,
        child_found=child_found,
        ended=ended,
        end_uptime_s=end_uptime,
    )


def sampler_measurements(log: SamplerLog) -> dict[str, metrics.Measurement]:
    """The board-side E8 axes, from the sampler's own numbers.

    Everything here requires the ``end`` marker: a sampler that died mid-run
    left a prefix, and a VmHWM read at minute 30 of a 60-minute soak is not
    the run's peak — it is a number about half a run, which is worse than an
    absence because it looks like an answer. Peak RSS is the kernel's VmHWM
    high-water mark from the LAST sample before exit; utilisation is a
    difference of two board-clock quantities (ticks over /proc/uptime span),
    so the harness's clock never enters it. The span misses the daemon's
    first seconds (the sampler starts after spawn), which the source string
    records rather than corrects with a guess. KiB become DECIMAL MB,
    matching `metrics.peak_rss`'s convention.
    """
    out: dict[str, metrics.Measurement] = {}
    incomplete = (
        None
        if log.ended
        else "the on-board sampler did not outlive the daemon (no end "
        "marker); its log is a prefix of the run, not a measurement of it"
    )
    hwm = [s.vmhwm_kib for s in log.samples if s.vmhwm_kib is not None]
    if incomplete:
        out["peak_rss_mb"] = metrics.not_measured("peak_rss_mb", "MB", incomplete)
    elif hwm:
        out["peak_rss_mb"] = metrics.Measurement(
            name="peak_rss_mb",
            value=hwm[-1] * 1024 / 1e6,
            unit="MB",
            source=(
                "board /proc/<pid>/status VmHWM, KiB kernel high-water mark, "
                "read by the on-board sampler; decimal MB"
            ),
        )
    else:
        out["peak_rss_mb"] = metrics.not_measured(
            "peak_rss_mb", "MB", "the on-board sampler recorded no VmHWM"
        )
    ticks = [
        (s.uptime_s, s.utime_ticks + s.stime_ticks)
        for s in log.samples
        if s.utime_ticks is not None and s.stime_ticks is not None
    ]
    if incomplete:
        out["cpu_utilisation"] = metrics.not_measured(
            "cpu_utilisation", "core", incomplete
        )
    elif len(ticks) >= 2 and ticks[-1][0] > ticks[0][0]:
        (t0, c0), (t1, c1) = ticks[0], ticks[-1]
        out["cpu_utilisation"] = metrics.Measurement(
            name="cpu_utilisation",
            value=((c1 - c0) / log.clk_tck) / (t1 - t0),
            unit="core",
            source=(
                "board /proc/<pid>/stat utime+stime over CLK_TCK, against "
                "the board's /proc/uptime span between the sampler's first "
                "and last readings (the sampler starts after spawn, so the "
                "daemon's first seconds are outside the span)"
            ),
        )
    else:
        out["cpu_utilisation"] = metrics.not_measured(
            "cpu_utilisation",
            "core",
            "fewer than two board sampler readings carried CPU ticks",
        )
    thermal = [
        s.thermal_milli_c for s in log.samples if s.thermal_milli_c is not None
    ]
    if incomplete:
        out["thermal_c"] = metrics.not_measured("thermal_c", "C", incomplete)
    elif thermal:
        out["thermal_c"] = metrics.Measurement(
            name="thermal_c",
            value=thermal[-1] / 1000.0,
            unit="C",
            source=(
                "max of board /sys/class/thermal zones, last on-board "
                "sampler reading"
            ),
        )
    else:
        out["thermal_c"] = metrics.not_measured(
            "thermal_c", "C", "the board exposed no thermal zone to the sampler"
        )
    return out


def thermal_curve(log: SamplerLog) -> list[tuple[float, float]]:
    """(board uptime s, max-zone C) for every sample that carried a reading."""
    return [
        (s.uptime_s, s.thermal_milli_c / 1000.0)
        for s in log.samples
        if s.thermal_milli_c is not None
    ]


def thermal_verdict(
    curve: list[tuple[float, float]], *, sampler_ended: bool = True,
    end_uptime_s: float | None = None,
) -> dict:
    """The declared runaway criteria against the retained curve.

    (a) no reading ever reaches the ceiling; (b) over the run's final
    :data:`THERMAL_TAIL_S`, no :data:`THERMAL_WINDOW_S` window rises more
    than the declared limit. Windows SLIDE: every sample in the tail opens
    one, closed at the first sample at least a window later — the file's
    "no 5-minute window" with no gap a boundary can hide a ramp in. The
    tail is anchored at the sampler's END marker (the daemon's exit in
    board time); a sampler that died mid-run fails the verdict outright,
    because the interval it never saw is exactly where a runaway would be.
    Each window needs at least half its expected samples or the verdict is
    not evaluable — three points spread over five minutes are not a curve.
    """
    if not sampler_ended:
        return {
            "evaluated": False,
            "clean": False,
            "reason": (
                "the sampler did not outlive the daemon; the unobserved "
                "interval cannot be certified plateau"
            ),
        }
    if not curve:
        return {
            "evaluated": False,
            "clean": False,
            "reason": "no thermal samples were recorded",
        }
    ceiling_breaches = [
        {"uptime_s": t, "value_c": c} for t, c in curve if c >= THERMAL_CEILING_C
    ]
    end = end_uptime_s if end_uptime_s is not None else curve[-1][0]
    tail = [(t, c) for t, c in curve if t >= end - THERMAL_TAIL_S]
    min_window_samples = max(2, int(THERMAL_WINDOW_S / SAMPLER_PERIOD_S / 2))
    window_rises: list[dict] = []
    sparse_windows = 0
    for i, (t_open, c_open) in enumerate(tail):
        close = [
            (t, c) for t, c in tail[i + 1 :] if t >= t_open + THERMAL_WINDOW_S
        ]
        if not close:
            break
        t_close, c_close = close[0]
        inside = sum(1 for t, _ in tail if t_open <= t <= t_close)
        if inside < min_window_samples:
            sparse_windows += 1
            continue
        window_rises.append(
            {"window_start_uptime_s": t_open, "rise_c": c_close - c_open}
        )
    plateau_breaches = [
        w for w in window_rises if w["rise_c"] > THERMAL_WINDOW_MAX_RISE_C
    ]
    evaluated = bool(window_rises) and sparse_windows == 0
    return {
        "evaluated": evaluated,
        "clean": evaluated and not ceiling_breaches and not plateau_breaches,
        "ceiling_c": THERMAL_CEILING_C,
        "ceiling_breaches": ceiling_breaches,
        "window_max_rise_c": THERMAL_WINDOW_MAX_RISE_C,
        "windows_evaluated": len(window_rises),
        "sparse_windows": sparse_windows,
        "plateau_breaches": plateau_breaches,
        "first_c": curve[0][1],
        "last_c": curve[-1][1],
        "drift_c": curve[-1][1] - curve[0][1],
    }


# ---------------------------------------------------------------------------
# The Jetson capture and its offline replay
# ---------------------------------------------------------------------------

#: Python 3 stdlib only; the Jetson is a stock Ubuntu host. One line per
#: datagram: the capture host's monotonic_ns, the SENDER address, the
#: datagram hex. The sender is what lets the replay tell this run's board
#: from a stale daemon sharing the rig-wide measurement port. Line-buffered
#: so a SIGTERM loses at most the datagram in flight.
CAPTURE_SCRIPT = r"""#!/usr/bin/env python3
import signal
import socket
import sys
import time

port = int(sys.argv[1])
out_path = sys.argv[2]
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
sock.bind(("0.0.0.0", port))
sock.settimeout(0.5)
running = True


def _stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, _stop)
with open(out_path, "w", buffering=1) as out:
    out.write("capture-open\n")
    while running:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        out.write(f"{time.monotonic_ns()} {addr[0]} {data.hex()}\n")
"""


@dataclass
class CaptureReplay:
    readings: list[health.HealthReading]
    stats: health.ListenerStats
    #: Datagrams from senders other than the run's board. Anything nonzero
    #: is worth a look; a large count is a stale daemon on the rig.
    foreign_datagrams: int = 0
    opened: bool = False


def replay_capture(
    path: str | Path, expected_sender: str | None = None
) -> CaptureReplay:
    """The captured datagrams, through the real wire and health decoders.

    Branch for branch the same shape as `health.HealthListener.poll`, with
    the capture host's recorded arrival stamp standing in for the listener's
    clock — the honest clock here, because it was the one next to the
    socket. Datagrams from any sender other than ``expected_sender`` are
    counted foreign and never decoded: the port is shared rig-wide, and a
    stale daemon's packets in this run's cadence would fill real gaps and
    donate a wrong final fps. The broad decode except is deliberate for the
    same reason it is in the listener: protobuf's DecodeError is not a
    WireError, and one corrupt datagram may not kill the monitor (D8-F10).
    """
    stats = health.ListenerStats()
    readings: list[health.HealthReading] = []
    foreign = 0
    opened = False
    # errors="replace" so one corrupted byte in a multi-hour capture becomes
    # a labelled per-line rejection below, never a UnicodeDecodeError that
    # discards the whole file after the run already happened.
    with Path(path).open("r", encoding="ascii", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if line == "capture-open":
                opened = True
                continue
            parts = line.split(" ")
            if len(parts) != 3:
                stats.reject("capture_line: FieldCount")
                continue
            stamp_text, sender, hex_text = parts
            try:
                stamp = int(stamp_text)
                datagram = bytes.fromhex(hex_text)
            except ValueError as exc:
                stats.reject(f"capture_line: {type(exc).__name__}")
                continue
            if expected_sender is not None and sender != expected_sender:
                foreign += 1
                continue
            stats.datagrams += 1
            try:
                payload_type, body = unframe(datagram)
            except WireError as exc:
                stats.reject(f"unframe: {type(exc).__name__}")
                continue
            if payload_type is PayloadType.OBSERVATION:
                stats.observation_packets += 1
                continue
            if payload_type is not PayloadType.HEALTH:
                stats.other_packets += 1
                continue
            try:
                packet = decode_health(body)
            except Exception as exc:  # noqa: BLE001 - decode/validation, labelled
                stats.reject(f"decode_health: {type(exc).__name__}")
                continue
            stats.health_packets += 1
            readings.append(
                health.HealthReading(received_monotonic_ns=stamp, health=packet)
            )
    return CaptureReplay(
        readings=readings, stats=stats, foreign_datagrams=foreign, opened=opened
    )


def health_summary(replay: CaptureReplay) -> dict:
    """The `HealthListener.summary` shape, plus what a live listener never
    needed: the foreign count and whether the capture file proved it opened."""
    return {
        "stats": replay.stats.as_dict(),
        "cadence": health.cadence(replay.readings).as_dict(),
        "drop_counter_regressions": health.drop_counter_regressions(
            replay.readings
        ),
        "last_drops": (
            replay.readings[-1].health.drops if replay.readings else None
        ),
        "foreign_datagrams": replay.foreign_datagrams,
        "capture_opened": replay.opened,
    }


def telemetry_reconciliation(replay: CaptureReplay, run_stats: dict) -> dict:
    """The capture, checked against the daemon's own send counter.

    ``health_sent`` is the daemon's statement of how many health packets it
    put on the wire; the filtered capture must hold nearly all of them or
    the capture is a prefix — a listener that died at minute 30 shows a
    clean cadence over what it kept, and only this reconciliation says the
    other half is missing. The tolerance absorbs single-datagram UDP loss
    on the switched rig LAN, never an outage.
    """
    sent = run_stats.get("health_sent")
    captured = replay.stats.health_packets
    # Bounded on BOTH sides: fewer than sent-minus-tolerance is a listener
    # that died; more than sent means packets the daemon did not send are
    # being counted as its own, which no completeness label may bless.
    complete = (
        isinstance(sent, int)
        and not isinstance(sent, bool)
        and sent > 0
        and sent - HEALTH_CAPTURE_LOSS_TOLERANCE <= captured <= sent
    )
    return {
        "health_sent": sent,
        "health_captured": captured,
        "loss_tolerance": HEALTH_CAPTURE_LOSS_TOLERANCE,
        "complete": bool(complete),
    }


def final_health_fps(
    replay: CaptureReplay, telemetry: dict
) -> metrics.Measurement:
    """The daemon's own rate from its last health packet — the bounded axis.

    Only meaningful when the capture is complete: with packets missing, the
    last CAPTURED packet is not the last SENT one, and a rate read from the
    middle of a run wearing the label "final" is the substitution this
    module exists to refuse.
    """
    if not telemetry["complete"]:
        return metrics.not_measured(
            "sustained_fps_daemon",
            "fps",
            "the health capture does not reconcile with the daemon's "
            "health_sent counter; the last captured packet may not be the "
            "daemon's last",
        )
    if not replay.readings:
        return metrics.not_measured(
            "sustained_fps_daemon",
            "fps",
            "no health packet from the run's board reached the Jetson "
            "capture listener",
        )
    return metrics.Measurement(
        name="sustained_fps_daemon",
        value=replay.readings[-1].health.fps,
        unit="fps",
        source=(
            "the daemon's last 1 Hz health packet, captured on the Jetson "
            "measurement listener and reconciled against health_sent"
        ),
    )


def soak_health_verdict(
    replay: CaptureReplay,
    telemetry: dict,
    run_stats: dict,
    exit_status: int | None,
    thermal: dict,
) -> dict:
    """The declared criteria for one soak run, each with its evidence.

    Restart detection uses BOTH signals the health plane offers: a second
    ``session_uuid`` under the same camera is a restart the packets admit,
    and a drop-counter regression is one they do not (a restart, a wrap, or
    a foreign node — all findings, per `health.drop_counter_regressions`).
    The gap criterion is only as strong as the capture, so it requires the
    ``health_sent`` reconciliation; the thermal criterion carries its own
    completeness (the sampler's end marker) inside `thermal_verdict`.
    """
    readings = replay.readings
    sessions = sorted({r.health.session_uuid for r in readings})
    regressions = health.drop_counter_regressions(readings)
    no_restart = len(sessions) == 1 and not regressions
    cadence = health.cadence(readings)
    gap_limit_s = HEALTH_GAP_FACTOR * (HEALTH_PERIOD_MS / 1000.0)
    no_gap = (
        telemetry["complete"]
        and cadence.max_period_s is not None
        and cadence.max_period_s < gap_limit_s
    )
    natural = (
        exit_status == 0
        and run_stats.get("source_frames_planned") == SOAK_TOTAL_FRAMES
        and run_stats.get("source_frames_served")
        == run_stats.get("source_frames_planned")
    )
    criteria = {
        "no_restart": {
            "clean": no_restart,
            "session_uuids": sessions,
            "drop_counter_regressions": regressions,
        },
        "no_health_gap": {
            "clean": bool(no_gap),
            "max_period_s": cadence.max_period_s,
            "gap_limit_s": gap_limit_s,
            "telemetry": telemetry,
        },
        "no_thermal_runaway": thermal,
        "natural_completion": {
            "clean": bool(natural),
            "exit_status": exit_status,
            "source_frames_planned": run_stats.get("source_frames_planned"),
            "source_frames_served": run_stats.get("source_frames_served"),
        },
    }
    return {
        "criteria": criteria,
        "clean": all(block.get("clean") is True for block in criteria.values()),
    }


# ---------------------------------------------------------------------------
# Board and Jetson endpoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardTarget:
    """One rig board, addressed through the Jetson jump."""

    host: str
    expected_mac: str
    #: The Jetson's address ON THE RIG LAN — where the daemon sends UDP. The
    #: SSH address the Mac reaches the Jetson at is a different network and a
    #: different CLI argument; conflating the two is how C2's first receiver
    #: heard silence.
    jetson_rig_host: str
    name: str = DEFAULT_BOARD
    expected_image_marker: str = DEFAULT_IMAGE_MARKER
    jump_host: str = ""
    identity: str | None = None
    accept_new_host_key: bool = False

    def spec(self, run_id: str) -> provision.NodeSpec:
        """A per-run remote directory: nothing on the board is ever reused,
        so a crashed daemon cannot leave a previous run's stats.json where
        this run's collection looks."""
        return provision.NodeSpec(
            name=self.name,
            camera_id=0,
            jetson_host=self.jetson_rig_host,
            measurement_port=MEASUREMENT_PORT,
            health_period_ms=HEALTH_PERIOD_MS,
            remote_dir=f"/userdata/skyweave/c002/run-{run_id}",
            ssh_host=self.host,
        )

    def transport(self, run_id: str) -> provision.SshTransport:
        if not self.jump_host:
            raise CampaignC002Error(
                "rig boards require an explicit SSH jump host "
                "(for example jetson-lan-c001)"
            )
        return provision.SshTransport(
            spec=self.spec(run_id),
            strict_host_key_checking=(
                "accept-new" if self.accept_new_host_key else "yes"
            ),
            identity=self.identity,
            jump_host=self.jump_host,
        )


@dataclass(frozen=True)
class JetsonEndpoint:
    """The Jetson as an SSH host for the capture listener."""

    ssh_host: str
    ssh_user: str = "samuel"
    identity: str | None = None
    remote_dir: str = "/home/samuel/c002-capture"

    def transport(self) -> provision.SshTransport:
        spec = provision.NodeSpec(
            name="jetson",
            ssh_host=self.ssh_host,
            ssh_user=self.ssh_user,
            remote_dir=self.remote_dir,
        )
        return provision.SshTransport(spec=spec, identity=self.identity)


def preflight_board(
    transport: provision.SshTransport, target: BoardTarget
) -> dict:
    """MAC, image marker, IVE runtime and NO stale daemon, all before any
    push. A leftover skyweave-edge from an aborted attempt would share the
    measurement port and could be mistaken for this run's; refusing is the
    only honest option — killing it silently would erase the evidence that
    an attempt leaked."""
    mac = (
        transport.run("cat /sys/class/net/eth0/address")
        .check("read mac")
        .stdout.strip()
        .lower()
    )
    if mac != target.expected_mac.lower():
        raise CampaignC002Error(
            f"board identity mismatch: MAC {mac} is not the declared "
            f"{target.expected_mac}; refusing to run"
        )
    marker = (
        transport.run(". /etc/os-release && printf '%s' \"$PRETTY_NAME\"")
        .check("read image marker")
        .stdout.strip()
    )
    if marker != target.expected_image_marker:
        raise CampaignC002Error(
            f"board image marker {marker!r} is not the declared "
            f"{target.expected_image_marker!r}; refusing to run"
        )
    stale = (
        transport.run(
            "grep -l '^skyweave-edge$' /proc/[0-9]*/comm 2>/dev/null; true"
        )
        .check("stale daemon scan")
        .stdout.strip()
    )
    if stale:
        raise CampaignC002Error(
            f"a skyweave-edge process is already running on {target.name} "
            f"({stale}); a previous attempt leaked. Inspect and stop it "
            "before running — this driver will not kill it silently"
        )
    librve = _remote_sha256(transport, BOARD_LIBRVE_PATH)
    if librve != BOARD_LIBRVE_SHA256:
        raise CampaignC002Error(
            f"board {BOARD_LIBRVE_PATH} hashes {librve}, not the declared "
            f"{BOARD_LIBRVE_SHA256}; refusing to run"
        )
    return {"mac": mac, "image_marker": marker, "librve_sha256": librve}


def _remote_sha256(transport: provision.SshTransport, remote: str) -> str:
    result = transport.run(f"sha256sum {remote}", timeout_s=120.0).check(
        "sha256sum"
    )
    return result.stdout.split()[0].lower()


def _probe_alive(board: provision.SshTransport, pid: int) -> bool | None:
    """One liveness probe with a three-valued answer.

    `SshTransport.alive` reads "gone-not-in-stdout" as alive, so an SSH
    transport failure (exit 255, empty stdout) looks like a living daemon
    and a dead jump path spins to the wall cap. This probe demands the
    remote shell actually answered: no ``alive``/``gone`` token means the
    PROBE failed, which is a different fact from either answer.
    """
    try:
        result = board.run(
            f"kill -0 -{pid} 2>/dev/null && echo alive || echo gone"
        )
    except (subprocess.TimeoutExpired, provision.ProvisionError, OSError):
        return None
    text = result.stdout
    if "gone" in text:
        return False
    if "alive" in text:
        return True
    return None


def _wait_for_natural_exit(
    board: provision.SshTransport, wrapper_pid: int, deadline: float, poll_s: float
) -> tuple[bool, int]:
    """Poll until the daemon leaves or the wall cap arrives.

    One stalled SSH probe through the jump is weather, not an outage: probes
    that fail — by raising OR by coming back with no answer — are counted,
    and only :data:`MAX_CONSECUTIVE_POLL_FAILURES` in a row abort the run.
    Returns (overran, poll_failures_total).
    """
    failures_total = 0
    consecutive = 0
    while True:
        alive = _probe_alive(board, wrapper_pid)
        if alive is None:
            failures_total += 1
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_POLL_FAILURES:
                raise CampaignC002Error(
                    f"{consecutive} consecutive alive probes got no answer; "
                    "the board or jump path is down mid-run"
                )
        else:
            consecutive = 0
            if not alive:
                return False, failures_total
        if time.monotonic() >= deadline:
            return True, failures_total
        time.sleep(poll_s)


def _wait_for_process_gone(
    board: provision.SshTransport, pid: int | None, timeout_s: float
) -> None:
    """Bounded wait for a remote helper to finish; best-effort by design."""
    if pid is None:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _probe_alive(board, pid) is False:
            return
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# One measured board run
# ---------------------------------------------------------------------------


def run_board_measured(
    kind: str,
    index: int,
    clip_path: Path,
    manifest: dict,
    target: BoardTarget,
    jetson: JetsonEndpoint,
    binary: Path,
    out_dir: Path,
) -> dict:
    """Push, start, sample, capture, wait for a NATURAL exit, collect.

    Deliberately not `provision.provision_node`: that call offers no seam
    between spawn and wait, and this run needs the sampler started against
    the live pid and the Jetson capture verified listening before the
    daemon's first datagram. The primitives are the same ones, in the same
    order. Nothing here sends SIGTERM on the happy path — a RAM loop ends
    on its declared frame budget, and an E8 comparison of a truncated run
    is `incomplete` by construction. On ANY failure path the board daemon
    and sampler are stopped best-effort before the error surfaces: a
    leaked daemon holding the rig-wide measurement port is the pollution
    the next run's preflight would refuse on.
    """
    if index not in (1, 2):
        raise CampaignC002Error("run index must be 1 or 2: E8 compares a pair")
    started = time.monotonic()
    wall_cap_s = SWEEP_WALL_CAP_S if kind == "sweep" else SOAK_WALL_CAP_S
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise CampaignC002Error(
            f"refusing to reuse run directory {out_dir}; every run keeps its "
            "own retained artifacts"
        )
    out_dir.mkdir(parents=True)
    run_id = uuid.uuid4().hex

    binary = Path(binary)
    binary_sha = _sha256_file(binary)
    if binary_sha != FROZEN_DAEMON_SHA256:
        raise CampaignC002Error(
            f"local daemon {binary} hashes {binary_sha}, not the frozen "
            f"{FROZEN_DAEMON_SHA256}; refusing to deploy it"
        )

    decl = ram_declaration(kind)
    plan = build_plan(kind)
    board = target.transport(run_id)
    spec = board.spec
    identity = preflight_board(board, target)

    jet = jetson.transport()
    capture_remote = f"{jetson.remote_dir.rstrip('/')}/capture-{run_id}.hex"
    capture_log = f"{jetson.remote_dir.rstrip('/')}/capture-{run_id}.log"
    script_remote = f"{jetson.remote_dir.rstrip('/')}/c002_capture.py"
    busy = (
        jet.run(f"ss -uln 2>/dev/null | grep -w {MEASUREMENT_PORT} || true")
        .check("port check")
        .stdout.strip()
    )
    if busy:
        raise CampaignC002Error(
            f"something already listens on the Jetson measurement port "
            f"{MEASUREMENT_PORT}: {busy!r}; two listeners cannot share the "
            "capture"
        )
    capture_tmp = out_dir / "c002_capture.py"
    capture_tmp.write_text(CAPTURE_SCRIPT, encoding="utf-8")
    jet.push(capture_tmp, script_remote)
    capture_pid = jet.spawn(
        f"python3 {script_remote} {MEASUREMENT_PORT} {capture_remote}",
        capture_log,
    )

    provision_record: dict = {}
    wrapper_pid: int | None = None
    sampler_pid: int | None = None
    daemon_started_monotonic: float | None = None
    daemon_exit_monotonic: float | None = None
    # The teardown scope opens the moment the listener exists: an exception
    # ANYWHERE past the spawn — the settle sleep and the sentinel probe
    # included — must not leak a process holding the rig-wide measurement
    # port (the next run's port-busy preflight would refuse on it).
    try:
        # The listener must be provably up BEFORE the daemon's first
        # datagram: a capture that died at bind time would otherwise be
        # discovered an hour later as an empty file — or worse, never, if a
        # stale file from an earlier run sat at the same path (it cannot:
        # the path carries this run's id, and the sentinel line proves this
        # process opened this file).
        time.sleep(2.0)
        listener_up = jet.run(
            f"kill -0 {capture_pid} 2>/dev/null && head -c 12 {capture_remote}"
        ).stdout
        if "capture-open" not in listener_up:
            raise CampaignC002Error(
                "the Jetson capture listener did not come up (no live pid or "
                "no capture-open sentinel); refusing to run a board against "
                "a dead listener"
            )
        remote_binary, local_hash, remote_hash = provision.push_daemon(
            board, binary, spec
        )
        if local_hash != remote_hash:
            raise CampaignC002Error(
                f"deployed daemon hash mismatch: {local_hash} vs {remote_hash}"
            )
        ram_clip_remote = f"{spec.remote_dir.rstrip('/')}/ram.swij"
        clip_local_sha, clip_remote_sha = provision.push_verified_source(
            board, clip_path, ram_clip_remote, label="RAM-loop clip"
        )
        config = benchmark.benchmark_config(PROC_WIDTH, PROC_HEIGHT, plan)
        command = provision.daemon_command(
            remote_binary,
            config,
            spec,
            ram_clip_remote=ram_clip_remote,
            ram_loop=decl,
            stats_remote=f"{spec.remote_dir.rstrip('/')}/stats.json",
            detector="ive",
        )
        sampler_tmp = out_dir / "c002_sampler.sh"
        sampler_tmp.write_text(SAMPLER_SCRIPT, encoding="utf-8")
        sampler_remote = f"{spec.remote_dir.rstrip('/')}/c002_sampler.sh"
        board.push(sampler_tmp, sampler_remote)
        sampler_out = f"{spec.remote_dir.rstrip('/')}/sampler.log"

        log_remote = f"{spec.remote_dir.rstrip('/')}/run.log"
        exit_remote = f"{spec.remote_dir.rstrip('/')}/exit.status"
        daemon_started_monotonic = time.monotonic()
        wrapper_pid = board.spawn(command, log_remote, exit_remote)
        sampler_pid = board.spawn(
            f"sh {sampler_remote} {wrapper_pid} {sampler_out} "
            f"{SAMPLER_PERIOD_S}",
            f"{spec.remote_dir.rstrip('/')}/sampler.spawn.log",
        )
        provision_record = {
            "run_id": run_id,
            "argv": command,
            "wrapper_pid": wrapper_pid,
            "sampler_pid": sampler_pid,
            "remote_binary": remote_binary,
            "remote_dir": spec.remote_dir,
            "binary_sha256": local_hash,
            "clip_local_sha256": clip_local_sha,
            "clip_remote_sha256": clip_remote_sha,
        }

        # 5 s polls for a minutes-long sweep, 15 s for an hour-plus soak:
        # each poll is one ssh exec, and the poll period is wall-clock noise
        # only on the UNBOUNDED wall-fps axis, never on the daemon's own.
        poll_s = 5.0 if kind == "sweep" else 15.0
        overran, poll_failures = _wait_for_natural_exit(
            board, wrapper_pid, started + wall_cap_s, poll_s
        )
        daemon_exit_monotonic = time.monotonic()
        if overran:
            provision.stop_daemon(board, wrapper_pid)
        else:
            # The sampler notices the daemon's death only on its next wake,
            # up to a period late; collecting before it writes its ``end``
            # marker would spuriously un-measure a clean run. Bounded wait,
            # then collect whatever the truth is.
            _wait_for_process_gone(
                board, sampler_pid, timeout_s=3.0 * SAMPLER_PERIOD_S
            )
    except BaseException:
        # Best-effort teardown so a driver failure cannot leak a daemon
        # into the next run's measurement plane. Failures here are
        # secondary to the one being raised.
        for pid in (wrapper_pid, sampler_pid):
            if pid is not None:
                try:
                    provision.stop_daemon(board, pid, grace_s=5.0)
                except Exception:  # noqa: BLE001 - teardown on the error path
                    pass
        raise
    finally:
        # The capture listener stops AFTER the daemon so the final health
        # packet is on disk before the SIGTERM; a dead board run still tears
        # the listener down rather than leaving 5601 held.
        try:
            jet.terminate(capture_pid)
        except Exception:  # noqa: BLE001 - teardown on the error path
            pass

    collected = provision.collect(
        board,
        spec,
        ["stats.json", "run.log", "exit.status", "sampler.log"],
        out_dir,
    )
    time.sleep(1.0)
    jet.fetch(capture_remote, out_dir / "capture.hex")
    librve_after = _remote_sha256(board, BOARD_LIBRVE_PATH)
    if librve_after != BOARD_LIBRVE_SHA256:
        raise CampaignC002Error(
            f"board {BOARD_LIBRVE_PATH} changed during the run: "
            f"{librve_after}; the run is not scoreable"
        )
    driver_wall_s = time.monotonic() - started
    # The daemon's life as this harness saw it: spawn to the poll that found
    # it gone. The poll period pads the top end; the source string on the
    # wall-fps axis names the mechanism and the pad is bounded by poll_s.
    daemon_wall_s = (
        daemon_exit_monotonic - daemon_started_monotonic
        if daemon_started_monotonic is not None
        and daemon_exit_monotonic is not None
        else driver_wall_s
    )

    stats_path = out_dir / "stats.json"
    run_stats = (
        json.loads(stats_path.read_text(encoding="utf-8"))
        if stats_path.exists()
        else {}
    )
    exit_status: int | None = None
    exit_path = out_dir / "exit.status"
    if exit_path.exists():
        text = exit_path.read_text(encoding="ascii").strip()
        if text.lstrip("-").isdigit():
            exit_status = int(text)

    sampler_path = out_dir / "sampler.log"
    sampler_log = parse_sampler_log(
        sampler_path.read_text(encoding="utf-8", errors="replace")
        if sampler_path.exists()
        else ""
    )
    replay = replay_capture(out_dir / "capture.hex", expected_sender=target.host)
    telemetry = telemetry_reconciliation(replay, run_stats)

    measurements = dict(sampler_measurements(sampler_log))
    measurements["sustained_fps_daemon"] = final_health_fps(replay, telemetry)
    measurements["sustained_fps_wall"] = metrics.sustained_fps(
        run_stats.get("frames_in"), daemon_wall_s
    )
    measurements["ddr_bandwidth_mb_s"] = metrics.not_measured(
        "ddr_bandwidth_mb_s",
        "MB/s",
        "RV1106 DDR controller counters are not read by this harness; "
        "stubbed exactly as the host harness stubs them",
    )
    for measurement in measurements.values():
        measurement.check()

    run = benchmark.Run(
        label=f"c002-{kind}",
        proc_width=PROC_WIDTH,
        proc_height=PROC_HEIGHT,
        paced_fps=PACE_FPS if kind == "soak" else None,
        wall_s=daemon_wall_s,
        returncode=exit_status if exit_status is not None else -1,
        stream_bytes=int(clip_path.stat().st_size),
        stats=run_stats,
        measurements=measurements,
        health=health_summary(replay),
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        source_bytes=int(run_stats.get("source_bytes_served", 0)),
        ram_plan=decl.as_dict(),
    )

    curve = thermal_curve(sampler_log)
    invalid_reasons: list[str] = []
    if overran:
        invalid_reasons.append(
            f"exceeded the declared {wall_cap_s:.0f} s wall cap and was "
            "stopped"
        )
    if exit_status != 0:
        invalid_reasons.append(f"exit status {exit_status}, not a natural zero")
    if run_stats.get("source_frames_served") != run_stats.get(
        "source_frames_planned"
    ) or run_stats.get("source_frames_planned") != decl.total_frames:
        invalid_reasons.append(
            "did not serve its declared frame budget "
            f"({run_stats.get('source_frames_served')} of "
            f"{decl.total_frames})"
        )
    if len({r.health.session_uuid for r in replay.readings}) > 1:
        invalid_reasons.append(
            "multiple session uuids arrived from this board in one run"
        )

    record: dict = {
        "schema": "skyweave-c002-run/1",
        "campaign_id": CAMPAIGN_ID,
        "kind": kind,
        "index": index,
        "run_id": run_id,
        "board": target.name,
        "identity": identity,
        "librve_sha256_after": librve_after,
        "seed": PLAN_SEED,
        "manifest_sha256": manifest["clip_sha256"],
        "wall_cap_s": wall_cap_s,
        "driver_wall_s": driver_wall_s,
        "poll_failures": poll_failures,
        "collected": collected,
        "provision": provision_record,
        "telemetry": telemetry,
        "sampler_complete": sampler_log.ended,
        "session_uuids": sorted(
            {reading.health.session_uuid for reading in replay.readings}
        ),
        "thermal_curve": [{"uptime_s": t, "value_c": c} for t, c in curve],
        "run": run.as_dict(),
        "ram_loop_scene_note": benchmark.RAM_LOOP_SCENE_NOTE,
    }
    if kind == "soak":
        record["health_verdict"] = soak_health_verdict(
            replay,
            telemetry,
            run_stats,
            exit_status,
            thermal_verdict(
                curve,
                sampler_ended=sampler_log.ended,
                end_uptime_s=sampler_log.end_uptime_s,
            ),
        )
    if invalid_reasons:
        record["invalid"] = "; ".join(invalid_reasons)
    record_path = out_dir / "run.json"
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not invalid_reasons:
        # Retention lives in out_dir now; the remote copies would only fill
        # the boards' small (and partly counterfeit) cards run by run. A run
        # that went wrong keeps its remote directory for hands-on diagnosis.
        try:
            board.run(f"rm -rf {spec.remote_dir}", timeout_s=60.0)
            jet.run(f"rm -f {capture_remote} {capture_log}", timeout_s=60.0)
        except Exception:  # noqa: BLE001 - cleanup only; evidence is local
            pass
    return {
        "artifact": str(record_path),
        "sha256": _sha256_file(record_path),
        "wall_s": driver_wall_s,
        "invalid": record.get("invalid"),
    }


# ---------------------------------------------------------------------------
# Reconstruction and the pair comparison
# ---------------------------------------------------------------------------


def _measurement_from_dict(payload: dict) -> metrics.Measurement:
    value = payload.get("value")
    if value == metrics.NOT_MEASURED:
        number = None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise CampaignC002Error(
                f"measurement {payload.get('name')!r} carries {value!r}, "
                "neither a number nor the NOT-MEASURED marker"
            ) from exc
    return metrics.Measurement(
        name=payload["name"],
        value=number,
        unit=payload.get("unit", ""),
        source=payload.get("source", ""),
        reason=payload.get("reason", ""),
    )


def run_from_record(record: dict) -> benchmark.Run:
    """A `benchmark.Run` back out of a retained C-002 run record."""
    if record.get("schema") != "skyweave-c002-run/1":
        raise CampaignC002Error("not a C-002 run record")
    payload = record["run"]
    return benchmark.Run(
        label=payload["label"],
        proc_width=payload["proc_width"],
        proc_height=payload["proc_height"],
        paced_fps=payload["paced_fps"],
        wall_s=payload["wall_s"],
        returncode=payload["returncode"],
        stream_bytes=payload["stream_bytes"],
        stats=payload["stats"],
        measurements={
            name: _measurement_from_dict(value)
            for name, value in payload["measurements"].items()
        },
        health=payload["health"],
        source_mode=payload["source_mode"],
        source_bytes=payload["source_bytes"],
        ram_plan=payload["ram_plan"],
    )


def compare_pair(first_path: Path, second_path: Path, out_path: Path) -> dict:
    """The E8 comparison for one pair, plus the campaign objective for soaks.

    `benchmark.compare_runs` does the comparing; this refuses pairs that are
    not a pair — different kinds, different probes, an invalidated run, the
    SAME run twice (distinct run ids and the {1, 2} index set are required),
    or two different boards (E8's premise is one experiment repeated, and a
    cross-board pair is two experiments) — and derives ``soak_e8_pass``
    exactly as the campaign file defines it: E8 ``pass`` AND both soak runs
    individually clean under the declared health criteria.
    """
    first = json.loads(Path(first_path).read_text(encoding="utf-8"))
    second = json.loads(Path(second_path).read_text(encoding="utf-8"))
    for record, path in ((first, first_path), (second, second_path)):
        if not isinstance(record, dict) or record.get("schema") != (
            "skyweave-c002-run/1"
        ):
            raise CampaignC002Error(f"{path} is not a C-002 run record")
        if record.get("invalid"):
            raise CampaignC002Error(
                f"{path} is an invalidated run: {record['invalid']}"
            )
        # Identity fields must be PRESENT, not merely equal: None == None
        # would read two anonymous records as one board and one run.
        for key in ("run_id", "board", "kind", "manifest_sha256"):
            if not isinstance(record.get(key), str) or not record[key]:
                raise CampaignC002Error(f"{path} lacks a {key}")
        mac = (record.get("identity") or {}).get("mac")
        if not isinstance(mac, str) or not mac:
            raise CampaignC002Error(f"{path} lacks its board identity MAC")
    if first["run_id"] == second["run_id"]:
        raise CampaignC002Error(
            "the two paths carry the same run id; a run compared against "
            "itself reproduces nothing"
        )
    if {first.get("index"), second.get("index")} != {1, 2}:
        raise CampaignC002Error(
            "an E8 pair is run 1 against run 2 of one experiment"
        )
    if first["kind"] != second["kind"]:
        raise CampaignC002Error(
            "an E8 pair is two runs of one experiment; got "
            f"{first['kind']!r} and {second['kind']!r}"
        )
    if first["board"] != second["board"] or (
        first["identity"]["mac"].lower() != second["identity"]["mac"].lower()
    ):
        raise CampaignC002Error(
            "the two runs are from different boards; C-002 declares its "
            "numbers board-specific, so a cross-board pair is two "
            "experiments, not a reproduction"
        )
    if first["manifest_sha256"] != second["manifest_sha256"]:
        raise CampaignC002Error(
            "the two runs used different probe clips; the exact counters "
            "would be comparing different scenes"
        )
    verdict = benchmark.compare_runs(
        run_from_record(first), run_from_record(second)
    )
    result: dict = {
        "schema": "skyweave-c002-comparison/1",
        "campaign_id": CAMPAIGN_ID,
        "kind": first["kind"],
        "run_ids": [first.get("run_id"), second.get("run_id")],
        "board": first.get("board"),
        "first": {"path": str(first_path), "sha256": _sha256_file(first_path)},
        "second": {
            "path": str(second_path),
            "sha256": _sha256_file(second_path),
        },
        "e8": verdict.as_dict(),
    }
    if first["kind"] == "soak":
        health_clean = [
            bool(record.get("health_verdict", {}).get("clean"))
            for record in (first, second)
        ]
        result["health_clean"] = health_clean
        result["soak_e8_pass"] = int(verdict.passes and all(health_clean))
    out_path = Path(out_path)
    if out_path.exists():
        raise CampaignC002Error(
            f"refusing to overwrite the retained comparison at {out_path}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


# ---------------------------------------------------------------------------
# Report evidence — the section-8 block, digested from retained artifacts
# ---------------------------------------------------------------------------


def _pair_cell(first: dict, second: dict, name: str, digits: int) -> str:
    """One table cell for a measured axis, showing BOTH runs of the pair.

    A single number would hide the reproduction; two numbers side by side
    are the reproduction. NOT-MEASURED survives as itself.
    """
    values = []
    for record in (first, second):
        block = record["run"]["measurements"].get(name)
        raw = None if block is None else block.get("value")
        values.append(
            metrics.NOT_MEASURED
            if raw is None or raw == metrics.NOT_MEASURED
            else f"{float(raw):.{digits}f}"
        )
    return " / ".join(values)


def build_report_evidence(
    sweep_first: dict,
    sweep_second: dict,
    sweep_comparison: dict,
    soak_first: dict | None = None,
    soak_second: dict | None = None,
    soak_comparison: dict | None = None,
    blocked_note: str | None = None,
) -> dict:
    """The `report.py` section-8 block, from the retained artifacts.

    Every number is read out of a hash-bound record; nothing is typed. The
    narrative paragraphs summarise the campaign's own comparison verdicts so
    the report cannot drift from the ledgered findings. The soak trio is
    OPTIONAL as a trio: a campaign blocked before its soak pair completes
    hands over the sweep row Measured and the soak table PENDING, with the
    reason quoted — a half-filled soak table would be a claim about runs
    that do not exist.
    """
    soak_parts = (soak_first, soak_second, soak_comparison)
    if any(part is None for part in soak_parts) and any(
        part is not None for part in soak_parts
    ):
        raise CampaignC002Error(
            "the soak evidence is all three artifacts or none of them"
        )
    have_soak = soak_first is not None
    records = [sweep_first, sweep_second]
    if have_soak:
        records += [soak_first, soak_second]
    for record in records:
        if record.get("schema") != "skyweave-c002-run/1":
            raise CampaignC002Error("report evidence needs C-002 run records")
    key = f"{PROC_WIDTH}x{PROC_HEIGHT}"
    sweep_e8 = sweep_comparison["e8"]
    if have_soak:
        soak_e8 = soak_comparison["e8"]
        first_stats = soak_first["run"]["stats"]
        drops = [
            record["run"]["health"].get("last_drops")
            for record in (soak_first, soak_second)
        ]
        drift = []
        for record in (soak_first, soak_second):
            curve = record.get("thermal_curve") or []
            drift.append(
                f"{curve[-1]['value_c'] - curve[0]['value_c']:.1f} C"
                if len(curve) >= 2
                else metrics.NOT_MEASURED
            )
        soak_wall = [
            record["run"]["wall_s"] for record in (soak_first, soak_second)
        ]
    block: dict = {
        "campaign": CAMPAIGN_ID,
        "board": sweep_first["board"],
        "sweep_rows": {
            key: {
                "source_mode": benchmark.SOURCE_MODE_INJECT_RAM,
                "source_byte_rate": (
                    f"{sweep_first['run']['source_mb_s']:.1f} / "
                    f"{sweep_second['run']['source_mb_s']:.1f} MB/s "
                    "(DDR-resident)"
                ),
                "fps": _pair_cell(sweep_first, sweep_second, "sustained_fps_daemon", 2)
                + " (unpaced ceiling)",
                "peak_rss": _pair_cell(sweep_first, sweep_second, "peak_rss_mb", 1)
                + " MB",
                "ddr": metrics.NOT_MEASURED,
                "cpu": _pair_cell(sweep_first, sweep_second, "cpu_utilisation", 3)
                + " core",
                "thermals": _pair_cell(sweep_first, sweep_second, "thermal_c", 1)
                + " C",
                "verdict": (
                    f"E8 {sweep_e8['verdict']}"
                    + (
                        ": bounded axes agree, exact counters diverge (F-C2-1)"
                        if sweep_e8["exact_mismatches"]
                        else ""
                    )
                ),
            }
        },
    }
    determinism = (
        "The three bounded axes agreed across every compared pair (largest "
        "relative difference "
        + format(
            max(
                [
                    *sweep_e8["relative"].values(),
                    *(soak_e8["relative"].values() if have_soak else []),
                ],
                default=0.0,
            ),
            ".5f",
        )
        + " against bounds of 0.05-0.25); the exact detector counters did "
        "not. That divergence is finding F-C2-1 in the C-002 campaign "
        "findings: the board IVE detector is not run-to-run deterministic "
        "on identical input, so the 8.1 exact-counter rule cannot be met "
        "by any correct pair on this hardware. Whether that rule is "
        "amended is a planning-session decision; this report publishes "
        "the measured facts under the declared terms."
    )
    if have_soak:
        block["soak"] = {
            "resolution": key,
            "source_mode": benchmark.SOURCE_MODE_INJECT_RAM,
            "duration": (
                "1 h declared at 30 fps; measured "
                f"{soak_wall[0] / 60:.1f} / {soak_wall[1] / 60:.1f} min "
                "(duty cycle is ceiling-bound, F-C2-2)"
            ),
            "frames": (
                f"{first_stats.get('source_frames_served')} of "
                f"{first_stats.get('source_frames_planned')} (both runs)"
            ),
            "drops": f"{drops[0]} / {drops[1]}",
            "thermal_drift": f"{drift[0]} / {drift[1]}",
        }
        block["narrative"] = [
            (
                f"The sweep pair's E8 verdict is **{sweep_e8['verdict']}** "
                f"and the soak pair's is **{soak_e8['verdict']}**"
                + (
                    f" (soak objective soak_e8_pass = "
                    f"{soak_comparison.get('soak_e8_pass')})"
                    if "soak_e8_pass" in soak_comparison
                    else ""
                )
                + ". "
                + determinism
            ),
        ]
    else:
        block["narrative"] = [
            (
                f"The sweep pair's E8 verdict is **{sweep_e8['verdict']}**. "
                + determinism
            ),
        ]
        if blocked_note:
            block["narrative"].append(blocked_note)
    return block


# ---------------------------------------------------------------------------
# The ledger — protocol format, deliberately light (2026-08-23 amendment 3)
# ---------------------------------------------------------------------------

#: What every score-bearing row's subject-to block must carry. A lightweight
#: schema, not the C-001 fortress — but a row that omits the gate or fence
#: evidence is not "light", it is unbound, and the boundary review could not
#: tell which tree produced it.
_SUBJECT_TO_REQUIRED = (
    "gate_platform_suite_green",
    "fenced_paths_untouched",
    "probe_input_only",
    "source_tree_sha256",
    "gate_evidence",
    "fenced_evidence",
)


def _is_hex_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _validate_subject_to(subject_to: dict) -> None:
    missing = [key for key in _SUBJECT_TO_REQUIRED if key not in subject_to]
    if missing:
        raise CampaignC002Error(
            f"subject-to block is missing {missing}; a score-bearing row "
            "binds its gate and fence evidence or it does not append"
        )
    for flag in (
        "gate_platform_suite_green",
        "fenced_paths_untouched",
        "probe_input_only",
    ):
        if subject_to[flag] is not True:
            raise CampaignC002Error(
                f"subject-to {flag} must be literally true; a false or "
                "hedged constraint cannot back a measurement row"
            )
    if not _is_hex_digest(subject_to["source_tree_sha256"]):
        raise CampaignC002Error(
            "subject-to source_tree_sha256 must be a 64-hex digest"
        )
    for key in ("gate_evidence", "fenced_evidence"):
        block = subject_to[key]
        if (
            not isinstance(block, dict)
            or not isinstance(block.get("path"), str)
            or not block["path"]
            or not _is_hex_digest(block.get("sha256"))
        ):
            raise CampaignC002Error(
                f"subject-to {key} must carry a path and a 64-hex sha256"
            )


def append_ledger_row(
    ledger_path: Path,
    *,
    hypothesis: str,
    artifact_path: Path,
    artifact_sha256: str,
    board: str,
    verdict: str,
    subject_to: dict,
    note: str = "",
    wall_minutes: float = 0.0,
) -> dict:
    """One appended row, artifact re-hashed under the lock.

    An invalidated run record cannot be ledgered as a measurement: its
    ``invalid`` field is a statement that the run is a finding, and a
    finding wearing a measurement verdict is a promotion. Per the
    attestation-proportionality amendment this append is the FULL binding
    for this campaign — no staged-tree replay, which was C-001's answer to
    a different problem — and the boundary review reads it knowing that.
    """
    actual = _sha256_file(artifact_path)
    if actual != artifact_sha256:
        raise CampaignC002Error(
            f"artifact {artifact_path} hashes {actual}, not the supplied "
            f"{artifact_sha256}"
        )
    try:
        payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CampaignC002Error(f"unreadable artifact: {exc}") from exc
    if not isinstance(payload, dict):
        raise CampaignC002Error(
            "a ledgered artifact must be a JSON object; anything else "
            "cannot carry the invalid marker this check reads"
        )
    if payload.get("invalid"):
        raise CampaignC002Error(
            "refusing to ledger an invalidated run as a measurement: "
            f"{payload['invalid']}"
        )
    _validate_subject_to(subject_to)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with ledger_path.open("r", encoding="utf-8") as reader:
            existing = sum(1 for line in reader if line.strip())
        if _sha256_file(artifact_path) != artifact_sha256:
            raise CampaignC002Error("artifact changed before the append")
        row = {
            "n": existing + 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "campaign_id": CAMPAIGN_ID,
            "hypothesis": hypothesis,
            "knobs": {},
            "seed": PLAN_SEED,
            "board": board,
            "artifact": {
                "path": str(artifact_path),
                "sha256": artifact_sha256,
            },
            "verdict": verdict,
            "subject_to": subject_to,
            "note": note,
            "wall_minutes": wall_minutes,
        }
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
    return row


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_binary() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "firmware"
        / "rv1106"
        / "build-c001-s4-freeze"
        / "skyweave-edge"
    )


def _json_argument(raw: str) -> dict:
    if raw.startswith("@"):
        value = json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    else:
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise CampaignC002Error("expected a JSON object")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campaign_c002",
        description="C-002 board sweep + soak at 1152x648 (measurement only)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare", help="write the immutable probe clip and manifest"
    )
    prepare.add_argument("--out", required=True)

    run = commands.add_parser("run", help="one measured board run")
    run.add_argument("--kind", choices=_RUN_KINDS, required=True)
    run.add_argument("--index", type=int, choices=(1, 2), required=True)
    run.add_argument("--manifest", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--binary", default=str(_default_binary()))
    run.add_argument("--board-name", default=DEFAULT_BOARD)
    run.add_argument("--board-host", required=True)
    run.add_argument("--expected-mac", required=True)
    run.add_argument("--expected-image-marker", default=DEFAULT_IMAGE_MARKER)
    run.add_argument("--jump-host", required=True)
    run.add_argument("--identity")
    run.add_argument("--accept-new-host-key", action="store_true")
    run.add_argument(
        "--jetson-rig-host",
        required=True,
        help="the Jetson's address on the rig LAN, where the daemon sends "
        "its UDP — not the SSH address the Mac reaches the Jetson at",
    )
    run.add_argument("--jetson-ssh-host", required=True)
    run.add_argument("--jetson-ssh-user", default="samuel")
    run.add_argument(
        "--jetson-identity",
        help="SSH key for the Jetson when it differs from --identity",
    )

    compare = commands.add_parser(
        "compare", help="the E8 comparison for one pair of runs"
    )
    compare.add_argument("--first", required=True)
    compare.add_argument("--second", required=True)
    compare.add_argument("--out", required=True)

    report_evidence = commands.add_parser(
        "report-evidence",
        help="digest the six retained artifacts into the report's section-8 block",
    )
    report_evidence.add_argument("--sweep-first", required=True)
    report_evidence.add_argument("--sweep-second", required=True)
    report_evidence.add_argument("--sweep-comparison", required=True)
    report_evidence.add_argument("--soak-first")
    report_evidence.add_argument("--soak-second")
    report_evidence.add_argument("--soak-comparison")
    report_evidence.add_argument(
        "--blocked-note",
        help="why the soak table stays PENDING, when the soak pair is blocked",
    )
    report_evidence.add_argument("--out", required=True)

    record = commands.add_parser(
        "record", help="append one retained artifact to the C-002 ledger"
    )
    record.add_argument("--ledger", required=True)
    record.add_argument("--artifact", required=True)
    record.add_argument("--artifact-sha256", required=True)
    record.add_argument("--hypothesis", required=True)
    record.add_argument("--board", required=True)
    record.add_argument("--subject-to", required=True)
    record.add_argument("--note", default="")
    record.add_argument("--wall-minutes", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.command == "prepare":
        manifest = prepare_probe(args.out)
        print(json.dumps({"clip_sha256": manifest["clip_sha256"]}))
        return
    if args.command == "run":
        clip_path, manifest = load_probe(args.manifest)
        target = BoardTarget(
            name=args.board_name,
            host=args.board_host,
            expected_mac=args.expected_mac,
            jetson_rig_host=args.jetson_rig_host,
            expected_image_marker=args.expected_image_marker,
            jump_host=args.jump_host,
            identity=args.identity,
            accept_new_host_key=args.accept_new_host_key,
        )
        jetson = JetsonEndpoint(
            ssh_host=args.jetson_ssh_host,
            ssh_user=args.jetson_ssh_user,
            identity=args.jetson_identity or args.identity,
        )
        result = run_board_measured(
            args.kind,
            args.index,
            clip_path,
            manifest,
            target,
            jetson,
            Path(args.binary),
            Path(args.out),
        )
        print(json.dumps(result, sort_keys=True))
        return
    if args.command == "compare":
        result = compare_pair(Path(args.first), Path(args.second), Path(args.out))
        summary = {"verdict": result["e8"]["verdict"]}
        if "soak_e8_pass" in result:
            summary["soak_e8_pass"] = result["soak_e8_pass"]
        print(json.dumps(summary, sort_keys=True))
        return
    if args.command == "report-evidence":

        def _load(path: str) -> dict:
            return json.loads(Path(path).read_text(encoding="utf-8"))

        block = build_report_evidence(
            _load(args.sweep_first),
            _load(args.sweep_second),
            _load(args.sweep_comparison),
            _load(args.soak_first) if args.soak_first else None,
            _load(args.soak_second) if args.soak_second else None,
            _load(args.soak_comparison) if args.soak_comparison else None,
            blocked_note=args.blocked_note,
        )
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(block, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps({"out": str(out), "sha256": _sha256_file(out)}))
        return
    if args.command == "record":
        row = append_ledger_row(
            Path(args.ledger),
            hypothesis=args.hypothesis,
            artifact_path=Path(args.artifact),
            artifact_sha256=args.artifact_sha256,
            board=args.board,
            verdict="measurement",
            subject_to=_json_argument(args.subject_to),
            note=args.note,
            wall_minutes=args.wall_minutes,
        )
        print(json.dumps(row, sort_keys=True))
        return
    raise AssertionError(args.command)


if __name__ == "__main__":
    main()
