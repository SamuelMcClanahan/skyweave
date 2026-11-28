"""K series: the Provisional tracker (DRONE_CONTRACTS_D0.md [K1]-[K4], [P2], [P2a]).

The tracker is a reimplementation of the brief §2 spec (tracker v0 source is
missing); v0's numbers are not inherited and nothing here asserts them.
Expected values come from the contract text and from synthetic truth (box
positions the test itself placed), never from a re-run of the filter math.
Real detection packets go through the real ``Tracker``, and every packet it
returns goes through the real wire encoder and decoder ([P2], [C5]).

World angles (V-D10): the tracker de-rotates each box with the attitude at
``t_cap`` and filters in a tangent plane fixed in NED. Its attitude source
here is the production one, a ``VehicleState`` fed real ``ATTITUDE`` frames
(``fc_attitude.FcAttitude``). In the tests written for the E1 pixel tracker
the vehicle is level and does not turn, so the world is the image: their
pixel expectations still hold. Two of them place their boxes around the
image center, where the tangent plane is the image plane exactly, because
they pin exact IoU values ([K2] now compares boxes in the tangent plane).

Defaults under test (Provisional, brief §6): M = 3 of N = 5, coast_cap = 20.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable

import numpy as np
import pytest

from skyweave2.drone import packets as P
from skyweave2.drone.camera import CONE_UP_HFOV100, CameraModel
from skyweave2.drone.guidance import derotate
from skyweave2.drone.harness.camera_sim import CameraSimConfig, Pose, SyntheticCamera
from skyweave2.drone.harness.targets import SlowKite, StaticBalloon, Trajectory
from skyweave2.drone.packets import Box, DetectionPacket, TrackPacket, TrackState
from skyweave2.drone.tracker import Tracker, TrackerConfig, id_base_from_start
from skyweave2.drone.vehicle_state import VehicleState
from tests.drone.fc_attitude import LINK, FcAttitude, RollPitchYaw

T, C, CO = TrackState.TENTATIVE, TrackState.CONFIRMED, TrackState.COASTING
FRAME_MS = 17
T0_MS = 1000
CAM = CameraModel()  # the E1 camera: c3_forward, f = 1000 px, no distortion
CX, CY = CAM.cx, CAM.cy


def _box(u: float, v: float, w: float = 40.0, h: float = 30.0) -> Box:
    return Box(x=u - w / 2.0, y=v - h / 2.0, w=w, h=h, conf=0.9)


def _det(i: int, *boxes: Box, t0: int = T0_MS) -> DetectionPacket:
    return DetectionPacket(t_cap=t0 + FRAME_MS * i, frame_seq=i, boxes=tuple(boxes))


def _feed(trk: Tracker, det: DetectionPacket) -> list[TrackPacket]:
    """One frame through the real tracker; every packet must survive the wire."""
    out = trk.update(det)
    for pkt in out:
        assert P.decode(P.PacketKind.TRACK, P.encode(pkt)) == pkt
    return out


def _script(
    trk: Tracker, pattern: str, t0: int = T0_MS, at: tuple[float, float] = (500.0, 400.0)
) -> list[list[TrackPacket]]:
    """One static target at ``at``: 'H' = detected this frame, 'M' = missed."""
    return [
        _feed(trk, _det(i, *((_box(*at),) if c == "H" else ()), t0=t0))
        for i, c in enumerate(pattern)
    ]


def _cfg(**kw) -> TrackerConfig:
    return TrackerConfig(id_base=0, **kw)


def _tracker(cfg: TrackerConfig | None = None) -> Tracker:
    """The real tracker on the E1 camera, on a level vehicle that does not
    turn (V-D10: the attitude at ``t_cap`` from real ATTITUDE frames)."""
    return Tracker(cfg or _cfg(), CAM, FcAttitude())


# ---------------------------------------------------------------------------
# [K2] M-of-N confirmation
# ---------------------------------------------------------------------------


def test_k_third_hit_inside_n_confirms() -> None:
    """[K2], [P2]: M = 3 of N = 5. Pattern H M M H H: the 3rd hit lands on frame
    5, inside the first 5 frames, so the track is tentative on frames 1-4 and
    confirmed on frame 5, one id throughout."""
    outs = _script(_tracker(), "HMMHH")
    assert all(len(o) == 1 for o in outs)
    assert [o[0].state for o in outs] == [T, T, T, T, C]
    assert {o[0].track_id for o in outs} == {1}
    assert [o[0].age_frames for o in outs] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("pattern", "dies_at"),
    [
        ("HHMMMH", 4),  # M just fails: 2 hits in the first 5 frames
        ("HMMHMH", 4),  # N just fails: the 3rd hit is frame 6, outside the first 5
    ],
    ids=["two_hits_in_n", "third_hit_after_n"],
)
def test_k_failed_confirmation_is_deleted_silently(pattern: str, dies_at: int) -> None:
    """[K2], [P2a]: a tentative track that cannot reach 3 hits within its first
    5 frames is deleted with NO final packet: frame 5 carries no packet at
    all, and its id never returns. The box on frame 6 is a new track (id 2,
    age 1). Discriminates both M (2 hits) and N (3rd hit on frame 6)."""
    outs = _script(_tracker(), pattern)
    assert all(o[0].track_id == 1 and o[0].state is T for o in outs[:dies_at])
    assert outs[dies_at] == []
    (born,) = outs[5]
    assert (born.track_id, born.state, born.age_frames, born.hits) == (2, T, 1, 1)


# ---------------------------------------------------------------------------
# [K3] / [P2a] coasting and death
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("coast_cap", [20, 0])
def test_k_coast_cap_final_packet_then_never_again(coast_cap: int) -> None:
    """[K3], [P2a]: a confirmed track coasts while misses <= coast_cap; at
    misses == coast_cap it is still alive (the next frame carries the id
    again). In the frame where misses reaches coast_cap + 1 it emits one final
    packet, state coasting, misses == coast_cap + 1, and is deleted: a box
    back at its spot afterwards is a NEW id. coast_cap 0 discriminates a cap
    hard-coded to the 20-frame default."""
    trk = _tracker(_cfg(coast_cap=coast_cap))
    outs = _script(trk, "HHH" + "M" * (coast_cap + 1) + "HH")
    coast = outs[3 : 3 + coast_cap]
    assert [o[0].misses for o in coast] == list(range(1, coast_cap + 1))
    assert all(len(o) == 1 and o[0].track_id == 1 and o[0].state is CO for o in coast)
    (final,) = outs[3 + coast_cap]
    assert (final.track_id, final.state, final.misses, final.hits) == (
        1,
        CO,
        coast_cap + 1,
        0,
    )
    after = [p for o in outs[4 + coast_cap :] for p in o]
    assert after and all(p.track_id != 1 for p in after)
    assert after[0].track_id == 2 and after[0].state is T


def test_k_coasting_rehit_returns_to_confirmed_same_id() -> None:
    """[K3], [P2]: H H H M M M H: the hit after 3 coasting frames returns the
    track to confirmed under the same id; no new track is spawned for it."""
    outs = _script(_tracker(), "HHHMMMH")
    assert [o[0].state for o in outs[3:6]] == [CO, CO, CO]
    (rehit,) = outs[6]
    assert (rehit.track_id, rehit.state) == (1, C)


def test_k_hits_and_misses_are_consecutive_counts() -> None:
    """[P2]: hits and misses are consecutive counts ending at this frame (0 on
    the other kind of frame), not totals; age_frames counts every frame from
    birth, birth frame counted."""
    outs = _script(_tracker(), "HMHHMMHH")
    pk = [o[0] for o in outs]
    assert [p.hits for p in pk] == [1, 0, 1, 2, 0, 0, 1, 2]
    assert [p.misses for p in pk] == [0, 1, 0, 0, 1, 2, 0, 0]
    assert [p.age_frames for p in pk] == list(range(1, 9))


# ---------------------------------------------------------------------------
# [K4] ids
# ---------------------------------------------------------------------------


def _churn(trk: Tracker, t0: int, n_frames: int, seed: int) -> list[list[TrackPacket]]:
    """One persistent target plus 3 seeded clutter boxes per frame (most die
    tentative), so the run spawns and kills many tracks."""
    rng = np.random.default_rng(seed)
    outs = []
    for i in range(n_frames):
        clutter = [_box(*rng.uniform((100.0, 100.0), (1800.0, 1100.0)), 20.0, 20.0) for _ in "abc"]
        outs.append(_feed(trk, _det(i, _box(960.0, 600.0), *clutter, t0=t0)))
    return outs


def _runs(outs: list[list[TrackPacket]]) -> dict[int, list[int]]:
    seen: dict[int, list[int]] = {}
    for i, o in enumerate(outs):
        for p in o:
            seen.setdefault(p.track_id, []).append(i)
    return seen


def test_k_ids_unique_across_restart_and_never_reused() -> None:
    """[K4], [P2]: ids start from a base derived from the tracker's start time:
    a run started at board ms 0 issues id 1 first (ids are >= 1), a restart
    at a later board ms starts at id_base_from_start(t) + 1. No id is shared
    between the two runs, and within a run an id, once gone, never comes
    back (each id's frames are one unbroken run)."""
    a = _churn(_tracker(TrackerConfig(id_base=id_base_from_start(0))), 0, 40, seed=7)
    restart_ms = 40 * FRAME_MS + 250
    b_base = id_base_from_start(restart_ms)
    b = _churn(_tracker(TrackerConfig(id_base=b_base)), restart_ms, 40, seed=8)
    ra, rb = _runs(a), _runs(b)
    assert len(ra) > 40 and len(rb) > 40  # the churn really spawned many tracks
    assert min(ra) == 1 and min(rb) == b_base + 1
    assert not set(ra) & set(rb)
    for runs in (ra, rb):
        for frames in runs.values():
            assert frames == list(range(frames[0], frames[-1] + 1))


# ---------------------------------------------------------------------------
# [K2] association
# ---------------------------------------------------------------------------


def test_k_association_of_two_nearby_boxes_is_deterministic() -> None:
    """[K2]: two targets 30 px apart (boxes overlap, cross IoU 0.14 > iou_min)
    moving together at 120 px/s with seeded 1 px noise. Each track stays on its
    own target for 60 frames, and listing the boxes in reversed order on every
    other frame gives byte-identical track output (box order matters only for
    spawn order and exact ties)."""
    rng = np.random.default_rng(3)
    truth, frames_fwd, frames_alt = [], [], []
    for i in range(60):
        ua = 500.0 + 120.0 * (FRAME_MS * i) / 1000.0
        a = _box(ua + rng.normal(0, 1.0), 400.0 + rng.normal(0, 1.0))
        b = _box(ua + 30.0 + rng.normal(0, 1.0), 400.0 + rng.normal(0, 1.0))
        truth.append((ua, ua + 30.0))
        frames_fwd.append(_det(i, a, b))
        frames_alt.append(_det(i, *((b, a) if i % 2 else (a, b))))
    trk_f, trk_a = _tracker(), _tracker()
    out_f = [_feed(trk_f, d) for d in frames_fwd]
    out_a = [_feed(trk_a, d) for d in frames_alt]
    assert [[P.encode(p) for p in o] for o in out_f] == [[P.encode(p) for p in o] for o in out_a]
    for (ua, ub), o in zip(truth, out_f, strict=True):
        assert [p.track_id for p in o] == [1, 2]
        assert abs(o[0].u - ua) < abs(o[0].u - ub)
        assert abs(o[1].u - ub) < abs(o[1].u - ua)


@pytest.mark.parametrize("left_first", [True, False])
def test_k_exact_iou_tie_goes_to_lower_track_id(left_first: bool) -> None:
    """[K2]: two tracks born touching (centers 20 px left and right of the
    image center, w = 40); the next frame has one box at the image center.
    The two tangent planes mirror each other about the optical axis (V-D10),
    so its IoU with each track is the same number, about 1/3. The tie goes to
    the lower track_id, whichever side it is on; the other track misses."""
    left, right = _box(CX - 20.0, CY), _box(CX + 20.0, CY)
    trk = _tracker()
    _feed(trk, _det(0, *((left, right) if left_first else (right, left))))
    t1, t2 = _feed(trk, _det(1, _box(CX, CY)))
    assert (t1.track_id, t1.hits, t1.misses) == (1, 2, 0)
    assert (t2.track_id, t2.hits, t2.misses) == (2, 0, 1)
    if left_first:
        assert CX - 20.0 < t1.u < CX
    else:
        assert CX < t1.u < CX + 20.0


@pytest.mark.parametrize(
    ("iou_scale", "associated"), [(1.01, True), (0.99, False)], ids=["just_above", "just_below"]
)
def test_k_iou_min_gates_association(iou_scale: float, associated: bool) -> None:
    """[K2], finding DT-9: iou_min discriminates. A static target confirmed over
    3 frames has du = dv = 0, so its predicted box is its last packet's box.
    The next frame's one box is that box slid along u until its IoU with it is
    1 % above iou_min (associated: one packet, same id 1, hits 4) or 1 % below
    (no association: track 1 misses and coasts, and the box is born as a new
    tentative id 2). Two equal w x h boxes offset by dx overlap with
    IoU = (w - dx) / (w + dx), so dx = w (1 - IoU) / (1 + IoU). The target
    sits at the image center: its tangent plane is the image plane there
    (V-D10), so a slide of dx px is a slide of dx in the plane."""
    trk = _tracker()
    (last,) = _script(trk, "HHH", at=(CX, CY))[-1]
    assert (last.track_id, last.state, last.du, last.dv) == (1, C, 0.0, 0.0)
    iou = trk.config.iou_min * iou_scale
    dx = last.w * (1.0 - iou) / (1.0 + iou)
    slid = Box(
        x=last.u - last.w / 2.0 + dx, y=last.v_px - last.h / 2.0, w=last.w, h=last.h, conf=0.9
    )
    out = [(p.track_id, p.state, p.hits, p.misses) for p in _feed(trk, _det(3, slid))]
    if associated:
        assert out == [(1, C, 4, 0)]
    else:
        assert out == [(1, CO, 0, 1), (2, T, 1, 0)]


# ---------------------------------------------------------------------------
# [P1] ordering, [K1] filter
# ---------------------------------------------------------------------------


def test_k_stale_t_cap_ignored_and_counted() -> None:
    """[P1], [K1]: a detection whose t_cap is not newer than the last accepted
    one (equal or older) returns no packets, changes nothing, and is counted
    in stale_dropped. The next fresh frame gives byte-identical output to a
    run that never saw the stale packets (and no track spawned from them)."""
    here = _box(500.0, 400.0)
    elsewhere = _box(1500.0, 900.0)
    trk, ctl = _tracker(), _tracker()
    for t in (1000, 1017):
        _feed(trk, DetectionPacket(t_cap=t, frame_seq=t, boxes=(here,)))
        _feed(ctl, DetectionPacket(t_cap=t, frame_seq=t, boxes=(here,)))
    assert _feed(trk, DetectionPacket(t_cap=1017, frame_seq=9, boxes=(elsewhere,))) == []
    assert _feed(trk, DetectionPacket(t_cap=990, frame_seq=10, boxes=(elsewhere,))) == []
    assert (trk.stale_dropped, ctl.stale_dropped) == (2, 0)
    nxt = DetectionPacket(t_cap=1034, frame_seq=11, boxes=(here,))
    got, want = _feed(trk, nxt), _feed(ctl, nxt)
    assert [P.encode(p) for p in got] == [P.encode(p) for p in want]
    assert [(p.track_id, p.age_frames, p.state) for p in got] == [(1, 3, C)]


def test_k_constant_pixel_velocity_estimated() -> None:
    """[K1]: a target moving at a constant (du, dv) = (300, -150) px/s, seen at
    60 fps (integer-ms t_cap) with seeded 2 px Gaussian center noise, default
    config. Stated tolerance: after 3 s the final du and dv are within 60 px/s
    of truth (about 2x the default filter's steady-state velocity std of
    30 px/s), and their mean over the last 1 s is within 5 px/s (an unbiased
    constant-velocity estimate). A position-only filter (du = 0) or a
    per-frame rate (px/frame) fails both by an order of magnitude."""
    rng = np.random.default_rng(20261008)
    trk = _tracker()
    est = []
    for i in range(180):
        t_cap = 10_000 + (i * 1000) // 60
        ts = (t_cap - 10_000) / 1000.0
        u = 600.0 + 300.0 * ts + rng.normal(0.0, 2.0)
        v = 700.0 - 150.0 * ts + rng.normal(0.0, 2.0)
        (pkt,) = _feed(trk, DetectionPacket(t_cap=t_cap, frame_seq=i, boxes=(_box(u, v),)))
        est.append((pkt.du, pkt.dv))
    final = est[-1]
    tail = np.mean(est[-60:], axis=0)
    assert abs(final[0] - 300.0) <= 60.0 and abs(final[1] + 150.0) <= 60.0
    assert abs(tail[0] - 300.0) <= 5.0 and abs(tail[1] + 150.0) <= 5.0


# ---------------------------------------------------------------------------
# [P2] per-frame output
# ---------------------------------------------------------------------------


def test_k_frame_output_sorted_shared_t_cap_one_per_live_track() -> None:
    """[P2], [P2a]: a seeded scene (3 moving targets with 20 % dropouts, plus
    2 clutter boxes per frame). Every frame's packets share its t_cap and are
    sorted by strictly increasing track_id (one packet per track). Every
    track's packets form one unbroken run of frames with age_frames 1, 2, ...
    (one packet per live track per frame), and a track that disappears before
    the last frame last showed either the [P2a] final packet or a tentative
    state (silent deletion)."""
    rng = np.random.default_rng(11)
    trk = _tracker()
    outs, dets = [], []
    for i in range(150):
        boxes = []
        for k in range(3):
            if rng.uniform() >= 0.2:
                boxes.append(_box(300.0 + 500.0 * k + 0.9 * i, 300.0 + 200.0 * k - 0.5 * i))
        for _ in range(2):
            boxes.append(_box(*rng.uniform((100.0, 900.0), (1800.0, 1100.0)), 16.0, 16.0))
        det = _det(i, *boxes)
        dets.append(det)
        outs.append(_feed(trk, det))
    for det, o in zip(dets, outs, strict=True):
        assert all(p.t_cap == det.t_cap for p in o)
        ids = [p.track_id for p in o]
        assert ids == sorted(set(ids))
    by_id: dict[int, list[tuple[int, TrackPacket]]] = {}
    for i, o in enumerate(outs):
        for p in o:
            by_id.setdefault(p.track_id, []).append((i, p))
    confirmed_seen = False
    for life in by_id.values():
        frames = [i for i, _ in life]
        assert frames == list(range(frames[0], frames[0] + len(frames)))
        assert [p.age_frames for _, p in life] == list(range(1, len(life) + 1))
        last_i, last = life[-1]
        confirmed_seen |= any(p.state is C for _, p in life)
        if last_i < len(outs) - 1:
            final = last.state is CO and last.misses == trk.config.coast_cap + 1
            assert final or last.state is T
    assert confirmed_seen


def test_k_shrinking_track_coasts_with_valid_size() -> None:
    """[P2], [K3]: a confirmed track whose box shrinks fast (w 30 -> 6 px over
    8 frames) then coasts the full cap would predict w <= 0 under a pure
    constant-rate size model; [P2] requires w, h > 0. Every packet still
    passes the wire encoder (see _feed), and the coasting width bottoms out at
    the configured min_size_px floor, so the case is really exercised."""
    trk = _tracker()
    outs = []
    for i in range(8):
        w = 30.0 - 3.0 * i
        outs.append(_feed(trk, _det(i, _box(500.0, 400.0, w, w))))
    for i in range(8, 8 + trk.config.coast_cap + 1):
        outs.append(_feed(trk, _det(i)))
    coast = [o[0] for o in outs[8:]]
    assert all(p.state is CO and p.w > 0.0 and p.h > 0.0 for p in coast)
    assert coast[-1].misses == trk.config.coast_cap + 1
    assert min(p.w for p in coast) == trk.config.min_size_px


# ---------------------------------------------------------------------------
# World angles (V-D10, E2a-4): [K1], [K2], [P2], [G1]
# ---------------------------------------------------------------------------

DEG = math.pi / 180.0
VEHICLE_NED = (0.0, 0.0, -10.0)  # the vehicle hovers 10 m above home
NOISE = CameraSimConfig(center_sigma_px=1.0, size_sigma_frac=0.03, dropout_p=0.02)
"""The harness's Provisional detector noise (``PROVISIONAL_NOISE``) without
its mount error: 1 px center jitter, 3 % size jitter, 2 % misses."""
CLEAN = CameraSimConfig()
SETTLE_MS = 500  # the filter's start-up, left out of the rate and angle bounds


def _render(
    cam: CameraModel,
    rpy: Callable[[int], RollPitchYaw],
    target: Trajectory,
    seconds: float,
    sim_cfg: CameraSimConfig = NOISE,
    seed: int = 20261128,
) -> list[DetectionPacket]:
    """Detection frames from the real synthetic camera (60 fps from ``T0_MS``):
    the vehicle hovers at ``VEHICLE_NED`` with attitude ``rpy(t)``."""
    sim = SyntheticCamera(sim_cfg, seed=seed, nominal=cam, t0_ms=T0_MS)
    dets = []
    k = 0
    while sim.capture_time(k) < T0_MS + round(seconds * 1000):
        roll, pitch, yaw = rpy(sim.capture_time(k))
        pose = Pose(pos_ned=VEHICLE_NED, roll=roll, pitch=pitch, yaw=yaw)
        dets.append(sim.capture(k, pose, [target]).packet)
        k += 1
    return dets


def _truth_los(target: Trajectory, t_ms: int) -> np.ndarray:
    rel = np.asarray(target.position(t_ms), dtype=float) - np.asarray(VEHICLE_NED)
    return rel / np.linalg.norm(rel)


def _angle_deg(a: tuple[float, float, float] | None, b: np.ndarray | tuple) -> float:
    assert a is not None
    return math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(a, b))))))


