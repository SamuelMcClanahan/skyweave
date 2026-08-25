"""C-002 driver: declarations, sampler parsing, capture replay, verdicts.

Everything here is host-exercised. The board path (SSH push, spawn, collect)
reuses `provision` primitives that carry their own suite; what C-002 adds —
the declared arithmetic, the on-board sampler's log format, the offline
capture replay with sender filtering and completeness reconciliation, the
health/thermal verdicts and the hardened pair comparison — is what these
tests pin. The E8 bounds themselves stay `tolerance.py`'s; no test here
retypes one.
"""

from __future__ import annotations

import json

import pytest

from skyweave2.contracts import ClockDomain
from skyweave2.edge import benchmark, campaign_c002, metrics, provision
from skyweave2.transport.codec import Health, encode_health
from skyweave2.transport.wire import PayloadType, frame

# Obvious dummies: RFC 5737 documentation addresses and locally-administered
# MACs. Real rig identities are operator-supplied and live in the private rig
# log, never in this tree.
BOARD_IP = "192.0.2.104"
BOARD_MAC = "02:00:00:00:00:aa"
JETSON_RIG_IP = "192.0.2.110"

# ---------------------------------------------------------------------------
# The declared arithmetic
# ---------------------------------------------------------------------------


def test_the_declarations_are_the_campaign_files_numbers():
    """36-frame clip, 1.2e9 ns stride, unpaced sweep, 30 fps soak.

    Derived through `benchmark.ram_loop_declaration`, never typed: if the
    budget arithmetic moves, this test moves the campaign file's numbers or
    fails — which is the declaration staying honest either way.
    """
    sweep = campaign_c002.ram_declaration("sweep")
    assert sweep.as_dict() == {
        "clip_frames": 36,
        "total_frames": 6300,
        "pts_stride_ns": 1_200_000_000,
        "budget_mb": 160,
        "period_ns": 0,
    }
    soak = campaign_c002.ram_declaration("soak")
    assert soak.as_dict() == {
        "clip_frames": 36,
        "total_frames": 108_000,
        "pts_stride_ns": 1_200_000_000,
        "budget_mb": 160,
        "period_ns": 33_333_333,
    }


def test_the_frozen_config_reaches_the_daemon_command():
    """The exact argv a run sends, spelled from the frozen constants."""
    decl = campaign_c002.ram_declaration("soak")
    plan = campaign_c002.build_plan("soak")
    spec = campaign_c002.BoardTarget(
        host=BOARD_IP,
        expected_mac=BOARD_MAC,
        jetson_rig_host=JETSON_RIG_IP,
        jump_host="jetson-lan-c001",
    ).spec("abc123")
    command = provision.daemon_command(
        "/userdata/skyweave/c002/run-abc123/skyweave-edge",
        benchmark.benchmark_config(
            campaign_c002.PROC_WIDTH, campaign_c002.PROC_HEIGHT, plan
        ),
        spec,
        ram_clip_remote="/userdata/skyweave/c002/run-abc123/ram.swij",
        ram_loop=decl,
        stats_remote="/userdata/skyweave/c002/run-abc123/stats.json",
        detector="ive",
    )
    for expected in (
        "--ram-loop-frames 108000",
        "--ram-loop-pts-stride-ns 1200000000",
        "--ram-loop-period-ns 33333333",
        "--ram-budget-mb 160",
        f"--jetson {JETSON_RIG_IP}",
        "--measurement-port 5601",
        "--health-period-ms 1000",
        "--detector ive",
    ):
        assert expected in command


def test_the_remote_directory_is_per_run():
    """Two runs may never share a board directory: a crashed daemon must not
    leave a previous run's stats.json where this run's collection looks."""
    target = campaign_c002.BoardTarget(
        host=BOARD_IP,
        expected_mac=BOARD_MAC,
        jetson_rig_host=JETSON_RIG_IP,
        jump_host="j",
    )
    assert (
        target.spec("run-a").remote_dir != target.spec("run-b").remote_dir
    )


def test_an_unknown_run_kind_is_refused():
    with pytest.raises(campaign_c002.CampaignC002Error):
        campaign_c002.build_plan("bench")


# ---------------------------------------------------------------------------
# The sampler log
# ---------------------------------------------------------------------------

