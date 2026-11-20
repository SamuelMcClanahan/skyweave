"""S series, pure parts: synthetic world, [S7] seeds, and the [S8] scorecard.

Contract: DRONE_CONTRACTS_D0.md §8 ([S0], [S7], [S8], the scenario table) and
[C2]/[C3] for the projection. Each expected value is derived by hand in the
test's docstring from the contract formulas and the Provisional §9 camera
(f = 1000 px, cx = 959.5, cy = 599.5, upright [C3] mount). Seed values come
from ``sha256sum`` in a shell, not from the module under test. Traces are
built from real packets, written and read back through the real
``Recorder``/``read_records``, with real MAVLink2 frames from pymavlink.
Nothing we own is mocked.
"""

from __future__ import annotations

import io
import json
import math
import time
from dataclasses import replace

import numpy as np
import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.harness.camera_sim import (
    CameraSimConfig,
    FrameTruth,
    ObjectTruth,
    Pose,
    SyntheticCamera,
)
from skyweave2.drone.harness.scorecard import (
    RunTrace,
    Thresholds,
    TruthSample,
    abort_latency_ms,
    attribute,
    check_lock_retained,
    check_lock_window_covers,
    check_reacquired,
    commit_plane_miss,
    hold_error,
    lock_retention,
    miss_p95,
    p95,
    retargets,
    safety_floors,
    score_run,
    scorecard_json,
)
from skyweave2.drone.harness.seeds import (
    SCENARIOS,
    SeedSet,
    check_seed,
    gate_seed_count,
    gate_seeds,
    probe_seeds,
    seed_for,
)
from skyweave2.drone.harness.targets import (
    CrossingBird,
    SlowKite,
    StaticBalloon,
    ThrownPlane,
    require_above_horizon,
)
from skyweave2.drone.packets import (
    AckPacket,
    AckResult,
    CommandName,
    CommandPacket,
    DetectionPacket,
    Event,
    MissionState,
    MissionStatePacket,
    PrimeParams,
    TrackPacket,
    TrackState,
    TrialType,
    canonical_json,
    encode,
)
from skyweave2.drone.recording import Recorder, read_records

S = MissionState
DEG = math.pi / 180.0
TAN1 = 0.017455064928217585  # tan(1 deg)
TOL = 1e-9
HOVER = Pose(pos_ned=(0.0, 0.0, -10.0))  # level, nose north, 10 m above home
UI_TOKEN = "fixture-ui-token"


def _clean(**kw) -> CameraSimConfig:
    return CameraSimConfig(latency_ms=0.0, **kw)


def _balloon_at(rel_ned, *, w: float = 1.0, h: float | None = None, name: str = "balloon"):
    """A static object at ``rel_ned`` from the HOVER pose."""
    p = (rel_ned[0], rel_ned[1], rel_ned[2] - 10.0)
    return StaticBalloon(anchor_ned=p, name=name, width_m=w, height_m=w if h is None else h)


# ---------------------------------------------------------------------------
# Trajectories
# ---------------------------------------------------------------------------


def test_s_balloon_sway_at_known_times() -> None:
    """[S0] truth trajectories: anchor (25, 0, -15), sway 0.5 m, period 8 s, heading 90 deg
    (east), phase 0. t = 2 s is a quarter period, so E = +0.5. t = 6 s gives E = -0.5.
    t = 4 s gives E = 0. N and D stay at the anchor. Without sway the balloon never moves."""
    b = StaticBalloon(anchor_ned=(25.0, 0.0, -15.0), sway_amp_m=0.5, sway_heading_rad=90 * DEG)
    assert np.allclose(b.position(2000), (25.0, 0.5, -15.0), atol=TOL)
    assert np.allclose(b.position(4000), (25.0, 0.0, -15.0), atol=TOL)
    assert np.allclose(b.position(6000), (25.0, -0.5, -15.0), atol=TOL)
    still = StaticBalloon(anchor_ned=(25.0, 0.0, -15.0))
    assert np.array_equal(still.position(123_456), (25.0, 0.0, -15.0))
    assert b.present(0) and (b.width_m, b.height_m) == (1.0, 1.0)


def test_s_kite_reversal_at_known_times() -> None:
    """[S0] S6 kite: start (30, -5, -15), v = (0, 1, 0) m/s, reversal at 10 s, 2 s turn.
    E(5 s) = 0. E(10 s) = 5. Mid-turn (sigma = 1 s): 5 + (1 - 1/2) = 5.5. At the end of
    the turn it is back at 5. Then E(15 s) = 5 - 3 = 2. An instant reversal gives
    E(12 s) = 5 - 2 = 3."""
    k = SlowKite(
        start_ned=(30.0, -5.0, -15.0), velocity_ned=(0.0, 1.0, 0.0), t_reverse_ms=10_000, turn_s=2.0
    )
    for t, e in ((5000, 0.0), (10_000, 5.0), (11_000, 5.5), (12_000, 5.0), (15_000, 2.0)):
        assert np.allclose(k.position(t), (30.0, e, -15.0), atol=TOL), t
    instant = SlowKite(
        start_ned=(30.0, -5.0, -15.0), velocity_ned=(0.0, 1.0, 0.0), t_reverse_ms=10_000
    )
    assert np.allclose(instant.position(12_000), (30.0, 3.0, -15.0), atol=TOL)


def test_s_thrown_plane_ballistic_and_drag() -> None:
    """[S0] thrown plane: launch (0, 0, -1.5), v0 = (5, 0, -5), throw at t = 1 s, g = 9.80665.
    No drag, tau = 0.5 s: N = 2.5, D = -1.5 - 2.5 + 9.80665 * 0.125 = -2.77416875.
    tau = 1 s: D = -1.5 - 5 + 4.903325 = -1.596675. tau = 2 s: D = 8.1133, which is
    below ground, so the plane is not present. Before the throw it is held at the launch
    point and not present. Linear drag k = 0.5/s at tau = 1 s: (1 - e^-0.5)/0.5 =
    0.786938680574734 (bc), N = 3.93469340287367, D = -1.5 + 19.6133 - 24.6133 *
    0.786938680574734 = -1.2558578265901."""
    p = ThrownPlane(launch_ned=(0.0, 0.0, -1.5), v0_ned=(5.0, 0.0, -5.0), t_throw_ms=1000)
    assert np.allclose(p.position(1500), (2.5, 0.0, -2.77416875), atol=TOL)
    assert np.allclose(p.position(2000), (5.0, 0.0, -1.596675), atol=TOL)
    assert np.array_equal(p.position(500), (0.0, 0.0, -1.5))
    assert not p.present(500) and p.present(1500) and not p.present(3000)
    d = ThrownPlane(
        launch_ned=(0.0, 0.0, -1.5), v0_ned=(5.0, 0.0, -5.0), t_throw_ms=1000, drag_per_s=0.5
    )
    assert np.allclose(d.position(2000), (3.93469340287367, 0.0, -1.2558578265901), atol=1e-9)


