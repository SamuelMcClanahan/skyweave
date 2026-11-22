"""S series, probe side: probe scenarios, wind, bad frames, airframe ([S9]-[S12]).

Contract: DRONE_CONTRACTS_D0.md §8.1 ([S9] probe scenarios, [S10] wind, [S11]
bad frames, [S12] airframe), implementing §11.1 V-F3, V-D15, V-D16, V-F2.

Fast tests drive the real camera, worlds, script, scorecard, closed loop and
SITL launch preparation, with no SITL process. Expected values come from the
contract, from fixtures recorded before this change (the E1 fingerprint),
from ``sha256sum`` in a shell (seed values), or from V-F6 (frame numbers).
The two slow tests fly the pinned SITL: the cone frame hovers near its hover
throttle and follows a velocity setpoint, and the wind pushes the vehicle.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.fc_link import FcLink
from skyweave2.drone.harness.batch import plan_seeds
from skyweave2.drone.harness.camera_sim import (
    PROVISIONAL_NOISE,
    Pose,
    SyntheticCamera,
)
from skyweave2.drone.harness.environment import CONE_X, PLUS, Wind, sitl_params
from skyweave2.drone.harness.probes import ScriptedThrow
from skyweave2.drone.harness.scenarios import Script, build_world
from skyweave2.drone.harness.scorecard import RunTrace, score_run
from skyweave2.drone.harness.seeds import (
    PROBE_SCENARIOS,
    SCENARIOS,
    SeedSet,
    check_seed,
    gate_seeds,
    probe_seeds,
    seed_for,
)
from skyweave2.drone.harness.sitl_loop import ClosedLoop, RunSpec
from skyweave2.drone.harness.targets import StaticBalloon
from skyweave2.drone.packets import (
    Event,
    MissionState,
    MissionStatePacket,
    TrialType,
    canonical_json,
    encode,
)
from skyweave2.drone.recording import Recorder, Stream, read_records
from skyweave2.drone.sitl import SitlInstance, SitlPaths, free_instance

S = MissionState
HOVER = Pose(pos_ned=(0.0, 0.0, -10.0))

E1_FINGERPRINT = "4adfda96f34dbac54a703e9a88a667d464c0c4e7eff2476858ef5fbc2d058fda"
"""sha256 over every gate world's run-log object and 60 rendered frames per gate
world (its own camera, and the E1 noise preset with a dropout window), computed
with the E1 code at d56445d before any [S9]-[S12] change (2026-11-20)."""

V_PROBE_SEEDS = {
    # printf 'V-probe:P1:0' | shasum -a 256 | cut -c1-8, and so on
    "P1": (0x6B0FF2F4, 0x3F8CA410, 0x9606FE70),
    "P2": (0x44D92EFD, 0x0F8D12C3, 0x73C58146),
    "P3": (0x3D04500A, 0x002C1F54, 0x5FDB921D),
    "P4": (0x37C327A7, 0xF2D2BC67, 0xFD81EBD0),
    "P5": (0xA72646AB, 0xD03322FA, 0x1782D9A5),
    "P6": (0xA4D605B7, 0x886F2487, 0xB4EAB667),
    "P7": (0x7F60680D, 0x276A078A, 0x97E8541F),
}

PROBE_TABLE = {  # contract §8.1 [S9] table: trial and "Scored as"
    "P1": (TrialType.TOUCH, ("S2",)),
    "P2": (TrialType.TOUCH, ("S2",)),
    "P3": (TrialType.STANDOFF, ("S6",)),
    "P4": (TrialType.TOUCH, ("S2", "S6")),
    "P5": (TrialType.STANDOFF, ("S1", "S6")),
    "P6": (TrialType.TOUCH, ("S2",)),
    "P7": (TrialType.TOUCH, ("S2",)),
}

SIM_FRAME_KEYS_4_7_0 = {
    # Frame::load_frame_params at tag Copter-4.7.0 (SIM_Frame.cpp), read 2026-11-20
    "mass", "diagonal_size", "refSpd", "refAngle", "refVoltage", "refCurrent", "refAlt",
    "refTempC", "maxVoltage", "battCapacityAh", "refBatRes", "propExpo", "refRotRate",
    "hoverThrOut", "pwmMin", "pwmMax", "spin_min", "spin_max", "slew_max", "disc_area",
    "mdrag_coef", "moment_inertia", "num_motors",
    *(f"motor{i}_{k}" for i in range(1, 33) for k in ("position", "vector", "yaw")),
}  # fmt: skip


def _world_target(world):
    return next(o for o in world.scene if o.name == world.target)


# ---------------------------------------------------------------------------
# [S11] the E1 camera and the gate worlds are unchanged
# ---------------------------------------------------------------------------


def test_s11_s12_gate_worlds_and_e1_camera_output_are_unchanged() -> None:
    """[S11] ("with all of them off, the camera's output is identical to E1's"), [S10]
    and [S12] ("gate worlds carry no wind", "gate scenes S1-S6 fly plus"): every gate
    world's run-log object and 60 frames rendered from it, with its own camera and with
    the E1 noise preset plus a dropout window, hash to the fingerprint recorded with the
    E1 code before this change. Each gate world flies PLUS with no wind and no extra
    defaults-file entries, and scores against its own row."""
    h = hashlib.sha256()
    for sc in SCENARIOS:
        for seed in gate_seeds(sc):
            w = build_world(sc, seed)
            assert (w.airframe, w.wind, w.checks_as) == (PLUS, None, ())
            assert sitl_params(w.airframe, w.wind) == {}
            h.update(canonical_json(w.to_obj()))
            n, e, _ = (float(x) for x in _world_target(w).position(0))
            pose = Pose(pos_ned=(0.0, 0.0, -10.0), yaw=math.atan2(e, n))
            for cfg in (w.camera, replace(PROVISIONAL_NOISE, dropout_windows_ms=((1100, 1300),))):
                assert not cfg.bad_frames
                cam = SyntheticCamera(cfg, seed=seed, t0_ms=1000)
                for k in range(60):
                    fr = cam.capture(k, pose, w.scene)
                    assert not fr.dropped
                    h.update(encode(fr.packet))
                    h.update(str(fr.t_deliver_ms).encode())
                    h.update(repr(fr.truth).encode())
    assert h.hexdigest() == E1_FINGERPRINT


# ---------------------------------------------------------------------------
# [S11] bad frames
# ---------------------------------------------------------------------------


def _ahead(scene_dist: float = 20.0):
    return [StaticBalloon(anchor_ned=(scene_dist, 0.0, -10.0), name="target")]


def test_s11_clutter_adds_false_boxes_and_leaves_true_boxes_alone() -> None:
    """[S11] clutter: with ``clutter_per_frame`` 2, over 400 frames the true box is
    unchanged frame by frame (it comes first, same numbers as without clutter), the
    false boxes average about 2 per frame (Poisson mean; 400 frames give a standard
    error of 0.07, the bounds are 5 of them), every false box lies on the grid with a
    width no larger than the clutter maximum and a confidence in [0.3, 0.9], and the
    same seed gives the same boxes. Discrimination: clutter 0 adds nothing."""
    base = replace(PROVISIONAL_NOISE, dropout_p=0.0)
    clut = replace(base, clutter_per_frame=2.0, clutter_w_px=(8.0, 80.0))
    scene = _ahead()
    a = SyntheticCamera(base, seed=11, t0_ms=0)
    b = SyntheticCamera(clut, seed=11, t0_ms=0)
    b2 = SyntheticCamera(clut, seed=11, t0_ms=0)
    extra = []
    for k in range(400):
        fa, fb = a.capture(k, HOVER, scene), b.capture(k, HOVER, scene)
        assert len(fa.packet.boxes) == 1 and fb.packet.boxes[0] == fa.packet.boxes[0]
        assert fb.packet == b2.capture(k, HOVER, scene).packet
        false = fb.packet.boxes[1:]
        extra.append(len(false))
        for box in false:
            assert -0.5 <= box.x and box.x + box.w <= 1919.5 + 1e-9
            assert -0.5 <= box.y and box.y + box.h <= 1199.5 + 1e-9
            assert 0.0 < box.w <= 80.0 + 1e-9 and 0.3 <= box.conf <= 0.9
    assert 1.65 <= sum(extra) / len(extra) <= 2.35
    assert max(extra) > min(extra)  # a count per frame, not a constant


def test_s11_frame_drops_and_bursts_drop_whole_frames() -> None:
    """[S11] frame drops: a dropped frame is flagged and has no boxes. Random drops at
    ``frame_drop_p`` 0.1 hit 7-13 % of 1000 frames (binomial standard error 0.95 %) and
    are spread out. Bursts at 0.5 per second of 100-200 ms: every frame captured inside a
    burst window is dropped and no other frame is; each window lasts 100-200 ms and
    starts after the previous one ends. The schedule depends only on the seed and t0:
    a camera rebuilt with new dropout windows (as the loop does for S3) drops the same
    frames. Discrimination: with both off nothing is dropped."""
    scene = _ahead()
    base = replace(PROVISIONAL_NOISE, dropout_p=0.0)
    rnd = SyntheticCamera(replace(base, frame_drop_p=0.1), seed=5, t0_ms=0)
    drops = [rnd.capture(k, HOVER, scene) for k in range(1000)]
    flagged = [f for f in drops if f.dropped]
    assert 70 <= len(flagged) <= 130 and all(f.packet.boxes == () for f in flagged)
    burst_cfg = replace(base, burst_per_s=0.5, burst_ms=(100, 200))
    cam = SyntheticCamera(burst_cfg, seed=5, t0_ms=1000)
    frames = [cam.capture(k, HOVER, scene) for k in range(1200)]  # 20 s at 60 fps
    windows = cam.bursts_until(frames[-1].packet.t_cap)
    assert len(windows) >= 4
    for (a0, a1), (b0, _) in zip(windows, windows[1:], strict=False):
        assert 100 <= a1 - a0 <= 200 and b0 > a1
    for f in frames:
        inside = any(a <= f.packet.t_cap < b for a, b in windows)
        assert f.dropped is inside
    rebuilt = SyntheticCamera(replace(burst_cfg, dropout_windows_ms=((5000, 6000),)), seed=5,
                              t0_ms=1000)  # fmt: skip
    assert [rebuilt.frame_dropped(k) for k in range(1200)] == [f.dropped for f in frames]
    clean = SyntheticCamera(base, seed=5, t0_ms=1000)
    assert not any(clean.capture(k, HOVER, scene).dropped for k in range(200))


def test_s11_latency_jitter_keeps_frame_order() -> None:
    """[S11] jitter: frame k is delivered no sooner than capture plus the base latency
    (rounded up) and no later than that plus the cap, never before the frame ahead of
    it, and jitter varies frame to frame (mean 10 ms, cap 40 ms). Discrimination: with
    jitter off, every delivery equals the E1 ``deliver_time`` of its capture time."""
    cfg = replace(PROVISIONAL_NOISE, latency_jitter_ms=10.0, latency_jitter_max_ms=40.0)
    cam = SyntheticCamera(cfg, seed=3, t0_ms=500)
    last = -1
    extra = []
    for k in range(600):
        t_cap = cam.capture_time(k)
        d = cam.deliver_time_of(k)
        base = cam.deliver_time(t_cap)
        assert base <= d <= t_cap + math.ceil(cfg.latency_ms + 40.0)
        assert d >= last
        last = d
        extra.append(cam.jitter_ms(k))
        assert cam.capture(k, HOVER, _ahead()).t_deliver_ms == d
    assert 0.0 <= min(extra) and max(extra) <= 40.0 and 5.0 < sum(extra) / len(extra) < 15.0
    off = SyntheticCamera(PROVISIONAL_NOISE, seed=3, t0_ms=500)
    assert all(off.deliver_time_of(k) == off.deliver_time(off.capture_time(k)) for k in range(600))


def _offline_loop(tmp_path: Path, scenario: str) -> ClosedLoop:
    spec = RunSpec(
        scenario=scenario,
        seed=probe_seeds(scenario, 1)[0],
        seed_set=SeedSet.PROBE,
        out_dir=tmp_path,
        sitl_paths=SitlPaths(binary=tmp_path / "arducopter", parm=tmp_path / "copter.parm"),
    )
    return ClosedLoop(spec)


@pytest.mark.parametrize("drop_p", [1.0, 0.0])
def test_s11_dropped_frames_never_reach_the_tracker_or_the_recording(
    tmp_path: Path, drop_p: float
) -> None:
    """[S11] ("a dropped frame produces no detection packet"): driven through the real
    ClosedLoop delivery path on a P6 world, 30 dropped frames leave no detection record,
    no frame truth, and nothing in the tracker, and are counted. Discrimination: with
    drops off, the same 30 frames are recorded and tracked."""
    loop = _offline_loop(tmp_path, "P6")
    buf = io.StringIO()
    loop.recorder = Recorder(buf)
    loop.fc = FcLink("tcp:127.0.0.1:9", loop.clock, loop.config.link, loop.recorder)
    loop._start_core()  # fc_link is never connected: nothing is written anywhere
    loop._start_camera(1000)
    cfg = replace(loop.world.camera, frame_drop_p=drop_p, burst_per_s=0.0, dropout_p=0.0)
    loop.camera = SyntheticCamera(cfg, seed=loop.spec.seed, t0_ms=1000)
    loop.truth.add(0, Pose(pos_ned=(0.0, 0.0, -10.0)), (0.0, 0.0, 0.0))
    loop.truth.add(10_000, Pose(pos_ned=(0.0, 0.0, -10.0)), (0.0, 0.0, 0.0))
    for _ in range(30):
        t_deliver, _t_cap = loop._next_frame()
        loop._deliver_frame(t_deliver)
    dets = [r for r in read_records(buf.getvalue().splitlines()) if r.stream is Stream.DETECTION]
    if drop_p:
        assert dets == [] and loop.frame_truths == [] and loop.core.tracker.last_t_cap is None
        assert loop.counters["frames_dropped"] == 30
    else:
        assert len(dets) == 30 and len(loop.frame_truths) == 30
        assert "frames_dropped" not in loop.counters


# ---------------------------------------------------------------------------
# [S9] probe seeds and worlds
# ---------------------------------------------------------------------------


def test_s9_probe_seeds_use_their_own_namespace_and_have_no_gate_set() -> None:
    """[S9]: a probe scenario's seeds are sha256("V-probe:<scenario>:<i>") (values from
    shasum in a shell); it has no gate seeds, a gate seed request is refused at every
    entry point, and an S1-S6 gate seed is refused under the probe label."""
    for sc in PROBE_SCENARIOS:
        assert probe_seeds(sc, 3) == V_PROBE_SEEDS[sc]
        assert gate_seeds(sc) == ()
        with pytest.raises(ValueError):
            seed_for(SeedSet.GATE, sc, 0)
        with pytest.raises(ValueError):
            plan_seeds(sc, SeedSet.GATE, None, 3)
        with pytest.raises(ValueError):
            check_seed(SeedSet.GATE, sc, V_PROBE_SEEDS[sc][0])
        with pytest.raises(ValueError):
            check_seed(SeedSet.PROBE, sc, gate_seeds("S2")[0])
        assert plan_seeds(sc, SeedSet.PROBE, None, 2) == list(V_PROBE_SEEDS[sc][:2])


def test_s9_probe_worlds_follow_the_table() -> None:
    """[S9] table, [S10], [S12], brief 3.9: each probe world is a function of (scenario,
    seed) alone, flies its table trial and is scored as its table rows, and keeps its
    target inside the primed geofence for the first 60 s. Per row: P1's target is 20-60
    deg above the search point; P2's balloon hangs downwind of its ground anchor on a
    string of fixed length, with the wind from within 30 deg of the balloon's bearing
    (string off the approach side, V-D14); P3's kite flies on a fixed line, above the
    search altitude; P5 has a bird and clutter; P6 has bad frames; P7 flies the cone
    frame; only P2 and P3 carry wind. Each world's run-log object names its airframe,
    wind and rows."""
    for sc in PROBE_SCENARIOS:
        trial, rows = PROBE_TABLE[sc]
        for seed in V_PROBE_SEEDS[sc]:
            w = build_world(sc, seed)
            assert w.to_obj() == build_world(sc, seed).to_obj()
            assert w.prime.trial_type is trial and w.checks_as == rows
            obj = json.loads(canonical_json(w.to_obj()))
            assert obj["checks_as"] == list(rows)
            assert (w.airframe is CONE_X) is (sc == "P7") and ("airframe" in obj) is (sc == "P7")
            assert (w.wind is not None) is (sc in ("P2", "P3")) and ("wind" in obj) is (
                sc in ("P2", "P3")
            )
            assert w.camera.bad_frames is (sc in ("P5", "P6"))
            tgt = _world_target(w)
            if sc == "P4":
                continue  # held by the thrower until the script throws (next test)
            for t in range(0, 60_001, 500):
                p = tgt.position(t)
                assert math.hypot(p[0], p[1]) < w.prime.geofence_radius_m
                assert -p[2] > w.prime.search_alt
            p0 = tgt.position(0)
            if sc == "P1":
                elev = math.degrees(
                    math.atan2(-p0[2] - w.prime.search_alt, math.hypot(p0[0], p0[1]))
                )
                assert 20.0 <= elev <= 60.0
            if sc in ("P2", "P3"):
                anchor = np.asarray(tgt.anchor_ned)
                down = np.asarray(w.wind.downwind_ned())
                length = tgt.tether_m if sc == "P2" else tgt.line_m
                for t in range(0, 60_001, 500):
                    rel = tgt.position(t) - anchor
                    assert anchor[2] == 0.0 and float(rel @ down) > 0.0
                    assert abs(float(np.linalg.norm(rel)) - length) < 1e-9
                brg = math.degrees(w.geometry["bearing_rad"])  # the target's mean bearing
                assert abs(math.remainder(w.wind.from_deg - brg, 360.0)) <= 30.0
            if sc == "P5":
                assert [o.name for o in w.scene] == ["target", "bird"]
                assert w.camera.clutter_per_frame > 0.0


def test_s9_p4_plane_is_thrown_after_search_starts_and_crosses_the_scan() -> None:
    """[S9] P4: engage is preauthorized and the search altitude is low (3 m). The plane
    is not in the world until the script sees SEARCH start; it is thrown
    ``throw_delay_ms`` later (future times only) and crosses the line of sight at the
    drawn range and bearing 0.8 s after the throw, above the ground."""
    for seed in V_PROBE_SEEDS["P4"]:
        w = build_world("P4", seed)
        assert w.prime.engage_preauthorized and w.prime.search_alt == 3.0
        plane = _world_target(w)
        assert isinstance(plane, ScriptedThrow)
        script = Script(w, approve_settle_ms=1000, camera_latency_ms=28)
        t_s = 50_000
        assert not plane.present(t_s)
        pkt = MissionStatePacket(
            t=t_s,
            mission_state=S.SEARCH,
            engaged_track_id=None,
            trial=w.prime.echo(),
            events=(Event(t=t_s, name="transition:LAUNCH->SEARCH"),),
        )
        script.on_output(t_s, [pkt], [])
        t_throw = t_s + round(w.geometry["throw_delay_ms"])
        assert not plane.present(t_throw - 1) and plane.present(t_throw)
        p = plane.position(t_throw + 800)
        geo = w.geometry
        assert math.isclose(math.hypot(p[0], p[1]), geo["cross_range_m"], abs_tol=1e-9)
        brg = math.atan2(p[1], p[0])
        assert abs(math.remainder(brg - geo["cross_bearing_rad"], 2 * math.pi)) < 1e-9
        assert plane.present(t_throw + 800) and -p[2] == pytest.approx(geo["cross_alt_m"])


# ---------------------------------------------------------------------------
# [S9] scoring
# ---------------------------------------------------------------------------


def _empty_trace() -> RunTrace:
    buf = io.StringIO()
    Recorder(buf).meta(0, {"fixture": "test_s_probes"})
    records = tuple(read_records(buf.getvalue().splitlines()))
    return RunTrace(records=records, frames=(), truth=(), target="target")


def _card(scenario: str, **kw):
    return score_run(
        _empty_trace(),
        scenario=scenario,
        seed=probe_seeds(scenario, 1)[0],
        seed_set=SeedSet.PROBE,
        backend={"name": "fixture"},
        versions={},
        law="pure_pursuit",
        replay_ok=True,
        end_reason="landed",
        **kw,
    )


def test_s9_probe_run_is_scored_as_the_union_of_its_rows() -> None:
    """[S9] ("the union of those rows' checks, plus the safety floors, replay, and run
    completion"; "its scorecard names those rows and carries its world"): a P5 card
    scored as S1 and S6 carries S1's checks, then S6's checks that S1 lacks, then the
    common tail, in that order (the expected names come from real S1 and S6 cards); it
    names its rows and world. A gate card carries neither key. A probe scenario without
    rows, rows on a gate scene, S3 as a row, and a repeated row are refused."""
    s1 = [c["name"] for c in _card("S1")["checks"]]
    s6 = [c["name"] for c in _card("S6")["checks"]]
    world = build_world("P5", probe_seeds("P5", 1)[0]).to_obj()
    p5 = _card("P5", checks_as=("S1", "S6"), world=world)
    tail = ["floor_geofence", "floor_alt", "floor_setpoint_speed", "replay", "run_completed"]
    head1 = [n for n in s1 if n not in tail]
    head6 = [n for n in s6 if n not in tail and n not in head1]
    assert [c["name"] for c in p5["checks"]] == head1 + head6 + tail
    assert p5["checks_as"] == ["S1", "S6"] and p5["world"] == world and p5["passed"] is False
    gate_card = _card("S1")
    assert "checks_as" not in gate_card and "world" not in gate_card
    for bad in (
        dict(scenario="P5"),
        dict(scenario="S1", checks_as=("S1",)),
        dict(scenario="P5", checks_as=("S3",)),
        dict(scenario="P5", checks_as=("S1", "S1")),
    ):
        sc = bad.pop("scenario")
        with pytest.raises(ValueError):
            _card(sc, **bad)


# ---------------------------------------------------------------------------
# [S10], [S12] launch: defaults file and frame file
# ---------------------------------------------------------------------------


def _launch(loop: ClosedLoop) -> tuple[list[str], list[str], dict[str, bytes]]:
    """SITL's argv, the extra defaults-file lines, and the other files in its workdir."""
    inst = loop.sitl_instance()
    try:
        argv = inst.prepare()
        assert inst.workdir is not None
        defaults = Path(argv[argv.index("--defaults") + 1].split(",")[1])
        lines = defaults.read_text().splitlines()
        others = {f.name: f.read_bytes() for f in inst.workdir.iterdir() if f != defaults}
        return argv, lines, others
    finally:
        inst.stop()


def test_s10_s12_wind_and_airframe_reach_sitl_only_through_its_launch(tmp_path: Path) -> None:
    """[S10] (wind only through the launch defaults file, names checked on 4.7.0),
    [S12] (cone_x from its JSON frame file, FRAME_TYPE 1), [F1]: the SITL launch the
    real ClosedLoop prepares. A gate world (S1) runs ``--model +`` with exactly the E1
    defaults-file lines (SERIAL1 stream rate, then the mode switch). P2 adds its wind as
    defaults-file lines. P7 runs ``--model quad:cone_x.json`` with the frame file copied
    byte for byte beside SITL (a bare name: this build panics on an absolute path) and
    adds the airframe's lines."""
    argv, lines, others = _launch(_offline_loop(tmp_path / "s1", "S1"))
    assert argv[argv.index("--model") + 1] == "+" and others == {}
    assert lines == ["MAV2_EXTRA1 10"] + [
        f"FLTMODE{i} {v}" for i, v in zip(range(1, 7), (0, 0, 0, 4, 4, 6), strict=True)
    ]
    p2 = _offline_loop(tmp_path / "p2", "P2")
    argv, lines, _ = _launch(p2)
    w = p2.world.wind
    assert argv[argv.index("--model") + 1] == "+"
    assert lines[7:] == [
        f"SIM_WIND_SPD {w.speed_mps:g}",
        f"SIM_WIND_DIR {w.from_deg:g}",
        f"SIM_WIND_TURB {w.turbulence_mps:g}",
        "SIM_WIND_TC 5",
        "SIM_WIND_T 1",
    ]
    argv, lines, others = _launch(_offline_loop(tmp_path / "p7", "P7"))
    assert argv[argv.index("--model") + 1] == "quad:cone_x.json"
    assert others == {"cone_x.json": CONE_X.frame_file.read_bytes()}
    assert lines[7:] == [
        "FRAME_TYPE 1",
        "MOT_THST_HOVER 0.21",
        "MOT_BAT_VOLT_MAX 25.2",
        "MOT_BAT_VOLT_MIN 19.8",
    ]
    with pytest.raises(ValueError):
        Wind(speed_mps=3.0, from_deg=360.0)


def test_s12_cone_frame_file_carries_the_v_f6_numbers_as_an_x() -> None:
    """[S12], V-F6, V-F1: the cone frame file is strict JSON whose keys SITL 4.7.0 reads
    (Frame::load_frame_params); mass 0.64 kg; inertia (2.71, 2.69, 2.41)e-3 kg m^2;
    ``diagonal_size`` is the arm, half the 210 mm diagonal; motors 1-4 sit on that arm at
    45, -135, -45 and 135 deg (ArduCopter's quad X order, which keeps the quad
    template's spin directions: 1 and 2 CCW, 3 and 4 CW), 19 mm below the CoM; hover
    thrust fraction 0.2; a 6S 1300 mAh pack. Its README beside it labels it Provisional."""
    frame = json.loads(CONE_X.frame_file.read_text())
    assert set(frame) <= SIM_FRAME_KEYS_4_7_0
    assert frame["mass"] == 0.64 and frame["num_motors"] == 4
    assert frame["moment_inertia"] == [0.00271, 0.00269, 0.00241]
    assert frame["diagonal_size"] == pytest.approx(0.210 / 2)
    for i, angle in enumerate((45.0, -135.0, -45.0, 135.0), start=1):
        x, y, z = frame[f"motor{i}_position"]
        assert math.hypot(x, y) == pytest.approx(frame["diagonal_size"], abs=1e-4)
        assert math.degrees(math.atan2(y, x)) == pytest.approx(angle, abs=0.01)
        assert z == 0.019
    assert frame["hoverThrOut"] == 0.2
    assert (frame["maxVoltage"], frame["battCapacityAh"]) == (25.2, 1.3)
    assert CONE_X.params["FRAME_TYPE"] == 1.0
    readme = (CONE_X.frame_file.parent / "README.md").read_text()
    assert "Provisional" in readme and "cone_x.json" in readme


# ---------------------------------------------------------------------------
# Slow tier: the cone frame and the wind on the pinned SITL
# ---------------------------------------------------------------------------

SITL_SPEEDUP = 2.0


class _Flight:
    """A GCS-only flight on SERIAL0 (test infrastructure; no companion, no fc_link).
    Durations are in SITL time from ``time_boot_ms``; waits are bounded wall time."""

    def __init__(self, inst: SitlInstance) -> None:
        assert inst.pilot is not None
        self.inst = inst
        self.pilot = inst.pilot
        self.mav = mavlink2.MAVLink(None, srcSystem=255, srcComponent=190)
        self.boot_ms = 0
        self.samples: list = []

    def pump(self, sim_s: float, every=None, until=None) -> list:
        start = None
        out = []
        deadline = time.monotonic() + 120.0 + 3.0 * sim_s / SITL_SPEEDUP
        while time.monotonic() < deadline:
            for msg in self.pilot.poll():
                if (msg.get_srcSystem(), msg.get_srcComponent()) != (1, 1):
                    continue
                boot = getattr(msg, "time_boot_ms", None)
                if boot is not None:
                    self.boot_ms = max(self.boot_ms, int(boot))
                out.append(msg)
            if every is not None:
                every()
            if until is not None and until():
                return out
            if self.boot_ms:
                start = self.boot_ms if start is None else start
                if self.boot_ms - start >= sim_s * 1000.0 and until is None:
                    return out
            time.sleep(0.005)
        raise AssertionError("SITL time did not advance")

    def take_off(self, alt_m: float = 10.0) -> None:
        for _ in range(3):
            for mid in (
                mavlink2.MAVLINK_MSG_ID_VFR_HUD,
                mavlink2.MAVLINK_MSG_ID_LOCAL_POSITION_NED,
                mavlink2.MAVLINK_MSG_ID_ATTITUDE,
                mavlink2.MAVLINK_MSG_ID_HEARTBEAT,
            ):
                self.pilot.request_interval(mid, 10.0)
            self.pump(1.0)
        self.pump(120.0, until=lambda: any("is using GPS" in s for s in self.pilot.statustext))
        self.pump(5.0)
        self.pilot.set_mode("GUIDED")
        self.pump(3.0)
        for _ in range(10):
            self.pilot.command(mavlink2.MAV_CMD_COMPONENT_ARM_DISARM, 1.0)
            self.pump(2.0)
            if self.pilot.armed:
                break
        assert self.pilot.armed, self.pilot.statustext[-10:]
        self.pilot.command(mavlink2.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, alt_m)
        lp = lambda: self.pilot.latest.get("LOCAL_POSITION_NED")  # noqa: E731
        self.pump(60.0, until=lambda: lp() is not None and -lp().z >= alt_m - 0.3)
        self.pump(10.0)  # settle

    def hover(self, sim_s: float) -> dict[str, float]:
        msgs = self.pump(sim_s)
        thr = [m.throttle for m in msgs if m.get_type() == "VFR_HUD"]
        roll = [math.degrees(m.roll) for m in msgs if m.get_type() == "ATTITUDE"]
        ve = [m.vy for m in msgs if m.get_type() == "LOCAL_POSITION_NED"]
        return {
            "throttle_pct": float(np.mean(thr)),
            "roll_deg": float(np.mean(roll)),
            "ve_mps": float(np.mean(ve)),
        }

    def reach_north(self, vn: float, sim_s: float) -> float:
        last = [0.0]

        def send() -> None:
            now = time.monotonic()
            if now - last[0] >= 0.1 / SITL_SPEEDUP:
                self.pilot.send(
                    self.mav.set_position_target_local_ned_encode(
                        0, 1, 1, mavlink2.MAV_FRAME_LOCAL_NED, 1479,
                        0, 0, 0, vn, 0, 0, 0, 0, 0, 0, 0,
                    )
                )  # fmt: skip
                last[0] = now

        msgs = self.pump(sim_s, every=send)
        return max(m.vx for m in msgs if m.get_type() == "LOCAL_POSITION_NED")


def _fly(sitl_paths: SitlPaths, airframe, wind) -> tuple[_Flight, SitlInstance]:
    inst = SitlInstance(
        sitl_paths,
        instance=free_instance(),
        speedup=SITL_SPEEDUP,
        model=airframe.model,
        frame_file=airframe.frame_file,
        extra_params=sitl_params(airframe, wind),
    )
    inst.start(timeout_s=60.0)
    return _Flight(inst), inst


@pytest.mark.slow
@pytest.mark.sitl
def test_s12_sitl_cone_frame_hovers_near_its_hover_throttle_and_follows_velocity(
    sitl_paths,
) -> None:
    """[S12], V-F6 ("hover throttle about 0.2"): SITL loads the cone frame file, the X
    quad takes off to 10 m in GUIDED, hovers in calm air at 15-30 % throttle (measured
    2026-11-20: 20 %; the plus frame hovers at 36 %, so the band tells the two
    apart), and reaches 1.9 m/s of a 2.0 m/s north setpoint within 10 s."""
    flight, inst = _fly(sitl_paths, CONE_X, None)
    try:
        assert "Loaded model params from cone_x.json" in inst.log_text()
        flight.take_off()
        h = flight.hover(10.0)
        assert 15.0 <= h["throttle_pct"] <= 30.0, h
        assert flight.reach_north(2.0, 10.0) >= 1.9
    finally:
        inst.stop()


@pytest.mark.slow
@pytest.mark.sitl
def test_s10_sitl_wind_from_the_defaults_file_pushes_the_vehicle(sitl_paths) -> None:
    """[S10]: a 6 m/s wind from the west, set only in the launch defaults file, makes
    the hovering plus frame lean west into it (mean roll below -3 deg; in calm air the
    roll is about 0) while it holds position (mean east speed under 0.3 m/s)."""
    flight, inst = _fly(sitl_paths, PLUS, Wind(speed_mps=6.0, from_deg=270.0))
    try:
        flight.take_off()
        h = flight.hover(10.0)
        assert h["roll_deg"] < -3.0 and abs(h["ve_mps"]) < 0.3, h
    finally:
        inst.stop()
