"""What a benchmark run measures, and what it refuses to invent.

The D8.1 benchmark asks for five quantities per resolution: sustained fps, peak
memory, DDR bandwidth, A7 utilisation and thermals. Two of them cannot be read
on a development host at all, and the brief is explicit about what to do then:
"Board-only collectors (DDR counters, thermals) may stub with a loud
NOT-MEASURED marker, never a fake number."

So every collector here returns a :class:`Measurement`, which is either a number
WITH the mechanism that produced it, or an absence WITH the reason. There is no
third state and no default value. A caller cannot accidentally treat "no DDR
counter on this machine" as "0 MB/s", because the absent case carries no number
to read — ``value`` is ``None`` and ``as_json()`` is the string
:data:`NOT_MEASURED`.

The mechanism matters as much as the number. Peak RSS read from
``/proc/<pid>/status`` is a kernel high-water mark; the same field sampled with
``ps`` is the largest value a sampler happened to see, which is a different
claim about the same quantity. Both are legitimate; conflating them is not, so
:attr:`Measurement.source` is mandatory and is printed beside the number.

Nothing here samples the frame loop. The node design's section 4 says the
daemon is one process and one thread on a core with no SMP, so a collector
inside the loop would be measuring itself; every collector in this module reads
the daemon from OUTSIDE its process.
"""

from __future__ import annotations

import os
import platform
import resource
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: The marker that appears wherever a number could not be measured. One string,
#: used by the harness artifacts and by the report, so a reader greps once.
NOT_MEASURED = "NOT-MEASURED"

#: How often the out-of-process sampler looks at the daemon. Declared rather
#: than tuned: at 20 ms a 60-frame run at 30 fps is sampled ~100 times, which
#: is enough for a high-water mark to be met, and the sampler costs one small
#: read per interval on a machine that is not the node.
SAMPLE_INTERVAL_S = 0.02

#: Where Linux keeps a process's peak resident set. Present on the board and on
#: the Linux mirror; absent on macOS, which is why there is a second path.
_PROCFS = Path("/proc")

#: Where the kernel exposes thermal zones. The RV1106 has one; a laptop may
#: have none, and "none" is an absence, not a temperature.
_THERMAL_ROOT = Path("/sys/class/thermal")


@dataclass(frozen=True)
class Measurement:
    """One collected quantity, or an honest absence.

    ``value is None`` and ``reason`` non-empty is the absent case; anything
    else is a measurement and must name its ``source``. The constructor does
    not enforce that — :meth:`check` does, and the harness calls it — because a
    dataclass that raises is awkward to build incrementally, and the invariant
    is worth stating in one place rather than defending in five.
    """

    name: str
    value: float | None
    unit: str
    source: str = ""
    reason: str = ""

    @property
    def measured(self) -> bool:
        return self.value is not None

    def check(self) -> None:
        if self.measured and not self.source:
            raise ValueError(
                f"{self.name} carries a number with no source. A benchmark row "
                "that does not say what produced it cannot be compared to the "
                "next one."
            )
        if not self.measured and not self.reason:
            raise ValueError(
                f"{self.name} is absent with no reason. NOT-MEASURED is a "
                "statement about the machine, and a statement needs a subject."
            )

    def as_json(self) -> float | str:
        """The number, or the marker. Never a zero standing in for a number."""
        return self.value if self.measured else NOT_MEASURED

    def rendered(self, digits: int = 2) -> str:
        """One cell for a table: the number with its unit, or the marker with
        its reason. The reason rides along because a reader who sees
        NOT-MEASURED in a report will ask why, and the answer should not be in
        a different document."""
        if not self.measured:
            return f"{NOT_MEASURED} ({self.reason})"
        return f"{self.value:.{digits}f} {self.unit}".strip()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.as_json(),
            "unit": self.unit,
            "source": self.source,
            "reason": self.reason,
        }


def not_measured(name: str, unit: str, reason: str) -> Measurement:
    """An absence with a subject. The only way this module says "no"."""
    return Measurement(name=name, value=None, unit=unit, reason=reason)


# ---------------------------------------------------------------------------
# The collectors
# ---------------------------------------------------------------------------


def sustained_fps(frames_in: int | None, wall_s: float) -> Measurement:
    """Frames the daemon actually consumed, over the wall time it took.

    The daemon writes no fps of its own into ``--stats`` (it puts one in the
    health packet, which is a different question: that one is a running average
    the Jetson sees, this one is the run). It is computed here so the number
    and the interval it covers come from the same place.

    On an UNPACED replay this is a throughput ceiling — how fast the daemon can
    chew — which is exactly the benchmark's question. On a paced soak it is the
    duty cycle and says nothing about the ceiling. Which one a row reports is
    recorded by the run, never inferred from the number.
    """
    if frames_in is None:
        return not_measured(
            "sustained_fps", "fps",
            "the daemon left no stats file, so how many frames it consumed is "
            "unknown — and unknown is not zero",
        )
    if wall_s <= 0.0:
        return not_measured(
            "sustained_fps", "fps", "the run took no measurable wall time"
        )
    return Measurement(
        name="sustained_fps",
        value=frames_in / wall_s,
        unit="fps",
        source="harness wall clock over the daemon's frames_in",
    )