def _rate(pkt: TrackPacket) -> float:
    assert pkt.dlos is not None
    return math.sqrt(sum(c * c for c in pkt.dlos))


def _level(_t_ms: int) -> RollPitchYaw:
    return (0.0, 0.0, 0.0)


def _yaw_scan(t_ms: int) -> RollPitchYaw:
    """The search scan: yaw at ``search_yaw_rate`` (45 deg/s, §9) while the
    vehicle pitches +-10 deg with a 1.5 s period."""
    s = (t_ms - T0_MS) / 1000.0
    return (0.0, 10.0 * DEG * math.sin(2.0 * math.pi * s / 1.5), 45.0 * DEG * s)


def _yaw_sweep(t_ms: int) -> RollPitchYaw:
    """Yaw at 45 deg/s back and forth across +-30 deg (the forward camera
    cannot keep a target through a full turn), with the same pitching."""
    s = (t_ms - T0_MS) / 1000.0
    phase = (45.0 * s + 30.0) % 120.0
    yaw = (phase if phase <= 60.0 else 120.0 - phase) - 30.0
    return (0.0, 10.0 * DEG * math.sin(2.0 * math.pi * s / 1.5), yaw * DEG)


OVERHEAD = StaticBalloon(anchor_ned=(10.0 * math.tan(15.0 * DEG), 0.0, -20.0))
"""A balloon 10 m above the vehicle, 15 deg off the zenith (``cone_up`` view)."""
AHEAD = StaticBalloon(anchor_ned=(20.0, 0.0, -12.0))
"""A balloon 20 m north of the vehicle, 2 m above it (forward view)."""


