"""E8's harness, exercised against the HOST-built daemon.

What this file gates and what it does NOT:

- it gates the RUNNER — that a sweep produces one measured run per resolution,
  that the collectors either carry a number with its mechanism or say
  NOT-MEASURED with a reason, that the soak paces and loops and stops, and that
  the E8 comparison can both pass and FAIL;
- it does not gate E8. E8 is two runs on the BOARD inside the declared
  run-to-run bounds, and no board has run anything. Every number produced here
  is about this host.

The D8.1-prep amendment asks for exactly this split: "All exercised against the
HOST-built daemon in tests so the first board session debugs the board, not the
tooling." A harness whose first execution is at a bench with three nodes on it
is not tooling.

Resolutions here are SMALL — a few hundred pixels a side, not the D4 sweep's
2304x1296. That is a deliberate, declared limitation: a full-resolution row is
358 MB of stream and tens of seconds of numpy per run, which would put minutes
into every suite run to test orchestration that is resolution-independent. The
three real resolutions are covered by ``test_the_sweep_runs_the_three_declared_
resolutions``, which checks the runner's plan rather than executing it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyweave2.edge import benchmark, metrics, report, tolerance

# Small enough that a run is milliseconds, large enough that the detector still
# finds every mover — at 160x90 the scene's blobs fall under the opening and the
# runner would be exercised against frames with nothing in them. Not D4
# resolutions and not pretending to be.
TEST_RESOLUTIONS = ((384, 216), (288, 162))
DOCS = Path(__file__).resolve().parents[2] / "docs"


@pytest.fixture(scope="module")
def sweep(edge_build_dir, tmp_path_factory):
    """One sweep, reused by every test that reads a run."""
    work = tmp_path_factory.mktemp("d8-sweep")
    plan = benchmark.BenchmarkPlan(
        frames=40, warmup_frames=10, source_medium="host-page-cache"
    )
    return benchmark.run_sweep(
        work, plan=plan, resolutions=TEST_RESOLUTIONS, build_dir=edge_build_dir
    )


#: The RAM-loop rows use a SHORT clip served many times, which is the shape the
#: board sweep has: at 2304x1296 the derived clip is 12 frames and the run is
#: 120. Six into twenty-four is the same shape at a size a suite can afford.
RAM_CLIP_FRAMES = 6
RAM_TOTAL_FRAMES = 24


@pytest.fixture(scope="module")
def ram_sweep(edge_build_dir, tmp_path_factory):
    """The same sweep, fed from a preloaded clip instead of a file."""
    work = tmp_path_factory.mktemp("d8-ram-sweep")
    plan = benchmark.BenchmarkPlan(
        frames=RAM_TOTAL_FRAMES, warmup_frames=6,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        ram_clip_frames=RAM_CLIP_FRAMES, ram_budget_mb=160,
    )
    return benchmark.run_sweep(
        work, plan=plan, resolutions=TEST_RESOLUTIONS, build_dir=edge_build_dir
    )


@pytest.fixture(scope="module")
def ram_soak(edge_build_dir, tmp_path_factory):
    """A RAM-fed soak, because a soak record is a scored artifact too."""
    work = tmp_path_factory.mktemp("d8-ram-soak")
    plan = benchmark.BenchmarkPlan(
        frames=RAM_TOTAL_FRAMES, warmup_frames=6,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        ram_clip_frames=RAM_CLIP_FRAMES, ram_budget_mb=160,
    )
    return benchmark.run_soak(
        work, 288, 162, duration_s=1.0, plan=plan, target_fps=30.0,
        build_dir=edge_build_dir, health_period_ms=200,
    )


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def test_the_sweep_produces_one_measured_run_per_resolution(sweep):
    assert [run.label for run in sweep.runs] == [
        f"{w}x{h}" for w, h in TEST_RESOLUTIONS
    ]
    for run in sweep.runs:
        assert run.returncode == 0, run.stderr_tail
        # Non-vacuous: a sweep of empty runs would satisfy every other
        # assertion in this file.
        assert run.frames_in == 40
        assert run.stats["capture_events"] > 0
        assert run.stats["observations_sent"] > 0


def test_the_sweep_runs_the_three_declared_resolutions_by_default():
    """The D4 three, and the same three the report prints.

    Checked without running them: a full-resolution sweep is minutes and
    gigabytes, and what can go wrong here is the LIST drifting from D4's, not
    the loop failing to iterate.
    """
    assert benchmark.D4_RESOLUTIONS == ((2304, 1296), (1536, 864), (1152, 648))
    from skyweave2.edge import report

    assert report.D4_RESOLUTIONS is benchmark.D4_RESOLUTIONS


def test_the_cap_never_bites_during_a_benchmark_run(sweep):
    """A sweep must measure finding components, not dropping them.

    Six movers against a cap of seven. If a scene change ever pushed a
    benchmark frame to the cap, the fps column would start including the cost
    of the cap's selection and two rows would stop being comparable.
    """
    for run in sweep.runs:
        assert run.stats["components_dropped_over_cap"] == 0, run.label
        assert run.stats["frames_at_cap"] == 0, run.label
        assert run.stats["max_components_offered"] <= benchmark.SCENE_MOVERS + 1


def test_every_measurement_either_carries_its_mechanism_or_says_why_not(sweep):
    """The NOT-MEASURED discipline, on real output.

    ``Measurement.check`` is what enforces it and the runner calls it, so this
    asserts the RESULT: no number without a source, no absence without a
    reason, and nothing in between.
    """
    for run in sweep.runs:
        assert run.measurements, run.label
        for name, measurement in run.measurements.items():
            measurement.check()
            if measurement.measured:
                assert measurement.source, name
                assert measurement.as_json() != metrics.NOT_MEASURED
            else:
                assert measurement.reason, name
                assert measurement.as_json() == metrics.NOT_MEASURED


def test_ddr_bandwidth_is_not_measured_and_never_becomes_a_number(sweep):
    """The brief's stubbed collector, checked to be a stub and not a zero.

    "Board-only collectors (DDR counters, thermals) may stub with a loud
    NOT-MEASURED marker, never a fake number." A zero here would be a fake
    number: it would compare, average and plot exactly like a measurement.
    """
    for run in sweep.runs:
        ddr = run.measurements["ddr_bandwidth_mb_s"]
        assert not ddr.measured
        assert ddr.value is None
        assert ddr.as_json() == metrics.NOT_MEASURED
        assert "counter" in ddr.reason
        assert metrics.NOT_MEASURED in ddr.rendered()


def test_a_run_records_the_byte_rate_that_fed_it(sweep):
    """Finding D8-F8: the source is part of the measurement.

    Without a declared medium there is no denominator, and the verdict says so
    rather than assuming one.
    """
    for run in sweep.runs:
        assert run.input_mb_s is not None and run.input_mb_s > 0.0
        assert "detector-bound" in run.source_verdict(
            sweep.plan.resolved_ceiling_mb_s()
        )
        assert "undeclared" in run.source_verdict(None)


def test_the_harness_reports_both_its_own_rate_and_the_daemons(sweep):
    """Two fps numbers, because they answer different questions.

    The harness's wall clock includes process startup; the daemon's own health
    packet starts after its sockets are open. Reporting one would hide the
    harness's overhead inside a number about the node.
    """
    for run in sweep.runs:
        wall = run.measurements["sustained_fps_wall"]
        own = run.measurements["sustained_fps_daemon"]
        assert wall.measured and own.measured, run.label
        # The daemon cannot be SLOWER than the wall figure: its interval is a
        # subset of the harness's. If this ever inverts, one of the two clocks
        # is measuring something other than what it says.
        assert own.value >= wall.value * 0.99, (wall.value, own.value)


def test_the_sweep_artifact_round_trips_through_json(sweep):
    """What lands on disk is what `compare` reads back."""
    payload = json.loads(json.dumps(sweep.as_dict()))
    assert payload["schema"] == "d8-benchmark/1"
    assert payload["kind"] == "sweep"
    assert payload["plan"]["seed"] == benchmark.SCENE_SEED
    rebuilt = [benchmark._run_from_dict(item) for item in payload["runs"]]
    assert [run.label for run in rebuilt] == [run.label for run in sweep.runs]
    for run in rebuilt:
        assert run.measurements["ddr_bandwidth_mb_s"].value is None


# ---------------------------------------------------------------------------
# The scene
# ---------------------------------------------------------------------------


def test_the_benchmark_scene_is_deterministic():
    plan = benchmark.BenchmarkPlan(frames=6, warmup_frames=2)
    first = list(benchmark.iter_scene_frames(plan, 96, 54))
    second = list(benchmark.iter_scene_frames(plan, 96, 54))
    assert [frame.tobytes() for frame in first] == [frame.tobytes() for frame in second]


def test_a_different_seed_produces_a_different_scene():
    """Otherwise the determinism test above is a test of a constant."""
    a = list(benchmark.iter_scene_frames(benchmark.BenchmarkPlan(frames=4), 96, 54))
    b = list(
        benchmark.iter_scene_frames(
            benchmark.BenchmarkPlan(frames=4, seed=benchmark.SCENE_SEED + 1), 96, 54
        )
    )
    assert [frame.tobytes() for frame in a] != [frame.tobytes() for frame in b]


def test_the_scene_declares_the_sensor_grid_not_the_processing_grid():
    """The D0 scaling law has to have something to scale.

    Injection carries pre-scaled frames and DECLARES the full grid; if the
    session claimed the processing grid as the full one, the scale would be 1
    and every centroid the board reports would be in the wrong units.
    """
    session = benchmark.benchmark_session(
        benchmark.BenchmarkPlan(), 1152, 648, "d8bench-test", 10
    )
    assert (session.full_width, session.full_height) == (2304, 1296)
    assert (session.proc_width, session.proc_height) == (1152, 648)
    assert session.scale_x == 2.0 and session.scale_y == 2.0


# ---------------------------------------------------------------------------
# The soak
# ---------------------------------------------------------------------------


def test_the_soak_paces_itself_and_loops_a_short_scene(edge_build_dir, tmp_path):
    """A two-second soak, which is the same machinery as an hour-long one.

    Paced: the daemon's own rate lands near the target rather than at the
    unpaced ceiling, which for this resolution is orders of magnitude higher.
    Looped: more frames are delivered than the scene contains.
    """
    plan = benchmark.BenchmarkPlan(frames=20, warmup_frames=5)
    result = benchmark.run_soak(
        tmp_path / "soak", 288, 162, duration_s=2.0, plan=plan, target_fps=30.0,
        build_dir=edge_build_dir, health_period_ms=200,
    )
    assert result.run.returncode == 0, result.run.stderr_tail
    assert result.frames_planned == 60
    assert result.run.frames_in == 60
    # More frames than the scene has: the loop is what makes an hour possible.
    assert result.frames_planned > plan.frames
    own = result.run.measurements["sustained_fps_daemon"]
    assert own.measured
    assert 20.0 <= own.value <= 45.0, own.value
    assert result.as_dict()["scene_looped"] is True


def test_a_soak_whose_scene_never_wrapped_does_not_claim_it_did(
    edge_build_dir, tmp_path
):
    """``scene_looped`` was the literal ``True``, on every soak record.

    The comment justifying it read "a soak loops its scene whichever source
    carries it", which is false for any soak that ends before the end of its
    scene: ``_feed_stream_over_tcp`` sends ``scene[frame_seq % len(scene)]``
    with ``len(scene) == plan.frames``, so 60 planned frames out of a 120-frame
    scene repeat nothing. A one-hour C5 soak does wrap, which is why the
    literal survived — the field was constant, not correct.

    ``ram_loop_note`` rides on the same fact and is asserted with it: it is a
    DECLARED SYSTEMATIC whose entire text describes a wrap, so shipping it
    beside a run that did not wrap is a declared systematic about an event that
    did not happen.
    """
    plan = benchmark.BenchmarkPlan(frames=120, warmup_frames=5)
    result = benchmark.run_soak(
        tmp_path / "nowrap", 192, 108, duration_s=2.0, plan=plan, target_fps=30.0,
        build_dir=edge_build_dir, health_period_ms=200,
    )
    assert result.run.returncode == 0, result.run.stderr_tail
    assert result.frames_planned == 60
    assert result.frames_planned < plan.frames, "this soak was supposed to not wrap"
    record = result.as_dict()
    assert record["scene_looped"] is False
    assert record["ram_loop_note"] is None


def test_a_ram_soak_shorter_than_its_clip_does_not_claim_a_wrap(
    edge_build_dir, tmp_path
):
    """The same fact on the RAM arm, where the note is louder.

    30 frames served out of a 60-frame resident clip cannot wrap, and nothing
    in ``ram_loop_declaration`` checks that ``total_frames > clip_frames`` —
    it validates budget, period exactness, drift, jitter and honesty. The
    record used to assert the wrap AND attach ``RAM_LOOP_SCENE_NOTE``.
    """
    plan = benchmark.BenchmarkPlan(
        frames=60, warmup_frames=6,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        ram_clip_frames=60, ram_budget_mb=160,
    )
    result = benchmark.run_soak(
        tmp_path / "ramnowrap", 288, 162, duration_s=1.0, plan=plan, target_fps=30.0,
        build_dir=edge_build_dir, health_period_ms=200,
    )
    assert result.run.returncode == 0, result.run.stderr_tail
    ram_plan = result.run.ram_plan
    assert ram_plan["total_frames"] <= ram_plan["clip_frames"]
    record = result.as_dict()
    assert record["scene_looped"] is False
    assert record["ram_loop_note"] is None
    # The DDR note is about where the bytes came from, not about a wrap, so it
    # still travels: the two systematics are separate claims.
    assert record["ddr_profile_note"] == benchmark.DDR_PROFILE_NOTE


def test_a_soak_on_a_host_without_thermals_reports_no_drift(edge_build_dir, tmp_path):
    """Two absences do not make a drift.

    On a machine with no thermal zone the first and last readings are both
    NOT-MEASURED, and the honest difference between them is not zero — it is
    another NOT-MEASURED. A zero would say the node held its temperature.
    """
    result = benchmark.run_soak(
        tmp_path / "soak2", 192, 108, duration_s=1.0,
        plan=benchmark.BenchmarkPlan(frames=10, warmup_frames=3),
        target_fps=20.0, build_dir=edge_build_dir, health_period_ms=200,
    )
    drift = result.thermal_drift_c
    if result.thermal_first.measured and result.thermal_last.measured:
        assert drift.measured  # a Linux host: a real drift, whatever it is
    else:
        assert not drift.measured
        assert drift.value is None
        assert "NOT-MEASURED" in drift.reason


# ---------------------------------------------------------------------------
# E8's comparison
# ---------------------------------------------------------------------------


def test_two_runs_of_one_configuration_agree_where_they_must(
    edge_build_dir, tmp_path
):
    """E8's shape, on the host: same config twice, compared.

    This is NOT E8, and the difference is load-bearing. E8 is this comparison
    on a BOARD — one core, a fixed clock, nothing else running — and the
    declared fps bound of 10% is a statement about that machine. An earlier
    version of this test enforced that bound here and went red at 17% while an
    emulated Buildroot build was using four cores of this laptop. Enforcing a
    board bound on a loaded developer machine is the same mistake W6 records in
    the D8 report, and the fix is not to widen the bound — it is to stop
    asserting it where it does not apply.

    So what is gated here is what a HOST can honestly gate: the deterministic
    counters really are identical run to run, the axes that have numbers get
    compared, and the verdict is derived from those rather than assumed. That
    the bound can FAIL is gated separately, synthetically, below.
    """
    plan = benchmark.BenchmarkPlan(frames=40, warmup_frames=10)
    runs = [
        benchmark.run_sweep(
            tmp_path / f"run{index}", plan=plan, resolutions=((288, 162),),
            build_dir=edge_build_dir,
        ).runs[0]
        for index in (0, 1)
    ]
    verdict = benchmark.compare_runs(runs[0], runs[1])
    # The part that is a property of the SYSTEM, not of the machine's mood:
    # identical frames through an identical detector produce identical
    # components, and any difference here is a defect no tolerance covers.
    assert verdict.exact_mismatches == []
    assert verdict.exact_compared == len(benchmark.EXACT_COUNTERS)
    assert verdict.compared_anything
    # The rate axes were compared — with numbers, not skipped — even though
    # this test does not judge them.
    assert "sustained_fps_daemon" in verdict.relative
    # Any breach is REPORTED with both numbers, which is what makes it
    # actionable on the board rather than a bare FAIL.
    for breach in verdict.breaches:
        assert "relative >" in breach and "declared" in breach


def _complete_run(counter: int, fps: float = 30.0, rss: float = 40.0,
                  cpu: float = 0.5) -> benchmark.Run:
    """A run with every counter and every bounded axis present.

    Comparisons of PARTIAL runs are now "incomplete" rather than "pass", so a
    test about counters or bounds has to start from a complete pair or it is
    testing incompleteness by accident.
    """
    run = benchmark.Run(label="x", proc_width=1, proc_height=1, paced_fps=None)
    run.stats = {key: counter for key in benchmark.EXACT_COUNTERS}
    for name, value, unit in (
        ("sustained_fps_daemon", fps, "fps"),
        ("peak_rss_mb", rss, "MB"),
        ("cpu_utilisation", cpu, "core"),
    ):
        run.measurements[name] = metrics.Measurement(
            name=name, value=value, unit=unit, source="test",
        )
    return run


def test_the_deterministic_counters_are_compared_exactly_and_can_fail():
    """A gate that cannot fail is decoration.

    Move one counter by one and the comparison must notice — no tolerance, no
    rounding, no "close enough" on a quantity that is either right or wrong.
    """
    left = _complete_run(10)
    right = _complete_run(10)
    assert benchmark.compare_runs(left, right).passes
    right.stats["observations_sent"] = 11
    verdict = benchmark.compare_runs(left, right)
    assert not verdict.passes
    assert verdict.verdict == "fail"
    assert any("observations_sent" in item for item in verdict.exact_mismatches)


def test_a_bounded_axis_outside_its_bound_fails():
    left = _complete_run(1)
    right = _complete_run(1, fps=60.0)
    verdict = benchmark.compare_runs(left, right)
    assert not verdict.passes
    assert verdict.verdict == "fail"
    assert any("sustained_fps_daemon" in item for item in verdict.breaches)


def test_two_not_measured_axes_are_uncompared_and_never_a_pass():
    """The failure mode this comparator was written to avoid.

    If DDR bandwidth is NOT-MEASURED in both runs, the axes "agree" in the
    sense that neither has a number. Calling that a pass would be reporting
    agreement about nothing, so it lands in `uncompared`.
    """
    left = _complete_run(1)
    right = _complete_run(1)
    for run in (left, right):
        run.measurements["peak_rss_mb"] = metrics.not_measured(
            "peak_rss_mb", "MB", "no mechanism on this machine"
        )
    verdict = benchmark.compare_runs(left, right)
    assert any("peak_rss_mb" in item for item in verdict.uncompared)
    assert "peak_rss_mb" not in verdict.relative
    # The assertion this test is NAMED for, which it did not make until a
    # skeptic pointed out that a name is not a gate.
    assert not verdict.passes
    assert verdict.verdict == "incomplete"


def test_two_runs_that_are_not_the_same_configuration_never_pass():
    """E8's premise is "two runs, SAME config", and it was never checked.

    ``compare_runs`` guarded exactly one configuration axis — ``source_mode``,
    and only when both stats files carried it. Everything else went unread: a
    288x162 run compared against a 2304x1296 one certified ``pass`` with an
    empty ``uncompared``, and ``Reproducibility.label`` took the LEFT label, so
    the artifact did not even record that two different labels had been
    compared.

    Each axis separately, so no one of them can be carrying the others.
    """
    def pair():
        return _complete_run(10), _complete_run(10)

    left, right = pair()
    assert benchmark.compare_runs(left, right).passes  # the control

    left, right = pair()
    right.proc_width, right.proc_height = 2304, 1296
    verdict = benchmark.compare_runs(left, right)
    assert verdict.verdict == "fail"
    assert any("proc grid" in item for item in verdict.config_mismatches)

    left, right = pair()
    right.label = "1152x648"
    verdict = benchmark.compare_runs(left, right)
    assert verdict.verdict == "fail"
    assert any("label" in item for item in verdict.config_mismatches)
    # Both sides in the artifact, so a published verdict can be audited.
    assert verdict.as_dict()["labels"] == ["x", "1152x648"]

    left, right = pair()
    right.paced_fps = 30.0
    assert benchmark.compare_runs(left, right).verdict == "fail"

    left, right = pair()
    right.ram_plan = {"clip_frames": 6, "total_frames": 24}
    assert benchmark.compare_runs(left, right).verdict == "fail"

    # A bounded axis read by two different mechanisms. `metrics`' own docstring
    # says procfs VmHWM and sampled `ps` "are different claim[s] about the same
    # quantity", which is why `Measurement.source` is mandatory — and then the
    # comparator that consumes them dropped it.
    left, right = pair()
    right.measurements["peak_rss_mb"] = metrics.Measurement(
        name="peak_rss_mb", value=40.0, unit="MB",
        source="ps RSS sampled every 20 ms",
    )
    verdict = benchmark.compare_runs(left, right)
    assert verdict.verdict == "fail"
    assert any(
        "different mechanisms" in item for item in verdict.config_mismatches
    ), verdict.config_mismatches
    # Still compared on the axis it could compare, so the report says both.
    assert "peak_rss_mb" in verdict.relative


def _compare_record(seed: int, detector: str = "soft", host: str = "testhost") -> dict:
    """A minimal sweep record in the shape `compare` reads off disk."""
    return {
        "schema": "d8-benchmark/1",
        "kind": "sweep",
        "plan": benchmark.BenchmarkPlan(seed=seed).as_dict(),
        "detector": detector,
        "host": host,
        "source_mode": benchmark.SOURCE_MODE_INJECT_FILE,
        "runs": [
            {
                "label": "288x162",
                "proc_width": 288,
                "proc_height": 162,
                "paced_fps": None,
                "stats": {key: 10 for key in benchmark.EXACT_COUNTERS},
                "measurements": {
                    "sustained_fps_daemon": {
                        "value": 30.0, "unit": "fps", "source": "health packet",
                    },
                    "peak_rss_mb": {"value": 40.0, "unit": "MB", "source": "procfs"},
                    "cpu_utilisation": {
                        "value": 0.5, "unit": "core", "source": "rusage",
                    },
                },
            }
        ],
    }


def test_two_records_that_configured_different_experiments_do_not_pass():
    """The record-level half, which no ``Run`` can see.

    Seed, frames, warm-up, movers, cap, detector and host are sweep-level keys.
    The ``compare`` CLI zipped the two run lists by index and cross-checked none
    of them, so two sweeps at different seeds — genuinely different scenes,
    byte-for-byte different streams — reported ``passes: true`` at every
    resolution, with every deterministic counter agreeing because the counters
    happen not to depend on the seed at these sizes.
    """
    same = benchmark.compare_records(_compare_record(7), _compare_record(7))
    assert same["passes"] is True
    assert same["config_mismatches"] == []

    seeded = benchmark.compare_records(_compare_record(7), _compare_record(8))
    assert seeded["passes"] is False
    assert any("plan.seed" in item for item in seeded["config_mismatches"])
    # Every per-run verdict still passes: the difference is not a run's.
    assert all(v["passes"] for v in seeded["verdicts"])
    # Both records identified in the artifact.
    assert [record["seed"] for record in seeded["records"]] == [7, 8]

    backend = benchmark.compare_records(
        _compare_record(7), _compare_record(7, detector="ive")
    )
    assert backend["passes"] is False
    assert any("detector" in item for item in backend["config_mismatches"])

    machine = benchmark.compare_records(
        _compare_record(7), _compare_record(7, host="other")
    )
    assert machine["passes"] is False
    assert any("host" in item for item in machine["config_mismatches"])


# ---------------------------------------------------------------------------
# The DECLARED bounds, and the document that publishes them
# ---------------------------------------------------------------------------


def _table_rows(text: str, heading: str) -> dict:
    """{row label: second column} from the markdown table under `heading`.

    The E5 twin of this helper broke out of its section only on a line starting
    with ``###``, so a scan of section 6.2 ran on through sections 7 and 8 and
    swallowed their tables. Here it stops at ANY heading, which is what the
    caller means by "the table under this one".
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    except StopIteration:
        return {}
    rows = {}
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] not in ("Axis", "---"):
            rows[cells[0]] = cells[1]
    return rows