_SAMPLER_TEXT = """clk_tck 100
daemon_pid 1201
s 100.00 28000 50 10 45000
s 105.00 29500 200 40 45500
s 110.00 30000 350 70 -
s 115.00 30000 500 100 46000
end 115.40
"""

_SAMPLER_TEXT_NO_END = "\n".join(_SAMPLER_TEXT.splitlines()[:-1]) + "\n"


def test_sampler_log_parsing_keeps_absences_and_the_end_marker():
    log = campaign_c002.parse_sampler_log(_SAMPLER_TEXT)
    assert log.clk_tck == 100
    assert log.daemon_pid == 1201
    assert log.child_found is True
    assert log.ended is True
    assert log.end_uptime_s == pytest.approx(115.40)
    assert len(log.samples) == 4
    assert log.samples[2].thermal_milli_c is None
    assert log.samples[3].vmhwm_kib == 30000


def test_sampler_measurements_convert_like_the_host_collectors():
    """KiB to DECIMAL MB, ticks over CLK_TCK over the board's own span."""
    log = campaign_c002.parse_sampler_log(_SAMPLER_TEXT)
    out = campaign_c002.sampler_measurements(log)
    rss = out["peak_rss_mb"]
    assert rss.measured and rss.value == pytest.approx(30000 * 1024 / 1e6)
    cpu = out["cpu_utilisation"]
    # (600 - 60) ticks / 100 Hz over 15 s of board uptime.
    assert cpu.measured and cpu.value == pytest.approx((540 / 100) / 15.0)
    thermal = out["thermal_c"]
    assert thermal.measured and thermal.value == pytest.approx(46.0)


def test_a_sampler_prefix_measures_nothing():
    """No end marker means the log is a prefix of the run: a VmHWM read at
    minute 30 of a 60-minute soak is not the run's peak, and reporting it
    would be a number about half a run wearing a whole run's label."""
    log = campaign_c002.parse_sampler_log(_SAMPLER_TEXT_NO_END)
    assert log.ended is False
    out = campaign_c002.sampler_measurements(log)
    assert not out["peak_rss_mb"].measured
    assert not out["cpu_utilisation"].measured
    assert not out["thermal_c"].measured
    assert "prefix" in out["peak_rss_mb"].reason


def test_a_sampler_that_never_found_the_daemon_measures_nothing():
    log = campaign_c002.parse_sampler_log("clk_tck 100\nno_daemon_child_found\n")
    assert log.child_found is False
    out = campaign_c002.sampler_measurements(log)
    assert not out["peak_rss_mb"].measured
    assert not out["cpu_utilisation"].measured


def test_the_sampler_script_is_posix_sh_and_writes_its_end_marker():
    """BusyBox ash runs it: no bash-only syntax, the stat parse splits after
    the comm parens so a comm with spaces cannot shift the fields, and the
    end marker exists so a dead sampler is distinguishable from a done one."""
    script = campaign_c002.SAMPLER_SCRIPT
    assert script.startswith("#!/bin/sh")
    assert "[[" not in script
    assert "function " not in script
    assert "cut -d')' -f2-" in script
    assert "skyweave-edge" in script
    assert 'echo "end ' in script


# ---------------------------------------------------------------------------
# Capture replay
# ---------------------------------------------------------------------------


def _health_datagram(
    ns: int, fps: float = 25.0, drops: int = 0, session: str = "sess-1"
) -> bytes:
    return encode_health(
        Health(
            camera_id=0,
            session_uuid=session,
            ts_ns=ns,
            clock_domain=ClockDomain.NODE_MONO,
            fps=fps,
            drops=drops,
            time_sync_error_ms=0.0,
        )
    )


def _write_capture(path, entries, opened=True):
    lines = ["capture-open\n"] if opened else []
    for ns, sender, data in entries:
        lines.append(f"{ns} {sender} {data.hex()}\n")
    path.write_text("".join(lines), encoding="ascii")