@pytest.mark.parametrize(
    ("cam", "rpy", "target"),
    [(CONE_UP_HFOV100, _yaw_scan, OVERHEAD), (CAM, _yaw_sweep, AHEAD)],
    ids=["cone_up_full_scan", "c3_forward_sweep"],
)
def test_k_vd10_static_target_keeps_one_id_while_the_vehicle_yaws_and_pitches(
    cam: CameraModel, rpy: Callable[[int], RollPitchYaw], target: StaticBalloon
) -> None:
    """[K1], [K2], V-D10 (the E2a-4 fast gate): a static target seen for 3 s
    while the vehicle yaws at search_yaw_rate (45 deg/s) and pitches +-10 deg
    keeps one track id on every frame, with detector noise and 2 % misses.

    The vehicle's rotation is not in the track: after the first 0.5 s the
    line-of-sight rate stays under 0.2 rad/s (the vehicle turns at 0.79 rad/s
    or more; 0.2 rad/s is about 5 standard deviations of the filter's rate
    noise at 1 px detector noise) and the line of sight stays within 0.25 deg
    of the true one (the target and vehicle positions this test placed).

    Mutation check: the same frames with the attitude withheld (a source that
    says "level, not turning") give a line-of-sight rate over 0.4 rad/s, so
    the bound really tests the de-rotation."""
    dets = _render(cam, rpy, target, 3.0)
    assert len(dets) == 180
    trk = Tracker(_cfg(), cam, FcAttitude(rpy))
    outs = [_feed(trk, det) for det in dets]
    assert all(len(o) == 1 for o in outs)
    assert len({o[0].track_id for o in outs}) == 1
    assert outs[-1][0].state is C
    settled = [o[0] for o in outs if o[0].t_cap >= T0_MS + SETTLE_MS]
    assert max(_rate(p) for p in settled) < 0.2
    assert max(_angle_deg(p.los, _truth_los(target, p.t_cap)) for p in settled) < 0.25

    blind = Tracker(_cfg(), cam, FcAttitude())  # no de-rotation: level, not turning
    seen = [p for det in dets for p in _feed(blind, det) if p.t_cap >= T0_MS + SETTLE_MS]
    assert max(_rate(p) for p in seen) > 0.4


