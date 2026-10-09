"""S series, closed loop: scenarios S1-S6 against the pinned SITL, plus their pure parts.

Contract: DRONE_CONTRACTS_D0.md §8 ([S0], [S7], [S8], the scenario table),
[R3] for replay, [M8] for the scripted approve. The slow tests run the gate
exactly as ``python -m skyweave2.drone.harness --seed-set gate`` does
(``harness.batch``): every gate seed of the scenario, ``gate_repeats``
times, each run on its own SITL with its own clock, then [R3] replay of each
run's recording. The fast tests feed real packets, real MAVLink frames, and
real sockets through the real code (Mission, SyntheticCamera, Tracker, the
relay); expected values come from the contract, a hand derivation written in
the docstring, or the system's own outputs. Nothing we own is mocked.

Slow-tier knobs (process control only, never scored): ``SKYWEAVE_HARNESS_JOBS``
(parallel SITL runs, default 3) and ``SKYWEAVE_HARNESS_SPEEDUP`` (SITL
``--speedup``, default 4).
"""

from __future__ import annotations

import copy
import json
import math
import os
import select
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.core import replay
from skyweave2.drone.harness.__main__ import main as harness_main
from skyweave2.drone.harness.batch import (
    plan_runs,
    plan_seeds,
    run_plan,
    versions,
    write_aggregate,
)
from skyweave2.drone.harness.camera_sim import Pose, SyntheticCamera
from skyweave2.drone.harness.gate import RunEntry, aggregate
from skyweave2.drone.harness.scenarios import (
    TRIAL_TYPES,
    Script,
    SendCommand,
    SetBlackout,
    SetSwitch,
    SwitchPosition,
    build_world,
)
from skyweave2.drone.harness.seeds import (
    GATE_REPEATS,
    SCENARIOS,
    SeedSet,
    gate_seeds,
    probe_seeds,
)
from skyweave2.drone.harness.sitl_loop import (
    Home,
    LinkRelay,
    TruthBuffer,
    truth_from_sim_state,
)
from skyweave2.drone.mission import Mission, MissionConfig
from skyweave2.drone.packets import (
    AckResult,
    CommandName,
    CommandPacket,
    Event,
    MissionState,
    MissionStatePacket,
    PrimeParams,
    TrackPacket,
    TrackState,
    TrialType,
)
from skyweave2.drone.tracker import Tracker, TrackerConfig
from skyweave2.drone.types import LandedState, VehicleSnapshot

S = MissionState
JOBS = int(os.environ.get("SKYWEAVE_HARNESS_JOBS", "3"))
SPEEDUP = float(os.environ.get("SKYWEAVE_HARNESS_SPEEDUP", "4"))

# Contract §8 "Scorecard JSON per run": scenario, seed and seed set, backend and
# versions, law, the [S8] metrics, safety floors, final state, transition list,
# [R3] replay result, pass/fail per check.
DECLARED_FIELDS = (
    "scenario",
    "seed",
    "seed_set",
    "backend",
    "versions",
    "law",
    "metrics",
    "safety_floors",
    "final_state",
    "transitions",
    "replay",
    "checks",
    "passed",
)