def test_the_declared_run_to_run_bounds_are_the_ones_the_report_quotes():
    """The anti-tuning gate for E8, in the shape section 6 already uses.

    Parsed out of the TABLE, not searched for in the prose: a substring test
    passes on any number that happens to appear somewhere in 700 lines, which
    is how a bound nobody enforces gets published.
    """
    report_path = DOCS / "D8_EDGE_REPORT.md"
    if not report_path.exists():
        pytest.skip("D8_EDGE_REPORT.md has not been written yet")
    rows = _table_rows(
        report_path.read_text(encoding="utf-8"),
        "### 8.1 Declared benchmark run-to-run bounds",
    )
    assert rows, "section 8.1 has no table"
    declared = tolerance.BENCHMARK_RUN_TO_RUN
    labels = {
        "fps_relative": "sustained fps (relative)",
        "peak_rss_relative": "peak RSS (relative)",
        "cpu_utilisation_relative": "A7 utilisation (relative)",
    }
    for field_name, label in labels.items():
        assert label in rows, f"section 8.1 does not declare {label}"
        assert rows[label] == f"{getattr(declared, field_name):g}", label


def test_the_bounds_gate_would_notice_a_corrupted_declaration():
    """The E5 review found the old version of this gate accepting 9 of 12
    corrupted bounds. Prove this one reads the table it claims to read."""
    text = "\n".join(
        [
            "### 8.1 Declared benchmark run-to-run bounds (E8)",
            "",
            "| Axis | Declared bound | Measured |",
            "| --- | --- | --- |",
            "| sustained fps (relative) | 0.9 | PENDING (D8.1) |",
            "",
            "## 9. Findings",
            "| sustained fps (relative) | 0.1 | ignored |",
        ]
    )
    rows = _table_rows(text, "### 8.1 Declared benchmark run-to-run bounds")
    assert rows["sustained fps (relative)"] == "0.9"
    # And the row after the next heading did not leak in.
    assert len(rows) == 1