def test_k_vd10_track_crosses_the_zenith() -> None:
    """[K1], V-D10: the tracker filters in a tangent plane, not azimuth and
    elevation. With the ``cone_up`` camera a target 10 m above a level vehicle
    flies north at 2 m/s straight through the zenith, where azimuth jumps by
    180 deg. One track id on every frame; the line of sight stays within
    0.25 deg of truth after the first 0.5 s; and at the zenith its rate is
    the target's angular rate there, v / h = 0.2 rad/s northward (within
    0.05 rad/s), with no eastward part."""
    kite = SlowKite(
        start_ned=(-4.0, 0.0, -20.0), velocity_ned=(2.0, 0.0, 0.0), t_reverse_ms=10**9, t0_ms=T0_MS
    )
    dets = _render(CONE_UP_HFOV100, _level, kite, 4.0)
    trk = Tracker(_cfg(), CONE_UP_HFOV100, FcAttitude())
    outs = [_feed(trk, det) for det in dets]
    assert all(len(o) == 1 for o in outs)
    assert len({o[0].track_id for o in outs}) == 1
    settled = [o[0] for o in outs if o[0].t_cap >= T0_MS + SETTLE_MS]
    assert max(_angle_deg(p.los, _truth_los(kite, p.t_cap)) for p in settled) < 0.25
    zenith = min(settled, key=lambda p: abs(p.t_cap - (T0_MS + 2000)))
    assert zenith.dlos is not None
    assert zenith.dlos[0] == pytest.approx(0.2, abs=0.05)
    assert abs(zenith.dlos[1]) < 0.05