# ---------------------------------------------------------------------------
# Slow tier: the gate, closed loop against SITL
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.sitl
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_s_gate_scenario_green_every_repeat(scenario: str, sitl_paths, tmp_path: Path) -> None:
    """[S0], [S7], [S8], [R3], contract §8 table: the scenario is green under every one
    of its gate seeds in each of the ``gate_repeats`` runs (and, for S2, the p95
    commit-plane miss over the 20 gate seeds is within ``miss_p95_max_m``); every run
    emits a scorecard with the declared fields; and every run's recording replays
    exactly through a fresh core."""
    seeds = {scenario: plan_seeds(scenario, SeedSet.GATE, None, 0)}
    run_versions = versions()
    plan = plan_runs(
        out=tmp_path,
        scenarios=[scenario],
        seeds=seeds,
        seed_set=SeedSet.GATE,
        laws=["pure_pursuit"],
        repeats=GATE_REPEATS,
        speedup=SPEEDUP,
        sitl_paths=sitl_paths,
        run_versions=run_versions,
    )
    entries = run_plan(plan, out=tmp_path, jobs=JOBS)
    agg = write_aggregate(
        entries,
        out=tmp_path,
        seed_set=SeedSet.GATE,
        scenarios=[scenario],
        laws=["pure_pursuit"],
        repeats=GATE_REPEATS,
        run_versions=run_versions,
    )
    on_disk = json.loads((tmp_path / "scorecard.json").read_text())
    assert on_disk == json.loads(json.dumps(agg))
    assert len(entries) == len(gate_seeds(scenario)) * GATE_REPEATS
    for e in entries:
        run_dir = tmp_path / e.path
        card = json.loads((run_dir / "scorecard.json").read_text())
        assert card == json.loads(json.dumps(e.card))
        assert all(f in card for f in DECLARED_FIELDS), sorted(card)
        assert card["seed_set"] == "gate" and card["seed"] in gate_seeds(scenario)
        result = replay(run_dir / "recording.jsonl")  # [R3], independent of the run's own check
        assert result.matches, (e.path, result.mismatch())
        assert card["replay"]["ok"] is True
    reds = {
        (r["repeat"], run["seed"]): (run["failed_checks"], run["end_reason"])
        for r in agg["results"]
        for run in r["scenarios"][scenario]["runs"]
        if not run["passed"]
    }
    assert not reds, reds
    for r in agg["results"]:
        cell = r["scenarios"][scenario]
        assert cell["complete"] and cell["passed"], (r["repeat"], cell.get("miss_p95"))


# ---------------------------------------------------------------------------
# Fast tier: worlds and the scripted human
# ---------------------------------------------------------------------------


def test_s7_worlds_follow_the_seed_and_the_scenario_table() -> None:
    """[S7], brief 3.9, contract §8 table and §9: a world is a function of (scenario, seed)
    alone; each scenario flies its table trial type with the §9 prime defaults; every gate
    world puts the target above the search altitude (above the drone's horizon) and inside
    the primed geofence. (A structural check of the declared gate worlds; nothing runs.)"""
    for scenario in SCENARIOS:
        for seed in gate_seeds(scenario):
            a, b = build_world(scenario, seed), build_world(scenario, seed)
            assert a.to_obj() == b.to_obj()
            assert a.prime == replace(PrimeParams(), trial_type=TRIAL_TYPES[scenario])
            target = next(o for o in a.scene if o.name == a.target)
            n, e, d = (float(x) for x in target.position(0))
            assert -d > a.prime.search_alt
            assert math.hypot(n, e) < a.prime.geofence_radius_m
        worlds = {json.dumps(build_world(scenario, s).to_obj()) for s in gate_seeds(scenario)}
        assert len(worlds) == len(gate_seeds(scenario))
    assert {s: t.value for s, t in TRIAL_TYPES.items()} == {
        "S1": "standoff",
        "S2": "touch",
        "S3": "standoff",
        "S4": "touch",
        "S5": "touch",
        "S6": "standoff",
    }


def _snap(t: int, mode: str, *, armed: bool, landed: LandedState, alt: float = 0.0):
    return VehicleSnapshot(
        t_ms=t,
        fc_link_up=True,
        mode=mode,
        armed=armed,
        landed_state=landed,
        rel_alt_m=alt,
        home_dist_m=0.0,
        battery_pct=90.0,
        attitude_age_ms=0,
        attitude_degraded=False,
        rc_seen=True,
    )


def _track(t_cap: int, track_id: int) -> TrackPacket:
    return TrackPacket(
        t_cap=t_cap,
        track_id=track_id,
        state=TrackState.CONFIRMED,
        u=960.0,
        v_px=600.0,
        du=0.0,
        dv=0.0,
        w=50.0,
        h=50.0,
        hits=3,
        misses=0,
        age_frames=3,
    )