def test_s_crossing_bird_and_horizon_rule() -> None:
    """[S0] bird: start (40, -30, -20), v = (0, 6, 0), so at 5 s it is at (40, 0, -20).
    Brief 3.9 demo geometry: a balloon 15 m above home is above a 10 m hover horizon. The
    same check refuses a vehicle at 15 m."""
    bird = CrossingBird(start_ned=(40.0, -30.0, -20.0), velocity_ned=(0.0, 6.0, 0.0))
    assert np.allclose(bird.position(5000), (40.0, 0.0, -20.0), atol=TOL)
    balloon = StaticBalloon(anchor_ned=(25.0, 0.0, -15.0))
    require_above_horizon(balloon, vehicle_alt_m=10.0, t_ms=range(0, 10_000, 1000))
    with pytest.raises(ValueError, match="horizon"):
        require_above_horizon(balloon, vehicle_alt_m=15.0, t_ms=[0])


# ---------------------------------------------------------------------------
# Synthetic camera
# ---------------------------------------------------------------------------


def test_s_projection_of_known_geometry() -> None:
    """[C2]/[C3]: level hover with the nose north. A 1 m balloon at relative (20, 2, -2):
    camera X = body y = 2, Y = body z = -2, Z = body x = 20, so u = 959.5 + 1000*2/20 =
    1059.5, v = 599.5 - 100 = 499.5, w = h = 1000/20 = 50, and the box is x = 1034.5,
    y = 474.5. Nose east (yaw 90): relative (2, 20, -2) is body (20, -2, -2), so u = 859.5."""
    cam = SyntheticCamera(_clean(), seed=1)
    frame = cam.capture(0, HOVER, [_balloon_at((20.0, 2.0, -2.0))])
    (box,) = frame.packet.boxes
    assert (box.x, box.y, box.w, box.h) == pytest.approx((1034.5, 474.5, 50.0, 50.0), abs=TOL)
    truth = frame.truth.get("balloon")
    assert truth.center_px == pytest.approx((1059.5, 499.5), abs=TOL)
    assert truth.depth_m == pytest.approx(20.0) and truth.in_fov
    east = Pose(pos_ned=(0.0, 0.0, -10.0), yaw=90 * DEG)
    (box_e,) = cam.capture(0, east, [_balloon_at((2.0, 20.0, -2.0))]).packet.boxes
    assert box_e.center == pytest.approx((859.5, 499.5), abs=TOL)
    encode(frame.packet)  # [P1]: the real encoder accepts it


def test_s_boresight_error_shifts_box_by_expected_pixels() -> None:
    """[S0] injected boresight error. Target dead ahead at 20 m. With a +1 deg yaw error
    (boresight turned right) it appears left: u = 959.5 - 1000 tan(1 deg) = 942.044935.
    With a +1 deg pitch error (boresight up) it appears lower: v = 599.5 + 17.455065. The
    depth is 20 cos(1 deg), so w = 50 / cos(1 deg) = 50.0076164 (bc). Guidance's nominal
    camera is unchanged."""
    target = [_balloon_at((20.0, 0.0, 0.0))]
    yaw_cam = SyntheticCamera(_clean(boresight_error_rad=(0.0, 0.0, 1 * DEG)), seed=1)
    (by,) = yaw_cam.capture(0, HOVER, target).packet.boxes
    assert by.center == pytest.approx((959.5 - 1000 * TAN1, 599.5), abs=1e-6)
    pitch_cam = SyntheticCamera(_clean(boresight_error_rad=(0.0, 1 * DEG, 0.0)), seed=1)
    (bp,) = pitch_cam.capture(0, HOVER, target).packet.boxes
    assert bp.center == pytest.approx((959.5, 599.5 + 1000 * TAN1), abs=1e-6)
    assert bp.w == pytest.approx(50.007616402195395, abs=1e-9)
    assert pitch_cam.nominal.r_body_cam == ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))


def test_s_clipping_to_flight_grid() -> None:
    """[C2] grid edges are -0.5 and 1919.5 / 1199.5. A 2 m object at relative (20, 18.81, 0)
    projects to u = 959.5 + 940.5 = 1900 with w = 100, so it is clipped at 1919.5:
    x = 1850, w = 69.5. One at (20, -18.81, -11) projects to u = 19, v = 49.5, so it is
    clipped at the left: x = -0.5, w = 69.5, and it touches the top edge exactly
    (y = -0.5, h = 100). One at (20, 30, 0) has u = 2459.5 and no overlap, so it gives no
    box and is not in view, but its truth center is kept."""
    cam = SyntheticCamera(_clean(), seed=1)
    right = cam.capture(0, HOVER, [_balloon_at((20.0, 18.81, 0.0), w=2.0)])
    (b,) = right.packet.boxes
    assert (b.x, b.y, b.w, b.h) == pytest.approx((1850.0, 549.5, 69.5, 100.0), abs=1e-9)
    corner = cam.capture(0, HOVER, [_balloon_at((20.0, -18.81, -11.0), w=2.0)])
    (c,) = corner.packet.boxes
    assert (c.x, c.y, c.w, c.h) == pytest.approx((-0.5, -0.5, 69.5, 100.0), abs=1e-9)
    out = cam.capture(0, HOVER, [_balloon_at((20.0, 30.0, 0.0), w=2.0)])
    assert out.packet.boxes == ()
    assert out.truth.get("balloon").center_px == pytest.approx((2459.5, 599.5))
    assert not out.truth.get("balloon").in_fov
    for f in (right, corner, out):
        encode(f.packet)  # [P1] w, h > 0 and finite


