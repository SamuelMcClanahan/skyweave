"""Runbook A4: the RAM-loop source, exercised against the HOST-built daemon.

What this file gates and what it does NOT:

- it gates the SOURCE — that a preloaded clip serves a DECLARED frame budget
  and then ends cleanly, that frame_seq stays continuous across every wrap so
  warm-up runs exactly once, that capture time advances by the harness's
  declared per-pass stride and never repeats, that every declaration is
  required rather than defaulted, that the RAM budget is a check the daemon
  EXECUTES rather than a sentence in a document, and that pacing moves the wall
  clock without moving one byte on the wire;
- it does not gate a board. Every byte here is served out of this host's DDR by
  the host-built daemon, and the arithmetic pinned below is DERIVED from the
  backends' own allocator sizes — no reading of any kind, on any machine.

This is NOT E9. The E-series is a reported, closed list, E1 through E8, and so
is the W-series; a runbook item gets a ``test_d81_*`` file of its own, beside
``test_d81_provisioning.py`` and ``test_d81_image_build.py``. Assertions whose
SUBJECT is an existing axis live in that axis's file instead: replay
determinism in E3, PTS honesty in E4, the sweep record and the E8 comparison in
E8, the health cadence under a pace in E7, and the source refusal in
``test_d81_provisioning.py``.

Resolutions here are SMALL, for the reason test_e8 states: the RAM loop is
resolution-independent orchestration, and a 2304x1296 row would put minutes
into every suite run. The full-resolution numbers are covered by pinning the
ARITHMETIC that produces them.

Every test takes the session ``edge_build_dir`` fixture, which ASSERTS rather
than skips on a compile failure — so A4's "host-buildable and host-tested like
every other source" is enforced here, not claimed.
"""

from __future__ import annotations

import json
import math
import re
import socket
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from skyweave2.contracts import ClockDomain
from skyweave2.edge import benchmark, daemon
from skyweave2.edge.injection import (
    InjectionFrame,
    PtsProfile,
    encode_frame_record,
    encode_session_header,
    encode_trailer,
    read_injection_session,
)
from skyweave2.transport import codec
from skyweave2.transport.wire import unframe

#: Small enough that a whole 24-frame run is milliseconds, large enough that
#: the six movers survive the opening — the same trade test_e8 records.
RAM_RESOLUTION = (288, 162)
#: The clip on disk, and the run it serves. Two different numbers on purpose:
#: a loop's length and a run's length are unrelated, and conflating them is how
#: "it ran 24 frames" turns into "it ran the clip once".
CLIP_FRAMES = 6
TOTAL_FRAMES = 24
#: 6 frames at 30 fps. ASSERTED equal to the harness's own arithmetic below
#: rather than assumed: a literal that drifted from the formula would make
#: every wrap assertion in this file agree with the wrong number.
PTS_STRIDE_NS = 200_000_000

SESSION = "d81-ram-loop-0000-0000-000000001"


def _plan(warmup_frames: int = 2, clip_frames: int = CLIP_FRAMES,
          frames: int = TOTAL_FRAMES) -> benchmark.BenchmarkPlan:
    return benchmark.BenchmarkPlan(
        frames=frames,
        warmup_frames=warmup_frames,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        ram_clip_frames=clip_frames,
    )


def _write_clip(path: Path, plan: benchmark.BenchmarkPlan,
                profile: PtsProfile | None = None,
                clip_frames: int = CLIP_FRAMES,
                resolution: tuple[int, int] = RAM_RESOLUTION) -> Path:
    """The clip a RAM loop reads: an ORDINARY SWIJ file, no format change.

    Written through ``write_benchmark_stream``'s ``frame_count``, which is the
    one writer, so the clip cannot drift from the PTS function the daemon's
    preload guard checks it against.
    """
    benchmark.write_benchmark_stream(
        path, plan, resolution[0], resolution[1], session_uuid=SESSION,
        profile=profile, frame_count=clip_frames,
    )
    return path


def _declaration(plan: benchmark.BenchmarkPlan, period_ns: int = 0,
                 resolution: tuple[int, int] = RAM_RESOLUTION,
                 total_frames: int = TOTAL_FRAMES) -> daemon.RamLoopDeclaration:
    return daemon.RamLoopDeclaration(
        clip_frames=plan.ram_clip_frames,
        total_frames=total_frames,
        pts_stride_ns=int(round(plan.ram_clip_frames / plan.fps * 1e9)),
        budget_mb=plan.ram_budget_mb,
        period_ns=period_ns,
    )


def _run_ram(clip: Path, plan: benchmark.BenchmarkPlan, work_dir: Path,
             build_dir: Path, declaration: daemon.RamLoopDeclaration | None = None,
             resolution: tuple[int, int] = RAM_RESOLUTION,
             extra_args: list[str] | None = None) -> daemon.DaemonRun:
    """One RAM-loop run, through the path that also writes a packet log.

    ``run_daemon_on_stream`` rather than ``run_daemon_measured``: the wrap
    arithmetic is asserted against the datagrams the daemon actually emitted,
    and only this path writes ``--packet-log``.
    """
    return daemon.run_daemon_on_stream(
        clip,
        benchmark.benchmark_config(resolution[0], resolution[1], plan),
        work_dir,
        build_dir=build_dir,
        ram_loop=declaration if declaration is not None else _declaration(plan),
        extra_args=extra_args,
    )


def _run_raw(build_dir: Path, work_dir: Path, source_args: list[str],
             plan: benchmark.BenchmarkPlan,
             resolution: tuple[int, int] = RAM_RESOLUTION,
             extra_args: list[str] | None = None):
    """Run the daemon with a HAND-BUILT source argv.

    The refusal tests need argv shapes ``RamLoopDeclaration`` cannot produce —
    a missing ``--ram-loop-frames``, a stride nobody declared — which is the
    whole point of testing that nothing is silently defaulted. Returns the
    ``CompletedProcess`` and the stats path, which for a refusal must not exist.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    stats_path = work_dir / "stats.json"
    config = benchmark.benchmark_config(resolution[0], resolution[1], plan)
    # A real bound socket, unread, for the reason run_daemon_on_stream gives:
    # without one, loopback replies port-unreachable and the next send fails.
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    try:
        argv = [
            str(daemon.daemon_path(build_dir)),
            *source_args,
            "--stats", str(stats_path),
            "--measurement-port", str(sink.getsockname()[1]),
            "--control-port", "0",
        ] + daemon.daemon_args(config, detector="soft") + list(extra_args or [])
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=300.0, check=False
        )
    finally:
        sink.close()
    return completed, stats_path


def _clip_frames_on_disk(clip: Path) -> list:
    from skyweave2.edge.injection import iter_injection_frames

    with clip.open("rb") as handle:
        read_injection_session(handle)
        return list(iter_injection_frames(handle))


def _envelopes(run: daemon.DaemonRun) -> list:
    return [
        codec.decode_observation_packet(unframe(packet)[1])[0]
        for packet in run.packets
    ]


# ---------------------------------------------------------------------------
# D2: a source with no trailer still ends, and it ends on a DECLARATION
# ---------------------------------------------------------------------------


def test_the_declared_frame_budget_is_what_ends_a_run(edge_build_dir, tmp_path):
    """A RAM loop has no trailer, so something else has to end it.

    That something is a declared FRAME COUNT and never a duration, because
    every one of ``benchmark.EXACT_COUNTERS`` is monotone in frames served: a
    wall-clock bound would make a slow CI box indistinguishable from a real
    determinism defect. The end travels through the UNCHANGED 0/1/-1 contract
    of ``sw_inject_next`` — status 1, the same clean end a trailer gives — so
    the daemon takes its ordinary exit path and writes its stats.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    run = _run_ram(clip, plan, tmp_path / "run", edge_build_dir)

    assert run.returncode == 0, run.stderr
    assert run.stats["frames_in"] == TOTAL_FRAMES
    assert run.stats["source_frames_planned"] == TOTAL_FRAMES
    assert run.stats["source_frames_served"] == TOTAL_FRAMES
    assert run.stats["ram_clip_frames"] == CLIP_FRAMES
    assert run.stats["source_mode"] == benchmark.SOURCE_MODE_INJECT_RAM
    # Non-vacuous. A loop that served 24 frames of nothing would satisfy every
    # counter above: the clip is built with movers from frame 0 precisely so
    # the detector has something to find on every pass.
    assert run.stats["capture_events"] > 0
    assert run.stats["observations_sent"] > 0