def _execute(m: Mission, script: Script, t: int) -> list[Any]:
    """Run the script's due actions against the real mission, as the loop does."""
    done = []
    for action in script.pop_due(t):
        done.append(action)
        if isinstance(action, SendCommand):
            cmd = CommandPacket(
                cmd_id=action.cmd_id, token="t0k", command=action.command, params=action.params
            )
            ack = m.on_command(cmd, True, t)
            pkt = m.maybe_publish(t)
            script.on_output(t, [pkt] if pkt else [], [ack])
    return done


def test_s8_scripted_human_approves_after_the_settle_time() -> None:
    """[M8], contract §8 ("waiting at least approve_settle_ms after the candidate
    appears"), E1-D4: driven through the real Mission, the script primes when the
    vehicle is ready, moves the switch MANUAL -> GUIDED only after the prime is
    accepted, and schedules its approve no sooner than approve_settle_ms after the
    candidate packet; the mission accepts it (T07). Discrimination: the same approve
    1 ms before the settle time is rejected_state by the same mission."""
    cfg = MissionConfig()
    world = build_world("S1", probe_seeds("S1", 1)[0])
    script = Script(world, approve_settle_ms=cfg.approve_settle_ms, camera_latency_ms=28)
    m = Mission(cfg)
    assert script.initial_switch is SwitchPosition.MANUAL
    m.on_tick(_snap(0, "STABILIZE", armed=False, landed=LandedState.ON_GROUND), 0)
    script.on_ready(0)
    t_prime = script.next_time()
    assert t_prime is not None
    acts = _execute(m, script, t_prime)
    assert [type(a) for a in acts] == [SendCommand] and acts[0].command is CommandName.PRIME
    assert m.state is S.PRIMED
    t_switch = script.next_time()
    assert t_switch is not None and t_switch > t_prime
    switch = script.pop_due(t_switch)
    assert switch == [SetSwitch(t_ms=t_switch, position=SwitchPosition.GUIDED)]
    for t, mode, armed, landed, alt in (
        (t_prime + 50, "STABILIZE", False, LandedState.ON_GROUND, 0.0),  # T03 edge latch
        (t_switch + 50, "GUIDED", False, LandedState.ON_GROUND, 0.0),  # T03
        (t_switch + 100, "GUIDED", True, LandedState.IN_AIR, 10.0),  # T04
    ):
        m.on_tick(_snap(t, mode, armed=armed, landed=landed, alt=alt), t)
    assert m.state is S.SEARCH
    t_c = t_switch + 200
    m.on_track(_track(t_c - 28, 7), t_c)
    pkt = m.maybe_publish(t_c)
    assert pkt is not None and Event(t=t_c, name="candidate:7") in pkt.events
    script.on_output(t_c, [pkt], [])
    t_approve = script.next_time()
    assert t_approve is not None and t_approve >= t_c + cfg.approve_settle_ms
    early = copy.deepcopy(m)
    cmd = CommandPacket(cmd_id="early", token="t0k", command=CommandName.APPROVE_ENGAGE)
    ack = early.on_command(cmd, True, t_c + cfg.approve_settle_ms - 1)
    assert ack.result is AckResult.REJECTED_STATE and early.state is S.ACQUIRING
    assert script.pop_due(t_approve - 1) == []
    acts = _execute(m, script, t_approve)
    assert [a.command for a in acts] == [CommandName.APPROVE_ENGAGE]
    assert m.state is S.ENGAGED and m.engaged_track_id == 7