def test_s_behind_camera_and_absent_give_no_box() -> None:
    """[S0]: a target behind the camera (relative (-20, 0, 0), Z = -20) gives no box and has
    no truth center. A thrown plane before its throw is not present. A frame with no boxes
    is still a packet ([P1])."""
    cam = SyntheticCamera(_clean(), seed=1)
    plane = ThrownPlane(launch_ned=(20.0, 0.0, -10.0), v0_ned=(0.0, 0.0, -5.0), t_throw_ms=10_000)
    frame = cam.capture(0, HOVER, [_balloon_at((-20.0, 0.0, 0.0), w=5.0), plane])
    assert frame.packet.boxes == ()
    assert frame.truth.get("balloon") == ObjectTruth(
        name="balloon", center_px=None, depth_m=None, in_fov=False
    )
    assert frame.truth.get("plane").center_px is None
    encode(frame.packet)


def test_s_dropout_windows_and_probability() -> None:
    """[S0] dropout. At 50 fps frames fall every 20 ms. A [1000, 1100) window on t_cap
    empties the frames at 1000..1080 and keeps those at 980 and 1100. Those frames still
    yield packets, with zero boxes. dropout_p = 0 never drops, 1 always drops, and 0.25
    drops about a quarter of 2000 frames for a fixed seed."""
    scene = [_balloon_at((20.0, 0.0, -2.0))]
    cam = SyntheticCamera(_clean(fps=50.0, dropout_windows_ms=((1000, 1100),)), seed=3)
    counts = {
        cam.capture_time(k): len(cam.capture(k, HOVER, scene).packet.boxes) for k in range(49, 56)
    }
    assert counts == {980: 1, 1000: 0, 1020: 0, 1040: 0, 1060: 0, 1080: 0, 1100: 1}

    def kept(p: float) -> int:
        c = SyntheticCamera(_clean(dropout_p=p), seed=3)
        return sum(len(c.capture(k, HOVER, scene).packet.boxes) for k in range(2000))

    assert kept(0.0) == 2000 and kept(1.0) == 0
    assert 0.72 <= kept(0.25) / 2000 <= 0.78


def test_s_noise_is_deterministic_per_seed_and_frame() -> None:
    """[S0], [C1] determinism: frame k's noise depends only on (seed, k). It is the same
    when frame k is rendered alone or after other frames, and different for another seed.
    With center_sigma = 2 px and size_sigma = 0.05, the sample spreads over 2000 frames
    are near those values (truth: u = 959.5, w = 50)."""
    cfg = _clean(center_sigma_px=2.0, size_sigma_frac=0.05)
    scene = [_balloon_at((20.0, 0.0, -2.0))]
    a = SyntheticCamera(cfg, seed=11)
    seq = [a.capture(k, HOVER, scene).packet for k in range(6)]
    alone = SyntheticCamera(cfg, seed=11).capture(5, HOVER, scene).packet
    assert alone == seq[5]
    other = SyntheticCamera(cfg, seed=12).capture(5, HOVER, scene).packet
    assert other != seq[5]
    boxes = [a.capture(k, HOVER, scene).packet.boxes[0] for k in range(2000)]
    du = np.array([b.center[0] - 959.5 for b in boxes])
    lw = np.log(np.array([b.w for b in boxes]) / 50.0)
    assert 1.8 <= du.std() <= 2.2 and abs(du.mean()) < 0.15
    assert 0.045 <= lw.std() <= 0.055


def test_s_latency_and_frame_stamping() -> None:
    """[S0]/[P1] timing: at 60 fps from t0 = 1000, frames are captured at 1000, 1017,
    1033, 1050 (k * 1000/60 rounded to ms). packet.t_cap is the capture time and
    frame_seq is k. The 27.3 ms latency is delivered at t_cap + 28: rounded up, because a
    packet is never usable before it exists. A 27 ms latency gives exactly 27."""
    scene = [_balloon_at((20.0, 0.0, -2.0))]
    cam = SyntheticCamera(CameraSimConfig(), seed=1, t0_ms=1000)
    frames = [cam.capture(k, HOVER, scene) for k in range(4)]
    assert [f.packet.t_cap for f in frames] == [1000, 1017, 1033, 1050]
    assert [f.packet.frame_seq for f in frames] == [0, 1, 2, 3]
    assert [f.t_deliver_ms - f.packet.t_cap for f in frames] == [28, 28, 28, 28]
    assert frames[2].truth.t_cap == 1033
    exact = SyntheticCamera(CameraSimConfig(latency_ms=27.0), seed=1)
    assert exact.capture(0, HOVER, scene).t_deliver_ms == 27


# ---------------------------------------------------------------------------
# [S7] seeds
# ---------------------------------------------------------------------------


def test_s7_seed_values_for_known_labels() -> None:
    """[S7]: seed_i = first 4 bytes, big-endian, of sha256("E1-gate:<scenario>:<i>").
    Values from `printf %s LABEL | sha256sum`: E1-gate:S1:0 -> ba9b1eea = 3130728170,
    E1-gate:S1:2 -> 1ec31891 = 516102289, E1-gate:S2:0 -> 0eda9a3d = 249207357,
    E1-gate:S2:19 -> 8f74c645 = 2406794821, E1-probe:S1:0 -> e2ae7b8e = 3803085710.
    gate_seed_count is 20 for S2 and 3 for the others."""
    assert gate_seeds("S1") == (3130728170, seed_for(SeedSet.GATE, "S1", 1), 516102289)
    s2 = gate_seeds("S2")
    assert len(s2) == gate_seed_count("S2") == 20
    assert (s2[0], s2[19]) == (249207357, 2406794821)
    assert all(gate_seed_count(s) == 3 for s in ("S1", "S3", "S4", "S5", "S6"))
    assert probe_seeds("S1", 1) == (3803085710,)
    with pytest.raises(ValueError):
        gate_seeds("S7")