def test_frame_seq_is_continuous_across_a_wrap_so_warm_up_runs_once(
    edge_build_dir, tmp_path
):
    """The reason the loop renumbers instead of replaying stored frame_seq.

    ``main.c`` decides warm-up with ``envelope.frame_seq < warmup_frames``. A
    frame_seq that reset on every wrap would re-enter warm-up on every pass, and
    at a warm-up above the clip length it would discard EVERY frame — frames_in
    large, frames_scored zero, a run that produces no measurement at all while
    exiting 0. The EXACT number is what makes this a gate: 24 served minus 8
    warmed is 16, and nothing else.
    """
    plan = _plan(warmup_frames=8)
    assert plan.warmup_frames > CLIP_FRAMES, (
        "the warm-up has to outlast the clip, or a per-wrap reset would still "
        "score every frame and this test would pass under the defect"
    )
    clip = _write_clip(tmp_path / "clip.swij", plan)
    run = _run_ram(clip, plan, tmp_path / "run", edge_build_dir)

    assert run.returncode == 0, run.stderr
    assert run.stats["frames_in"] == TOTAL_FRAMES
    assert run.stats["frames_scored"] == TOTAL_FRAMES - plan.warmup_frames == 16


# ---------------------------------------------------------------------------
# D1: the advance is the harness's declaration, carried forward
# ---------------------------------------------------------------------------