def test_s3_scripted_dropouts_straddle_the_coast_cap() -> None:
    """[S8] S3 row, [P2a], [K3]: the script's short dropout keeps the engaged track
    (it coasts, misses stay <= coast_cap, then hits again under the same id) and its
    long dropout kills it (the final packet carries misses = coast_cap + 1). Checked
    with the real SyntheticCamera and the real Tracker on the S3 gate world."""
    seed = probe_seeds("S3", 1)[0]
    world = build_world("S3", seed)
    script = Script(world, approve_settle_ms=1000, camera_latency_ms=28)
    t7 = 2_000
    trial = world.prime.echo()
    script.on_output(
        t7,
        [
            MissionStatePacket(
                t=t7,
                mission_state=S.ENGAGED,
                engaged_track_id=1,
                trial=trial,
                events=(Event(t=t7, name="transition:ACQUIRING->ENGAGED"),),
            )
        ],
        [],
    )
    windows = {w.kind: w for w in script.dropouts}
    assert set(windows) == {"short", "long"}
    target = world.scene[0]
    n, e, d = (float(x) for x in target.position(0))
    pose = Pose(pos_ned=(0.0, 0.0, d), yaw=math.atan2(e, n))  # level, facing the balloon
    cam = SyntheticCamera(
        replace(
            world.camera,
            dropout_windows_ms=tuple((w.start_ms, w.end_ms) for w in script.dropouts),
        ),
        seed=seed,
    )
    coast_cap = 20
    tracker = Tracker(TrackerConfig(id_base=0, coast_cap=coast_cap))
    by_id: dict[int, list[TrackPacket]] = {}
    k = 0
    while cam.capture_time(k) < windows["long"].end_ms + 500:
        for pkt in tracker.update(cam.capture(k, pose, world.scene).packet):
            by_id.setdefault(pkt.track_id, []).append(pkt)
        k += 1
    first_id = min(by_id)
    first = by_id[first_id]
    short = [p for p in first if windows["short"].start_ms <= p.t_cap < windows["long"].start_ms]
    assert max(p.misses for p in short) <= coast_cap
    assert any(p.state is TrackState.COASTING for p in short)
    assert short[-1].state is TrackState.CONFIRMED and short[-1].misses == 0
    assert first[-1].state is TrackState.COASTING and first[-1].misses == coast_cap + 1
    assert windows["long"].start_ms <= first[-1].t_cap < windows["long"].end_ms
    assert len(by_id) == 2  # after the long dropout the balloon is a new track


def test_s4_s5_faults_are_timed_from_the_commit() -> None:
    """Contract §8 S4 ("FC-link blackout at commit") and S5 ("abort during TOUCH"):
    the S4 script starts the blackout at the commit's own stamp and ends it after a
    blackout longer than fc_link_bound_ms (so T18 can fire); the S5 script sends abort
    after the commit, inside the [G5] fly phase; S1 injects nothing."""
    t = 50_000
    commit = MissionStatePacket(
        t=t,
        mission_state=S.TOUCH,
        engaged_track_id=3,
        trial=replace(PrimeParams(), trial_type=TrialType.TOUCH).echo(),
        events=(
            Event(t=t, name="commit:3:49972"),
            Event(t=t, name="transition:ENGAGED->TOUCH"),
        ),
    )
    for scenario in ("S1", "S4", "S5"):
        world = build_world(scenario, probe_seeds(scenario, 1)[0])
        script = Script(world, approve_settle_ms=1000, camera_latency_ms=28)
        script.on_output(t, [commit], [])
        due = script.pop_due(t + 10_000)
        if scenario == "S1":
            assert due == []
        elif scenario == "S4":
            on, off = due
            assert on == SetBlackout(t_ms=t, on=True)
            assert off.on is False and off.t_ms - t > 1000  # > fc_link_bound_ms (§9)
        else:
            (abort,) = due
            assert isinstance(abort, SendCommand) and abort.command is CommandName.ABORT
            assert t < abort.t_ms < t + 1000


# ---------------------------------------------------------------------------
# Fast tier: truth and the FC-link relay
# ---------------------------------------------------------------------------