def test_s7_seed_set_label_is_enforced() -> None:
    """[S7]: each scorecard names its seed set. A gate label needs a gate seed, and a gate
    seed under a probe label is refused, because tuning happens on probe seeds only."""
    assert check_seed("gate", "S1", 3130728170) is SeedSet.GATE
    assert check_seed("probe", "S1", 3803085710) is SeedSet.PROBE
    with pytest.raises(ValueError, match="not a gate seed"):
        check_seed("gate", "S1", 3803085710)
    with pytest.raises(ValueError, match="gate seed"):
        check_seed("probe", "S2", 3130728170)


# ---------------------------------------------------------------------------
# Hand-built traces
# ---------------------------------------------------------------------------


class _Trace:
    """Writes a recording through the real Recorder and reads it back."""

    def __init__(self, trial: PrimeParams | None = None) -> None:
        self.buf = io.StringIO()
        self.rec = Recorder(self.buf)
        self.rec.meta(0, {"fixture": "test_s_world"})
        self.trial = trial or PrimeParams()
        self.frames: list[FrameTruth] = []
        self.truth: list[TruthSample] = []

    def command(
        self,
        t: int,
        command: CommandName,
        cmd_id: str,
        result: AckResult = AckResult.ACCEPTED,
        params=None,
        *,
        auth_ok: bool = True,
    ) -> None:
        pkt = CommandPacket(cmd_id=cmd_id, token=UI_TOKEN, command=command, params=params)
        self.rec.command(t, pkt, auth_ok)
        self.rec.packet(t, AckPacket(cmd_id=cmd_id, result=result))

    def prime(self, t: int) -> None:
        self.command(t, CommandName.PRIME, f"prime-{t}", params=self.trial.to_obj())
        self.state(t, S.PRIMED, None, "cmd:prime:accepted", "transition:UNPRIMED->PRIMED")

    def state(self, t: int, st: MissionState, engaged: int | None, *events: str) -> None:
        self.rec.packet(
            t,
            MissionStatePacket(
                t=t,
                mission_state=st,
                engaged_track_id=engaged,
                trial=self.trial.echo(),
                events=tuple(Event(t=t, name=n) for n in events),
            ),
        )

    def frame(
        self,
        t_cap: int,
        centers: dict[str, tuple[float, float] | None],
        in_fov: dict[str, bool] | None = None,
    ) -> None:
        in_fov = in_fov or {}
        self.rec.packet(t_cap + 28, DetectionPacket(t_cap=t_cap, frame_seq=t_cap, boxes=()))
        self.frames.append(
            FrameTruth(
                t_cap=t_cap,
                frame_seq=t_cap,
                objects=tuple(
                    ObjectTruth(
                        name=n,
                        center_px=c,
                        depth_m=None if c is None else 20.0,
                        in_fov=in_fov.get(n, c is not None),
                    )
                    for n, c in centers.items()
                ),
            )
        )

    def track(
        self,
        t_cap: int,
        track_id: int,
        u: float,
        v: float,
        *,
        w: float = 40.0,
        state: TrackState = TrackState.CONFIRMED,
        misses: int = 0,
    ) -> None:
        self.rec.packet(
            t_cap + 28,
            TrackPacket(
                t_cap=t_cap,
                track_id=track_id,
                state=state,
                u=u,
                v_px=v,
                du=0.0,
                dv=0.0,
                w=w,
                h=w,
                hits=0 if misses else 5,
                misses=misses,
                age_frames=10,
            ),
        )

    def mav(self, t: int, direction: str, mav, msg) -> None:
        self.rec.mavlink(t, direction, bytes(msg.pack(mav)))

    def sample(self, t: int, pos, *, vel=(0.0, 0.0, 0.0), objects=None, yaw: float = 0.0):
        self.truth.append(
            TruthSample(t_ms=t, pose=Pose(pos_ned=pos, yaw=yaw), vel_ned=vel, objects=objects or {})
        )

    def run(self, target: str = "balloon") -> RunTrace:
        records = tuple(read_records(self.buf.getvalue().splitlines()))
        return RunTrace(
            records=records, frames=tuple(self.frames), truth=tuple(self.truth), target=target
        )


FC = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)
COMPANION = mavlink2.MAVLink(None, srcSystem=1, srcComponent=191)
GCS = mavlink2.MAVLink(None, srcSystem=255, srcComponent=190)
BALLOON_C = (960.0, 500.0)
BIRD_C = (1200.0, 500.0)


def _heartbeat(custom_mode: int):
    return FC.heartbeat_encode(
        mavlink2.MAV_TYPE_QUADROTOR,
        mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
        mavlink2.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        custom_mode,
        4,
    )


def _engaged_until_t07(tr: _Trace, *, t07: int = 950, track_id: int = 1001) -> None:
    tr.prime(100)
    tr.state(200, S.LAUNCH, None, "transition:PRIMED->LAUNCH")
    tr.state(300, S.SEARCH, None, "transition:LAUNCH->SEARCH")
    tr.state(400, S.ACQUIRING, None, f"candidate:{track_id}", "transition:SEARCH->ACQUIRING")
    tr.frame(900, {"balloon": BALLOON_C, "bird": BIRD_C})
    tr.track(900, track_id, *BALLOON_C)
    tr.command(t07, CommandName.APPROVE_ENGAGE, "approve-1")
    tr.state(
        t07,
        S.ENGAGED,
        track_id,
        "cmd:approve_engage:accepted",
        f"engaged:{track_id}",
        "transition:ACQUIRING->ENGAGED",
    )


def test_s8_attribution_inside_box_nearest_wins() -> None:
    """[S8] attribution: a 40 px box at (960, 500) holds the balloon center (960, 500) and,
    on its edge, an object at (980, 500): both are inside (edges count), and the nearer
    wins. A box holding no projected center is "none". An object with no center (behind
    the camera) is never attributed."""
    truth = FrameTruth(
        t_cap=0,
        frame_seq=0,
        objects=(
            ObjectTruth(name="edge", center_px=(980.0, 500.0), depth_m=20.0, in_fov=True),
            ObjectTruth(name="balloon", center_px=BALLOON_C, depth_m=20.0, in_fov=True),
            ObjectTruth(name="behind", center_px=None, depth_m=None, in_fov=False),
        ),
    )
    pkt = TrackPacket(
        t_cap=0,
        track_id=1,
        state=TrackState.CONFIRMED,
        u=960.0,
        v_px=500.0,
        du=0.0,
        dv=0.0,
        w=40.0,
        h=40.0,
        hits=5,
        misses=0,
        age_frames=5,
    )
    assert attribute(pkt, truth) == "balloon"
    edge_only = FrameTruth(t_cap=0, frame_seq=0, objects=truth.objects[::2])
    assert attribute(pkt, edge_only) == "edge"
    far = TrackPacket(**{**pkt.__dict__, "u": 1500.0})
    assert attribute(far, truth) is None