def test_capture_time_never_goes_backwards_across_a_wrap(edge_build_dir, tmp_path):
    """The wrap arithmetic, asserted on the datagrams the daemon emitted.

    Replaying the clip's stored timestamps unchanged would collide frame_seq,
    reset capture time on every pass and re-trigger warm-up, all of it silently.
    What the daemon does instead is add the harness's DECLARED per-pass stride —
    ``capture_ts_ns[p*N + i] == clip[i].capture_ts_ns + p * stride`` — and that
    equality, not a monotonicity check alone, is what says the daemon carried a
    declaration rather than formed an opinion.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    stored = _clip_frames_on_disk(clip)
    assert len(stored) == CLIP_FRAMES
    run = _run_ram(clip, plan, tmp_path / "run", edge_build_dir)
    assert run.returncode == 0, run.stderr

    stride = run.stats["ram_loop_pts_stride_ns"]
    assert stride == PTS_STRIDE_NS
    envelopes = _envelopes(run)
    assert envelopes, "the daemon sent nothing to compare"
    # More than one pass reached the wire, or the wrap is untested.
    assert max(e.frame_seq for e in envelopes) >= CLIP_FRAMES

    seqs = [e.frame_seq for e in envelopes]
    times = [e.capture_ts_ns for e in envelopes]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(b > a for a, b in zip(times, times[1:], strict=False))
    for envelope in envelopes:
        passes, slot = divmod(envelope.frame_seq, CLIP_FRAMES)
        assert envelope.capture_ts_ns == (
            stored[slot].capture_ts_ns + passes * stride
        ), envelope.frame_seq


def test_a_clip_not_numbered_from_zero_is_refused(edge_build_dir, tmp_path):
    """The loop continues the harness's numbering, so it has to start at 0.

    ``ram_next`` sets ``frame_seq = index`` and reads slot ``index % N``: if the
    stored clip were numbered from 5, the emitted numbering and the emitted
    PIXELS would come from different frames on every pass, and nothing
    downstream could tell. The daemon refuses rather than renumbering.

    The second half is what makes it an ADDED invariant rather than a general
    parser change: the SAME BYTES still replay under ``--inject-file``, so no
    existing behaviour was tightened to buy this refusal.
    """
    plan = _plan()
    width, height = RAM_RESOLUTION
    session = benchmark.benchmark_session(
        replace(plan, frames=CLIP_FRAMES, warmup_frames=0), width, height,
        SESSION, CLIP_FRAMES,
    )
    clip = tmp_path / "offset.swij"
    with clip.open("wb") as handle:
        handle.write(encode_session_header(session))
        for index, luma in enumerate(
            benchmark.iter_scene_frames(
                replace(plan, frames=CLIP_FRAMES, warmup_frames=0), width, height
            )
        ):
            handle.write(
                encode_frame_record(
                    InjectionFrame(
                        frame_seq=index + 5,
                        capture_ts_ns=int(round((index + 5) / plan.fps * 1e9)),
                        time_sync_error_ms=0.0,
                        luma=luma,
                    )
                )
            )
        handle.write(encode_trailer(CLIP_FRAMES))

    refused, stats_path = _run_raw(
        edge_build_dir, tmp_path / "ram",
        _declaration(plan).as_daemon_args(str(clip)), plan,
    )
    assert refused.returncode != 0
    assert not stats_path.exists(), "a refused run left a stats file behind"
    assert "numbered 0..N-1" in refused.stderr

    accepted, _ = _run_raw(
        edge_build_dir, tmp_path / "file", ["--inject-file", str(clip)], plan,
    )
    assert accepted.returncode == 0, accepted.stderr


def test_a_known_lie_clip_may_not_become_a_looped_source(edge_build_dir, tmp_path):
    """A fixture that declares OVERRIDDEN time_sync_error_ms is a known lie.

    Under ``--inject-file`` it is allowed and announced, because a labelled lie
    is a legitimate D6 experiment. As a LOOPED SWEEP SOURCE it is not: the loop
    copies ``time_sync_error_ms`` byte for byte from the clip onto every pass,
    so a sweep run against it would carry a zero declaration over an hour of
    fabricated timestamps and be filed as a benchmark rather than as a lie.

    The header flag is set here by hand. ``benchmark.write_benchmark_stream``
    cannot currently produce one — ``benchmark_session`` never threads the
    profile into ``declaration_overridden`` the way ``injection.py``'s builder
    does — so a test that went through the harness would exercise a clip whose
    flag is CLEAR and would need an inverted assertion to be green.
    """
    plan = _plan()
    width, height = RAM_RESOLUTION
    clip_plan = replace(plan, frames=CLIP_FRAMES, warmup_frames=0)
    session = benchmark.benchmark_session(
        clip_plan, width, height, SESSION, CLIP_FRAMES
    )
    profile = PtsProfile(offset_ms=25.0, declared_error_ms_override=0.0)
    assert not profile.is_honest
    clip = tmp_path / "lie.swij"
    with clip.open("wb") as handle:
        handle.write(
            encode_session_header(replace(session, declaration_overridden=True))
        )
        for index, luma in enumerate(
            benchmark.iter_scene_frames(clip_plan, width, height)
        ):
            handle.write(benchmark._frame_record(clip_plan, profile, luma, index))
        handle.write(encode_trailer(CLIP_FRAMES))

    refused, stats_path = _run_raw(
        edge_build_dir, tmp_path / "ram",
        _declaration(plan).as_daemon_args(str(clip)), plan,
    )
    assert refused.returncode != 0
    assert not stats_path.exists()
    assert "OVERRIDDEN" in refused.stderr

    # ...and the same bytes still run as a plain file source, warned about but
    # not refused. The RAM loop added an invariant; it did not tighten one.
    accepted, _ = _run_raw(
        edge_build_dir, tmp_path / "file", ["--inject-file", str(clip)], plan,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "OVERRIDDEN" in accepted.stderr


@pytest.mark.parametrize(
    ("dropped", "expected"),
    [
        ("--ram-loop-frames", "--ram-loop-frames"),
        ("--ram-loop-pts-stride-ns", "--ram-loop-pts-stride-ns"),
    ],
)
def test_every_ram_declaration_is_required(edge_build_dir, tmp_path, dropped, expected):
    """Nothing about a RAM loop is silently defaulted.

    The whole D1 argument is that the daemon CARRIES the harness's declarations
    rather than deriving them. A default would quietly turn one of them into the
    daemon's own opinion — and a zero frame budget in particular would be an
    unbounded run, which D2 refused to make reachable by choosing a flag.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    full = _declaration(plan).as_daemon_args(str(clip))
    index = full.index(dropped)
    trimmed = full[:index] + full[index + 2:]

    completed, stats_path = _run_raw(edge_build_dir, tmp_path / "run", trimmed, plan)
    assert completed.returncode != 0
    assert not stats_path.exists()
    assert expected in completed.stderr


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        (("--ram-loop-frames", "0"), "--ram-loop-frames"),
        (("--ram-loop-pts-stride-ns", "0"), "--ram-loop-pts-stride-ns"),
        (("--ram-loop-pts-stride-ns", "-1"), "--ram-loop-pts-stride-ns"),
    ],
)
def test_a_declaration_that_declares_nothing_is_refused(
    edge_build_dir, tmp_path, mutation, needle
):
    """Present-but-empty is the same failure as absent, and is refused the same.

    A ``--ram-loop-frames 0`` that was accepted would be an unbounded run
    wearing a declaration; a non-positive stride would be a capture time that
    stands still or walks backwards across the wrap.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    argv = list(_declaration(plan).as_daemon_args(str(clip)))
    argv[argv.index(mutation[0]) + 1] = mutation[1]

    completed, stats_path = _run_raw(edge_build_dir, tmp_path / "run", argv, plan)
    assert completed.returncode != 0
    assert not stats_path.exists()
    assert needle in completed.stderr


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        # strtoul is DEFINED to return ULONG_MAX here; the cast made it
        # 4294967295 and the run never ended.
        ("--ram-loop-frames", "-1"),
        # Wraps modulo 2^32 to 2, a run three orders shorter than declared.
        ("--ram-loop-frames", "4294967298"),
        # Stops at the comma: a 108000-frame soak became 200 frames, and every
        # counter in the record was self-consistent about the wrong run.
        ("--ram-loop-frames", "200,000"),
        ("--ram-loop-frames", "12x"),
        ("--ram-loop-frames", ""),
        # Stops at the '.': "33.3e6" became 33 ns, so a paced soak ran unpaced
        # with every frame late and no refusal anywhere.
        ("--ram-loop-period-ns", "33.3e6"),
        ("--ram-loop-pts-stride-ns", "2e8"),
    ],
)
def test_a_numeric_declaration_that_was_not_understood_is_refused(
    edge_build_dir, tmp_path, flag, value
):
    """Refuse, never truncate — the rule the old comment claimed and broke.

    The comment above the parse said ``strtoul``/``strtoll`` were chosen over
    ``atoi`` so a declaration would not "truncate ... into a shorter run that
    still looked declared", and then took the value with no ``endptr`` check,
    no ``errno`` check, and a narrowing cast. Every value below produced a
    RUNNING daemon: rc=0 with a self-consistent record of a run nobody asked
    for, or, for ``-1``, a loop with no end. ``sw_config_validate`` cannot see
    any of them — its only test is ``< 1`` on an unsigned field, which a
    wrapped negative satisfies exactly never.

    ``--ram-loop-frames 4294967296`` and ``abc`` are deliberately NOT in this
    list: they wrap to 0 and parse to 0, so the old code refused them BY
    ACCIDENT, and a test that only used those would have passed throughout.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    argv = list(_declaration(plan).as_daemon_args(str(clip)))
    argv[argv.index(flag) + 1] = value

    completed, stats_path = _run_raw(edge_build_dir, tmp_path / "run", argv, plan)
    assert completed.returncode != 0, completed.stderr
    assert not stats_path.exists(), "a refused declaration produced counters"
    assert flag in completed.stderr
    assert "refusing rather than truncating" in completed.stderr


def test_a_warmup_at_or_past_the_frame_budget_is_refused(edge_build_dir, tmp_path):
    """A run that is all warm-up scores nothing and still exits 0.

    Because frame_seq is continuous across wraps, a warm-up longer than the
    clip is ordinary and correct — but a warm-up at or beyond the BUDGET means
    the whole run is warm-up, and the sweep row would be an fps number over
    zero scored frames.
    """
    plan = _plan(warmup_frames=TOTAL_FRAMES)
    clip = _write_clip(tmp_path / "clip.swij", plan)
    completed, stats_path = _run_raw(
        edge_build_dir, tmp_path / "run",
        _declaration(plan).as_daemon_args(str(clip)), plan,
    )
    assert completed.returncode != 0
    assert not stats_path.exists()
    assert "nothing would be scored" in completed.stderr


