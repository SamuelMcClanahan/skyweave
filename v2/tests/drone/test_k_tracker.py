"""K series: the Provisional tracker (DRONE_CONTRACTS_D0.md [K1]-[K4], [P2], [P2a]).

The tracker is a reimplementation of the brief §2 spec (tracker v0 source is
missing); v0's numbers are not inherited and nothing here asserts them.
Expected values come from the contract text and from synthetic truth (box
positions the test itself placed), never from a re-run of the filter math.
Real detection packets go through the real ``Tracker``, and every packet it
returns goes through the real wire encoder and decoder ([P2], [C5]).

Defaults under test (Provisional, brief §6): M = 3 of N = 5, coast_cap = 20.
"""

from __future__ import annotations

import numpy as np
import pytest

from skyweave2.drone import packets as P
from skyweave2.drone.packets import Box, DetectionPacket, TrackPacket, TrackState
from skyweave2.drone.tracker import Tracker, TrackerConfig, id_base_from_start

T, C, CO = TrackState.TENTATIVE, TrackState.CONFIRMED, TrackState.COASTING
FRAME_MS = 17
T0_MS = 1000


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


def _script(trk: Tracker, pattern: str, t0: int = T0_MS) -> list[list[TrackPacket]]:
    """One static target at (500, 400): 'H' = detected this frame, 'M' = missed."""
    return [
        _feed(trk, _det(i, *((_box(500.0, 400.0),) if c == "H" else ()), t0=t0))
        for i, c in enumerate(pattern)
    ]


def _cfg(**kw) -> TrackerConfig:
    return TrackerConfig(id_base=0, **kw)


# ---------------------------------------------------------------------------
# [K2] M-of-N confirmation
# ---------------------------------------------------------------------------


def test_k_third_hit_inside_n_confirms() -> None:
    """[K2], [P2]: M = 3 of N = 5. Pattern H M M H H: the 3rd hit lands on frame
    5, inside the first 5 frames, so the track is tentative on frames 1-4 and
    confirmed on frame 5, one id throughout."""
    outs = _script(Tracker(_cfg()), "HMMHH")
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
    outs = _script(Tracker(_cfg()), pattern)
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
    trk = Tracker(_cfg(coast_cap=coast_cap))
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
    outs = _script(Tracker(_cfg()), "HHHMMMH")
    assert [o[0].state for o in outs[3:6]] == [CO, CO, CO]
    (rehit,) = outs[6]
    assert (rehit.track_id, rehit.state) == (1, C)


def test_k_hits_and_misses_are_consecutive_counts() -> None:
    """[P2]: hits and misses are consecutive counts ending at this frame (0 on
    the other kind of frame), not totals; age_frames counts every frame from
    birth, birth frame counted."""
    outs = _script(Tracker(_cfg()), "HMHHMMHH")
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
    a = _churn(Tracker(TrackerConfig(id_base=id_base_from_start(0))), 0, 40, seed=7)
    restart_ms = 40 * FRAME_MS + 250
    b_base = id_base_from_start(restart_ms)
    b = _churn(Tracker(TrackerConfig(id_base=b_base)), restart_ms, 40, seed=8)
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
    trk_f, trk_a = Tracker(_cfg()), Tracker(_cfg())
    out_f = [_feed(trk_f, d) for d in frames_fwd]
    out_a = [_feed(trk_a, d) for d in frames_alt]
    assert [[P.encode(p) for p in o] for o in out_f] == [[P.encode(p) for p in o] for o in out_a]
    for (ua, ub), o in zip(truth, out_f, strict=True):
        assert [p.track_id for p in o] == [1, 2]
        assert abs(o[0].u - ua) < abs(o[0].u - ub)
        assert abs(o[1].u - ub) < abs(o[1].u - ua)


@pytest.mark.parametrize("left_first", [True, False])
def test_k_exact_iou_tie_goes_to_lower_track_id(left_first: bool) -> None:
    """[K2]: two tracks born touching (centers u = 100 and 140, w = 40); the next
    frame has one box centered at u = 120, IoU exactly 1/3 with each. The tie
    goes to the lower track_id, whichever side it is on; the other track
    misses."""
    left, right = _box(100.0, 400.0), _box(140.0, 400.0)
    trk = Tracker(_cfg())
    _feed(trk, _det(0, *((left, right) if left_first else (right, left))))
    t1, t2 = _feed(trk, _det(1, _box(120.0, 400.0)))
    assert (t1.track_id, t1.hits, t1.misses) == (1, 2, 0)
    assert (t2.track_id, t2.hits, t2.misses) == (2, 0, 1)
    if left_first:
        assert 100.0 < t1.u < 120.0
    else:
        assert 120.0 < t1.u < 140.0


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
    trk, ctl = Tracker(_cfg()), Tracker(_cfg())
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
    trk = Tracker(_cfg())
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
    trk = Tracker(_cfg())
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
    trk = Tracker(_cfg())
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