def test_k_vd10_tangent_plane_recenters_without_a_jump() -> None:
    """[K1], V-D10: a track that drifts far from its reference direction gets
    a new reference; the line of sight and its rate do not jump. A target
    crosses 40 deg of azimuth in 3 s in front of a level vehicle (noise-free
    boxes). The default tracker (re-center past 5 deg) and one that never
    re-centers on this path (45 deg) both keep one id. They are different
    computations (their packets differ), yet their lines of sight agree
    within 0.05 deg on every frame. For the default tracker the rate stays
    within 0.02 rad/s of the true rate (finite difference of the true line of
    sight, about 0.25 rad/s) after the first 0.5 s, through every
    re-centering: a re-centering that dropped or turned the rate would show
    as a step of that size."""
    kite = SlowKite(
        start_ned=(18.8, -6.84, -12.0),
        velocity_ned=(0.0, 4.56, 0.0),
        t_reverse_ms=10**9,
        t0_ms=T0_MS,
    )
    dets = _render(CAM, _level, kite, 3.0, CLEAN)
    near = Tracker(_cfg(), CAM, FcAttitude())
    far = Tracker(_cfg(recenter_deg=45.0), CAM, FcAttitude())
    out_near = [_feed(near, det) for det in dets]
    out_far = [_feed(far, det) for det in dets]
    for outs in (out_near, out_far):
        assert all(len(o) == 1 for o in outs) and len({o[0].track_id for o in outs}) == 1
    assert [P.encode(o[0]) for o in out_near] != [P.encode(o[0]) for o in out_far]
    for a, b in zip(out_near, out_far, strict=True):
        assert b[0].los is not None
        assert _angle_deg(a[0].los, b[0].los) < 0.05
    for (pkt,) in out_near:
        if pkt.t_cap < T0_MS + SETTLE_MS:
            continue
        true_rate = (_truth_los(kite, pkt.t_cap + 5) - _truth_los(kite, pkt.t_cap - 5)) / 0.010
        assert pkt.dlos is not None
        assert float(np.linalg.norm(np.asarray(pkt.dlos) - true_rate)) < 0.02


