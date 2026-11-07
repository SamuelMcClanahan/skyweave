"""G series: guidance v1 on synthetic geometry (DRONE_CONTRACTS_D0.md §5, [C2]-[C4]).

Every expected value below is derived by hand in the test's docstring from
the contract formulas and the Provisional §9 numbers (f = 1000 px,
cx = 959.5, cy = 599.5, Kp = 0.8, deadband 0.25 m, a_max = 2 m/s^2,
K_yaw = 1.5, yaw_rate_max = 90 deg/s, K_alt = 0.5, v_alt_max = 1 m/s,
w_min_px = 4, r_assume / r_min / r_max = 15 / 1 / 60 m, t_overrun 0.5 s,
t_brake 1 s, v_climb 1.5 m/s, t_climb 2 s, hold 1 m for 10 s). Nothing is
re-derived with the code under test. Real packets, real snapshots, and the
production attitude source go through the real ``Guidance``: a
``VehicleState`` fed pymavlink-encoded FC ``ATTITUDE`` frames through
``ingest``, the way fc_link and replay feed it (finding DT-2). ``ATTITUDE``
carries its angles as float32, so attitude-derived values match the hand
numbers to 1e-6. Nothing we own is mocked.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from skyweave2.drone.camera import CameraModel
from skyweave2.drone.guidance import (
    LAWS,
    Guidance,
    GuidanceConfig,
    LosObservation,
    PurePursuit,
    RangeSource,
    derotate,
    make_law,
    make_observation,
    miss_vector,
    size_range,
    standoff_distance,
    standoff_offset,
)
from skyweave2.drone.mission import MissionView
from skyweave2.drone.packets import (
    MissionState,
    PrimeParams,
    TrackPacket,
    TrackState,
    TrialType,
    canonical_json,
)
from skyweave2.drone.types import (
    Attitude,
    GuidanceEventKind,
    LandedState,
    VehicleSnapshot,
    VelocityCommand,
)
from skyweave2.drone.vehicle_state import LinkConfig, VehicleState, parse_frames

S = MissionState
DEG = math.pi / 180.0
R2 = math.sqrt(0.5)
CX, CY, F = 959.5, 599.5, 1000.0
CAM = CameraModel()
CFG = GuidanceConfig()
STANDOFF = PrimeParams(trial_type=TrialType.STANDOFF)  # d_s 5, v_max 2.5, search_alt 10, W 1
TOUCH = PrimeParams(trial_type=TrialType.TOUCH)  # alpha 0.4, beta 0.1, k 5
ID = 7
TOL = 1e-9
LINK = LinkConfig()  # attitude_bound_ms 100 ([F3], §9)
FC = mavlink2.MAVLink(None, srcSystem=LINK.fc_sysid, srcComponent=LINK.fc_compid)
FC_BOOT_MS = 10_000  # FC time_boot_ms = board ms + this: its own clock, carried, not mapped (E1-F8)


def _att(t: int, *, yaw: float = 0.0, pitch: float = 0.0, roll: float = 0.0) -> Attitude:
    return Attitude(t_ms=t, time_boot_ms=t + FC_BOOT_MS, roll=roll, pitch=pitch, yaw=yaw)


def _ingest(vs: VehicleState, t: int, msg: Any) -> None:
    """Encode ``msg`` as the FC and feed the parsed frame to ``vs`` at board ms ``t``."""
    for parsed in parse_frames(bytes(msg.pack(FC))):
        vs.ingest(parsed, t)


class _Fc:
    """The FC's ATTITUDE stream into the production [G1] source ([F2], [F3]).

    Each sample becomes a real ATTITUDE frame received at its ``t_ms``.
    ``fc(t)`` ingests every frame received at or before ``t`` (fc_link has
    delivered them before the core processes an input stamped ``t``) and
    returns the one ``VehicleState`` that guidance then queries.
    """

    def __init__(self, *samples: Attitude) -> None:
        self.vs = VehicleState(LINK)
        self._samples = samples
        self._next = 0
        self._t: int | None = None

    def __call__(self, t_ms: int) -> VehicleState:
        assert self._t is None or t_ms >= self._t, "the FC stream never runs backwards"
        self._t = t_ms
        while self._next < len(self._samples) and self._samples[self._next].t_ms <= t_ms:
            s = self._samples[self._next]
            msg = FC.attitude_encode(s.time_boot_ms, s.roll, s.pitch, s.yaw, 0.0, 0.0, 0.0)
            _ingest(self.vs, s.t_ms, msg)
            self._next += 1
        return self.vs


def _steady(t0: int, t1: int, *, yaw: float = 0.0, pitch: float = 0.0) -> _Fc:
    """50 Hz frames of one attitude over [t0, t1]."""
    return _Fc(*(_att(t, yaw=yaw, pitch=pitch) for t in range(t0, t1 + 1, 20)))


def _pkt(
    *,
    tid: int = ID,
    t_cap: int = 1000,
    u: float = CX,
    v: float = CY,
    w: float = 100.0,
    state: TrackState = TrackState.CONFIRMED,
    hits: int = 10,
    misses: int = 0,
) -> TrackPacket:
    return TrackPacket(
        t_cap=t_cap,
        track_id=tid,
        state=state,
        u=u,
        v_px=v,
        du=0.0,
        dv=0.0,
        w=w,
        h=w,
        hits=hits,
        misses=misses,
        age_frames=hits + misses + 1,
    )


def _snap(
    t: int,
    *,
    rel_alt: float | None = 10.0,
    mode: str | None = "GUIDED",
    link: bool = True,
    degraded: bool = False,
) -> VehicleSnapshot:
    return VehicleSnapshot(
        t_ms=t,
        fc_link_up=link,
        mode=mode,
        armed=True,
        landed_state=LandedState.IN_AIR,
        rel_alt_m=rel_alt,
        home_dist_m=0.0,
        battery_pct=90.0,
        attitude_age_ms=0,
        attitude_degraded=degraded,
        rc_seen=True,
    )


def _view(
    state: MissionState | None,
    trial: PrimeParams | None = STANDOFF,
    *,
    engaged: int | None = None,
    candidate: int | None = None,
) -> MissionView:
    return MissionView(state=state, trial=trial, engaged_track_id=engaged, candidate_id=candidate)


def _guidance(cfg: GuidanceConfig = CFG) -> Guidance:
    return Guidance(CAM, cfg, make_law("pure_pursuit", cfg))


def _cmd(cmd: VelocityCommand | None) -> tuple[float, float, float, float]:
    assert cmd is not None
    return (cmd.vn, cmd.ve, cmd.vd, cmd.yaw_rate)


def _approx(got: tuple[float, ...] | list[float], want: tuple[float, ...]) -> None:
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        assert g == pytest.approx(w, abs=1e-6), (got, want)


ZERO = (0.0, 0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# [G1] de-rotation and the attitude sample
# ---------------------------------------------------------------------------


def test_g1_derotation_known_pixels_and_attitudes() -> None:
    """[G1], [C2], [C3]: los = R_ned_body R_body_cam normalize(ray).

    Hand derivation (R_body_cam: cam Z -> body x, cam X -> body y, cam Y -> body z):
    - principal point, level, yaw 0: ray (0,0,1) -> body (1,0,0) -> NED (1,0,0), north.
    - u = cx + f: ray (1,0,1)/sqrt2 -> body (r2, r2, 0) -> NED (r2, r2, 0): azimuth +45 deg.
    - v = cy + f: ray (0,1,1)/sqrt2 -> body (r2, 0, r2): 45 deg below the horizon.
    - pitch +30 deg, principal point: Ry(30)(1,0,0) = (cos30, 0, -sin30) = (0.866025, 0, -0.5).
    - yaw +90 deg, principal point: Rz(90)(1,0,0) = (0, 1, 0), east.
    - yaw +90 deg, u = cx + f: Rz(90)(r2, r2, 0) = (-r2, r2, 0): azimuth 135 deg.
    - roll +90 deg (right wing down), u = cx + f: Rx(90)(r2, r2, 0) = (r2, 0, r2):
      the image's right edge now looks down.
    """
    cases = [
        (CX, CY, _att(0), (1.0, 0.0, 0.0)),
        (CX + F, CY, _att(0), (R2, R2, 0.0)),
        (CX, CY + F, _att(0), (R2, 0.0, R2)),
        (CX, CY, _att(0, pitch=30 * DEG), (math.sqrt(3) / 2, 0.0, -0.5)),
        (CX, CY, _att(0, yaw=90 * DEG), (0.0, 1.0, 0.0)),
        (CX + F, CY, _att(0, yaw=90 * DEG), (-R2, R2, 0.0)),
        (CX + F, CY, _att(0, roll=90 * DEG), (R2, 0.0, R2)),
    ]
    for u, v, att, want in cases:
        _approx(list(derotate(_pkt(u=u, v=v), CAM, att)), want)


def test_g1_attitude_sample_interpolates_holds_and_bounds() -> None:
    """[G1], [F2], finding DT-2: the attitude sample for t_cap, from the
    production source (a VehicleState fed real ATTITUDE frames; float32 angles
    on the wire, so 1e-6). One test for the three [G1] sampling facts.

    Interpolation: roll, pitch, and unwrapped yaw are linear between the two
    samples bracketing t_cap. Samples t=1000 (pitch 0, yaw 0) and t=1020
    (pitch 0.2, yaw 0.4): t=1010 -> pitch 0.1, yaw 0.2; t=1005 -> pitch 0.05,
    yaw 0.1. Wrap: t=2000 yaw +170 deg, t=2020 yaw -170 deg is a 20 deg turn
    through 180, so t=2010 -> yaw 180 deg (the principal point looks south,
    (-1,0,0)) and t=2005 -> 175 deg. A linear blend of the raw angles would
    give 0 deg (north) at t=2010.

    Hold: outside the stored span the nearest sample is held, never
    extrapolated. Samples t=1000 yaw 0.0 and t=1020 yaw 0.2 (10 rad/s):
    t=950 holds the t=1000 sample, yaw 0.0 (extrapolation would give -0.5);
    t=1070 holds the t=1020 sample, yaw 0.2 (extrapolation would give 0.7).

    Bound: degraded (None) when the sample used lies more than
    attitude_bound_ms (100) from t_cap, or no sample exists. One sample at
    t=1000: t=1100 and t=900 are exactly 100 ms away (not "more than"): held;
    t=1101 and t=899: degraded. Samples at 1000 and 1400: t=1200 is 200 ms from
    the nearer one: degraded; t=1050 is 50 ms from it: interpolated (frac 0.125
    of the 0.4 rad yaw step = 0.05 rad).
    """
    src = _Fc(_att(1000), _att(1020, pitch=0.2, yaw=0.4))(1020)
    mid, quarter = src.attitude_at(1010), src.attitude_at(1005)
    assert mid is not None and quarter is not None
    _approx((mid.pitch, mid.yaw), (0.1, 0.2))
    _approx((quarter.pitch, quarter.yaw), (0.05, 0.1))

    wrap = _Fc(_att(2000, yaw=170 * DEG), _att(2020, yaw=-170 * DEG))(2020)
    mid, quarter = wrap.attitude_at(2010), wrap.attitude_at(2005)
    assert mid is not None and quarter is not None
    assert abs(mid.yaw) == pytest.approx(math.pi, abs=1e-6)
    assert quarter.yaw == pytest.approx(175 * DEG, abs=1e-6)
    _approx(list(derotate(_pkt(t_cap=2010), CAM, mid)), (-1.0, 0.0, 0.0))

    span = _Fc(_att(1000, yaw=0.0), _att(1020, yaw=0.2))(1020)
    before, after = span.attitude_at(950), span.attitude_at(1070)
    assert before is not None and after is not None
    assert (before.t_ms, before.yaw) == (1000, 0.0)
    assert after.t_ms == 1020 and after.yaw == pytest.approx(0.2, abs=1e-6)

    one = _Fc(_att(1000))(1000)
    assert one.attitude_at(1100) is not None and one.attitude_at(900) is not None
    assert one.attitude_at(1101) is None and one.attitude_at(899) is None
    assert VehicleState(LINK).attitude_at(1000) is None
    gap = _Fc(_att(1000), _att(1400, yaw=0.4))(1400)
    assert gap.attitude_at(1200) is None
    near = gap.attitude_at(1050)
    assert near is not None and near.yaw == pytest.approx(0.05, abs=1e-6)


# ---------------------------------------------------------------------------
# [G2] size range, [G6] miss vector
# ---------------------------------------------------------------------------


def test_g2_size_range_and_assumed_fallback() -> None:
    """[G2]: Z = f W / w, r = Z |ray|; below w_min_px the assume-and-bound
    fallback r = clamp(r_assume, r_min, r_max).

    Hand: W = 1 m, w = 50 px -> Z = 1000/50 = 20 m; at u = cx + f the ray is
    (1, 0, 1), |ray| = sqrt2 -> r = 28.284271 m. At the principal point
    w = 4.0 (= w_min_px) -> Z = r = 250 m, source size; w = 3.999 -> r = 15 m,
    source assumed. r_assume 100 -> r_max 60; r_assume 0.5 -> r_min 1.
    """
    z, r, src = size_range(_pkt(u=CX + F, w=50.0), CAM, 1.0, CFG)
    _approx((z, r), (20.0, 20.0 * math.sqrt(2.0)))
    assert src is RangeSource.SIZE
    assert size_range(_pkt(w=4.0), CAM, 1.0, CFG) == (250.0, 250.0, RangeSource.SIZE)
    z, r, src = size_range(_pkt(w=3.999), CAM, 1.0, CFG)
    assert (z, r, src) == (15.0, 15.0, RangeSource.ASSUMED)
    assert size_range(_pkt(w=2.0), CAM, 1.0, GuidanceConfig(r_assume=100.0))[1] == 60.0
    assert size_range(_pkt(w=2.0), CAM, 1.0, GuidanceConfig(r_assume=0.5))[1] == 1.0


def test_g6_miss_vector_from_pixel_offset_and_range() -> None:
    """[G6], [R4]: eps = (u - cx, v_px - cy); miss = Z eps / f (camera right, down).

    Hand: u = cx + 10, v = cy - 5 -> eps = (10, -5) px; W = 1 m, w = 100 px ->
    Z = 10 m; miss = 10 * (10, -5) / 1000 = (0.1, -0.05) m.
    Fallback (w = 2 < w_min): r = 15 m, |ray| = sqrt(1 + 0.01^2 + 0.005^2) =
    sqrt(1.000125) -> Z = 15 / 1.0000624980 = 14.999063 m ->
    miss = (0.14999063, -0.07499531) m.
    """
    mv = miss_vector(_pkt(u=CX + 10, v=CY - 5, w=100.0), CAM, 1.0, CFG)
    _approx(mv.eps_px, (10.0, -5.0))
    assert mv.z_m == pytest.approx(10.0)
    _approx(mv.miss_m, (0.1, -0.05))
    assumed = miss_vector(_pkt(u=CX + 10, v=CY - 5, w=2.0), CAM, 1.0, CFG)
    assert assumed.range_source is RangeSource.ASSUMED
    _approx(assumed.miss_m, (0.14999063, -0.07499531))


# ---------------------------------------------------------------------------
# [G3] standoff point, s_touch, pursuit, yaw law
# ---------------------------------------------------------------------------


def test_g3_standoff_point_is_level_with_the_target() -> None:
    """[G3]: q = p - s p_h/|p_h|, p_h = (p_N, p_E, 0).

    Hand: target 10 m north and 5 m up -> p = (10, 0, -5); d_s = 5 ->
    q = (10, 0, -5) - 5 (1, 0, 0) = (5, 0, -5). Through a packet: the camera
    sees that target at cam (0, -5, 10) -> u = cx, v = cy - 1000*5/10 = 99.5,
    Z = 10 so w = f W / Z = 100 px; r = 10 sqrt(1.25) and p = r los = (10, 0, -5).
    Directly overhead (p = (0, 0, -5), |p_h| < 0.01) the heading stands in:
    yaw_now 90 deg -> q = (0, 0, -5) - 5 (0, 1, 0) = (0, -5, -5).
    """
    _approx(list(standoff_offset((10.0, 0.0, -5.0), 5.0, 0.0)), (5.0, 0.0, -5.0))
    obs = make_observation(_pkt(v=CY - 500.0, w=100.0), CAM, _att(1000), 0.0, 1.0, CFG, 1000)
    _approx(list(obs.p_ned()), (10.0, 0.0, -5.0))
    _approx(list(standoff_offset(obs.p_ned(), 5.0, 0.0)), (5.0, 0.0, -5.0))
    _approx(list(standoff_offset((0.0, 0.0, -5.0), 5.0, 90 * DEG)), (0.0, -5.0, -5.0))


def test_g3_touch_standoff_s_touch_and_its_floor() -> None:
    """[G3]: s = d_s in standoff trials; in touch trials
    s_touch = max(f W / (1920 alpha) - deadband - margin, W/2 + nose_clearance).

    Hand: f = 1000, W = 1, alpha = 0.4 -> 1000/768 = 1.3020833 - 0.25 - 0.05 =
    1.0020833 m (floor 0.8 m). alpha = 1.0 -> 1000/1920 - 0.3 = 0.2208333 < 0.8 ->
    the floor 0.5 + 0.3 = 0.8 m. Standoff trial -> d_s = 5 m.
    """
    assert standoff_distance(TOUCH, CAM, CFG) == pytest.approx(1.0020833333)
    floor_trial = PrimeParams(trial_type=TrialType.TOUCH, alpha=1.0)
    assert standoff_distance(floor_trial, CAM, CFG) == pytest.approx(0.8)
    assert standoff_distance(STANDOFF, CAM, CFG) == 5.0


def _north_obs(dist_m: float) -> LosObservation:
    """A level target dist_m north, nose north (az 0)."""
    return LosObservation(
        t_ms=0,
        t_cap=0,
        track_id=ID,
        los_ned=(1.0, 0.0, 0.0),
        r_m=dist_m,
        range_source=RangeSource.SIZE,
        z_m=dist_m,
        az_body=0.0,
        el_body=0.0,
        yaw_now=0.0,
    )


def test_g3_pure_pursuit_gain_deadband_clamp_and_rate_limit() -> None:
    """[G3]: v = Kp q, zero when |q| <= deadband; clamp |v| <= v_max; then
    |v_k - v_(k-1)| <= a_max dt.

    Hand (s = 5, level target north, generous dt = 10 s unless noted):
    - 5.5 m: q = 0.5 -> v = 0.8 * 0.5 = 0.4 m/s north.
    - 5.25 m: |q| = 0.25 = deadband -> 0. 5.26 m: q = 0.26 -> 0.208 m/s.
    - 10 m: q = 5 -> Kp q = 4 > v_max 2.5 -> 2.5 m/s.
    - 10 m from rest with dt = 0.1, 0.1, 0.25 s: steps of a_max dt = 0.2, 0.2, 0.5 ->
      0.2, 0.4, 0.9 m/s.
    - no observation -> zero, and the next step starts from rest again.
    """

    def run(dist: float, dt: float = 10.0) -> tuple[float, float, float, float]:
        return _cmd(PurePursuit(CFG).command(_north_obs(dist), 5.0, 2.5, dt))

    _approx(run(5.5), (0.4, 0.0, 0.0, 0.0))
    _approx(run(5.25), ZERO)
    _approx(run(5.26), (0.208, 0.0, 0.0, 0.0))
    _approx(run(10.0), (2.5, 0.0, 0.0, 0.0))
    law = PurePursuit(CFG)
    got = [_cmd(law.command(_north_obs(10.0), 5.0, 2.5, dt))[0] for dt in (0.1, 0.1, 0.25)]
    _approx(got, (0.2, 0.4, 0.9))
    _approx(_cmd(law.command(None, 5.0, 2.5, 0.1)), ZERO)
    _approx(_cmd(law.command(_north_obs(10.0), 5.0, 2.5, 0.1)), (0.2, 0.0, 0.0, 0.0))


def test_g3_limiter_dt_from_step_stamps_and_resets_on_t07_t09_and_clear() -> None:
    """[G3], [G10]: the limiter takes dt from consecutive step stamps and
    v_(k-1) resets to zero on T07, T09, and when degradation clears.

    Hand: target 10 m north (principal point, w = 100, W = 1), d_s 5 ->
    Kp q = 4 -> clamped 2.5 m/s north; a_max = 2.
    - ACQUIRING step at 1100, rel_alt 12 vs search_alt 10: v_D = 0.5*2 = 1.0.
    - T07, ENGAGED steps at 1200, 1300, 1500 (dt 0.1, 0.1, 0.2): 0.2, 0.4, 0.8 north,
      v_D 0 (from rest, not from the ACQUIRING (0, 0, 1)).
    - T08 then T09 between steps; step at 1600 (dt 0.1): 0.2 (not 1.0).
    - degraded step at 1700: zero; clear step at 1800 (dt 0.1): 0.2 (not 0.4).
    """
    g = _guidance()
    fc = _steady(0, 3000)
    acq = _view(S.ACQUIRING, candidate=ID)
    eng = _view(S.ENGAGED, engaged=ID)
    g.on_track(_pkt(t_cap=1000), 1010, acq, fc(1010))
    _approx(_cmd(g.step(1100, acq, fc(1100), _snap(1100, rel_alt=12.0))[0]), (0.0, 0.0, 1.0, 0.0))
    got = [_cmd(g.step(t, eng, fc(t), _snap(t))[0]) for t in (1200, 1300, 1500)]
    _approx([c[0] for c in got], (0.2, 0.4, 0.8))
    _approx([c[2] for c in got], (0.0, 0.0, 0.0))
    coast = _pkt(t_cap=1520, state=TrackState.COASTING, hits=0, misses=1)
    g.on_track(coast, 1530, _view(S.COASTING, engaged=ID), fc(1530))
    g.on_track(_pkt(t_cap=1560, hits=1), 1570, eng, fc(1570))
    _approx(_cmd(g.step(1600, eng, fc(1600), _snap(1600))[0]), (0.2, 0.0, 0.0, 0.0))
    _approx(_cmd(g.step(1700, eng, fc(1700), _snap(1700, degraded=True))[0]), ZERO)
    _approx(_cmd(g.step(1800, eng, fc(1800), _snap(1800))[0]), (0.2, 0.0, 0.0, 0.0))


def test_g3_yaw_law_sign_clamp_and_azimuth_against_yaw_now() -> None:
    """[G3], [C4]: az = wrap_pi(atan2(los_E, los_N) - yaw_now), yaw_now the
    newest sample; yaw_rate = clamp(1.5 az, +-90 deg/s); az > 0 (target right)
    turns the nose right (positive). Checked in ACQUIRING (rel_alt = search_alt).

    Hand (los taken at t_cap = 1000, yaw_now from the sample at 1100):
    - u = cx + f, yaw 0 -> 0: az 45 deg -> 67.5 deg/s = +1.178097 rad/s.
    - u = cx - f -> az -45 deg -> -1.178097.
    - u = cx + f, yaw 0 at t_cap, 30 deg now: az 15 deg -> 22.5 deg/s = 0.392699.
    - u = cx + f, yaw 0 at t_cap, -45 deg now: az 90 deg -> 135 deg/s, clamped to
      90 deg/s = 1.570796.
    - principal point, yaw 170 deg at t_cap, -170 deg now: az = wrap(340) =
      -20 deg -> -30 deg/s = -0.523599 (without the wrap: +90 deg/s).
    """
    cases = [
        (CX + F, 0.0, 0.0, 67.5 * DEG),
        (CX - F, 0.0, 0.0, -67.5 * DEG),
        (CX + F, 0.0, 30.0, 22.5 * DEG),
        (CX + F, 0.0, -45.0, 90.0 * DEG),
        (CX, 170.0, -170.0, -30.0 * DEG),
    ]
    acq = _view(S.ACQUIRING, candidate=ID)
    for u, yaw_cap, yaw_now, want in cases:
        g = _guidance()
        fc = _Fc(_att(1000, yaw=yaw_cap * DEG), _att(1100, yaw=yaw_now * DEG))
        g.on_track(_pkt(t_cap=1000, u=u), 1010, acq, fc(1010))
        _approx(_cmd(g.step(1100, acq, fc(1100), _snap(1100))[0]), (0.0, 0.0, 0.0, want))


# ---------------------------------------------------------------------------
# [G4] commit gate
# ---------------------------------------------------------------------------


def _gate(
    pkt: TrackPacket,
    *,
    trial: PrimeParams = TOUCH,
    fc: _Fc | None = None,
    t_ms: int = 1010,
    state: MissionState = S.ENGAGED,
) -> list:
    g = _guidance()
    view = _view(state, trial, engaged=ID, candidate=ID)
    return g.on_track(pkt, t_ms, view, (fc if fc is not None else _steady(0, 3000))(t_ms))


def test_g4_commit_gate_truth_table() -> None:
    """[G4], [G1a], [G8]: commit when w/1920 >= alpha AND hypot(offset) <=
    beta*1920 AND confirmed AND hits >= k AND attitude not degraded; each
    condition alone blocks; only in ENGAGED, touch trials.

    Hand (alpha 0.4 -> 768 px, beta 0.1 -> 192 px, k 5): the base packet is
    centered, w = 800 (0.4167), confirmed, hits 5, attitude fresh -> commits.
    Alone: w = 700 (0.3646) blocks; u = cx + 200 (200 > 192) blocks; tentative
    or coasting blocks; hits 4 blocks; t_cap 1000 ms before the first sample
    (packet degraded) blocks; input stamp 200 ms after the newest sample ([F3]
    at the stamp) blocks; ACQUIRING (candidate) does not commit.
    """
    base = {"w": 800.0, "hits": 5}
    assert len(_gate(_pkt(**base))) == 1
    assert _gate(_pkt(w=700.0, hits=5)) == []
    assert _gate(_pkt(u=CX + 200.0, **base)) == []
    assert _gate(_pkt(state=TrackState.TENTATIVE, **base)) == []
    assert _gate(_pkt(state=TrackState.COASTING, **base)) == []
    assert _gate(_pkt(w=800.0, hits=4)) == []
    assert _gate(_pkt(**base), fc=_steady(2000, 3000), t_ms=2010) == []
    assert _gate(_pkt(**base), fc=_steady(0, 1000), t_ms=1200) == []
    assert _gate(_pkt(**base), state=S.ACQUIRING) == []


def test_g4_alpha_and_beta_boundaries() -> None:
    """[G4] discrimination on the load-bearing thresholds (>= alpha, <= beta).

    Hand: alpha 0.4 * 1920 = 768 px: w = 768 commits, w = 767.5 does not.
    beta 0.1 * 1920 = 192 px (w = 800): u = cx + 192 commits, cx + 192.5 does not.
    """
    assert len(_gate(_pkt(w=768.0, hits=5))) == 1
    assert _gate(_pkt(w=767.5, hits=5)) == []
    assert len(_gate(_pkt(u=CX + 192.0, w=800.0, hits=5))) == 1
    assert _gate(_pkt(u=CX + 192.5, w=800.0, hits=5)) == []


def test_g4_commits_exactly_once_and_never_in_standoff() -> None:
    """[G4]: at most one commit per pass; standoff trials never commit.

    Hand: the base qualifying packet (w = 800, centered, hits 5) arrives five
    times: one commit event, at the first packet's input stamp 1010.
    """
    view = _view(S.ENGAGED, TOUCH, engaged=ID)
    g, fc = _guidance(), _steady(0, 3000)
    events = []
    for i in range(5):
        t = 1010 + 20 * i
        events += g.on_track(_pkt(t_cap=1000 + 20 * i, w=800.0, hits=5 + i), t, view, fc(t))
    assert [(e.kind, e.t_ms) for e in events] == [(GuidanceEventKind.COMMIT, 1010)]
    g, fc = _guidance(), _steady(0, 3000)
    sview = _view(S.ENGAGED, STANDOFF, engaged=ID)
    for i in range(5):
        t = 1010 + 20 * i
        pkt = _pkt(t_cap=1000 + 20 * i, w=800.0, hits=5 + i)
        assert g.on_track(pkt, t, sview, fc(t)) == []


def test_g6_commit_event_carries_the_miss_vector() -> None:
    """[G6], [G4]: the commit event carries id, t_cap, miss_m, z_m.

    Hand: alpha 0.05 (96 px <= w = 100), eps = (10, -5) px (11.2 <= 192),
    Z = 1000 * 1 / 100 = 10 m -> miss = (0.1, -0.05) m; stamp = input 1010,
    t_cap 1000.
    """
    trial = PrimeParams(trial_type=TrialType.TOUCH, alpha=0.05)
    (ev,) = _gate(_pkt(u=CX + 10.0, v=CY - 5.0, w=100.0, hits=5), trial=trial)
    assert (ev.kind, ev.t_ms, ev.track_id, ev.t_cap) == (GuidanceEventKind.COMMIT, 1010, ID, 1000)
    assert ev.z_m == pytest.approx(10.0)
    assert ev.miss_m is not None
    _approx(ev.miss_m, (0.1, -0.05))


# ---------------------------------------------------------------------------
# [G5] terminal segment
# ---------------------------------------------------------------------------


def test_g5_terminal_timing_from_commit_stamp() -> None:
    """[G5]: fly v_c c/|c| for |c|/v_c + t_overrun, brake t_brake, climb
    v_climb for t_climb, then pass_done once; yaw rate 0; timed from the
    commit stamp; track packets ignored; attitude degradation ignored.

    Hand: touch trial alpha 0.2 (384 px <= w = 400), centered, yaw 90 deg ->
    Z = r = 1000/400 = 2.5 m, c = (0, 2.5, 0) east, v_c = v_max = 2.5 ->
    fly (0, 2.5, 0) for 2.5/2.5 + 0.5 = 1.5 s, brake to 2.5 s, climb
    (0, 0, -1.5) to 4.5 s. Commit stamp 10000 (t_cap 9950): fly at 11499,
    brake at 11500..12499, climb at 12500..14499, pass_done at 14500 and not
    again at 14600. Timing from t_cap would brake already at 11450.
    """
    trial = PrimeParams(trial_type=TrialType.TOUCH, alpha=0.2)
    g = _guidance()
    fc = _steady(9000, 10_100, yaw=90 * DEG)
    (ev,) = g.on_track(
        _pkt(t_cap=9950, w=400.0, hits=5), 10_000, _view(S.ENGAGED, trial, engaged=ID), fc(10_000)
    )
    assert ev.kind is GuidanceEventKind.COMMIT
    rec = g.last_commit
    assert rec is not None and (rec.t_ms, rec.v_close, rec.t_fly_s) == (10_000, 2.5, 1.5)
    _approx(rec.contact_ned, (0.0, 2.5, 0.0))

    touch = _view(S.TOUCH, trial, engaged=ID)
    blind = VehicleState(LINK)  # no ATTITUDE frames: degraded everywhere, ignored in TOUCH
    other = _pkt(t_cap=10_150, u=CX + F, w=900.0, hits=9)
    assert g.on_track(other, 10_160, touch, fc(10_160)) == []
    fly, brake, climb = (0.0, 2.5, 0.0, 0.0), ZERO, (0.0, 0.0, -1.5, 0.0)
    plan = [
        (10_100, fly),
        (11_449, fly),
        (11_499, fly),
        (11_500, brake),
        (12_499, brake),
        (12_500, climb),
        (14_499, climb),
    ]
    for t, want in plan:
        cmd, events = g.step(t, touch, blind, _snap(t, degraded=True))
        _approx(_cmd(cmd), want)
        assert events == []
    cmd, events = g.step(14_500, touch, blind, _snap(14_500))
    _approx(_cmd(cmd), ZERO)
    assert [(e.kind, e.t_ms) for e in events] == [(GuidanceEventKind.PASS_DONE, 14_500)]
    assert g.step(14_600, touch, blind, _snap(14_600))[1] == []


def test_g5_terminal_resets_outside_touch() -> None:
    """[G5], [G10]: the segment resets when the mission leaves TOUCH (an abort
    mid-pass): the exit state gets zero setpoints and no pass_done ever comes.

    Hand: same commit as the timing test (fly 1.5 s, done at 4.5 s); abort at
    10500; steps in ABORT to 16000 output zero and emit nothing.
    """
    trial = PrimeParams(trial_type=TrialType.TOUCH, alpha=0.2)
    g = _guidance()
    fc = _steady(9000, 10_100)
    eng = _view(S.ENGAGED, trial, engaged=ID)
    g.on_track(_pkt(t_cap=9950, w=400.0, hits=5), 10_000, eng, fc(10_000))
    g.step(10_100, _view(S.TOUCH, trial, engaged=ID), fc(10_100), _snap(10_100))
    abort = _view(S.ABORT, trial, engaged=ID)
    for t in range(10_500, 16_001, 500):
        cmd, events = g.step(t, abort, fc(t), _snap(t))
        _approx(_cmd(cmd), ZERO)
        assert events == []


# ---------------------------------------------------------------------------
# [G7] standoff hold
# ---------------------------------------------------------------------------


def _hold_events(
    d_s: float,
    *,
    coast_at: int | None = None,
    coast_state: MissionState = S.COASTING,
    until: int = 16_000,
) -> list[tuple[GuidanceEventKind, int]]:
    """Packets every 100 ms (input stamp t, t_cap t - 20) of a level target
    5 m north (principal point, w = 200, W = 1); one coasting packet at
    ``coast_at``, seen with the mission in ``coast_state``."""
    trial = PrimeParams(trial_type=TrialType.STANDOFF, d_s=d_s)
    g, fc = _guidance(), _steady(-100, until)
    eng, coasting = _view(S.ENGAGED, trial, engaged=ID), _view(coast_state, trial, engaged=ID)
    out = []
    for t in range(0, until + 1, 100):
        if t == coast_at:
            pkt = _pkt(t_cap=t - 20, w=200.0, state=TrackState.COASTING, hits=0, misses=1)
            out += [(e.kind, e.t_ms) for e in g.on_track(pkt, t, coasting, fc(t))]
        else:
            pkt = _pkt(t_cap=t - 20, w=200.0)
            out += [(e.kind, e.t_ms) for e in g.on_track(pkt, t, eng, fc(t))]
    return out


def test_g7_hold_complete_after_hold_time_reset_by_coasting() -> None:
    """[G7]: hold_complete once |q| <= hold_tol_m held without a break for
    hold_time_s, on engaged-track input stamps; a coasting packet breaks it.

    Hand: target 5 m north, d_s 4.5 -> |q| = 0.5 <= 1: first in-tolerance
    packet at 0 -> hold_complete at 10000 (not 9900), exactly once.
    Coasting at 5000, confirmed again at 5100 -> hold_complete at 15100.
    Tolerance boundary: d_s 4.0 -> |q| = 1.0 holds (event at 10000);
    d_s 3.99 -> |q| = 1.01 never holds.
    """
    hold = GuidanceEventKind.HOLD_COMPLETE
    assert _hold_events(4.5) == [(hold, 10_000)]
    assert _hold_events(4.5, coast_at=5_000) == [(hold, 15_100)]
    # the packet itself breaks the hold, whatever the view says
    assert _hold_events(4.5, coast_at=5_000, coast_state=S.ENGAGED) == [(hold, 15_100)]
    assert _hold_events(4.0, until=11_000) == [(hold, 10_000)]
    assert _hold_events(3.99, until=11_000) == []


# ---------------------------------------------------------------------------
# [G8] degraded, [G9] lock, [G10] per-state output, [G11] cadence
# ---------------------------------------------------------------------------


def test_g8_degraded_attitude_commands_zero() -> None:
    """[G8], [G1a]: in SEARCH, ACQUIRING, ENGAGED, COASTING a degraded step
    commands zero velocity and yaw rate 0: [F3] at the step (snapshot flag, or
    no sample within 100 ms of the stamp) or the used packet degraded.

    Hand: rel_alt 14 vs search_alt 10 would give v_D = clamp(2.0) = 1.0 and the
    target at u = cx + f a yaw rate of 1.178 rad/s; each degraded variant
    gives (0, 0, 0, 0). The undegraded SEARCH step is (0, 0, 1.0, pi/4).
    """
    states = {
        S.SEARCH: _view(S.SEARCH),
        S.ACQUIRING: _view(S.ACQUIRING, candidate=ID),
        S.ENGAGED: _view(S.ENGAGED, engaged=ID),
        S.COASTING: _view(S.COASTING, engaged=ID),
    }
    # frames over [0, 2000] with the snapshot flag set, or frames over [0, 1000]
    # (step at 1500: the newest sample is 500 ms old)
    for state, view in states.items():
        for last_frame, degraded in ((2000, True), (1000, False)):
            g, fc = _guidance(), _steady(0, last_frame)
            pkt_state = TrackState.COASTING if state is S.COASTING else TrackState.CONFIRMED
            g.on_track(_pkt(t_cap=1000, u=CX + F, state=pkt_state), 1010, view, fc(1010))
            cmd, _ = g.step(1500, view, fc(1500), _snap(1500, rel_alt=14.0, degraded=degraded))
            _approx(_cmd(cmd), ZERO)
    for state in (S.ACQUIRING, S.ENGAGED, S.COASTING):
        g, view = _guidance(), states[state]
        late = _steady(2000, 3000)  # packet t_cap 1000: 1000 ms before the first sample
        g.on_track(_pkt(t_cap=1000, u=CX + F), 2010, view, late(2010))
        _approx(_cmd(g.step(2100, view, late(2100), _snap(2100, rel_alt=14.0))[0]), ZERO)
    fresh = _steady(0, 2000)(1500)
    cmd, _ = _guidance().step(1500, states[S.SEARCH], fresh, _snap(1500, rel_alt=14.0))
    _approx(_cmd(cmd), (0.0, 0.0, 1.0, math.pi / 4))


def test_g9_other_track_ids_are_ignored() -> None:
    """[G9], [M7]: only the engaged id (ENGAGED, COASTING) or the candidate
    (ACQUIRING) is consumed; a better-looking other track changes nothing.

    Hand: engaged 7 is 10 m north (centered, w = 100): with s_touch 1.002 m,
    Kp q = 7.2 -> 2.5 m/s, rate limited from rest over the 100 ms since the
    previous step to (0.2, 0, 0), yaw 0. Track 8, centered with w = 900 and
    hits 50, meets every image-space commit condition: no event, command
    unchanged. In ACQUIRING with candidate 7 centered, track 8 at 45 deg right
    leaves the yaw rate at 0.
    """
    eng = _view(S.ENGAGED, TOUCH, engaged=ID)
    g, fc = _guidance(), _steady(0, 2000)
    g.step(1000, _view(S.ACQUIRING, TOUCH, candidate=ID), fc(1000), _snap(1000))
    assert g.on_track(_pkt(t_cap=1000), 1010, eng, fc(1010)) == []
    assert g.on_track(_pkt(tid=8, t_cap=1000, w=900.0, hits=50), 1010, eng, fc(1010)) == []
    _approx(_cmd(g.step(1100, eng, fc(1100), _snap(1100))[0]), (0.2, 0.0, 0.0, 0.0))
    acq = _view(S.ACQUIRING, candidate=ID)
    g, fc = _guidance(), _steady(0, 2000)
    g.on_track(_pkt(t_cap=1000), 1010, acq, fc(1010))
    g.on_track(_pkt(tid=8, t_cap=1000, u=CX + F), 1010, acq, fc(1010))
    _approx(_cmd(g.step(1100, acq, fc(1100), _snap(1100))[0]), ZERO)


def test_g10_per_state_outputs() -> None:
    """[G10]: every command carries an explicit yaw rate; per-state output.

    Hand (search_alt 10, K_alt 0.5, v_alt_max 1):
    - SEARCH: rel_alt 11 -> v_D = +0.5 (above -> descend, positive down);
      14 -> clamp(2.0) = 1.0; 7 -> clamp(-1.5) = -1.0; unknown -> 0; yaw
      rate = search_yaw_rate = 45 deg/s = pi/4.
    - ACQUIRING, candidate at u = cx + f (az 45 deg), rel_alt 11:
      (0, 0, 0.5, 1.178097).
    - COASTING on the coasting prediction at az 45 deg: (0, 0, 0, 1.178097).
    - LOST: zero.
    - ABORT, COMPLETE, MISS, RETURN, LAND: zero while link up and GUIDED; no
      setpoint in RTL or with the link down.
    - unprimed, PRIMED, LAUNCH: no setpoint.
    """
    at_1100 = _steady(0, 2000)(1100)  # the FC stream as of a step at 1100
    search = _view(S.SEARCH)
    for rel_alt, vd in ((11.0, 0.5), (14.0, 1.0), (7.0, -1.0), (None, 0.0)):
        cmd, _ = _guidance().step(1100, search, at_1100, _snap(1100, rel_alt=rel_alt))
        _approx(_cmd(cmd), (0.0, 0.0, vd, math.pi / 4))

    yaw45 = 1.5 * 45 * DEG
    acq = _view(S.ACQUIRING, candidate=ID)
    g, fc = _guidance(), _steady(0, 2000)
    g.on_track(_pkt(t_cap=1000, u=CX + F), 1010, acq, fc(1010))
    cmd, _ = g.step(1100, acq, fc(1100), _snap(1100, rel_alt=11.0))
    _approx(_cmd(cmd), (0.0, 0.0, 0.5, yaw45))
    coast = _view(S.COASTING, engaged=ID)
    g, fc = _guidance(), _steady(0, 2000)
    pkt = _pkt(t_cap=1000, u=CX + F, state=TrackState.COASTING, hits=0, misses=2)
    g.on_track(pkt, 1010, coast, fc(1010))
    cmd, _ = g.step(1100, coast, fc(1100), _snap(1100, rel_alt=11.0))
    _approx(_cmd(cmd), (0.0, 0.0, 0.0, yaw45))

    _approx(_cmd(_guidance().step(1100, _view(S.LOST), at_1100, _snap(1100))[0]), ZERO)
    for state in (S.ABORT, S.COMPLETE, S.MISS, S.RETURN, S.LAND):
        view = _view(state)
        _approx(_cmd(_guidance().step(1100, view, at_1100, _snap(1100))[0]), ZERO)
        assert _guidance().step(1100, view, at_1100, _snap(1100, mode="RTL")) == (None, [])
        assert _guidance().step(1100, view, at_1100, _snap(1100, link=False)) == (None, [])
    assert _guidance().step(1100, _view(None, None), at_1100, _snap(1100)) == (None, [])
    for state in (S.PRIMED, S.LAUNCH):
        assert _guidance().step(1100, _view(state), at_1100, _snap(1100)) == (None, [])


def test_g11_setpoint_cadence() -> None:
    """[G11]: a setpoint step is a tick at least 1000 / setpoint_hz ms after the
    previous step. Hand: 10 Hz -> 100 ms: after a step at 1000, ticks at 1050
    and 1099 are not steps; 1100 is."""
    g, fc = _guidance(), _steady(0, 2000)
    assert g.step_due(1000)
    g.step(1000, _view(S.LOST), fc(1000), _snap(1000))
    assert not g.step_due(1050) and not g.step_due(1099)
    assert g.step_due(1100)


# ---------------------------------------------------------------------------
# Law registry, camera model, recording meta
# ---------------------------------------------------------------------------


def test_g3_law_registry_and_meta_round_trip() -> None:
    """[G3] pluggable law behind a registry; [R2] the camera model and the
    guidance constants serialize into meta.config and back unchanged."""
    assert "pure_pursuit" in LAWS
    assert make_law("pure_pursuit", CFG).name == "pure_pursuit"
    with pytest.raises(ValueError):
        make_law("proportional_navigation", CFG)
    canonical_json({"camera": CAM.to_obj(), "guidance": CFG.to_obj()})  # finite, JSON-able
    assert CameraModel.from_obj(CAM.to_obj()) == CAM
    assert GuidanceConfig.from_obj(CFG.to_obj()) == CFG
    with pytest.raises(ValueError):
        GuidanceConfig.from_obj({**CFG.to_obj(), "Kq": 1.0})


def test_g1_camera_model_conventions() -> None:
    """[C2], [C3]: pinhole on the 1920 x 1200 grid; R_body_cam upright.

    Hand: cam point (1, 0.5, 10) -> u = 959.5 + 1000/10 = 1059.5,
    v = 599.5 + 500/10 = 649.5. A point 10 m east with yaw 90 deg lies on the
    optical axis -> (959.5, 599.5). A point behind the camera and the 640x360
    sprint grid are refused.
    """
    assert CAM.r_body_cam == ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    _approx(CAM.project_cam((1.0, 0.5, 10.0)), (1059.5, 649.5))
    _approx(CAM.project_ned((0.0, 10.0, 0.0), _att(0, yaw=90 * DEG)), (959.5, 599.5))
    with pytest.raises(ValueError):
        CAM.project_cam((0.0, 0.0, -1.0))
    with pytest.raises(ValueError):
        CameraModel(width=640, height=360)