def test_s8_retarget_counted_once_per_switch() -> None:
    """[S8] retarget. At T07 (t = 950) engaged track 1001's newest packet (frame 900) is on
    the balloon, so the reference is the balloon. The engaged packets that follow are
    attributed: balloon, none, bird (retarget 1), bird (same switch), balloon (back, not
    counted), bird (retarget 2). "none" never counts. Track 2002 sitting on the bird is
    not the engaged track and is ignored. Expected: 2 retargets, at t_rx 1228 and 1528."""
    tr = _Trace()
    _engaged_until_t07(tr)
    plan = [
        (1000, BALLOON_C),
        (1100, (1500.0, 900.0)),
        (1200, BIRD_C),
        (1300, BIRD_C),
        (1400, BALLOON_C),
        (1500, BIRD_C),
    ]
    for t_cap, (u, v) in plan:
        tr.frame(t_cap, {"balloon": BALLOON_C, "bird": BIRD_C})
        tr.track(t_cap, 1001, u, v)
        tr.track(t_cap, 2002, *BIRD_C)
    result = retargets(tr.run())
    assert result.count == 2
    assert result.switches == ((1228, 1001, "bird"), (1528, 1001, "bird"))


def _lock_trace() -> RunTrace:
    tr = _Trace()
    _engaged_until_t07(tr)
    seen = {"balloon": BALLOON_C, "bird": BIRD_C}
    off = {"balloon": False}
    steps = [
        (1000, BALLOON_C, TrackState.CONFIRMED, 0, None),
        (1100, BALLOON_C, TrackState.CONFIRMED, 0, None),
        (1200, (1500.0, 900.0), TrackState.CONFIRMED, 0, None),
        (1300, BALLOON_C, TrackState.CONFIRMED, 0, off),
        (1400, BALLOON_C, TrackState.COASTING, 1, None),
        (1500, BALLOON_C, TrackState.COASTING, 2, None),
        (1600, BIRD_C, TrackState.CONFIRMED, 0, None),
    ]
    for t_cap, uv, st, misses, fov in steps:
        tr.frame(t_cap, seen, fov)
        tr.track(t_cap, 1001, *uv, state=st, misses=misses)
        if t_cap == 1400:
            tr.state(1428, S.COASTING, 1001, "transition:ENGAGED->COASTING")
        if t_cap == 1600:
            tr.state(1628, S.ENGAGED, 1001, "transition:COASTING->ENGAGED")
    tr.state(1650, S.LOST, None, "track_dead:1001", "transition:ENGAGED->LOST")
    tr.frame(1700, seen)
    tr.frame(1800, seen, off)
    tr.frame(1900, seen)
    tr.state(1950, S.RETURN, None, "budget:reacquire", "transition:LOST->RETURN")
    tr.frame(2000, seen)
    return tr.run()


def test_s8_lock_retention_fraction() -> None:
    """[S8] lock retention over detection frames from the first T07 (950) to the first
    RETURN (1950), counting only frames with the target in view:
    1000, 1100 ENGAGED on the balloon (kept); 1200 box on nothing (not kept); 1300 target
    out of view (excluded); 1400, 1500 COASTING on the balloon (kept; T08 at 1428);
    1600 ENGAGED but on the bird (not kept; T09 at 1628); 1700 after T10 death at 1650,
    with no engaged packet (not kept); 1800 out of view (excluded); 1900 LOST (not kept).
    Frames 900 (before T07) and 2000 (after RETURN) are outside the window.
    Fraction 4/8 = 0.5."""
    result = lock_retention(_lock_trace())
    assert (result.retained, result.frames, result.fraction) == (4, 8, 0.5)


def _touch_pass(tr: _Trace) -> None:
    """Commit at 5000, TOUCH until 9000. The vehicle flies north at 2.5 m/s, sampled every
    20 ms, from x = 0.025 + 0.05 i at 10 m altitude. The balloon is static at
    (5, 0.3, -10.2)."""
    tr.state(
        5000,
        S.TOUCH,
        1001,
        "commit:1001:4972",
        "miss:0.300:-0.200:2.000",
        "transition:ENGAGED->TOUCH",
    )
    balloon = (5.0, 0.3, -10.2)
    tr.sample(4000, balloon, objects={"balloon": balloon})  # before the commit: outside
    for i in range(150):
        tr.sample(
            5010 + 20 * i,
            (0.025 + 0.05 * i, 0.0, -10.0),
            vel=(2.5, 0.0, 0.0),
            objects={"balloon": balloon},
        )
    tr.state(9000, S.MISS, 1001, "pass_done", "transition:TOUCH->MISS")
    tr.sample(9500, balloon, objects={"balloon": balloon})  # after TOUCH: outside the window
    tr.state(9001, S.RETURN, 1001, "transition:MISS->RETURN")
    tr.state(15_000, S.LAND, 1001, "transition:RETURN->LAND")


def test_s8_commit_plane_miss_straight_line_pass() -> None:
    """[S8] commit-plane miss for a straight pass. The true velocity is (2.5, 0, 0), so the
    plane through the balloon is x = 5. The vehicle crosses it between the samples at
    x = 4.975 and 5.025, at (5, 0, -10), and the miss is |(0, -0.3, 0.2)| = sqrt(0.13) =
    0.360555127546398 (bc). The nearest sample is 0.025 m along track, so the closest
    approach is sqrt(0.130625) = 0.361420807370024. The samples before the commit and
    after TOUCH, sitting on the balloon, are outside the window."""
    tr = _Trace(PrimeParams(trial_type=TrialType.TOUCH))
    _engaged_until_t07(tr)
    _touch_pass(tr)
    result = commit_plane_miss(tr.run())
    assert result is not None
    assert result.miss_m == pytest.approx(0.360555127546398, abs=1e-12)
    assert result.closest_approach_m == pytest.approx(0.361420807370024, abs=1e-12)
    assert result.t_closest == 5010 + 20 * 99