def test_the_run_to_run_bounds_are_declared_before_any_board_exists():
    declared = tolerance.BENCHMARK_RUN_TO_RUN
    assert tolerance.DECLARED_BENCHMARK_TOLERANCE["run_to_run"] is declared
    # The three numbers as LITERALS, and this is the assertion that carries the
    # anti-tuning weight. The table gate above compares the report against this
    # same constant — and the report is GENERATED from it — so on its own it
    # proves the document was rebuilt, not that the bounds are the ones
    # declared on 2026-08-10. Pinning them here means widening a bound has to
    # be done on purpose, in a test, where a reviewer sees it.
    assert (
        declared.fps_relative,
        declared.peak_rss_relative,
        declared.cpu_utilisation_relative,
    ) == (0.10, 0.05, 0.25), (
        "a declared benchmark bound moved. These were declared before any "
        "board ran; changing one is a decision to record in the report and "
        "the decisions log, not a test to update."
    )
    # Relative bounds, so every one is a fraction and none is a disguised
    # absolute. A bound above 1.0 would be "anything goes" wearing a number.
    for field_name in ("fps_relative", "peak_rss_relative", "cpu_utilisation_relative"):
        value = getattr(declared, field_name)
        assert 0.0 < value < 1.0, field_name
    # The deterministic counters are NOT bounded; they are compared exactly.
    assert not hasattr(declared, "frames_relative")
    assert "frames_in" in benchmark.EXACT_COUNTERS