def test_k_vd10_attitude_degraded_frames_are_dropped_whole() -> None:
    """[K1], [G1], V-D10: a detection frame with no attitude sample within
    attitude_bound_ms (100 ms) of its t_cap is dropped whole: no packet, no
    change to any track, one count in degraded_dropped.

    The FC is silent from board ms 1500 to 2000 (its last frame is at 1480).
    A static target is seen at 60 fps throughout. Frames captured from 1600
    to 1960 have no sample within 100 ms when they arrive: each returns no
    packet. Frames up to 1560 and from 2000 on return one. After the gap the
    track goes on under the same id, confirmed, with hits and age_frames one
    more than before it: the dropped frames were never counted. A second
    tracker that is never shown the dropped frames gives byte-identical
    packets. Before any ATTITUDE at all, a frame is dropped the same way."""
    dets = [_det(i, _box(500.0, 400.0)) for i in range(90)]  # t_cap 1000 .. 2513
    trk = Tracker(_cfg(), CAM, FcAttitude(silent=((1500, 2000),)))
    outs = [_feed(trk, det) for det in dets]
    for det, out in zip(dets, outs, strict=True):
        if 1600 <= det.t_cap <= 1960:
            assert out == []
        elif det.t_cap <= 1560 or det.t_cap >= 2000:
            assert len(out) == 1
    empty = [i for i, out in enumerate(outs) if not out]
    assert empty == list(range(empty[0], empty[-1] + 1))  # one unbroken stretch
    assert trk.degraded_dropped == len(empty) >= 21 and trk.stale_dropped == 0
    (before,), (after,) = outs[empty[0] - 1], outs[empty[-1] + 1]
    assert (after.track_id, after.state) == (before.track_id, C)
    assert (after.hits, after.age_frames) == (before.hits + 1, before.age_frames + 1)

    ctl = Tracker(_cfg(), CAM, FcAttitude(silent=((1500, 2000),)))
    kept = [det for det, out in zip(dets, outs, strict=True) if out]
    want = [P.encode(p) for det in kept for p in _feed(ctl, det)]
    assert [P.encode(p) for out in outs for p in out] == want

    cold = Tracker(_cfg(), CAM, VehicleState(LINK))  # the FC was never heard
    assert _feed(cold, dets[0]) == []
    assert (cold.degraded_dropped, cold.last_t_cap) == (1, None)


