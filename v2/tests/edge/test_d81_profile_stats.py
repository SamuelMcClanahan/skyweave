"""The intra-frame profiler's EMISSION contract, exercised on the HOST daemon.

What this file gates and what it does NOT:

- it gates the CONTRACT — that ``--profile-stats`` is opt-in, that with the
  flag the ``--stats`` JSON carries a top-level ``profile`` object whose
  stages on the host build are exactly the soft detector's declared five,
  that the per-stage accumulators satisfy the arithmetic a count/total/min/max
  quadruple must satisfy, that ``frame_total`` counts every processed frame
  and contains its parts, and that WITHOUT the flag the key is absent
  entirely — so an unprofiled artifact is byte-shaped like every artifact
  collected before the profiler existed;
- it does not gate a number. Every nanosecond here is a host timing of the
  portable soft detector; the board's IVE split is a different quantity and
  is NOT-MEASURED until a planning-sanctioned session runs the profiler
  build beside the frozen campaign binary (see
  ``docs/campaigns/C-002/PROFILER_NOTE.md``).

This is a runbook-adjacent daemon capability, not an E-series axis — the
E-series is a reported, closed list — so it gets a ``test_d81_*`` file of
its own, the same placement call ``test_d81_ram_loop_source.py`` records.
"""

from __future__ import annotations

import subprocess

from skyweave2.edge import benchmark, daemon

#: Small for the reason test_e8 states: the profiler's emission contract is
#: resolution-independent, and a full-size grid would buy nothing but wall
#: time. The movers still survive the opening at this size.
RESOLUTION = (288, 162)
TOTAL_FRAMES = 40
WARMUP_FRAMES = 4

SESSION = "d81-profile-stats-0000-00000001"

#: The soft detector's declared stage list, spelled exactly as the daemon
#: emits it. Compared with set EQUALITY below: a stage that appears without
#: being declared here is as much a contract break as a declared one missing.
SOFT_STAGES = frozenset(
    {"gmm2", "morph", "occupancy_scan", "ccl_label", "frame_total"}
)


def _profiled_run(build_dir, tmp_path, extra_args):
    """One 40-frame inject-file run through the ordinary stream path.

    ``run_daemon_on_stream`` rather than a hand-built argv: the profiler flag
    is an OUTPUT knob, so it must compose with the unchanged source and
    detector argv the other tests drive, not with a bespoke one.
    """
    plan = benchmark.BenchmarkPlan(frames=TOTAL_FRAMES, warmup_frames=WARMUP_FRAMES)
    clip = tmp_path / "clip.swij"
    benchmark.write_benchmark_stream(
        clip, plan, RESOLUTION[0], RESOLUTION[1], session_uuid=SESSION
    )
    return daemon.run_daemon_on_stream(
        clip,
        benchmark.benchmark_config(RESOLUTION[0], RESOLUTION[1], plan),
        tmp_path / "run",
        build_dir=build_dir,
        extra_args=extra_args,
    )


def test_the_flag_emits_the_declared_soft_stages_with_coherent_accumulators(
    edge_build_dir, tmp_path
):
    """The profile block is a closed list of stages, each a sane accumulator.

    Set EQUALITY on the stage names, because both directions are defects: a
    missing stage means part of the frame went untimed, and an undeclared one
    means the C grew a stage no comparison plan knows about. The arithmetic
    below is what a count/total/min/max quadruple cannot violate if it was
    accumulated rather than composed: a total below its own minimum, or a
    maximum below it, is a block that was assembled by hand.

    ``frame_total`` is pinned two ways. Its count is the daemon's processed
    frame count — the profiler runs on EVERY frame, warm-up included, which
    is what makes its overhead a property of the run rather than of the
    scored region. And its total bounds every other stage's total from above,
    because the whole contains its parts; equality is allowed, since a stage
    could in principle be the entire frame.
    """
    run = _profiled_run(edge_build_dir, tmp_path, ["--profile-stats"])
    assert run.returncode == 0, run.stderr
    assert "profile" in run.stats, sorted(run.stats)
    profile = run.stats["profile"]
    assert set(profile) == SOFT_STAGES, sorted(profile)

    # Non-vacuous: frames were processed and every post-warm-up frame ticked
    # every stage, so no stage can sit at zero while the run claims 40 frames.
    assert run.stats["frames_in"] == TOTAL_FRAMES
    assert profile["frame_total"]["count"] == run.stats["frames_in"]
    scored = run.stats["frames_scored"]
    assert scored == TOTAL_FRAMES - WARMUP_FRAMES

    for stage, block in profile.items():
        assert block["count"] >= scored > 0, stage
        assert block["total_ns"] >= block["min_ns"], stage
        assert block["max_ns"] >= block["min_ns"], stage
        assert (
            profile["frame_total"]["total_ns"] >= block["total_ns"]
        ), f"{stage} outran the frame that contains it"