# ---------------------------------------------------------------------------
# The RAM-loop source, as a sweep record
# ---------------------------------------------------------------------------
#
# The source's own behaviour lives in test_d81_ram_loop_source.py. What belongs
# here is what the HARNESS does with it: the second byte rate, what a sweep and
# a soak record carry, and whether the E8 comparison still means what it says.


def test_a_ram_loop_run_records_the_bytes_the_detector_actually_consumed(ram_sweep):
    """Two byte rates, from two mechanisms, and neither stands in for the other.

    ``input_mb_s`` keeps its meaning exactly — bytes the HOST PUSHED over the
    link — and for a RAM run that is the clip file, which crossed the medium
    once. ``source_mb_s`` is what the DETECTOR was fed, off the daemon's own
    ``source_bytes_served`` counter. For a 6-frame clip served 24 times they
    differ by a factor of four; at C5's hour from a 78-frame clip, by about
    1385. One field carrying both would report a detector feed as a link rate.

    The verdict is the part that would otherwise say something false. A RAM loop
    has NO source medium, so both "SOURCE-BOUND" and "undeclared source medium —
    cannot say" would be wrong about it, the second one especially: it reads as
    an omission an operator could fix. So the RAM branch answers BEFORE any
    ceiling is consulted, and is asserted here to ignore one even when offered.
    """
    for run in ram_sweep.runs:
        assert run.returncode == 0, run.stderr_tail
        assert run.source_mode == benchmark.SOURCE_MODE_INJECT_RAM
        assert run.frames_in == RAM_TOTAL_FRAMES
        assert run.source_bytes == run.frames_in * run.proc_width * run.proc_height
        # The loop factor is VISIBLE: the detector consumed several times what
        # the link carried, and both numbers are in the record.
        assert run.source_bytes > run.stream_bytes > 0
        assert run.source_mb_s is not None and run.source_mb_s > 0.0
        assert run.input_mb_s is not None and run.input_mb_s > 0.0
        assert "RAM-resident" in run.source_verdict(None)
        assert "RAM-resident" in run.source_verdict(
            benchmark.SOURCE_MEDIA["100m-ethernet"]
        )
        assert run.source_budget_met is True