def _hold_trace() -> RunTrace:
    tr = _Trace()
    _engaged_until_t07(tr)
    obj = {"balloon": (20.0, 0.0, -15.0)}
    tr.sample(9500, (12.0, 0.0, -15.0), objects=obj)
    for i in range(1, 21):
        pos = {7: (15.0, 0.0, -14.25), 13: (14.0, 0.0, -15.0)}.get(i, (15.0, 0.0, -15.0))
        tr.sample(10_000 + 500 * i, pos, objects=obj)
    tr.state(20_000, S.COMPLETE, 1001, "hold_complete", "transition:ENGAGED->COMPLETE")
    return tr.run()


def test_s8_hold_error_over_the_hold_window() -> None:
    """[S8] hold error: the true distance to the [G3] standoff point over [t_hc - 10 s, t_hc].
    The balloon is at (20, 0, -15) and d_s = 5. The vehicle at (15, 0, -15) is exactly on
    the standoff point (0). At (15, 0, -14.25): q = (0, 0, -0.75), so 0.75. At (14, 0, -15):
    q = (1, 0, 0), so 1.0. The 20 samples in the window are 18 x 0, 0.75 and 1.0, so the
    nearest-rank p95 (19th) is 0.75 and the max is 1.0. The 3 m sample before the window
    does not count."""
    result = hold_error(_hold_trace(), hold_time_s=10.0)
    assert result is not None
    assert (result.samples, result.t_hold_complete) == (20, 20_000)
    assert result.p95_m == 0.75
    assert result.max_m == pytest.approx(1.0, abs=1e-12)


def test_s8_p95_nearest_rank() -> None:
    """[S8] p95 is the nearest rank: the ceil(0.95 n)-th smallest. For 1..20 that is the
    19th, so 19 (linear interpolation would give 19.05). For 1..10 it is the 10th. For
    1..13, ceil(12.35) = 13 (rounding would give 12). A single value is itself, and inf
    (a run with no value) sorts last."""
    assert p95(list(range(20, 0, -1))) == 19.0
    assert p95(list(range(1, 11))) == 10.0
    assert p95(list(range(1, 14))) == 13.0
    assert p95([0.3]) == 0.3
    assert p95([0.1] * 19 + [math.inf]) == 0.1
    with pytest.raises(ValueError):
        p95([])


def _abort_trace(rtl_heartbeat_first: bool) -> RunTrace:
    tr = _Trace(PrimeParams(trial_type=TrialType.TOUCH))
    tr.prime(100)
    tr.mav(2900, "rx", FC, _heartbeat(6))
    tr.command(3000, CommandName.ABORT, "abort-1")
    tr.state(3000, S.ABORT, None, "cmd:abort:accepted", "transition:TOUCH->ABORT")
    tr.mav(3010, "rx", FC, FC.command_ack_encode(176, 0))  # before any RTL request
    tr.mav(
        3020, "tx", COMPANION, COMPANION.command_long_encode(1, 1, 176, 0, 1.0, 6.0, 0, 0, 0, 0, 0)
    )
    tr.mav(3100, "rx", FC, _heartbeat(4))
    tr.mav(3150, "rx", GCS, GCS.command_ack_encode(176, 0))
    if rtl_heartbeat_first:
        tr.mav(3180, "rx", FC, _heartbeat(6))
    tr.mav(3200, "rx", FC, FC.command_ack_encode(176, 0))
    return tr.run()


def test_s8_abort_latency_from_command_to_rtl_confirm() -> None:
    """[S8] abort latency: from the accepted abort's command record (t = 3000) to the first
    FC frame that confirms RTL. An FC HEARTBEAT in RTL before the abort does not count.
    Neither does an accepted DO_SET_MODE ack that arrives before the RTL request is sent,
    a GUIDED heartbeat, or an accepted ack from the GCS's component. The ack
    from the FC (1/1) after the companion's DO_SET_MODE RTL request (t = 3200) gives 200
    ms. If the FC's first confirmation is a HEARTBEAT in RTL at 3180, the result is 180."""

    assert abort_latency_ms(_abort_trace(False)) == 200
    assert abort_latency_ms(_abort_trace(True)) == 180


def test_s8_safety_floor_thresholds_discriminate() -> None:
    """[S8] safety floors, the load-bearing limits checked just inside and just outside.
    Setpoints: 2.5 m/s and the float32 vector 2.5 * (cos 30, sin 30) pass v_max = 2.5;
    2.51 is a breach. Altitude: 2.0 m in SEARCH passes alt_floor 2.0; 1.99 m is a breach;
    0.5 m in LAUNCH is not checked. Geofence: 60.0 m passes a 60 m radius; 60.01 m is a
    breach."""
    tr = _Trace()
    tr.prime(100)
    tr.state(200, S.LAUNCH, None, "transition:PRIMED->LAUNCH")
    tr.sample(250, (0.0, 0.0, -0.5))
    tr.state(300, S.SEARCH, None, "transition:LAUNCH->SEARCH")
    tr.sample(400, (60.0, 0.0, -2.0))
    for vn, ve in ((2.5, 0.0), (2.5 * math.cos(30 * DEG), 2.5 * math.sin(30 * DEG))):
        sp = COMPANION.set_position_target_local_ned_encode(
            0, 1, 1, 1, 1479, 0, 0, 0, vn, ve, 0, 0, 0, 0, 0, 0
        )
        tr.mav(500, "tx", COMPANION, sp)
    ok = safety_floors(tr.run(), alt_floor_m=2.0)
    assert ok.geofence_ok and ok.alt_ok and ok.setpoint_ok and ok.setpoints == 2
    assert ok.min_alt_m == 2.0
    tr.sample(600, (0.0, 60.01, -1.99))
    tr.mav(
        700,
        "tx",
        COMPANION,
        COMPANION.set_position_target_local_ned_encode(
            0, 1, 1, 1, 1479, 0, 0, 0, 2.51, 0, 0, 0, 0, 0, 0, 0
        ),
    )
    bad = safety_floors(tr.run(), alt_floor_m=2.0)
    assert (bad.geofence_breaches, bad.alt_breaches, bad.setpoint_breaches) == (1, 1, 1)