@pytest.mark.parametrize(
    "cam",
    [CAM, CameraModel.named(dist=(-0.3, 0.0, 0.0, 0.0, 0.0))],
    ids=["no_distortion", "k1_placeholder"],
)
def test_k_vd10_packet_pixels_are_the_line_of_sight_at_t_cap(cam: CameraModel) -> None:
    """[P2], [G1], V-D10, V-D11: ``u``, ``v_px`` are the filtered line of sight
    projected back into the raw image with the attitude at ``t_cap``. So
    de-rotating them the [G1] way, with that same attitude sample, gives the
    packet's own line of sight: the two [G1] paths agree to 1e-9 on every
    packet, with and without lens distortion (k1 = -0.3, the E2a-2
    placeholder). A packet that carries a line of sight gives it to guidance
    as it is. The vehicle yaws and pitches, so the attitude matters."""

    def rpy(t_ms: int) -> RollPitchYaw:
        s = (t_ms - T0_MS) / 1000.0
        return (0.05 * math.sin(3.0 * s), 0.1 * math.sin(2.0 * s), 0.3 * math.sin(1.5 * s))

    fc = FcAttitude(rpy)
    trk = Tracker(_cfg(), cam, fc)
    n = 0
    for det in _render(cam, rpy, AHEAD, 2.0):
        for pkt in _feed(trk, det):
            att = fc.vehicle.attitude_at(pkt.t_cap)  # the sample the tracker just used
            assert att is not None and pkt.los is not None
            bare = dataclasses.replace(pkt, **dict.fromkeys(P.LOS_FIELDS))
            assert bare.los is None
            assert np.allclose(derotate(bare, cam, att), pkt.los, rtol=0.0, atol=1e-9)
            assert tuple(derotate(pkt, cam, att)) == pkt.los
            n += 1
    assert n > 100