def test_a_declared_stride_shorter_than_the_clip_is_refused(edge_build_dir, tmp_path):
    """The exact-integer cross-check that makes the whole advance safe.

    A stride no longer than the clip's own span folds the loop back onto
    itself: pass 2's early frames would carry timestamps before pass 1's last,
    capture time would stop increasing at every wrap, and every downstream
    consumer would see a monotonic clock go backwards once every N frames. The
    daemon cannot know the harness's fps, so this is the check that catches a
    mis-declared stride, and it is integer arithmetic on the clip it just read.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    stored = _clip_frames_on_disk(clip)
    span_ns = stored[-1].capture_ts_ns - stored[0].capture_ts_ns
    assert 0 < span_ns < PTS_STRIDE_NS, span_ns

    argv = list(_declaration(plan).as_daemon_args(str(clip)))
    argv[argv.index("--ram-loop-pts-stride-ns") + 1] = str(span_ns)
    completed, stats_path = _run_raw(edge_build_dir, tmp_path / "run", argv, plan)
    assert completed.returncode != 0
    assert not stats_path.exists()
    assert "not longer than" in completed.stderr
    assert "increase across the wrap" in completed.stderr

    # One nanosecond more is enough: the check is an exact integer comparison,
    # not a margin somebody chose.
    argv[argv.index("--ram-loop-pts-stride-ns") + 1] = str(span_ns + 1)
    accepted, accepted_stats = _run_raw(
        edge_build_dir, tmp_path / "ok", argv, plan
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted_stats.exists()


def test_the_ram_source_is_exclusive_with_the_other_three(edge_build_dir, tmp_path):
    """Two sources in one argv is a refusal on the C side too, not just the host.

    ``provision.daemon_command`` counts its three sources and refuses; that
    check is only worth something if the daemon counts the same way, or a
    hand-typed bench command line would run against a source nobody declared.

    The second half is the POSITIVE CONTROL for that refusal: it proves
    ``RamLoopDeclaration.as_daemon_args`` emits a flag the daemon's parser turns
    into the RAM kind, so the refusal above is a refusal about source COUNT and
    not about an argv the daemon never understood. That contract is killable —
    make ``--inject-ram`` set ``SW_SOURCE_INJECT_FILE`` in ``sw_config.c`` and
    this line fails on ``source_mode``.

    What it does NOT cover, said plainly because this paragraph used to claim the
    opposite: main.c's open dispatch. ``source_mode`` is
    ``sw_source_name(config->source)`` (main.c's ``write_stats``), fixed by argv
    parsing before any open runs, and ``SW_SOURCE_INJECT_RAM`` deliberately
    shares ``sw_inject_open_file`` with the file source — so restoring the
    pre-D8.1 catch-all ``else`` is control-flow-equivalent on every state this
    program can reach, and a mutant carrying it passes this test with
    byte-identical stats. The explicit ``else`` arm is correct defensive code
    with no runtime coverage; a fifth source kind is caught at COMPILE time by
    the ``default:``-less switch in ``sw_source_name`` under ``-Wall -Werror``.
    No test is added for the arm: reaching it needs an enumerator that does not
    exist, and a test that fabricates an impossible state is not evidence.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    both = _declaration(plan).as_daemon_args(str(clip)) + [
        "--inject-file", str(clip)
    ]
    completed, stats_path = _run_raw(edge_build_dir, tmp_path / "both", both, plan)
    assert completed.returncode != 0
    assert not stats_path.exists()
    assert "choose exactly one source" in completed.stderr

    alone = _run_ram(clip, plan, tmp_path / "alone", edge_build_dir)
    assert alone.returncode == 0, alone.stderr
    assert alone.stats["source_mode"] == benchmark.SOURCE_MODE_INJECT_RAM


# ---------------------------------------------------------------------------
# D4: the budget is a check the daemon EXECUTES
# ---------------------------------------------------------------------------


def _budget_total_bytes(stderr: str) -> int:
    """The daemon's own total, off its INFO line. Never recomputed here."""
    match = re.search(r"= (\d+) B against a declared", stderr)
    assert match, f"the daemon printed no RAM budget line:\n{stderr}"
    return int(match.group(1))


def test_the_budget_check_refuses_rather_than_fitting_the_number(
    edge_build_dir, tmp_path
):
    """A check the runbook asks for that nothing executes is a claim.

    So the daemon computes it, at the one point where both the ADOPTED proc grid
    and the detector's real allocation total are known, prints every term, and
    refuses. Refusing rather than clamping is the house rule: a clamp here would
    produce a 12-frame sweep that the artifact reports at 24.

    The refusal must also leave NO stats file — it goes through ``goto cleanup``,
    which skips ``write_stats`` — or a refused run would be indistinguishable
    from a short one when the artifacts are collected off a node months later.

    The budget is taken from the daemon's own printed total rather than
    recomputed here, so this test cannot drift from the allocator the way a
    second copy of the formula would.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    argv = list(_declaration(plan).as_daemon_args(str(clip)))

    argv[argv.index("--ram-budget-mb") + 1] = "1"
    refused, stats_path = _run_raw(edge_build_dir, tmp_path / "tiny", argv, plan)
    assert refused.returncode != 0
    assert not stats_path.exists(), "a refused run wrote counters for a run it did not do"
    total = _budget_total_bytes(refused.stderr)
    # All four terms, each named and separate: a bare total says nothing about
    # which term to change.
    clip_bytes = benchmark.ram_clip_bytes(*RAM_RESOLUTION, CLIP_FRAMES)
    detector_bytes = benchmark.detector_state_bytes(*RAM_RESOLUTION, "soft")
    assert f"clip {clip_bytes} B" in refused.stderr
    assert f"detector {detector_bytes} B" in refused.stderr
    assert "fixed" in refused.stderr and "1000000 B" in refused.stderr
    assert "not a measurement" in refused.stderr
    assert clip_bytes + detector_bytes < total, "the fixed term vanished"

    fits_mb = math.ceil(total / 1_000_000)
    argv[argv.index("--ram-budget-mb") + 1] = str(fits_mb - 1)
    just_under, under_stats = _run_raw(edge_build_dir, tmp_path / "under", argv, plan)
    assert just_under.returncode != 0
    assert not under_stats.exists()

    argv[argv.index("--ram-budget-mb") + 1] = str(fits_mb)
    accepted, accepted_stats = _run_raw(edge_build_dir, tmp_path / "fits", argv, plan)
    assert accepted.returncode == 0, accepted.stderr
    assert accepted_stats.exists()
    assert json.loads(accepted_stats.read_text())["ram_budget_mb"] == fits_mb


def _daemon_fixed_bytes(stderr: str) -> int:
    """The daemon's own ``fixed`` term, off its INFO line. Never recomputed."""
    match = re.search(r"\+ fixed (\d+) B", stderr)
    assert match, f"the daemon printed no RAM budget line:\n{stderr}"
    return int(match.group(1))


def test_the_declared_struct_allowance_bounds_the_daemons_own_fixed_term(
    edge_build_dir, tmp_path
):
    """The gate that keeps the harness's budget and the daemon's the same sum.

    ``main.c`` enforces ``clip + detector + fixed``, where ``fixed`` is
    ``inject.luma_capacity`` — one luma frame, set in ``sw_inject.c``'s
    ``read_session`` on every injection open — plus five ``sizeof()`` terms
    over the daemon's own structs. The harness had no ``fixed`` term at all,
    so at 1152x648 it published a 174-frame clip as fitting that the daemon
    refused at startup by 517,856 B.

    The luma half is exact on both sides. The struct half is a per-target
    ``sizeof``, and transcribing five C structs into Python would be a second
    copy of the arithmetic that runs — the failure ``sw_detect_ive.c``'s own
    comment names ("the budget check could pass against a formula that is not
    the one that runs"). So the harness DECLARES an upper bound, and this test
    is what makes the declaration honest: it reads the daemon's printed
    ``fixed`` off a real run and asserts the bound covers it. Harness against
    DAEMON, never harness against a second copy of the same formula.

    Read on the host build. The board's residue is a different number and is
    NOT-MEASURED until C3 prints it; what this proves there is that the same
    assertion is the one to re-run, not that the host's value is the board's.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    run = _run_ram(clip, plan, tmp_path / "fixed", edge_build_dir)
    assert run.returncode == 0, run.stderr

    fixed = _daemon_fixed_bytes(run.stderr)
    luma = benchmark.frame_bytes(*RAM_RESOLUTION)
    # The luma term really is in there, so the bound is covering a residue and
    # not a whole term the harness forgot a second time.
    assert fixed > luma, (
        f"the daemon's fixed term {fixed} B does not even contain one "
        f"{luma} B luma frame; the terms have moved"
    )
    declared = benchmark.daemon_fixed_bytes(*RAM_RESOLUTION)
    assert fixed <= declared, (
        f"the daemon's fixed term is {fixed} B, over the harness's declared "
        f"{declared} B ({benchmark.DAEMON_STRUCT_ALLOWANCE_BYTES} B of struct "
        "allowance plus one luma frame). Raise DAEMON_STRUCT_ALLOWANCE_BYTES "
        "and re-derive the pinned clip lengths; do NOT lower the daemon's sum"
    )

    # The whole point of the bound, stated as the inequality that matters: the
    # harness's total is at or above the daemon's, so a row the harness calls
    # `fits` is a row the daemon accepts.
    row = benchmark.ram_budget_row(*RAM_RESOLUTION, CLIP_FRAMES, "soft")
    assert row["total_bytes"] >= _budget_total_bytes(run.stderr)


def test_the_derived_clip_length_is_one_the_daemon_accepts(edge_build_dir, tmp_path):
    """The derivation, executed end to end against a budget it nearly fills.

    ``RAM_RESOLUTION`` at the default 160 MB has megabytes of slack, so it
    cannot fail however wrong the arithmetic is. Five decimal MB puts the
    derived clip within one frame of the line, which is the shape 1152x648 has
    on the board — and it is the configuration the finding reproduced the
    refusal at.

    Both directions, so neither half can pass vacuously: the length the harness
    derives NOW is accepted and writes stats, and the length the old
    budget-minus-the-detector arithmetic derived is refused with no stats file.
    """
    budget_mb = 5
    budget_bytes = budget_mb * 1_000_000
    plan = benchmark.BenchmarkPlan(
        frames=TOTAL_FRAMES,
        warmup_frames=2,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        ram_clip_frames=None,
        ram_budget_mb=budget_mb,
    )
    derived = benchmark.ram_loop_max_frames(
        *RAM_RESOLUTION, "soft", budget_bytes, plan.fps
    )
    assert derived > 0
    declaration = benchmark.ram_loop_declaration(
        plan, *RAM_RESOLUTION, total_frames=TOTAL_FRAMES, detector="soft"
    )
    assert declaration.clip_frames == derived

    clip = _write_clip(tmp_path / "derived.swij", plan, clip_frames=derived)
    accepted = _run_ram(
        clip, plan, tmp_path / "derived", edge_build_dir, declaration=declaration
    )
    assert accepted.returncode == 0, accepted.stderr
    total = _budget_total_bytes(accepted.stderr)
    assert total <= budget_bytes
    row = benchmark.ram_budget_row(
        *RAM_RESOLUTION, derived, "soft", budget_bytes, plan.fps
    )
    assert row["fits"] is True
    assert row["total_bytes"] >= total

    # The old arithmetic: budget minus the detector, and nothing else.
    room = budget_bytes - benchmark.detector_state_bytes(*RAM_RESOLUTION, "soft")
    naive = room // benchmark.frame_bytes(*RAM_RESOLUTION)
    while naive > 0 and not benchmark._period_exact(naive, plan.fps):
        naive -= 1
    assert naive > derived, "this budget does not separate the two derivations"

    over_clip = _write_clip(tmp_path / "naive.swij", plan, clip_frames=naive)
    over = daemon.RamLoopDeclaration(
        clip_frames=naive,
        total_frames=TOTAL_FRAMES,
        pts_stride_ns=int(round(naive / plan.fps * 1e9)),
        budget_mb=budget_mb,
        period_ns=0,
    )
    refused, stats_path = _run_raw(
        edge_build_dir, tmp_path / "naive", over.as_daemon_args(str(over_clip)), plan
    )
    assert refused.returncode != 0, refused.stderr
    assert not stats_path.exists()
    assert "RAM budget exceeded" in refused.stderr


def test_a_clip_over_the_structural_frame_bound_is_refused(edge_build_dir, tmp_path):
    """A byte budget does not bound the metadata arrays, so a second bound does.

    Frame size varies 4x across the D4 rows, so one frame count is three
    different footprints — but a 1x1 clip would buy millions of per-frame
    records under any byte budget. ``SW_INJECT_RAM_MAX_FRAMES`` is the
    structural bound on the arrays, independent of bytes, and this clip is well
    inside the byte budget on purpose so the refusal can only be the structural
    one.
    """
    tiny = (64, 36)
    over = 4097  # SW_INJECT_RAM_MAX_FRAMES + 1
    plan = _plan(clip_frames=over)
    clip = _write_clip(tmp_path / "long.swij", plan, clip_frames=over, resolution=tiny)
    assert clip.stat().st_size < 20_000_000, "this clip is meant to be byte-cheap"

    argv = daemon.RamLoopDeclaration(
        clip_frames=over, total_frames=TOTAL_FRAMES, pts_stride_ns=PTS_STRIDE_NS,
    ).as_daemon_args(str(clip))
    completed, stats_path = _run_raw(
        edge_build_dir, tmp_path / "run", argv, plan, resolution=tiny
    )
    assert completed.returncode != 0
    assert not stats_path.exists()
    assert "SW_INJECT_RAM_MAX_FRAMES" in completed.stderr


def test_a_clip_longer_than_the_budget_is_refused_at_the_host_too(tmp_path):
    """The host twin of the daemon's refusal, with the arithmetic in the message.

    The daemon finds out at startup; the harness finds out before it writes a
    358 MB clip. Both refuse, and the host one carries the terms — detector
    bytes, per-frame bytes, budget — because "it does not fit" without them is
    a fact nobody can act on.
    """
    width, height = 2304, 1296
    derived = benchmark.ram_loop_max_frames(width, height, "ive")
    plan = benchmark.BenchmarkPlan(
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM, ram_clip_frames=derived + 1
    )
    with pytest.raises(ValueError) as excinfo:
        benchmark.ram_loop_declaration(plan, width, height, total_frames=120)
    message = str(excinfo.value)
    assert str(benchmark.detector_state_bytes(width, height, "ive")) in message
    assert str(benchmark.frame_bytes(width, height)) in message
    assert str(benchmark.RAM_LOOP_BUDGET_BYTES) in message


# ---------------------------------------------------------------------------
# D3: pacing moves the wall clock and nothing else
# ---------------------------------------------------------------------------


def test_pacing_changes_the_wall_clock_and_not_one_byte_on_the_wire(
    edge_build_dir, tmp_path
):
    """D3's guarantee, GATED rather than asserted in prose.

    The pace sleep reads the monotonic clock, which is the one thing this
    daemon's scored outputs are not allowed to contain. The guarantee is that a
    deadline never becomes a capture timestamp and never reaches a counter: the
    same clip and the same budget, run paced and unpaced, must produce
    BYTE-IDENTICAL packet logs and equal values for every EXACT_COUNTERS key.
    That is only true because the run is bounded by a frame budget rather than
    by a duration — which is why D2 chose one.

    The wall-time comparison is a LOOSE lower bound and deliberately not a
    scored number: it exists so "the pace did nothing at all" cannot pass.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    period_ns = 20_000_000

    started = time.monotonic()
    unpaced = _run_ram(clip, plan, tmp_path / "unpaced", edge_build_dir)
    unpaced_wall = time.monotonic() - started

    started = time.monotonic()
    paced = _run_ram(
        clip, plan, tmp_path / "paced", edge_build_dir,
        declaration=_declaration(plan, period_ns=period_ns),
    )
    paced_wall = time.monotonic() - started

    assert unpaced.returncode == 0, unpaced.stderr
    assert paced.returncode == 0, paced.stderr
    assert paced.packets == unpaced.packets
    assert paced.packets, "two empty logs are identical and prove nothing"
    for key in benchmark.EXACT_COUNTERS:
        assert paced.stats[key] == unpaced.stats[key], key

    assert unpaced.stats["ram_loop_period_ns"] == 0
    assert unpaced.stats["pace_late_frames"] == 0
    assert unpaced.stats["pace_max_late_ns"] == 0
    assert paced.stats["ram_loop_period_ns"] == period_ns
    # The declared floor for 24 frames at 20 ms is 0.46 s; the unpaced run is
    # milliseconds. Compared against the OTHER RUN rather than against a
    # constant, so a fast or slow machine cannot decide the outcome.
    assert paced_wall > unpaced_wall


def test_a_pace_slower_than_the_health_cadence_is_refused(edge_build_dir, tmp_path):
    """The validate arm that makes the un-serviced pace slice safe.

    The pace loop deliberately does NOT service health or control inside a
    slice — the alternative was a context struct and a function extraction in
    the hottest file. That is only safe while a pace period is shorter than the
    health period, which bounds health lateness by one pace period. So the
    refusal is not tidiness: it is the precondition the design traded for.

    The other two arms belong to the same decision. A period on a file or TCP
    source would be a second pace on top of whoever is writing the stream, and a
    negative one is a deadline in the past dressed as a rate.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)

    slow = _declaration(plan, period_ns=2_000_000_000).as_daemon_args(str(clip))
    completed, stats_path = _run_raw(
        edge_build_dir, tmp_path / "slow", slow, plan,
        extra_args=["--health-period-ms", "1000"],
    )
    assert completed.returncode != 0
    assert not stats_path.exists()
    assert "health cadence" in completed.stderr

    negative = _declaration(plan, period_ns=-1).as_daemon_args(str(clip))
    completed, stats_path = _run_raw(
        edge_build_dir, tmp_path / "negative", negative, plan
    )
    assert completed.returncode != 0
    assert not stats_path.exists()
    assert "is negative" in completed.stderr

    on_a_file, stats_path = _run_raw(
        edge_build_dir, tmp_path / "file",
        ["--inject-file", str(clip), "--ram-loop-period-ns", "20000000"], plan,
    )
    assert on_a_file.returncode != 0
    assert not stats_path.exists()
    assert "--inject-ram only" in on_a_file.stderr


# ---------------------------------------------------------------------------
# D6: what a collected artifact can still be read for, months later
# ---------------------------------------------------------------------------


def test_the_stats_file_reports_the_arena_it_actually_allocated(
    edge_build_dir, tmp_path
):
    """Every RAM key in ``--stats`` is an echo of a declaration or a malloc size.

    ``ram_clip_bytes`` in particular is the arena the daemon ACTUALLY malloc'd,
    not a product the harness believes — the daemon adopts the stream's proc
    grid over ``--proc``, so a host-side product is a belief and this is a
    statement. The source mode is here because it was previously unrecoverable
    from a collected node artifact: it existed only inside whatever argv string
    the launcher happened to record.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    ram_run = _run_ram(clip, plan, tmp_path / "ram", edge_build_dir)
    assert ram_run.returncode == 0, ram_run.stderr

    width, height = RAM_RESOLUTION
    assert ram_run.stats["ram_clip_bytes"] == CLIP_FRAMES * width * height
    assert ram_run.stats["ram_clip_bytes"] == benchmark.ram_clip_bytes(
        width, height, CLIP_FRAMES
    )
    assert ram_run.stats["ram_loop_pts_stride_ns"] == PTS_STRIDE_NS
    assert ram_run.stats["ram_budget_mb"] == 160
    assert ram_run.stats["source_bytes_served"] == TOTAL_FRAMES * width * height

    file_run = daemon.run_daemon_on_stream(
        clip, benchmark.benchmark_config(width, height, plan), tmp_path / "file",
        build_dir=edge_build_dir,
    )
    assert file_run.returncode == 0, file_run.stderr
    # One vocabulary on both sides. Two spellings of a source mode would make a
    # node artifact unreadable against a host one, which is the gap this key
    # exists to close.
    for stats in (ram_run.stats, file_run.stats):
        assert stats["source_mode"] in benchmark.SOURCE_MODES
    assert file_run.stats["source_mode"] == benchmark.SOURCE_MODE_INJECT_FILE
    # A file run declares no budget, so it must not claim to have met one.
    assert file_run.stats["source_frames_planned"] == 0
    assert file_run.stats["ram_clip_frames"] == 0


# ---------------------------------------------------------------------------
# The arithmetic, pinned. DERIVED, never Measured.
# ---------------------------------------------------------------------------


def test_the_budget_arithmetic_pins_the_derived_lengths_against_declared_literals():
    """Every integer A4's answer rests on, pinned so it cannot be re-fitted.

    DERIVED ARITHMETIC, never Measured: no reading of any kind reaches any of
    these numbers on any machine. They are the backends' own allocator sizes
    (``sw_detect_ive.c``'s four planes plus 12 B per model per pixel;
    ``sw_detect_soft.c``'s 46 B/px at model_num 3) and the clip geometry.

    What this test does NOT do, and used to say it did: compare anything to an
    allocator. It pins the Python against LITERALS a human typed, which is worth
    doing — it makes shortening a clip a decision somebody makes on purpose
    rather than a number that quietly moved until the check passed — but a
    literal cannot notice the Python drifting from the C. The comparisons
    against the binary are ``test_the_budget_check_refuses_rather_than_fitting_
    the_number`` (the soft allocator and the clip term, off the daemon's own
    printed line) and ``test_the_declared_struct_allowance_bounds_the_daemons_
    own_fixed_term`` (the fixed term). The IVE arm has no host-side pin at all
    and says so in ``detector_state_bytes``' docstring; C3 is where it gets one.

    The headline is the last block. A 24-frame full-resolution clip does NOT
    clear the 160 MB line once the detector and the daemon's fixed footprint are
    counted, which contradicts the D0 "D8.1 opening" entry's "~24 full-res
    frames" — that entry was reasoned against the 256 MB PHYSICAL total and is
    silent about both other terms. The derived lengths are what does clear it.
    """
    assert benchmark.detector_state_bytes(2304, 1296, "ive") == 119_439_360
    assert benchmark.detector_state_bytes(1536, 864, "ive") == 53_084_160
    assert benchmark.detector_state_bytes(1152, 648, "ive") == 29_859_840
    assert benchmark.detector_state_bytes(2304, 1296, "soft") == 137_355_264

    # The IVE stride rounds width up to 16, and all three D4 widths are already
    # multiples of 16, so the rounding is INERT here and cannot be hiding a
    # difference between the two sides of the arithmetic. That used to be
    # asserted as `(width + 15) & ~15 == width` over the three literals — three
    # numbers compared to themselves, with the function nowhere in the
    # expression. Ask the FUNCTION instead: a width that rounds UP to one of
    # these grids costs the same as the grid, and one pixel past it costs more.
    assert benchmark.detector_state_bytes(1140, 648, "ive") == (
        benchmark.detector_state_bytes(1152, 648, "ive")
    )
    assert benchmark.detector_state_bytes(1153, 648, "ive") > (
        benchmark.detector_state_bytes(1152, 648, "ive")
    )

    assert benchmark.ram_clip_bytes(2304, 1296, 24) == 71_663_616
    # `119_439_360 + 71_663_616 == 191_102_976` stood here: three literals and
    # no system under test, in the block that carries the headline. The same
    # claim, routed through the functions and over the THREE terms the daemon
    # sums, is the one that can notice any of them moving.
    full_res_24 = (
        benchmark.detector_state_bytes(2304, 1296, "ive")
        + benchmark.ram_clip_bytes(2304, 1296, 24)
        + benchmark.daemon_fixed_bytes(2304, 1296)
    )
    assert full_res_24 == 194_154_496
    assert full_res_24 > benchmark.RAM_LOOP_BUDGET_BYTES

    # THREE terms, because the daemon sums three. `daemon_fixed_bytes` is one
    # luma frame (exact, `sw_inject.c`'s `read_session`) plus a DECLARED bound
    # on the five `sizeof()` terms `main.c` adds. Pinned here as arithmetic;
    # the bound itself is proven against the daemon's own printed `fixed` by
    # `test_the_declared_struct_allowance_bounds_the_daemons_own_fixed_term`,
    # which is the only assertion in this file that can catch the two sums
    # drifting apart, because it is the only one that reads the daemon.
    for width, height in ((2304, 1296), (1536, 864), (1152, 648)):
        assert benchmark.daemon_fixed_bytes(width, height) == (
            benchmark.DAEMON_STRUCT_ALLOWANCE_BYTES + width * height
        )

    derived = {
        (2304, 1296): (12, 158_322_688),
        (1536, 864): (78, 157_990_912),
        (1152, 648): (171, 158_322_688),
    }
    for (width, height), (frames, total) in derived.items():
        assert benchmark.ram_loop_max_frames(width, height, "ive") == frames
        row = benchmark.ram_budget_row(width, height, None, "ive")
        assert row["clip_frames"] == frames
        assert (
            row["detector_state_bytes"] + row["clip_bytes"]
            + row["daemon_fixed_bytes"] == total
        ), row
        assert row["total_bytes"] == total
        assert total <= benchmark.RAM_LOOP_BUDGET_BYTES
        assert row["fits"] is True
        assert "NOT a measurement" in row["basis"]

    # The row this finding was written about. Without the fixed term the
    # derivation returned 174 frames and 159,750,144 B, which `fits` called
    # True and the daemon refused by 517,856 B.
    naive = benchmark.detector_state_bytes(1152, 648, "ive") + benchmark.ram_clip_bytes(
        1152, 648, 174
    )
    assert naive == 159_750_144
    assert naive <= benchmark.RAM_LOOP_BUDGET_BYTES
    assert naive + 1152 * 648 > benchmark.RAM_LOOP_BUDGET_BYTES, (
        "the luma term alone puts the old 174-frame row over the line, on any "
        "target, whatever the struct sizes are"
    )


def test_the_omitted_ive_blob_term_cannot_move_a_derived_clip_length(monkeypatch):
    """The IVE arm's declared omission, held inert where the report publishes it.

    ``ive_footprint_for`` sums four planes, the model store AND
    ``sizeof(IVE_CCBLOB_S)``; ``detector_state_bytes(..., "ive")`` counts the
    first two. The third cannot be written here — the type is an SDK header's,
    absent from this checkout, on an arm that compiles to a refusal without
    ``SKYWEAVE_HAVE_RKMPI`` — so it is DECLARED at an upper bound instead of
    left as a silent zero, and the claim that the omission changes nothing is
    asserted here rather than in a docstring.

    The finding that forced this test: the docstring said the arm "mirrors the C
    exactly" and that "the two are pinned equal by a test", and both were false
    for ``ive`` — the only pin against a daemon-printed footprint passes
    ``"soft"``. Two directions, both of which can fail: every published row must
    leave more budget slack than the declared bound, and making the Python arm
    C-faithful must not shorten a derived clip.
    """
    grids = ((2304, 1296), (1536, 864), (1152, 648))
    published = {}
    for width, height in grids:
        row = benchmark.ram_budget_row(width, height, None, "ive")
        slack = benchmark.RAM_LOOP_BUDGET_BYTES - row["total_bytes"]
        assert slack >= benchmark.IVE_BLOB_DECLARED_BOUND_BYTES, (
            f"{width}x{height} leaves {slack} B of slack, under the declared "
            f"{benchmark.IVE_BLOB_DECLARED_BOUND_BYTES} B blob bound: the "
            "omission stops being inert and the arm has to count it"
        )
        published[(width, height)] = row["clip_frames"]

    real = benchmark.detector_state_bytes

    def c_faithful(proc_width, proc_height, detector="ive", model_num=3):
        """What the C sums: the Python arm plus the blob it leaves out."""
        extra = benchmark.IVE_BLOB_DECLARED_BOUND_BYTES if detector == "ive" else 0
        return real(proc_width, proc_height, detector, model_num) + extra

    monkeypatch.setattr(benchmark, "detector_state_bytes", c_faithful)
    for width, height in grids:
        assert benchmark.ram_loop_max_frames(width, height, "ive") == published[
            (width, height)
        ], f"{width}x{height}: counting the blob moves the derived clip length"
        assert benchmark.ram_budget_row(width, height, None, "ive")["fits"] is True


def test_period_exactness_is_a_correctness_requirement_not_a_preference():
    """Why the full-res clip is 12 frames and not the 13 the bytes would allow.

    ``ram_loop_max_frames`` steps DOWN from the byte maximum until the clip is a
    whole number of nanoseconds at the declared fps. That step is not tidiness:
    the looped ``capture_ts_ns`` equals what an unrolled feed would have written
    only when ``N * 1e9 / fps`` is an integer. With it the identity holds for
    every pass and every slot; without it the two diverge by a rounding per
    pass, and D1's headline byte-identity gate against the harness's own looped
    TCP feed would fail — quietly, by a few nanoseconds, on pass 2.

    The FAILING half is what makes this a gate rather than a demonstration.
    """
    passes = range(400)
    for count in (6, 12, 78, 174):
        stride = int(round(count / 30.0 * 1e9))
        assert benchmark._period_exact(count, 30.0)
        for pass_index in passes:
            for slot in range(count):
                assert int(round((pass_index * count + slot) / 30.0 * 1e9)) == (
                    pass_index * stride + int(round(slot / 30.0 * 1e9))
                ), (count, pass_index, slot)

    for count in (13, 16, 80):
        assert not benchmark._period_exact(count, 30.0)
        stride = int(round(count / 30.0 * 1e9))
        assert any(
            int(round((pass_index * count + slot) / 30.0 * 1e9))
            != pass_index * stride + int(round(slot / 30.0 * 1e9))
            for pass_index in passes
            for slot in range(count)
        ), count


def test_the_declared_stride_is_the_harnesss_own_arithmetic():
    """The module constant above is a value, not a second formula.

    A literal that drifted from ``clip_frames / fps * 1e9`` would make every
    wrap assertion in this file agree with the wrong number, and agree
    consistently.
    """
    assert PTS_STRIDE_NS == int(round(CLIP_FRAMES / 30.0 * 1e9))
    assert benchmark._period_exact(CLIP_FRAMES, 30.0)
    declaration = benchmark.ram_loop_declaration(
        _plan(), *RAM_RESOLUTION, total_frames=TOTAL_FRAMES, detector="soft"
    )
    assert declaration.pts_stride_ns == PTS_STRIDE_NS
    assert declaration.clip_frames == CLIP_FRAMES
    assert declaration.total_frames == TOTAL_FRAMES
    assert declaration.period_ns == 0


def test_the_clip_is_built_with_movers_from_frame_zero(tmp_path):
    """The trap that decides whether a RAM sweep measures anything at all.

    ``iter_scene_frames`` only draws movers once ``frame_seq >= warmup_frames``,
    and the default plan warm-up is 30. A 12-frame clip built with the plan's
    own warm-up would be PURE BACKGROUND: the sweep would be GMM2 on flat
    noise, ``capture_events`` would be zero, and the suite would be green. So
    ``write_benchmark_stream``'s shadow plan sets ``warmup_frames=0``, and the
    daemon's own ``--warmup`` is untouched and applies over the RUN's continuous
    frame_seq instead.

    Read back out of the WRITTEN CLIP as pixels: "the shadow plan sets zero" is
    a statement about code, and this has to be a statement about the bytes a
    board would replay.
    """
    plan = benchmark.BenchmarkPlan(
        frames=TOTAL_FRAMES, warmup_frames=30,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM, ram_clip_frames=CLIP_FRAMES,
    )
    width, height = RAM_RESOLUTION
    clip = _write_clip(tmp_path / "clip.swij", plan)
    frames = [frame.luma for frame in _clip_frames_on_disk(clip)]
    assert len(frames) == CLIP_FRAMES
    for index, frame in enumerate(frames):
        assert int(frame.max()) > benchmark.SCENE_BACKGROUND_DN + 40, index

    # ...and the same length under the PLAN's own warm-up is the flat clip this
    # exists to avoid, so the assertion above is not true of any clip.
    flat = list(
        benchmark.iter_scene_frames(replace(plan, frames=CLIP_FRAMES), width, height)
    )
    assert max(int(frame.max()) for frame in flat) < (
        benchmark.SCENE_BACKGROUND_DN + 40
    )
    # Determinism survives the shadow plan: noise is keyed by (seed, frame_seq),
    # so a short plan's frames are byte-identical to a long one's leading ones.
    long_plan = replace(plan, frames=TOTAL_FRAMES, warmup_frames=0)
    leading = list(benchmark.iter_scene_frames(long_plan, width, height))[:CLIP_FRAMES]
    assert [np.asarray(f).tobytes() for f in frames] == [
        np.asarray(f).tobytes() for f in leading
    ]


def test_the_daemon_announces_the_advance_at_every_startup(edge_build_dir, tmp_path):
    """The daemon's one sanctioned advance of a capture timestamp says so.

    ``sw_inject.h`` and the firmware README both state that this daemon never
    invents a capture time. The RAM loop is the single named exception, and a
    run that took it quietly would eventually be read as evidence that the rule
    was never broken. So the warning is loud, at every startup, in the same
    register as the OVERRIDDEN one — and it names the declared stride, says the
    daemon reads no clock, and records the DDR systematic.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    run = _run_ram(clip, plan, tmp_path / "run", edge_build_dir)
    assert run.returncode == 0, run.stderr
    assert "RAM loop ADVANCES capture time" in run.stderr
    assert f"DECLARED {PTS_STRIDE_NS} ns stride" in run.stderr
    assert "reads no clock" in run.stderr
    assert "time_sync_error_ms is the clip's, untouched" in run.stderr
    assert "declared systematic" in run.stderr
    # The clock domain is still the harness's declaration, not a promotion:
    # advancing a synthetic timestamp does not make it a board clock.
    for envelope in _envelopes(run):
        assert envelope.clock_domain is ClockDomain.SYNTHETIC


# ---------------------------------------------------------------------------
# The heap preflight (F-C1-2)
# ---------------------------------------------------------------------------


def test_the_heap_preflight_refuses_when_the_heap_cannot_hold_the_arena(
    edge_build_dir, tmp_path
):
    """The arithmetic budget is pool-blind; the arena malloc is not.

    On the node the detector's term lives in the media heap while the clip
    arena is a plain malloc, so a clip can pass the 160 MB arithmetic and
    still be unservable from the ~29 MB of heap actually available — the
    first board run was OOM-killed exactly there (F-C1-2). The daemon reads
    MemAvailable from a declared path before the malloc and refuses with
    every number named, leaving no stats file, like every refusal.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        1000 kB\nMemAvailable:      64 kB\n")
    argv = list(_declaration(plan).as_daemon_args(str(clip)))
    argv += ["--meminfo-path", str(meminfo)]
    refused, stats_path = _run_raw(edge_build_dir, tmp_path / "starved", argv, plan)
    assert refused.returncode != 0
    assert not stats_path.exists(), "a refused run wrote counters for a run it did not do"
    assert "heap preflight refuses" in refused.stderr
    assert f"MemAvailable is {64 * 1024} B" in refused.stderr
    arena = benchmark.ram_clip_bytes(*RAM_RESOLUTION, CLIP_FRAMES)
    assert f"{arena} B arena" in refused.stderr


def test_a_missing_meminfo_skips_the_preflight_and_says_so(
    edge_build_dir, tmp_path
):
    """No MemAvailable (any non-Linux host) = no preflight, stated out loud.

    Skipping silently would make "the preflight passed" and "the preflight
    never ran" the same stderr, and the collected run.log is where a bench
    session tells them apart.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    argv = list(_declaration(plan).as_daemon_args(str(clip)))
    argv += ["--meminfo-path", str(tmp_path / "no-such-meminfo")]
    run, stats_path = _run_raw(edge_build_dir, tmp_path / "skipped", argv, plan)
    assert run.returncode == 0, run.stderr
    assert stats_path.exists()
    assert "heap preflight skipped" in run.stderr


def test_the_heap_preflight_reports_the_measured_value_it_read(
    edge_build_dir, tmp_path
):
    """The passing line carries the number, labeled measured-at-preload.

    Proved by pointing the daemon at a declared meminfo and reading the same
    number back; /proc is not fixture material.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:      16000000 kB\n"
        "MemFree:        8000000 kB\n"
        "MemAvailable:   8000000 kB\n"
    )
    argv = list(_declaration(plan).as_daemon_args(str(clip)))
    argv += ["--meminfo-path", str(meminfo)]
    run, stats_path = _run_raw(edge_build_dir, tmp_path / "measured", argv, plan)
    assert run.returncode == 0, run.stderr
    assert stats_path.exists()
    assert f"heap preflight: MemAvailable {8000000 * 1024} B" in run.stderr


def test_the_default_meminfo_path_is_wired_without_the_flag(
    edge_build_dir, tmp_path
):
    """No --meminfo-path: the daemon must reach for /proc/meminfo itself.

    On a Linux host that file exists and the preflight line must appear; on a
    host without /proc/meminfo the skip line must name the default path. A
    typo in the default string would fail both branches — and would be
    invisible to every other preflight test, because they all declare an
    explicit path.
    """
    plan = _plan()
    clip = _write_clip(tmp_path / "clip.swij", plan)
    argv = list(_declaration(plan).as_daemon_args(str(clip)))
    run, stats_path = _run_raw(edge_build_dir, tmp_path / "default", argv, plan)
    assert run.returncode == 0, run.stderr
    assert stats_path.exists()
    if Path("/proc/meminfo").exists():
        assert "heap preflight: MemAvailable" in run.stderr
        assert "/proc/meminfo" in run.stderr
    else:
        assert ("heap preflight skipped: no MemAvailable at /proc/meminfo"
                in run.stderr)