def _sim_state(lat_e7: int, lon_e7: int, alt: float, yaw: float = 0.0) -> Any:
    msg = mavlink2.MAVLink_sim_state_message(
        1, 0, 0, 0, 0.1, -0.2, yaw, 0, 0, -9.8, 0, 0, 0, 1e9, -1e9, alt, 0, 0, 1.5, -0.5, 0.25
    )
    msg.lat_int, msg.lon_int = lat_e7, lon_e7
    return msg


def test_s0_truth_from_sim_state_uses_integer_position_and_home() -> None:
    """[S0], [C3]: truth comes from SIM_STATE's 1e-7 deg integers (the float lat/lon
    fields are garbage here and must not be read), relative to the SITL home, with
    ArduPilot's scaling. By hand for home 37.0, -122.0, 10 m: +100 (1e-7 deg) of
    latitude is 100 * 0.011131884502145034 = 1.1131884502 m north; +100 of longitude
    at a mid latitude of 37.000005 deg is 1.1131884502 * cos(37.000005 deg) =
    0.8890311 m east; 22.5 m AMSL is 12.5 m above home, D = -12.5."""
    home = Home.parse("37.0,-122.0,10.0,0")
    pose, vel = truth_from_sim_state(
        _sim_state(home.lat_e7 + 100, home.lon_e7 + 100, 22.5, yaw=3.0), home
    )
    n, e, d = pose.pos_ned
    assert n == pytest.approx(1.1131884502, abs=1e-9)
    assert e == pytest.approx(1.1131884502 * math.cos(math.radians(37.000005)), abs=1e-9)
    assert e == pytest.approx(0.8890311, abs=1e-6)
    assert d == pytest.approx(-12.5, abs=1e-6)
    assert (pose.roll, pose.pitch) == pytest.approx((0.1, -0.2))
    assert pose.yaw == pytest.approx(3.0, abs=1e-6)
    assert vel == pytest.approx((1.5, -0.5, 0.25))


def test_s0_truth_buffer_interpolates_and_never_extrapolates() -> None:
    """[S0]: the camera renders frame k from truth at its capture time: linear in
    position, shortest-way in yaw across +-pi, held (never extrapolated) past the ends."""
    from skyweave2.drone.harness.camera_sim import Pose as P

    buf = TruthBuffer()
    buf.add(1000, P(pos_ned=(0.0, 0.0, -10.0), yaw=math.pi - 0.1), (0.0, 0.0, 0.0))
    buf.add(1020, P(pos_ned=(2.0, -4.0, -12.0), yaw=-math.pi + 0.1), (0.0, 0.0, 0.0))
    mid = buf.pose_at(1010)
    assert mid.pos_ned == pytest.approx((1.0, -2.0, -11.0))
    assert abs(abs(mid.yaw) - math.pi) < 1e-9  # through pi, not through 0
    assert buf.pose_at(900).pos_ned == (0.0, 0.0, -10.0)
    assert buf.pose_at(5000).pos_ned == (2.0, -4.0, -12.0)
    with pytest.raises(ValueError):
        buf.add(999, P(pos_ned=(0.0, 0.0, 0.0)), (0.0, 0.0, 0.0))


def _frame(src: tuple[int, int], msg: Any) -> bytes:
    return bytes(msg.pack(mavlink2.MAVLink(None, srcSystem=src[0], srcComponent=src[1])))


def _read_exact(sock: socket.socket, n: int) -> bytes:
    sock.settimeout(2.0)
    data = b""
    while len(data) < n:
        data += sock.recv(n - len(data))
    return data


def _pump_until(relay: LinkRelay, cond, tries: int = 200) -> None:
    for _ in range(tries):
        select.select([relay.upstream_fileno()], [], [], 0.01)
        relay.pump()
        if cond():
            return
    raise AssertionError("relay condition never held")