def test_k_vd10_absurd_box_positions_never_break_the_tracker() -> None:
    """[P1], [C7], E1-D11, V-D10: the wire bounds a box position only to a
    finite number. A box centered absurdly far out of frame must not break
    the de-rotation: at 1e300 px it has no finite ray and is ignored
    (counted); at 1e100 px its ray lies along the image plane, 90 deg off the
    optical axis, and it becomes a track like any other box. A real box in
    the same frames is tracked as if the others were not there, and every
    packet passes the wire."""
    trk, ctl = _tracker(), _tracker()
    for i in range(6):
        wild = [
            Box(x=1e300, y=0.0, w=10.0, h=10.0, conf=0.5),
            Box(x=-1e100, y=5e99, w=10.0, h=10.0, conf=0.5),
            Box(x=1e100, y=0.0, w=10.0, h=10.0, conf=0.5),
        ]
        got = _feed(trk, _det(i, _box(500.0, 400.0), *wild))
        (want,) = _feed(ctl, _det(i, _box(500.0, 400.0)))
        assert got[0] == want
    assert trk.boxes_unusable == 6 and want.state is C


def test_k_vd10_pixels_hold_when_the_line_of_sight_has_no_image() -> None:
    """[P2], V-D10 (finding E2a4-F5): a coasting track's line of sight can
    leave the camera's forward half-space. It then has no pixel. ``u`` and
    ``v_px`` keep their last values and ``du``, ``dv`` are 0; the line of
    sight in the packet stays where the target is.

    A static target dead ahead is confirmed with the vehicle level, facing
    north. Then the boxes stop and the vehicle yaws right at 300 deg/s, so the
    target passes 90 deg off the camera axis after 0.3 s, inside the 20-frame
    coast. While it still has an image, ``u`` runs left, off the frame. Past
    90 deg (the test's own yaw script) the pixels freeze. Every packet still
    passes the wire, and every line of sight stays north within 1e-6."""
    t_turn = T0_MS + 10 * FRAME_MS

    def yaw_deg(t_ms: int) -> float:
        return max(0.0, 300.0 * (t_ms - t_turn) / 1000.0)

    trk = Tracker(_cfg(), CAM, FcAttitude(lambda t: (0.0, 0.0, yaw_deg(t) * DEG)))
    pkts = []
    for i in range(10 + 21):
        boxes = (_box(CX, CY),) if i < 10 else ()
        pkts.extend(_feed(trk, _det(i, *boxes)))
    assert len(pkts) == 31 and len({p.track_id for p in pkts}) == 1
    assert all(np.allclose(p.los, (1.0, 0.0, 0.0), atol=1e-6) for p in pkts)
    coast = pkts[10:]
    in_view = [p for p in coast if yaw_deg(p.t_cap) < 85.0]
    behind = [p for p in coast if yaw_deg(p.t_cap) > 95.0]
    assert len(in_view) >= 10 and len(behind) >= 2
    us = [p.u for p in in_view]
    assert us == sorted(us, reverse=True) and us[-1] < -1000.0
    frozen = {(p.u, p.v_px, p.du, p.dv) for p in behind}
    assert len(frozen) == 1
    (u, v, du, dv) = next(iter(frozen))
    assert (du, dv) == (0.0, 0.0) and u <= us[-1] and math.isfinite(v)