def test_capture_replay_matches_the_listener_branches(tmp_path):
    """Health decoded, observations counted not decoded, garbage labelled."""
    capture = tmp_path / "capture.hex"
    entries = [
        (1_000_000_000, BOARD_IP, _health_datagram(1)),
        (2_000_000_000, BOARD_IP, frame(PayloadType.OBSERVATION, b"skipped")),
        (2_500_000_000, BOARD_IP, b"\xff\xfe garbage"),
        (3_000_000_000, BOARD_IP, _health_datagram(2)),
    ]
    _write_capture(capture, entries)
    replay = campaign_c002.replay_capture(capture, expected_sender=BOARD_IP)
    assert replay.opened is True
    assert replay.stats.datagrams == 4
    assert replay.stats.health_packets == 2
    assert replay.stats.observation_packets == 1
    assert replay.stats.rejected_total == 1
    assert replay.foreign_datagrams == 0
    assert [r.received_monotonic_ns for r in replay.readings] == [
        1_000_000_000,
        3_000_000_000,
    ]


def test_foreign_senders_are_counted_and_never_scored(tmp_path):
    """5601 is the rig-wide measurement port: a stale daemon from another
    board (or an aborted attempt) must not fill this run's cadence gaps or
    donate its final fps."""
    capture = tmp_path / "capture.hex"
    entries = [
        (1_000_000_000, BOARD_IP, _health_datagram(1, fps=24.0)),
        (1_500_000_000, "192.0.2.102", _health_datagram(2, fps=30.0)),
        (2_000_000_000, BOARD_IP, _health_datagram(3, fps=24.5)),
        (2_500_000_000, "192.0.2.102", _health_datagram(4, fps=30.0)),
    ]
    _write_capture(capture, entries)
    replay = campaign_c002.replay_capture(capture, expected_sender=BOARD_IP)
    assert replay.foreign_datagrams == 2
    assert replay.stats.health_packets == 2
    assert [r.health.fps for r in replay.readings] == [24.0, 24.5]


def test_a_corrupt_capture_line_is_a_labelled_rejection(tmp_path):
    capture = tmp_path / "capture.hex"
    capture.write_text(
        f"capture-open\n123 {BOARD_IP} zznothex\nonly-two fields\n",
        encoding="ascii",
    )
    replay = campaign_c002.replay_capture(capture, expected_sender=BOARD_IP)
    assert replay.readings == []
    assert replay.stats.rejected == {
        "capture_line: ValueError": 1,
        "capture_line: FieldCount": 1,
    }