def test_s4_relay_blackout_drops_both_ways_without_closing() -> None:
    """[S0] S4, [F1] (b), [R3]: the FC-link relay passes bytes both ways and reads SITL
    time only from the FC's own frames; a blackout drops every byte in both directions
    while the TCP connections stay open (so the SITL proof is not reset and fc_link's
    tx records still exist for replay); after the blackout bytes flow again."""
    sitl_side = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sitl_side.bind(("127.0.0.1", 0))
    sitl_side.listen(1)
    relay = LinkRelay(sitl_side.getsockname()[1], fc_ids=(1, 1), timeout_s=2.0)
    up, _ = sitl_side.accept()
    companion = socket.create_connection(("127.0.0.1", relay.port), timeout=2.0)
    relay.accept()
    try:
        att = mavlink2.MAVLink_attitude_message(12_345, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        foreign = mavlink2.MAVLink_attitude_message(99_999, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        fc_bytes, gcs_bytes = _frame((1, 1), att), _frame((255, 190), foreign)
        up.sendall(gcs_bytes + fc_bytes)
        _pump_until(relay, lambda: relay.newest_boot_ms is not None)
        assert relay.newest_boot_ms == 12_345  # the GCS frame's time is not SITL's FC time
        assert _read_exact(companion, len(gcs_bytes + fc_bytes)) == gcs_bytes + fc_bytes
        companion.sendall(b"setpoint")
        _pump_until(relay, lambda: True)
        assert _read_exact(up, 8) == b"setpoint"

        relay.blackout = True
        att2 = mavlink2.MAVLink_attitude_message(12_400, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        up.sendall(_frame((1, 1), att2))
        companion.sendall(b"lost-setpoint")
        _pump_until(
            relay,
            lambda: relay.dropped_bytes_to_fc == 13 and relay.dropped_bytes_to_companion > 0,
        )
        assert relay.newest_boot_ms == 12_400  # the clock keeps SITL time through a blackout
        companion.setblocking(False)
        with pytest.raises(BlockingIOError):
            companion.recv(1)
        companion.setblocking(True)

        relay.blackout = False
        companion.sendall(b"back")
        _pump_until(relay, lambda: True)
        assert _read_exact(up, 4) == b"back"
    finally:
        companion.close()
        up.close()
        relay.close()
        sitl_side.close()


# ---------------------------------------------------------------------------
# Fast tier: gate aggregation and the command line
# ---------------------------------------------------------------------------


def _card(scenario: str, seed: int, *, passed: bool = True, miss: float | None = 0.1) -> dict:
    return {
        "scenario": scenario,
        "seed": seed,
        "seed_set": "gate",
        "passed": passed,
        "final_state": "LAND",
        "checks": [{"name": "replay", "passed": passed, "value": None, "limit": None}],
        "metrics": {
            "hold_error": {"p95_m": 0.3} if scenario == "S1" else None,
            "lock_retention": {"fraction": 0.99 if scenario == "S6" else None},
            "commit_plane_miss": None if scenario != "S2" else {"miss_m": miss},
        },
    }


def _entries(law: str, repeat: int, cards: list[dict]) -> list[RunEntry]:
    return [
        RunEntry(
            law=law,
            repeat=repeat,
            scenario=c["scenario"],
            seed=c["seed"],
            card=c,
            end_reason="landed",
            path=f"{law}/rep{repeat}/{c['scenario']}/{c['seed']}",
        )
        for c in cards
    ]


def _agg(entries: list[RunEntry], laws: list[str]) -> dict:
    return aggregate(
        entries,
        seed_set=SeedSet.GATE,
        scenarios=["S1", "S2"],
        laws=laws,
        repeats=2,
        backend="test",
        versions={},
    )


def test_s7_s8_gate_green_needs_every_seed_every_repeat_and_the_s2_p95() -> None:
    """[S0] (gate_repeats, green every time), [S7] (the full gate seed set), [S8] (S2 p95
    commit-plane miss <= miss_p95_max_m, nearest rank: the 19th of 20): the aggregate is
    green only when every cell is. Discrimination on the load-bearing S2 limit (0.5 m):
    18 runs at 0.1 m, one without a crossing (infinite), and one at 0.49 m is green; the
    same with 0.51 m is red. A missing gate seed or one red run in repeat 2 turns the
    gate red, and the aggregate is byte-for-byte reproducible (no wall clock)."""
    s1 = [_card("S1", s) for s in gate_seeds("S1")]
    s2_seeds = gate_seeds("S2")

    def s2(last: float) -> list[dict]:
        cards = [_card("S2", s) for s in s2_seeds[:18]]
        return [*cards, _card("S2", s2_seeds[18], miss=None), _card("S2", s2_seeds[19], miss=last)]

    good = _entries("a", 1, s1 + s2(0.49)) + _entries("a", 2, s1 + s2(0.49))
    agg = _agg(good, ["a"])
    assert agg["passed"] and all(r["passed"] for r in agg["results"])
    assert agg["results"][0]["scenarios"]["S2"]["miss_p95"]["p95_m"] == 0.49
    assert json.dumps(_agg(good, ["a"]), sort_keys=True) == json.dumps(agg, sort_keys=True)

    over = _entries("a", 1, s1 + s2(0.51)) + _entries("a", 2, s1 + s2(0.49))
    agg = _agg(over, ["a"])
    assert not agg["passed"]
    assert [r["scenarios"]["S2"]["passed"] for r in agg["results"]] == [False, True]

    missing = _entries("a", 1, s1[:2] + s2(0.1)) + _entries("a", 2, s1 + s2(0.1))
    agg = _agg(missing, ["a"])
    assert agg["results"][0]["scenarios"]["S1"]["complete"] is False and not agg["passed"]

    red2 = _entries("a", 1, s1 + s2(0.1)) + _entries(
        "a", 2, [_card("S1", gate_seeds("S1")[0], passed=False), *s1[1:]] + s2(0.1)
    )
    agg = _agg(red2, ["a"])
    assert [r["passed"] for r in agg["results"]] == [True, False] and not agg["passed"]


def test_s_law_ranking_orders_by_green_cells_then_miss() -> None:
    """[G3] ("the harness ranks laws by scorecard"), brief 3.6: a law with more green
    (repeat, scenario) cells ranks first; at equal green cells the lower worst-case S2
    p95 miss ranks first."""
    s1 = [_card("S1", s) for s in gate_seeds("S1")]
    s2 = [_card("S2", s, miss=m) for s, m in zip(gate_seeds("S2"), [0.1] * 20, strict=True)]
    s2_worse = [_card("S2", s, miss=0.3) for s in gate_seeds("S2")]
    red = [_card("S1", gate_seeds("S1")[0], passed=False), *s1[1:]]
    entries = (
        _entries("b_worse_miss", 1, s1 + s2_worse)
        + _entries("b_worse_miss", 2, s1 + s2_worse)
        + _entries("c_red", 1, red + s2)
        + _entries("c_red", 2, s1 + s2)
        + _entries("a_best", 1, s1 + s2)
        + _entries("a_best", 2, s1 + s2)
    )
    ranking = _agg(entries, ["b_worse_miss", "c_red", "a_best"])["law_ranking"]
    assert [(r["rank"], r["law"]) for r in ranking] == [
        (1, "a_best"),
        (2, "b_worse_miss"),
        (3, "c_red"),
    ]


def test_s7_cli_requires_an_explicit_seed_choice(tmp_path: Path, capsys) -> None:
    """[S7]: the command line never defaults to a seed set; explicit seeds run under the
    probe label, so a gate seed passed without ``--seed-set gate`` is refused."""
    with pytest.raises(SystemExit) as exc:
        harness_main(["--out", str(tmp_path)])
    assert exc.value.code == 2
    gate_seed = gate_seeds("S1")[0]
    code = harness_main(["--seed", str(gate_seed), "--scenarios", "S1", "--out", str(tmp_path)])
    assert code == 2
    assert "gate seed" in capsys.readouterr().err