def _procfs_peak_rss_kb(pid: int) -> int | None:
    """VmHWM: the kernel's own high-water mark, no sampling involved."""
    try:
        text = (_PROCFS / str(pid) / "status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmHWM:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _ps_rss_kb(pid: int) -> int | None:
    """RSS right now, via ps. A sample, and labelled as one."""
    if shutil.which("ps") is None:
        return None
    completed = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True, text=True, check=False,
    )
    text = completed.stdout.strip()
    return int(text) if text.isdigit() else None


def _thermal_zones() -> list[Path]:
    if not _THERMAL_ROOT.is_dir():
        return []
    return sorted(
        zone / "temp"
        for zone in _THERMAL_ROOT.iterdir()
        if zone.name.startswith("thermal_zone") and (zone / "temp").exists()
    )


def thermal_c() -> Measurement:
    """The hottest thermal zone the kernel exposes, in degrees C.

    Board-only in practice: macOS has no ``/sys/class/thermal`` and no
    equivalent a normal user can read, so on a development host this is an
    absence with a reason rather than a number somebody would later quote.
    """
    zones = _thermal_zones()
    if not zones:
        return not_measured(
            "thermal_c",
            "C",
            f"no {_THERMAL_ROOT} on this {platform.system()} host; the RV1106 "
            "exposes one and this reads it there",
        )
    readings = []
    for zone in zones:
        try:
            raw = zone.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw.lstrip("-").isdigit():
            # Linux thermal zones report milli-degrees. A zone that reports
            # something else would show up as an absurd number, which is why
            # the conversion is stated here and not hidden in a caller.
            readings.append(int(raw) / 1000.0)
    if not readings:
        return not_measured(
            "thermal_c", "C", f"{len(zones)} thermal zones present, none readable"
        )
    return Measurement(
        name="thermal_c",
        value=max(readings),
        unit="C",
        source=f"max of {len(readings)} {_THERMAL_ROOT} zones",
    )


def ddr_bandwidth_mb_s() -> Measurement:
    """DDR bandwidth. NOT-MEASURED everywhere this harness can run today.

    The node design calls DDR bandwidth "the *real* ceiling" (section 6), so
    this row is the one the D8.1 benchmark most wants — and it is the one no
    host can answer. The number comes from the RV1106's DDR controller
    performance counters, which are a SoC facility: there is no portable
    interface, nothing under ``/proc`` or ``/sys`` on a development machine
    reports it, and the value a laptop could produce would be about the laptop.

    So it stubs, loudly, exactly as the brief permits — and the stub names what
    would fill it, so the board session knows what it is looking for instead of
    discovering an empty column.
    """
    return not_measured(
        "ddr_bandwidth_mb_s",
        "MB/s",
        "RV1106 DDR controller performance counters; no host-side equivalent "
        "exists, so this stays NOT-MEASURED until a node runs the sweep",
    )


@dataclass
class ProcessSample:
    """What the sampler saw. Counters first; the verdict is somebody else's."""

    samples: int = 0
    peak_rss_kb: int | None = None
    source: str = ""
    failure: str = ""
    #: True when the sampler had to FORK to take a reading. It matters far
    #: beyond bookkeeping: those forks are children of the harness, so they
    #: land in the same ``RUSAGE_CHILDREN`` counter the CPU measurement
    #: differences, and a CPU number taken while this is true is partly a
    #: measurement of the sampler. See :meth:`ChildCpuTimer.utilisation`.
    forks_children: bool = False