def test_every_ram_sweep_and_soak_record_carries_the_source_mode_the_byte_rate_and_the_notes(  # noqa: E501
    ram_sweep, ram_soak
):
    """A4's "every sweep record carries it", read strictly and on both records.

    Strictly, because a note that lives only in a live process is a note the
    board session cannot cite months later: it has to be on the RUN as well as
    on the result, and it has to survive ``_run_from_dict``. On BOTH records,
    because a soak is a scored artifact too and it was the asymmetric one — the
    sweep carried ``cap_note`` and the soak carried nothing.

    The other half of "declared systematic" — that neither note is ever folded
    into a bound — used to be four assertions at the end of this function, three
    of which could not fail. They now live in
    ``test_a_declared_systematic_never_becomes_a_bounded_axis``, which needs no
    fixture and therefore also runs on a machine with no C toolchain, where this
    test skips.
    """
    for record in (ram_sweep.as_dict(), ram_soak.as_dict()):
        assert record["source_mode"] == benchmark.SOURCE_MODE_INJECT_RAM
        assert record["ddr_profile_note"] == benchmark.DDR_PROFILE_NOTE
        assert "never folded into any bound" in record["ddr_profile_note"]
        assert record["ram_loop_note"] == benchmark.RAM_LOOP_SCENE_NOTE
        assert record["ram_budget"], "the budget arithmetic did not reach the record"
        assert record["scene_looped"] is True

    # The budget rows describe the runs, not a re-derived maximum. Built from
    # `ram_budget_table` the sweep's rows said 3381 and 1881 frames while every
    # run's own ram_plan said 6 — a 563x discrepancy inside one record — and
    # `fits` could not be False for any run that could exist. The gate here was
    # `assert record["ram_budget"]`, a truthiness check, which is why.
    rows = {row["resolution"]: row for row in ram_sweep.as_dict()["ram_budget"]}
    for payload in ram_sweep.as_dict()["runs"]:
        row = rows[payload["label"]]
        assert row["clip_frames"] == payload["ram_plan"]["clip_frames"]
        assert row["clip_bytes"] == payload["ram_plan"]["clip_bytes"]
        assert row["detector_state_bytes"] == payload["ram_plan"][
            "detector_state_bytes"
        ]
        assert row["daemon_fixed_bytes"] == payload["ram_plan"]["daemon_fixed_bytes"]
    soak_rows = ram_soak.as_dict()["ram_budget"]
    assert len(soak_rows) == 1
    assert soak_rows[0]["clip_frames"] == ram_soak.as_dict()["run"]["ram_plan"][
        "clip_frames"
    ]

    runs = [*ram_sweep.as_dict()["runs"], ram_soak.as_dict()["run"]]
    for payload in runs:
        assert payload["source_mode"] == benchmark.SOURCE_MODE_INJECT_RAM
        assert payload["source_bytes"] > 0
        assert payload["source_mb_s"] > 0.0
        assert payload["ram_plan"]["clip_frames"] == RAM_CLIP_FRAMES
        assert payload["ddr_profile_note"] == benchmark.DDR_PROFILE_NOTE

    # Through JSON and back, which is the only form a board session ever sees.
    payload = json.loads(json.dumps(ram_sweep.as_dict()))
    rebuilt = [benchmark._run_from_dict(item) for item in payload["runs"]]
    for run, original in zip(rebuilt, ram_sweep.runs, strict=True):
        assert run.source_mode == original.source_mode
        assert run.source_bytes == original.source_bytes
        assert run.ram_plan == original.ram_plan