def _s2_trace() -> RunTrace:
    tr = _Trace(PrimeParams(trial_type=TrialType.TOUCH))
    _engaged_until_t07(tr)
    for t_cap in (1000, 1100):
        tr.frame(t_cap, {"balloon": BALLOON_C, "bird": BIRD_C})
        tr.track(t_cap, 1001, *BALLOON_C)
    _touch_pass(tr)
    return tr.run()


def test_s8_scorecard_json_canonical_and_wall_clock_free(monkeypatch) -> None:
    """[S8] scorecard per run, [C5] canonical JSON, [S0] no wall-clock values. The card for
    a complete hand-built S2 run carries every listed field. Its bytes are the canonical
    encoding of themselves, and they are identical when built twice while every
    wall-clock read raises. The run passes its S2 checks (commit then TOUCH, MISS, RETURN
    then LAND, floors, replay). A failed replay fails the card."""

    def no_clock(*_a, **_k):
        raise AssertionError("scorecard read a wall clock")

    for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter"):
        monkeypatch.setattr(time, name, no_clock)
    trace = _s2_trace()
    kw = dict(
        scenario="S2",
        seed=249207357,
        seed_set="gate",
        backend={"name": "hand-built-trace"},
        versions={"fixture": "test_s_world"},
        law="pure_pursuit",
        end_reason="landed",
    )
    a = scorecard_json(score_run(trace, replay_ok=True, **kw))
    b = scorecard_json(score_run(trace, replay_ok=True, **kw))
    assert a == b == canonical_json(json.loads(a))
    card = json.loads(a)
    assert {
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
    } <= set(card)
    assert card["seed_set"] == "gate" and card["final_state"] == "LAND"
    assert card["transitions"][0] == [100, "UNPRIMED->PRIMED"]
    assert card["metrics"]["commit_plane_miss"]["miss_m"] == pytest.approx(0.360555127546398)
    assert {c["name"] for c in card["checks"]} >= {
        "commit_then_touch",
        "pass_ends_miss_or_complete",
        "return_then_land",
        "replay",
    }
    assert card["passed"] is True, [c for c in card["checks"] if not c["passed"]]
    failed = score_run(trace, replay_ok=False, **kw)
    assert failed["passed"] is False


def test_s8_miss_p95_over_the_s2_gate_set() -> None:
    """S2 green: p95 commit-plane miss <= miss_p95_max_m (0.5 m) over the 20 S2 gate seeds.
    The same 0.3606 m pass under all 20 gate seeds passes. With only 19 seeds the gate
    set is incomplete and fails. A 0.36 m miss fails a 0.35 m limit."""
    trace = _s2_trace()
    cards = [
        score_run(
            trace,
            scenario="S2",
            seed=s,
            seed_set="gate",
            backend={"name": "hand-built"},
            versions={},
            law="pure_pursuit",
            replay_ok=True,
            end_reason="landed",
        )
        for s in gate_seeds("S2")
    ]
    full = miss_p95(cards)
    assert full["complete"] and full["passed"]
    assert full["p95_m"] == pytest.approx(0.360555127546398)
    assert not miss_p95(cards[:19])["complete"]
    assert not miss_p95(cards, thresholds=Thresholds(miss_p95_max_m=0.35))["passed"]
    json.loads(scorecard_json(full))


def _checks(trace: RunTrace, scenario: str, **thresholds) -> dict[str, dict]:
    card = score_run(
        trace,
        scenario=scenario,
        seed=gate_seeds(scenario)[0],
        seed_set="gate",
        backend={"name": "hand-built"},
        versions={},
        law="pure_pursuit",
        replay_ok=True,
        end_reason="landed",
        thresholds=Thresholds(**thresholds),
    )
    return {c["name"]: c for c in card["checks"]}


def test_s8_scenario_threshold_checks_discriminate() -> None:
    """§8 scenario table against the §9 thresholds, just passing and just failing.
    S1 (hold trace, p95 0.75 m): hold_tol 0.75 passes, 0.74 fails. COMPLETE came by
    hold_complete, but the trace never reaches RETURN, so return_then_land fails.
    S5 (abort trace, 200 ms): abort_latency_max 200 passes, 199 fails. The abort came from
    TOUCH, but no ABORT->RETURN follows. The FC is seen in RTL only in the variant with
    the 3180 heartbeat (the 2900 one predates the abort).
    S6 (lock trace, 0.5): lock_retention_min 0.5 passes, 0.51 fails. Frame 1600 on the
    bird is one retarget, so zero_retargets fails."""
    s1 = _checks(_hold_trace(), "S1", hold_tol_m=0.75)
    assert s1["hold_error_p95"]["passed"] and s1["complete_by_hold"]["passed"]
    assert not s1["return_then_land"]["passed"]
    assert not _checks(_hold_trace(), "S1", hold_tol_m=0.74)["hold_error_p95"]["passed"]
    s5 = _checks(_abort_trace(False), "S5", abort_latency_max_ms=200)
    assert s5["abort_latency"]["passed"] and s5["abort_latency"]["value"] == 200
    assert s5["abort_in_touch"]["passed"] and not s5["abort_then_return"]["passed"]
    assert not s5["fc_rtl_after_abort"]["passed"]
    assert _checks(_abort_trace(True), "S5")["fc_rtl_after_abort"]["passed"]
    assert not _checks(_abort_trace(False), "S5", abort_latency_max_ms=199)["abort_latency"][
        "passed"
    ]
    s6 = _checks(_lock_trace(), "S6", lock_retention_min=0.5)
    assert s6["lock_retention"]["passed"] and s6["lock_retention"]["value"] == 0.5
    assert not s6["zero_retargets"]["passed"] and s6["zero_retargets"]["value"] == 1
    assert not _checks(_lock_trace(), "S6", lock_retention_min=0.51)["lock_retention"]["passed"]