def test_the_flag_changes_nothing_but_adding_the_profile_key(
    edge_build_dir, tmp_path
):
    """The flag's whole effect on the record is the ``profile`` object.

    The SAME stream file goes through the daemon twice, flag off and flag on,
    and the flag-on stats minus its ``profile`` key must equal the flag-off
    stats EXACTLY — every counter, every config echo. Anything less than
    dict equality would let the flag perturb a counter somewhere and still
    pass, and "the profiler changes nothing it measures" is the always-on
    design's entire justification (sw_profile.h).

    Both dicts come from ``json.loads`` over the FULL stats file
    (``run_daemon_on_stream``), so the profile block reaching us at all is
    also proof it round-trips as part of the complete JSON document rather
    than as a fragment.
    """
    plan = benchmark.BenchmarkPlan(frames=TOTAL_FRAMES, warmup_frames=WARMUP_FRAMES)
    clip = tmp_path / "clip.swij"
    benchmark.write_benchmark_stream(
        clip, plan, RESOLUTION[0], RESOLUTION[1], session_uuid=SESSION
    )
    config = benchmark.benchmark_config(RESOLUTION[0], RESOLUTION[1], plan)
    flag_off = daemon.run_daemon_on_stream(
        clip, config, tmp_path / "flag-off", build_dir=edge_build_dir
    )
    flag_on = daemon.run_daemon_on_stream(
        clip,
        config,
        tmp_path / "flag-on",
        build_dir=edge_build_dir,
        extra_args=["--profile-stats"],
    )
    assert flag_off.returncode == 0, flag_off.stderr
    assert flag_on.returncode == 0, flag_on.stderr
    assert "profile" in flag_on.stats, sorted(flag_on.stats)
    stripped = {key: value for key, value in flag_on.stats.items() if key != "profile"}
    assert stripped == flag_off.stats


def test_stage_counts_pin_the_warmup_accumulation(edge_build_dir, tmp_path):
    """Every stage count is derivable from the frame plan, so pin it exactly.

    The derivation, from ``sw_detect_soft.c``'s ``soft_apply_frame`` (frame
    sequence numbers are zero-based, ``warming = frame_seq < warmup_frames``
    in ``main.c``):

    - ``frame_total`` records in the ``soft_apply`` wrapper around EVERY
      exit of the body, so its count is ``frames_in`` (40);
    - frame 0 is the model-init frame: ``soft_init_models`` runs and the
      body returns before any stage timer, so ``gmm2`` does NOT record;
    - frames 1..3 are still warming: ``gmm2`` records, then the early
      return skips morph/occupancy_scan/ccl_label;
    - frames 4..39 record all four stages.

    So ``gmm2`` is ``frames_in - 1`` == 39, and morph, occupancy_scan and
    ccl_label are each ``frames_scored`` == 36. Equality, not ``>=``: any of
    these drifting by one is a warm-up boundary moved, which is exactly the
    off-by-one a comparison plan built on these counts would inherit.
    """
    run = _profiled_run(edge_build_dir, tmp_path, ["--profile-stats"])
    assert run.returncode == 0, run.stderr
    profile = run.stats["profile"]
    frames_in = run.stats["frames_in"]
    scored = run.stats["frames_scored"]
    assert frames_in == TOTAL_FRAMES
    assert scored == TOTAL_FRAMES - WARMUP_FRAMES
    assert profile["frame_total"]["count"] == frames_in
    assert profile["gmm2"]["count"] == frames_in - 1
    for stage in ("morph", "occupancy_scan", "ccl_label"):
        assert profile[stage]["count"] == scored, stage


def test_without_the_flag_the_profile_key_is_absent_not_empty(
    edge_build_dir, tmp_path
):
    """Opt-in means ABSENT, never present-but-empty.

    An empty ``profile`` block in an unprofiled artifact would make every
    collected stats file, months later, ambiguous about whether the profiler
    ran — and the frozen-binary comparison plan depends on an unprofiled
    run's record being byte-shaped like the frozen binary's. The known
    counters are asserted alongside, so "the key is gone" cannot pass because
    the whole record went missing.
    """
    run = _profiled_run(edge_build_dir, tmp_path, None)
    assert run.returncode == 0, run.stderr
    assert "profile" not in run.stats, sorted(run.stats)
    # The ordinary record is intact around the absence.
    assert run.stats["frames_in"] == TOTAL_FRAMES
    assert run.stats["frames_scored"] == TOTAL_FRAMES - WARMUP_FRAMES
    assert run.stats["source_mode"] == benchmark.SOURCE_MODE_INJECT_FILE
    assert run.stats["observations_sent"] > 0


def test_help_names_the_profile_flag(edge_build_dir):
    """A flag the usage text does not name is a flag only its author can use.

    The comparison session will be driven from a hand-typed command line on a
    board months from now; the usage text is the one piece of documentation
    guaranteed to travel inside the binary.
    """
    completed = subprocess.run(
        [str(daemon.daemon_path(edge_build_dir)), "--help"],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0
    # Usage goes to stderr (main.c prints it there for -h too); accept either
    # stream so the test pins the flag's presence, not the stream choice.
    assert "--profile-stats" in completed.stderr + completed.stdout