def test_a_declared_systematic_never_becomes_a_bounded_axis():
    """The other half of "declared systematic", asserted so it can fail.

    ``DDR_PROFILE_NOTE`` and ``RAM_LOOP_SCENE_NOTE`` are recorded beside every
    result and folded into NO bound. A systematic in a tolerance table is a bias
    folded into a random error, which is the one thing the two error channels
    exist to prevent.

    The finding that forced this shape: the guard was an intersection of
    ``{DDR_PROFILE_NOTE, RAM_LOOP_SCENE_NOTE}`` with
    ``BenchmarkTolerance.__dataclass_fields__``, plus two substring checks over
    ``json.dumps(report._BENCHMARK_AXES)``. The intersection is between
    multi-sentence prose and Python identifiers, which cannot hold a space, so it
    is empty for every value the dataclass can take. And ``json.dumps`` defaults
    to ``ensure_ascii=True``, which escapes ``RAM_LOOP_SCENE_NOTE``'s two
    em-dashes — the raw constant could not appear in the serialised text even
    when the axes literally carried it. Three of the four assertions could not
    fail, and the promotion the docstring named (a ``ddr_profile_relative``
    field with a matching axis pair) passed all four.

    So the invariant is asserted as what it actually is: the bounded axes are
    EXACTLY these three, which forces a phase that bounds a fourth to come back
    here and re-declare it. The substring guard stays as a second line — a note
    leaking into an axis LABEL is a different mistake from a note becoming a
    field — with ``ensure_ascii=False`` and a negative control under it, so the
    guard is itself proven live rather than assumed to be.
    """
    assert set(tolerance.BenchmarkTolerance.__dataclass_fields__) == {
        "fps_relative",
        "peak_rss_relative",
        "cpu_utilisation_relative",
    }, "a new bounded axis is a decision, and it gets declared here first"
    assert report._BENCHMARK_AXES == (
        ("sustained fps (relative)", "fps_relative"),
        ("peak RSS (relative)", "peak_rss_relative"),
        ("A7 utilisation (relative)", "cpu_utilisation_relative"),
    )
    # The two literals above are pinned to each other as well as to themselves:
    # an axis naming a field that does not exist, or a bounded field the report
    # never prints, is the same drift from the other end.
    assert {field for _, field in report._BENCHMARK_AXES} == set(
        tolerance.BenchmarkTolerance.__dataclass_fields__
    )

    axes = json.dumps(report._BENCHMARK_AXES, ensure_ascii=False)
    for note in (benchmark.DDR_PROFILE_NOTE, benchmark.RAM_LOOP_SCENE_NOTE):
        assert note not in axes
        # NEGATIVE CONTROL. Without it this loop is indistinguishable from the
        # escaping bug it replaced, which passed with the note sitting in the
        # tuple it was searching.
        poisoned = json.dumps(
            (*report._BENCHMARK_AXES, (note, "ddr_profile_relative")),
            ensure_ascii=False,
        )
        assert note in poisoned, (
            "the guard cannot see a note it is looking straight at, so a note "
            "in the axes would pass it too"
        )