class ProcessSampler:
    """Watch a live process from outside it, for as long as it lives.

    One thread, one small read per :data:`SAMPLE_INTERVAL_S`. It is started
    with a pid rather than a handle so it can watch a process this harness did
    not spawn — which is what a board run is.
    """

    def __init__(self, pid: int, interval_s: float = SAMPLE_INTERVAL_S) -> None:
        self.pid = pid
        self.interval_s = interval_s
        self.result = ProcessSample()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Chosen in start(), once, so a run cannot change what its peak-RSS
        # column means half way through.
        self._read: Callable[[int], int | None] = _procfs_peak_rss_kb

    def start(self) -> None:
        # Pick the mechanism ONCE, before sampling, so a run cannot silently
        # change what its peak-RSS column means half way through.
        if (_PROCFS / str(self.pid)).is_dir():
            self.result.source = "procfs VmHWM (kernel high-water mark)"
            self._read = _procfs_peak_rss_kb
        elif shutil.which("ps") is not None:
            self.result.source = f"ps RSS sampled every {self.interval_s * 1e3:.0f} ms"
            self._read = _ps_rss_kb
            self.result.forks_children = True
        else:
            self.result.failure = (
                "no /proc and no ps on this machine; peak RSS cannot be read "
                "from outside the process"
            )
            return
        self._thread = threading.Thread(target=self._run, name="skyweave-sampler",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            value = self._read(self.pid)
            if value is None:
                # The process is gone, or the mechanism stopped working. Either
                # way there is nothing further to see; what was already sampled
                # stands.
                break
            self.result.samples += 1
            if self.result.peak_rss_kb is None or value > self.result.peak_rss_kb:
                self.result.peak_rss_kb = value
            self._stop.wait(self.interval_s)

    def stop(self) -> ProcessSample:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return self.result

    def peak_rss(self) -> Measurement:
        if self.result.peak_rss_kb is None:
            return not_measured(
                "peak_rss_mb",
                "MB",
                self.result.failure
                or "the process exited before any sample landed",
            )
        return Measurement(
            name="peak_rss_mb",
            # Through BYTES and out in DECIMAL MB, which is what the unit
            # string has always said and what every other memory number in
            # this phase means: RAM_LOOP_BUDGET_BYTES is 160_000_000, the
            # daemon's own budget line is `ram_budget_mb * 1000000ULL`, and
            # the report says "the 160 MB is read as DECIMAL bytes".
            #
            # Both feeders are KiB, not kB: /proc/<pid>/status prints VmHWM as
            # `size << (PAGE_SHIFT-10)`, and `ps -o rss=` is in 1024-byte
            # blocks on Linux and Darwin alike. Dividing by 1024 therefore
            # produced MiB wearing the label MB — 4.86% low, which reported a
            # 164,167,680 B process as "156.56 MB", under a 160 MB line it was
            # over. `eval/scorecard.py` already converts the same quantity
            # decimally; this is now the same convention rather than the only
            # binary one in the repo.
            value=self.result.peak_rss_kb * 1024 / 1e6,
            unit="MB",
            source=self.result.source,
        )


def cpu_utilisation(usage: resource.struct_rusage | None, wall_s: float) -> Measurement:
    """CPU seconds per wall second, as a fraction of ONE core.

    On the node that is the A7 utilisation outright: one core, one thread, no
    SMP. On a multi-core host it is the same ratio, and it can exceed 1.0 only
    if the daemon grew a thread, which would itself be the finding.

    ``usage`` must be the rusage for THAT PROCESS, as ``os.wait4`` returns it.
    The obvious alternative — differencing ``getrusage(RUSAGE_CHILDREN)``
    around the subprocess — is what this used to do, and it is wrong in a way
    that only shows up on some machines: that counter accumulates over EVERY
    reaped child, so on a host without ``/proc``, where the peak-RSS sampler
    falls back to forking ``ps`` fifty times a second, the difference is the
    daemon's CPU plus the sampler's. Measured on this Mac: 0.08 core of
    "utilisation" attributed to a daemon that did not exist. ``wait4`` asks
    the kernel about one pid and cannot be contaminated by another.
    """
    if usage is None:
        return not_measured(
            "cpu_utilisation", "core",
            "the process was not reaped by this harness, so the kernel's "
            "per-process accounting for it was never collected",
        )
    if wall_s <= 0.0:
        return not_measured(
            "cpu_utilisation", "core", "the run took no measurable wall time"
        )
    return Measurement(
        name="cpu_utilisation",
        value=(usage.ru_utime + usage.ru_stime) / wall_s,
        unit="core",
        source="os.wait4 rusage for the daemon's own pid",
    )


#: The five benchmark axes, with where each one comes from and whether a
#: development host can answer it. The report prints this table, so the
#: "what will be missing on the board session" conversation happens before the
#: board session rather than in it.
COLLECTOR_REGISTRY = (
    ("sustained fps", "harness wall clock over the daemon's frames_in", "host and board"),
    # The unit is stated in the mechanism cell because both sources report
    # KiB and the column is DECIMAL MB: the conversion used to divide by 1024
    # and publish MiB under the label MB, 4.86% low against a budget every
    # other number in this phase reads decimally.
    ("peak RSS",
     "/proc/<pid>/status VmHWM, else sampled ps RSS; both KiB, reported in "
     "decimal MB",
     "host and board"),
    ("A7 utilisation", "os.wait4 rusage for the daemon's pid", "host and board"),
    ("DDR bandwidth", "RV1106 DDR controller counters", "board only — NOT-MEASURED on a host"),
    ("thermals", "/sys/class/thermal/thermal_zone*/temp", "board and Linux hosts only"),
)


def host_description() -> str:
    """One line naming the machine a measurement came off.

    Recorded in the run artifact, never in the committed report: it changes
    between machines, and the report is byte-identical for identical inputs.
    """
    return f"{platform.system()} {platform.machine()} ({os.cpu_count()} cpus)"


def monotonic_s() -> float:
    return time.monotonic()


__all__ = [
    "COLLECTOR_REGISTRY",
    "Measurement",
    "NOT_MEASURED",
    "ProcessSample",
    "ProcessSampler",
    "SAMPLE_INTERVAL_S",
    "cpu_utilisation",
    "ddr_bandwidth_mb_s",
    "host_description",
    "monotonic_s",
    "not_measured",
    "sustained_fps",
    "thermal_c",
]