def test_s3_dropout_checks_on_hand_built_traces() -> None:
    """S3 green, the two halves. Short dropout (lock trace): track 1001 is engaged at
    t0 = 1350. Its first fresh hit at or after t1 = 1500 is frame 1600 (t_rx 1628), with
    no LOST in between, so the lock is retained. A window ending at 1650 is never re-hit
    (death at 1650), so it fails. Long dropout: LOST at 2000, SEARCH at 2001, ACQUIRING
    at 2500, T07 at 3600, so it is re-acquired before budget:reacquire. When
    budget:reacquire (T17, SEARCH->RETURN at 3000) comes first, it fails."""
    short = _lock_trace()
    ok = check_lock_retained(short, t0_ms=1350, t1_ms=1500, name="short_1")
    assert (ok.name, ok.passed, ok.value) == ("short_1", True, 1001)
    assert not check_lock_retained(short, t0_ms=1350, t1_ms=1650).passed

    def long_trace(budget_first: bool) -> RunTrace:
        tr = _Trace()
        _engaged_until_t07(tr)
        tr.state(2000, S.LOST, None, "track_dead:1001", "transition:ENGAGED->LOST")
        tr.state(2001, S.SEARCH, None, "transition:LOST->SEARCH")
        if budget_first:
            tr.state(3000, S.RETURN, None, "budget:reacquire", "transition:SEARCH->RETURN")
            return tr.run()
        tr.state(2500, S.ACQUIRING, None, "candidate:1002", "transition:SEARCH->ACQUIRING")
        tr.command(3600, CommandName.APPROVE_ENGAGE, "approve-2")
        tr.state(3600, S.ENGAGED, 1002, "engaged:1002", "transition:ACQUIRING->ENGAGED")
        return tr.run()

    good = check_reacquired(long_trace(False), after_ms=1500)
    assert good.passed and good.value == 3600
    assert not check_reacquired(long_trace(True), after_ms=1500).passed


def test_s8_cc2_prime_retry_is_not_the_trial_in_force() -> None:
    """CC-2, [P5a], [S8] safety floors: a command record executes only when it is the
    first authenticated record of its cmd_id. Prime A (the §9 60 m geofence) is accepted
    at 100, prime B (30 m, a T02 re-prime) at 200, and A is re-sent with the same id and
    body at 300 (the page's retry): [P5a] acks it accepted again from its stored ack and
    executes nothing. The trial in force stays B, so a truth sample 40 m from home at 400
    is one geofence breach; counting the retry as A would hide it (40 <= 60).
    Discrimination on the auth rule: an unauthenticated record is not stored, so prime C
    (30 m) first sent with a bad token (rejected_auth) and then authenticated (accepted)
    executes, and C is in force."""
    a = PrimeParams()
    b = replace(a, geofence_radius_m=30.0)
    retry = _Trace()
    retry.command(100, CommandName.PRIME, "prime-a", params=a.to_obj())
    retry.command(200, CommandName.PRIME, "prime-b", params=b.to_obj())
    retry.command(300, CommandName.PRIME, "prime-a", params=a.to_obj())
    retry.sample(400, (40.0, 0.0, -10.0))
    floors = safety_floors(retry.run(), alt_floor_m=2.0)
    assert (floors.geofence_breaches, floors.max_horizontal_m) == (1, 40.0)

    auth = _Trace()
    auth.command(100, CommandName.PRIME, "prime-a", params=a.to_obj())
    auth.command(
        200, CommandName.PRIME, "prime-c", AckResult.REJECTED_AUTH, b.to_obj(), auth_ok=False
    )
    auth.command(300, CommandName.PRIME, "prime-c", params=b.to_obj())
    auth.sample(400, (40.0, 0.0, -10.0))
    assert safety_floors(auth.run(), alt_floor_m=2.0).geofence_breaches == 1


def test_s8_cc3_a_run_cut_short_is_red() -> None:
    """CC-3, [S0], [S8]: every scenario's card carries ``run_completed``, which passes
    only for the "landed" end. The complete hand-built S2 run that is green when it
    landed is red, with only run_completed failing, when the run ended on
    "error: ConnectionError: relay peer closed" (SITL died). S6 row: the lock window of
    the lock trace is [950 (T07), 1950 (RETURN)). It covers a maneuver [1000, 1900]; it
    does not cover one that ends at 1950 (the end is exclusive) or one that starts at
    900, before the T07. A run cut off with no end event has a window that runs to its
    newest detection (t_rx 1128 for frames 1000 and 1100): it covers [1000, 1100] and
    not [1000, 1128]."""
    for scenario in SCENARIOS:
        assert "run_completed" in _checks(_s2_trace(), scenario)
    kw = dict(
        scenario="S2",
        seed=gate_seeds("S2")[0],
        seed_set="gate",
        backend={"name": "hand-built"},
        versions={},
        law="pure_pursuit",
        replay_ok=True,
    )
    assert score_run(_s2_trace(), end_reason="landed", **kw)["passed"] is True
    died = score_run(_s2_trace(), end_reason="error: ConnectionError: relay peer closed", **kw)
    assert died["passed"] is False
    assert [c["name"] for c in died["checks"] if not c["passed"]] == ["run_completed"]

    lock = _lock_trace()
    ok = check_lock_window_covers(lock, t_from_ms=1000, t_to_ms=1900)
    assert (ok.name, ok.passed, ok.value) == ("lock_window_covers_maneuver", True, 1950)
    assert not check_lock_window_covers(lock, t_from_ms=1000, t_to_ms=1950).passed
    assert not check_lock_window_covers(lock, t_from_ms=900, t_to_ms=1900).passed
    tr = _Trace()
    _engaged_until_t07(tr)
    for t_cap in (1000, 1100):
        tr.frame(t_cap, {"balloon": BALLOON_C})
        tr.track(t_cap, 1001, *BALLOON_C)
    cut = tr.run()
    assert check_lock_window_covers(cut, t_from_ms=1000, t_to_ms=1100).passed
    assert not check_lock_window_covers(cut, t_from_ms=1000, t_to_ms=1128).passed