def test_two_ram_loop_runs_agree_exactly_on_every_deterministic_counter(
    edge_build_dir, tmp_path
):
    """C3's E8 shape on the RAM source, and the reason D2 chose a frame budget.

    Every one of ``EXACT_COUNTERS`` is monotone in frames served, so a run
    bounded by anything but a frame count would make these ten a sample of the
    machine's speed and a slow CI box indistinguishable from a real defect. A
    frame budget makes the served byte stream a pure function of (clip, budget).

    What is deliberately NOT asserted is the VERDICT. A 24-frame run is far too
    short for the declared 5% peak-RSS bound to be meaningful, and it breaches
    it routinely on a host — the same reason
    ``test_two_runs_of_one_configuration_agree_where_they_must`` refuses to
    enforce a board bound here. The counters are the property of the system;
    the bounds are a statement about a board.
    """
    plan = benchmark.BenchmarkPlan(
        frames=RAM_TOTAL_FRAMES, warmup_frames=6,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        ram_clip_frames=RAM_CLIP_FRAMES,
    )
    runs = [
        benchmark.run_sweep(
            tmp_path / f"ram{index}", plan=plan, resolutions=((288, 162),),
            build_dir=edge_build_dir,
        ).runs[0]
        for index in (0, 1)
    ]
    for run in runs:
        assert run.returncode == 0, run.stderr_tail
        assert run.stats["source_frames_served"] == run.stats["source_frames_planned"]
    verdict = benchmark.compare_runs(runs[0], runs[1])
    assert verdict.exact_mismatches == []
    assert verdict.exact_compared == len(benchmark.EXACT_COUNTERS)
    assert verdict.compared_anything
    assert "sustained_fps_daemon" in verdict.relative


def test_a_ram_run_stopped_short_is_uncompared_not_a_mismatch():
    """A SIGTERMed loop is an incomplete experiment, not a determinism defect.

    Without this guard a run an operator stopped and then compared reads as ten
    legitimate exact mismatches — every counter is monotone in frames served, so
    all ten differ, and the verdict would be ``fail`` on a pair of runs where
    nothing was wrong with the daemon at all.

    Two sources, by contrast, ARE two experiments, so a differing ``source_mode``
    is a mismatch and not an uncompared axis. It is deliberately not counted in
    ``exact_compared``, which stays pinned to the length of EXACT_COUNTERS.
    """
    left = _complete_run(10)
    right = _complete_run(10)
    for run, served in ((left, 10), (right, 10)):
        run.stats["source_frames_planned"] = 24
        run.stats["source_frames_served"] = served
        run.stats["source_mode"] = benchmark.SOURCE_MODE_INJECT_RAM
    verdict = benchmark.compare_runs(left, right)
    assert any("frame budget" in item for item in verdict.uncompared), (
        verdict.uncompared
    )
    assert verdict.exact_mismatches == []
    assert verdict.breaches == []
    assert verdict.verdict == "incomplete"
    assert not verdict.passes

    # ...and a complete pair is not swept into the same bucket.
    for run in (left, right):
        run.stats["source_frames_served"] = 24
    assert not any(
        "frame budget" in item for item in benchmark.compare_runs(left, right).uncompared
    )

    right.stats["source_mode"] = benchmark.SOURCE_MODE_INJECT_FILE
    mixed = benchmark.compare_runs(left, right)
    assert any("source_mode" in item for item in mixed.exact_mismatches)
    assert mixed.exact_compared == len(benchmark.EXACT_COUNTERS)
    assert mixed.verdict == "fail"


def test_the_source_media_table_gained_no_ram_entry():
    """SOURCE_MEDIA stays at three keys, for two independently sufficient reasons.

    A "node-ram" MB/s figure would be an INVENTED denominator, which is exactly
    what ``BenchmarkPlan.source_medium``'s own docstring refuses — the harness
    cannot tell an SD card from a tmpfs and a source-bound verdict against a
    guessed ceiling is worthless. And this dict is rendered verbatim into
    committed report prose inside finding D8-F8, so adding a key edits published
    prose about a Recorded finding.

    The RAM run's honest answer is a different sentence entirely, not a fourth
    row: ``source_verdict`` says "RAM-resident" and consults no ceiling at all.
    """
    assert set(benchmark.SOURCE_MEDIA) == {
        "100m-ethernet", "sd-card-class10", "host-page-cache",
    }
    assert benchmark.SOURCE_MODE_INJECT_RAM not in benchmark.SOURCE_MEDIA


def test_the_ram_budget_arithmetic_never_wears_a_measured_label(ram_sweep):
    """Derived arithmetic may not enter the record as a Measurement.

    Every RAM-budget number is a sum of allocator sizes and clip geometry; no
    reading of any kind produces one, on any machine. Peak RSS is the only
    measured memory number in a run record — and on a board it does not even
    contain the IVE detector's share, which comes out of MMZ/CMA rather than the
    process heap, so a reader who adds the two would be adding two different
    quantities.
    """
    for run in ram_sweep.runs:
        basis = run.ram_plan["basis"]
        assert "not a measurement" in basis.lower(), basis
        assert "MMZ" in basis
        assert not [name for name in run.measurements if name.startswith("ram")]
        memory_axes = {
            name for name, item in run.measurements.items() if item.unit == "MB"
        }
        assert memory_axes == {"peak_rss_mb"}, memory_axes
        assert run.ram_plan["detector_state_bytes"] == benchmark.detector_state_bytes(
            run.proc_width, run.proc_height, ram_sweep.detector
        )