def test_the_reconciliation_and_final_fps_require_a_complete_capture(tmp_path):
    """A listener that died mid-run leaves a clean-looking prefix; only the
    daemon's own health_sent counter says the rest is missing."""
    capture = tmp_path / "capture.hex"
    _write_capture(
        capture,
        [
            (1_000_000_000, BOARD_IP, _health_datagram(1, fps=20.0)),
            (2_000_000_000, BOARD_IP, _health_datagram(2, fps=24.5)),
        ],
    )
    replay = campaign_c002.replay_capture(capture, expected_sender=BOARD_IP)
    complete = campaign_c002.telemetry_reconciliation(replay, {"health_sent": 3})
    assert complete["complete"] is True
    fps = campaign_c002.final_health_fps(replay, complete)
    assert fps.measured and fps.value == pytest.approx(24.5)
    truncated = campaign_c002.telemetry_reconciliation(
        replay, {"health_sent": 60}
    )
    assert truncated["complete"] is False
    absent = campaign_c002.final_health_fps(replay, truncated)
    assert not absent.measured
    assert "health_sent" in absent.reason
    no_counter = campaign_c002.telemetry_reconciliation(replay, {})
    assert no_counter["complete"] is False


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def _soak_inputs(tmp_path, *, sessions=("sess-1",), gap_at=(), seconds=60):
    """A second-by-second capture for a run that completed its budget."""
    entries = []
    sent = 0
    for second in range(seconds):
        session = sessions[min(second // 30, len(sessions) - 1)]
        sent += 1
        if second in gap_at:
            continue
        entries.append(
            (second * 1_000_000_000, BOARD_IP, _health_datagram(second, session=session))
        )
    capture = tmp_path / "capture.hex"
    _write_capture(capture, entries)
    replay = campaign_c002.replay_capture(capture, expected_sender=BOARD_IP)
    run_stats = {
        "source_frames_planned": campaign_c002.SOAK_TOTAL_FRAMES,
        "source_frames_served": campaign_c002.SOAK_TOTAL_FRAMES,
        # The daemon's own counter: every second SENT one, including the
        # seconds the wire (gap_at) lost — that is exactly the loss the
        # reconciliation's tolerance absorbs.
        "health_sent": sent,
    }
    telemetry = campaign_c002.telemetry_reconciliation(replay, run_stats)
    thermal = campaign_c002.thermal_verdict(
        [(float(t), 45.0) for t in range(0, 4000, 5)],
        sampler_ended=True,
        end_uptime_s=3995.0,
    )
    return replay, telemetry, run_stats, thermal


def test_a_clean_soak_passes_all_criteria(tmp_path):
    replay, telemetry, run_stats, thermal = _soak_inputs(tmp_path)
    verdict = campaign_c002.soak_health_verdict(
        replay, telemetry, run_stats, 0, thermal
    )
    assert verdict["clean"] is True


def test_a_second_session_uuid_is_a_restart(tmp_path):
    replay, telemetry, run_stats, thermal = _soak_inputs(
        tmp_path, sessions=("sess-1", "sess-2")
    )
    verdict = campaign_c002.soak_health_verdict(
        replay, telemetry, run_stats, 0, thermal
    )
    assert verdict["clean"] is False
    assert verdict["criteria"]["no_restart"]["clean"] is False
    assert verdict["criteria"]["no_restart"]["session_uuids"] == [
        "sess-1",
        "sess-2",
    ]


def test_a_missed_health_second_is_a_gap(tmp_path):
    replay, telemetry, run_stats, thermal = _soak_inputs(tmp_path, gap_at={30})
    # The daemon SENT it; the wire lost it. Within the loss tolerance the
    # reconciliation stays complete, and the cadence gap is what fails.
    assert telemetry["complete"] is True
    verdict = campaign_c002.soak_health_verdict(
        replay, telemetry, run_stats, 0, thermal
    )
    assert verdict["criteria"]["no_health_gap"]["clean"] is False
    assert verdict["criteria"]["no_health_gap"]["max_period_s"] >= 2.0


def test_a_truncated_capture_fails_the_gap_criterion_not_silently(tmp_path):
    """The [0]/[6] review scenario: the listener dies at the half-way mark,
    the recorded prefix has perfect 1 Hz cadence, and only the health_sent
    reconciliation says half the evidence is missing."""
    replay, _telemetry, run_stats, thermal = _soak_inputs(tmp_path, seconds=30)
    run_stats = dict(run_stats, health_sent=60)
    telemetry = campaign_c002.telemetry_reconciliation(replay, run_stats)
    assert telemetry["complete"] is False
    verdict = campaign_c002.soak_health_verdict(
        replay, telemetry, run_stats, 0, thermal
    )
    assert verdict["criteria"]["no_health_gap"]["clean"] is False
    assert verdict["clean"] is False


def test_a_short_run_or_nonzero_exit_is_not_a_natural_completion(tmp_path):
    replay, telemetry, run_stats, thermal = _soak_inputs(tmp_path)
    short = dict(run_stats, source_frames_served=107_999)
    verdict = campaign_c002.soak_health_verdict(
        replay, telemetry, short, 0, thermal
    )
    assert verdict["criteria"]["natural_completion"]["clean"] is False
    verdict = campaign_c002.soak_health_verdict(
        replay, telemetry, run_stats, 143, thermal
    )
    assert verdict["criteria"]["natural_completion"]["clean"] is False


def test_thermal_verdict_ceiling_plateau_and_completeness():
    flat = [(float(t), 50.0) for t in range(0, 4000, 5)]
    clean = campaign_c002.thermal_verdict(
        flat, sampler_ended=True, end_uptime_s=3995.0
    )
    assert clean["clean"] is True and clean["evaluated"] is True
    hot = flat + [(4000.0, campaign_c002.THERMAL_CEILING_C)]
    assert (
        campaign_c002.thermal_verdict(
            hot, sampler_ended=True, end_uptime_s=4000.0
        )["clean"]
        is False
    )
    # A slow stair of 0.2 C per 5 minutes stays under the declared 0.5 C
    # per-window limit; 0.7 C steps breach it. Sliding windows mean a ramp
    # straddling a boundary is still one window's rise.
    slow = [(float(t), 40.0 + 0.2 * (t // 300)) for t in range(0, 4000, 5)]
    assert (
        campaign_c002.thermal_verdict(
            slow, sampler_ended=True, end_uptime_s=3995.0
        )["clean"]
        is True
    )
    steep = [(float(t), 40.0 + 0.7 * (t // 300)) for t in range(0, 4000, 5)]
    assert (
        campaign_c002.thermal_verdict(
            steep, sampler_ended=True, end_uptime_s=3995.0
        )["clean"]
        is False
    )
    empty = campaign_c002.thermal_verdict(
        [], sampler_ended=True, end_uptime_s=None
    )
    assert empty["clean"] is False and empty["evaluated"] is False


def test_a_dead_sampler_cannot_certify_a_thermal_plateau():
    """The tail is anchored at the sampler's END marker: a curve that stops
    at minute 30 must not be judged plateau over minutes 15-30 while the
    unobserved final half is where the runaway would be."""
    prefix = [(float(t), 50.0) for t in range(0, 1800, 5)]
    dead = campaign_c002.thermal_verdict(
        prefix, sampler_ended=False, end_uptime_s=None
    )
    assert dead["clean"] is False and dead["evaluated"] is False
    assert "outlive" in dead["reason"]


def test_sparse_thermal_windows_are_not_evaluable():
    """Three points spread over five minutes are not a curve."""
    sparse = [(0.0, 50.0), (200.0, 50.0), (400.0, 50.0), (900.0, 50.0), (1200.0, 50.0)]
    verdict = campaign_c002.thermal_verdict(
        sparse, sampler_ended=True, end_uptime_s=1200.0
    )
    assert verdict["evaluated"] is False
    assert verdict["clean"] is False


# ---------------------------------------------------------------------------
# Records and the pair comparison
# ---------------------------------------------------------------------------


def _measurements(fps=25.0, rss=30.0, cpu=0.9):
    return {
        "sustained_fps_daemon": metrics.Measurement(
            "sustained_fps_daemon", fps, "fps", "s-fps"
        ),
        "peak_rss_mb": metrics.Measurement("peak_rss_mb", rss, "MB", "s-rss"),
        "cpu_utilisation": metrics.Measurement(
            "cpu_utilisation", cpu, "core", "s-cpu"
        ),
    }


def _record(tmp_path, name, kind="sweep", index=1, invalid=None, **overrides):
    decl = campaign_c002.ram_declaration(kind)
    total = decl.total_frames
    stats = {
        "frames_in": total,
        "frames_scored": total - 30,
        "capture_events": 500,
        "observations_sent": 500,
        "components_offered": 500,
        "components_emitted": 500,
        "components_dropped_over_cap": 0,
        "frames_at_cap": 0,
        "events_unencodable": 0,
        "components_shed_by_detector": 0,
        "source_frames_planned": total,
        "source_frames_served": total,
        "source_bytes_served": total * 746_496,
        "health_sent": 200,
    }
    stats.update(overrides.pop("stats", {}))
    run = benchmark.Run(
        label=f"c002-{kind}",
        proc_width=campaign_c002.PROC_WIDTH,
        proc_height=campaign_c002.PROC_HEIGHT,
        paced_fps=campaign_c002.PACE_FPS if kind == "soak" else None,
        wall_s=250.0,
        returncode=0,
        stream_bytes=26_873_856,
        stats=stats,
        measurements=overrides.pop("measurements", _measurements()),
        health={"stats": {}, "cadence": {}},
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        source_bytes=stats["source_bytes_served"],
        ram_plan=decl.as_dict(),
    )
    record = {
        "schema": "skyweave-c002-run/1",
        "campaign_id": "C-002",
        "kind": kind,
        "index": index,
        "run_id": f"rid-{name}",
        "board": "board-104",
        "identity": {"mac": BOARD_MAC},
        "manifest_sha256": "a" * 64,
        "run": run.as_dict(),
    }
    if invalid:
        record["invalid"] = invalid
    record.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_an_identical_pair_passes_e8(tmp_path):
    first = _record(tmp_path, "r1.json", index=1)
    second = _record(tmp_path, "r2.json", index=2)
    result = campaign_c002.compare_pair(first, second, tmp_path / "cmp.json")
    assert result["e8"]["verdict"] == "pass"


def test_an_fps_swing_beyond_the_declared_bound_fails(tmp_path):
    first = _record(tmp_path, "r1.json")
    second = _record(
        tmp_path, "r2.json", index=2, measurements=_measurements(fps=20.0)
    )
    result = campaign_c002.compare_pair(first, second, tmp_path / "cmp.json")
    assert result["e8"]["verdict"] == "fail"
    assert any("sustained_fps_daemon" in b for b in result["e8"]["breaches"])


def test_a_missing_bounded_axis_is_incomplete_not_a_pass(tmp_path):
    absent = _measurements()
    absent["sustained_fps_daemon"] = metrics.not_measured(
        "sustained_fps_daemon", "fps", "no health packet arrived"
    )
    first = _record(tmp_path, "r1.json", measurements=absent)
    second = _record(tmp_path, "r2.json", index=2, measurements=absent)
    result = campaign_c002.compare_pair(first, second, tmp_path / "cmp.json")
    assert result["e8"]["verdict"] == "incomplete"


def test_a_soak_pair_needs_clean_health_for_the_objective(tmp_path):
    clean = {"clean": True, "criteria": {}}
    dirty = {"clean": False, "criteria": {}}
    first = _record(tmp_path, "k1.json", kind="soak", health_verdict=clean)
    second = _record(
        tmp_path, "k2.json", kind="soak", index=2, health_verdict=clean
    )
    result = campaign_c002.compare_pair(first, second, tmp_path / "cmp1.json")
    assert result["soak_e8_pass"] == 1
    third = _record(
        tmp_path, "k3.json", kind="soak", index=2, health_verdict=dirty
    )
    result = campaign_c002.compare_pair(first, third, tmp_path / "cmp2.json")
    assert result["soak_e8_pass"] == 0


def test_pairs_that_are_not_a_pair_are_refused(tmp_path):
    sweep = _record(tmp_path, "s.json")
    soak = _record(tmp_path, "k.json", kind="soak", index=2)
    with pytest.raises(campaign_c002.CampaignC002Error):
        campaign_c002.compare_pair(sweep, soak, tmp_path / "cmp.json")
    other_clip = _record(
        tmp_path, "o.json", index=2, manifest_sha256="b" * 64
    )
    with pytest.raises(campaign_c002.CampaignC002Error):
        campaign_c002.compare_pair(sweep, other_clip, tmp_path / "cmp.json")
    stopped = _record(
        tmp_path, "x.json", index=2, invalid="exceeded its wall cap"
    )
    with pytest.raises(campaign_c002.CampaignC002Error):
        campaign_c002.compare_pair(sweep, stopped, tmp_path / "cmp.json")


def test_a_run_compared_against_itself_is_refused(tmp_path):
    first = _record(tmp_path, "r1.json")
    clone = _record(tmp_path, "r1clone.json", run_id="rid-r1.json", index=2)
    with pytest.raises(campaign_c002.CampaignC002Error) as excinfo:
        campaign_c002.compare_pair(first, clone, tmp_path / "cmp.json")
    assert "same run id" in str(excinfo.value)
    same_index = _record(tmp_path, "r3.json", index=1)
    with pytest.raises(campaign_c002.CampaignC002Error):
        campaign_c002.compare_pair(first, same_index, tmp_path / "cmp.json")


def test_a_cross_board_pair_is_refused(tmp_path):
    first = _record(tmp_path, "r1.json")
    other_board = _record(
        tmp_path,
        "r2.json",
        index=2,
        board="board-102",
        identity={"mac": "02:00:00:00:00:bb"},
    )
    with pytest.raises(campaign_c002.CampaignC002Error) as excinfo:
        campaign_c002.compare_pair(first, other_board, tmp_path / "cmp.json")
    assert "different boards" in str(excinfo.value)


def test_run_records_round_trip_through_reconstruction(tmp_path):
    path = _record(tmp_path, "r1.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    run = campaign_c002.run_from_record(record)
    assert run.label == "c002-sweep"
    assert run.measurements["sustained_fps_daemon"].value == 25.0
    absent = campaign_c002.run_from_record(
        json.loads(
            _record(
                tmp_path,
                "r2.json",
                measurements={
                    "sustained_fps_daemon": metrics.not_measured(
                        "sustained_fps_daemon", "fps", "nothing arrived"
                    )
                },
            ).read_text(encoding="utf-8")
        )
    )
    assert absent.measurements["sustained_fps_daemon"].value is None


# ---------------------------------------------------------------------------
# The probe and the ledger
# ---------------------------------------------------------------------------


def test_prepare_probe_writes_a_generator_bound_manifest(tmp_path):
    manifest = campaign_c002.prepare_probe(tmp_path / "probe")
    assert manifest["clip_frames"] == 36
    # 36 luma frames plus the session header, frame records and trailer: the
    # file is a little larger than the raw 26,873,856 luma bytes, never less.
    assert manifest["clip_bytes"] > 26_873_856
    assert "numpy" in manifest["generator_versions"]
    clip, loaded = campaign_c002.load_probe(
        tmp_path / "probe" / "probe_manifest.json"
    )
    assert loaded["clip_sha256"] == manifest["clip_sha256"]
    assert clip.exists()
    with pytest.raises(campaign_c002.CampaignC002Error):
        campaign_c002.prepare_probe(tmp_path / "probe")


def test_a_substituted_clip_is_refused_even_with_a_matching_manifest(tmp_path):
    """load_probe regenerates from the frozen plan: a foreign clip whose
    manifest was rehashed to match it still fails, because the generator is
    the authority and the manifest is only a claim."""
    campaign_c002.prepare_probe(tmp_path / "probe")
    clip = tmp_path / "probe" / "probe.swij"
    manifest_path = tmp_path / "probe" / "probe_manifest.json"
    foreign = clip.read_bytes()[:-1] + b"\x00"
    clip.write_bytes(foreign)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["clip_sha256"] = campaign_c002._sha256_file(clip)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(campaign_c002.CampaignC002Error) as excinfo:
        campaign_c002.load_probe(manifest_path)
    assert "generator" in str(excinfo.value)


def _subject_to():
    return {
        "gate_platform_suite_green": True,
        "fenced_paths_untouched": True,
        "probe_input_only": True,
        "source_tree_sha256": "c" * 64,
        "gate_evidence": {"path": "subject/gate.json", "sha256": "d" * 64},
        "fenced_evidence": {"path": "subject/fenced.json", "sha256": "e" * 64},
    }


def test_the_ledger_binds_the_artifact_hash_and_subject_to(tmp_path):
    artifact = tmp_path / "run.json"
    artifact.write_text('{"a": 1}\n', encoding="utf-8")
    sha = campaign_c002._sha256_file(artifact)
    row = campaign_c002.append_ledger_row(
        tmp_path / "ledger.jsonl",
        hypothesis="h",
        artifact_path=artifact,
        artifact_sha256=sha,
        board="board-104",
        verdict="measurement",
        subject_to=_subject_to(),
    )
    assert row["n"] == 1
    with pytest.raises(campaign_c002.CampaignC002Error):
        campaign_c002.append_ledger_row(
            tmp_path / "ledger.jsonl",
            hypothesis="h",
            artifact_path=artifact,
            artifact_sha256="0" * 64,
            board="board-104",
            verdict="measurement",
            subject_to=_subject_to(),
        )
    incomplete = _subject_to()
    del incomplete["gate_evidence"]
    with pytest.raises(campaign_c002.CampaignC002Error) as excinfo:
        campaign_c002.append_ledger_row(
            tmp_path / "ledger.jsonl",
            hypothesis="h",
            artifact_path=artifact,
            artifact_sha256=sha,
            board="board-104",
            verdict="measurement",
            subject_to=incomplete,
        )
    assert "gate_evidence" in str(excinfo.value)


def test_an_invalidated_run_cannot_be_ledgered_as_a_measurement(tmp_path):
    artifact = tmp_path / "run.json"
    artifact.write_text(
        json.dumps({"schema": "skyweave-c002-run/1", "invalid": "wall cap"}),
        encoding="utf-8",
    )
    with pytest.raises(campaign_c002.CampaignC002Error) as excinfo:
        campaign_c002.append_ledger_row(
            tmp_path / "ledger.jsonl",
            hypothesis="h",
            artifact_path=artifact,
            artifact_sha256=campaign_c002._sha256_file(artifact),
            board="board-104",
            verdict="measurement",
            subject_to=_subject_to(),
        )
    assert "invalidated" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The report's section-8 block
# ---------------------------------------------------------------------------


def _comparison(kind, verdict="fail", exact=("components_offered: 1 != 2",)):
    result = {
        "schema": "skyweave-c002-comparison/1",
        "kind": kind,
        "e8": {
            "verdict": verdict,
            "exact_mismatches": list(exact),
            "relative": {"sustained_fps_daemon": 0.0002},
            "breaches": [],
            "uncompared": [],
            "config_mismatches": [],
        },
    }
    if kind == "soak":
        result["soak_e8_pass"] = 0
    return result


def _thermal(record_path, first_c, last_c):
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["thermal_curve"] = [
        {"uptime_s": 0.0, "value_c": first_c},
        {"uptime_s": 3600.0, "value_c": last_c},
    ]
    record["run"]["health"]["last_drops"] = 0
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return record


def test_report_evidence_reads_every_number_from_the_artifacts(tmp_path):
    sweep_first = _thermal(_record(tmp_path, "s1.json"), 51.1, 51.7)
    sweep_second = _thermal(_record(tmp_path, "s2.json", index=2), 51.0, 51.8)
    soak_first = _thermal(_record(tmp_path, "k1.json", kind="soak"), 50.0, 53.0)
    soak_second = _thermal(
        _record(tmp_path, "k2.json", kind="soak", index=2), 50.5, 53.2
    )
    block = campaign_c002.build_report_evidence(
        sweep_first,
        sweep_second,
        _comparison("sweep"),
        soak_first,
        soak_second,
        _comparison("soak"),
    )
    row = block["sweep_rows"]["1152x648"]
    assert row["fps"].startswith("25.00 / 25.00")
    assert "F-C2-1" in row["verdict"]
    assert block["soak"]["frames"] == "108000 of 108000 (both runs)"
    assert block["soak"]["thermal_drift"] == "3.0 C / 2.7 C"
    assert "soak_e8_pass = 0" in block["narrative"][0]


def test_the_report_renders_the_block_and_stays_pending_without_it(tmp_path):
    from skyweave2.edge import report

    sweep_first = _thermal(_record(tmp_path, "s1.json"), 51.1, 51.7)
    sweep_second = _thermal(_record(tmp_path, "s2.json", index=2), 51.0, 51.8)
    soak_first = _thermal(_record(tmp_path, "k1.json", kind="soak"), 50.0, 53.0)
    soak_second = _thermal(
        _record(tmp_path, "k2.json", kind="soak", index=2), 50.5, 53.2
    )
    block = campaign_c002.build_report_evidence(
        sweep_first, sweep_second, _comparison("sweep"),
        soak_first, soak_second, _comparison("soak"),
    )
    text = report.generate({"board_benchmark": block})
    assert "Measured (C-002, board-104)" in text
    assert "| 1152x648 | inject-ram |" in text
    # The other two resolutions stay PENDING: no board ran them in C-002.
    assert "| 1536x864 | PENDING |" in text
    assert "| Frames | 108000 of 108000 (both runs) |" in text
    # The declared systematics survive the measured rows verbatim.
    assert benchmark.DDR_PROFILE_NOTE in text
    assert benchmark.RAM_LOOP_SCENE_NOTE in text
    bare = report.generate(None)
    assert "## 8. Board benchmark and deployment resolution — PENDING" in bare
    assert "| 1152x648 | PENDING |" in bare


def test_report_evidence_without_a_soak_pair_stays_honest(tmp_path):
    """A blocked campaign hands over the sweep row Measured and the soak
    table PENDING with the reason quoted — never a half-filled soak."""
    from skyweave2.edge import report

    sweep_first = _thermal(_record(tmp_path, "s1.json"), 51.1, 51.7)
    sweep_second = _thermal(_record(tmp_path, "s2.json", index=2), 51.0, 51.8)
    note = "Soak pair blocked: the rig switch link died mid-run (F-C2-5)."
    block = campaign_c002.build_report_evidence(
        sweep_first, sweep_second, _comparison("sweep"), blocked_note=note
    )
    assert "soak" not in block
    assert note in block["narrative"][-1]
    text = report.generate({"board_benchmark": block})
    assert "| 1152x648 | inject-ram |" in text
    assert "| Frames | PENDING |" in text
    assert note in text
    with pytest.raises(campaign_c002.CampaignC002Error):
        campaign_c002.build_report_evidence(
            sweep_first, sweep_second, _comparison("sweep"),
            soak_first=_thermal(_record(tmp_path, "k.json", kind="soak"), 50.0, 51.0),
        )