def test_peak_rss_is_decimal_mb_like_every_other_memory_number_in_this_phase():
    """The only MEASURED memory number in the phase, and it had no test.

    Both feeders are KiB: ``/proc/<pid>/status`` prints VmHWM as
    ``size << (PAGE_SHIFT-10)``, and ``ps -o rss=`` is in 1024-byte blocks on
    Linux and Darwin alike. Dividing by 1024 produced MiB wearing the unit
    string ``MB`` — 4.86% low, in a phase where every other memory number is
    explicitly decimal: ``RAM_LOOP_BUDGET_BYTES`` is 160,000,000, the daemon's
    budget is ``ram_budget_mb * 1000000ULL``, and the report says outright that
    "the 160 MB is read as DECIMAL bytes".

    The number below is the one that made it concrete: a process at
    164,167,680 B is OVER the 160,000,000 B line and was reported as
    "156.56 MB". Pinned against ``RAM_LOOP_BUDGET_BYTES`` in the same
    assertion, so the day the two conventions diverge again the suite says so.

    No gate was wrong — ``peak_rss_mb``'s only consumer is a RELATIVE bound,
    which a constant scale factor cancels out of exactly. The label was wrong,
    which is the thing this project does not let slide.
    """
    sampler = metrics.ProcessSampler(pid=0)
    sampler.result.peak_rss_kb = 160_320
    sampler.result.source = "test"
    reading = sampler.peak_rss()

    assert reading.unit == "MB"
    assert reading.value == pytest.approx(164.16768)
    assert reading.value * 1e6 == pytest.approx(160_320 * 1024)
    # Over the line, and the number says so.
    assert reading.value * 1e6 > benchmark.RAM_LOOP_BUDGET_BYTES
    # The binary reading is what it must NOT be.
    assert reading.value != pytest.approx(160_320 / 1024.0)

    # And an absence stays an absence, with its reason.
    empty = metrics.ProcessSampler(pid=0)
    empty.result.failure = "no /proc and no ps on this machine"
    assert not empty.peak_rss().measured


def test_a_ram_run_reports_both_rates_and_the_daemons_is_the_subinterval(ram_sweep):
    """Both fps axes survive the RAM path, and the subset invariant holds on it.

    Same invariant ``test_the_harness_reports_both_its_own_rate_and_the_daemons``
    asserts for the file rows, restated on the source mode A4 added: the daemon's
    interval is a SUBSET of the harness's, so its rate cannot be the lower of the
    two. What has teeth here is that BOTH axes are measured at all on this path —
    a RAM run whose health packet never arrives, or whose budget check refuses
    before the sockets open, produces a NOT-MEASURED daemon rate, and that is a
    live A4 failure mode rather than a hypothetical one.

    NOT GATED HERE, declared rather than implied: WHERE the preload sits. This
    test was called ``test_the_preload_stays_out_of_the_daemons_fps_denominator``
    and its docstring said the inequality "holds only because
    ``sw_inject_preload_ram`` runs before ``started_ns``; a lazy preload ... would
    invert this". That is false by construction. Both rates share the numerator
    ``frames_in`` (``metrics.sustained_fps(run.frames_in, run.wall_s)`` here,
    ``stats.frames_in / elapsed_s`` in main.c), and the daemon's denominator is a
    strict subinterval of the harness's for EVERY placement of ``started_ns``
    inside the process, so moving cost into the daemon's clock inflates both
    denominators by the same amount: the ratio decays toward 1 from above and
    never crosses the line. The review that found this hoisted ``started_ns``
    above the preload and then parked half a second of sleep inside the daemon's
    clock — 1000x the real preload cost — and the assertion stayed True both
    times, the ratio moving only from 1.68 to 1.05.

    The placement IS load-bearing — on the board's 12-frame 2304x1296 clip a
    misplaced preload is ~36 MB inside the denominator — and nothing in this
    repository can currently fail if it regresses: ``write_stats`` records no
    preload timing, so there is no artifact to assert against. Gating it needs a
    ``ram_preload_ns`` (or ``started_ns - preload_end_ns``) in the daemon's stats
    file, which is firmware work and is recorded as a finding rather than faked
    with a bound on host timing noise.
    """
    for run in ram_sweep.runs:
        # The subject is the RAM path; a file row would satisfy everything below
        # and prove nothing about the source mode this test is named for.
        assert run.source_mode == benchmark.SOURCE_MODE_INJECT_RAM, run.label
        wall = run.measurements["sustained_fps_wall"]
        own = run.measurements["sustained_fps_daemon"]
        assert wall.measured and own.measured, run.label
        # The daemon's rate comes from the health plane, not from the harness's
        # clock; if these two ever shared a source the comparison below would be
        # a number against itself.
        assert own.source != wall.source, (run.label, own.source)
        assert own.value >= wall.value * 0.99, (run.label, wall.value, own.value)


def test_the_report_quotes_the_declared_notes_the_code_holds():
    """A declaration that exists only in code is not a declaration.

    Mirrors section 8.1's anti-drift pattern exactly: the report is GENERATED
    from these constants, so reading the COMMITTED document is what proves it
    was rebuilt after they changed. Reading the generator's output instead would
    be a test of report.py against itself and would pass over a stale file.

    Both notes are declared systematics — recorded beside every result, folded
    into no bound — and a systematic nobody published is one nobody can discount
    a number by.
    """
    report_path = DOCS / "D8_EDGE_REPORT.md"
    if not report_path.exists():
        pytest.skip("D8_EDGE_REPORT.md has not been written yet")
    text = report_path.read_text(encoding="utf-8")
    assert benchmark.DDR_PROFILE_NOTE in text
    assert benchmark.RAM_LOOP_SCENE_NOTE in text
