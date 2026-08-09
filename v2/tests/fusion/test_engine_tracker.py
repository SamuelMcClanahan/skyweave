"""F5 (replay determinism), F6 (NIS gate), F7 (lifecycle), F8 (confirmation)."""

from __future__ import annotations

import json

import numpy as np

from skyweave2.contracts import TrackState
from skyweave2.fusion.engine import run_stream
from skyweave2.fusion.tracker import Tracker
from tests.fusion.conftest import observe_point, target_position


def _stream(cameras, frames, camera_ids=None, noise_px=0.1, seed=11, offset=None):
    rng = np.random.default_rng(seed)
    out = []
    for frame in frames:
        point = target_position(frame)
        if offset is not None:
            point = point + offset(frame)
        out += observe_point(cameras, point, frame_seq=frame,
                             camera_ids=camera_ids, noise_px=noise_px, rng=rng)
    return out


def _serialize(run) -> str:
    return json.dumps(
        {
            "published": [
                (i, t.model_dump(mode="json")) for i, t in run.published
            ],
            "records": [r.model_dump(mode="json") for r in run.records],
            "statuses": run.statuses,
        },
        sort_keys=True,
    )


def test_f5_replay_is_byte_identical(cameras, config):
    stream = _stream(cameras, range(200, 240))
    run_a = run_stream(list(stream), cameras, config, session_uuid="f5")
    run_b = run_stream(list(stream), cameras, config, session_uuid="f5")
    assert _serialize(run_a) == _serialize(run_b)
    assert run_a.published, "nothing was published; determinism test is vacuous"


def test_f6_implausible_innovation_never_pulls_the_state(cameras, config):
    frames = list(range(200, 215))
    # One batch mid-stream displaced 8 m along the track: inside the 12 m
    # association gate, far outside any plausible innovation.
    def offset(frame):
        return np.array([0.0, 0.0, 8.0]) if frame == 210 else np.zeros(3)

    stream = _stream(cameras, frames, noise_px=0.05, offset=offset)
    run = run_stream(stream, cameras, config, session_uuid="f6")
    rejected = [r for r in run.records if not r.accepted]
    assert len(rejected) == 1
    assert rejected[0].nis is not None and rejected[0].nis > config.tracker.nis_gate
    assert rejected[0].obs_ids, "gated update must still record consumed obs_ids"
    # The published state right after the gated batch stays on the true
    # trajectory: the implausible measurement did not pull it.
    for batch_index, track in run.published:
        frame = batch_index  # batch period == frame period in fixtures
        if 210 <= frame <= 212:
            truth = target_position(frame)
            error = np.linalg.norm(np.asarray(track.state[0:3]) - truth)
            assert error < 2.0, f"state pulled to {track.state[0:3]} at frame {frame}"


def test_f7_lifecycle_all_transitions(cameras, config):
    tracker = Tracker(config, session_uuid="f7")
    # Confirm quickly (3-camera births), then starve to coast, feed again to
    # reacquire, then starve to deletion.
    frames_a = list(range(200, 205))
    stream_a = _stream(cameras, frames_a, noise_px=0.05)
    run_stream(stream_a, cameras, config, session_uuid="f7", tracker=tracker)
    (track_id, status), = tracker.statuses().items()
    assert status == TrackState.CONFIRMED

    gap = config.tracker.coast_after_misses
    frames_b = list(range(205 + gap, 205 + gap + 2))  # a gap, then two hits
    stream_b = _stream(cameras, frames_b, noise_px=0.05)
    run_b = run_stream(stream_b, cameras, config, session_uuid="f7", tracker=tracker)
    statuses_seen = [t.status for _, t in run_b.published]
    # The silent gap coasts the track; the first 3-camera hit REACQUIRES it
    # (exactly — a disjunction here hid the transition from mutation).
    assert statuses_seen[0] == TrackState.REACQUIRED
    assert TrackState.CONFIRMED in statuses_seen[1:]
    assert tracker.statuses()[track_id] == TrackState.CONFIRMED

    # Starvation through coast to deletion, using empty batches via a lone
    # far-away decoy stream that cannot associate to the track.
    frames_c = list(range(260, 260 + gap + config.tracker.delete_after_coast + 2))
    decoy_stream = _stream(cameras, frames_c, noise_px=0.05,
                           offset=lambda f: np.array([150.0, -100.0, 30.0]))
    run_stream(decoy_stream, cameras, config, session_uuid="f7", tracker=tracker)
    assert tracker.statuses()[track_id] == TrackState.DELETED


def test_f7_tentative_dies_without_persistence(cameras, config):
    tracker = Tracker(config, session_uuid="f7b")
    # A single 2-camera flash, then silence: tentative must die, never confirm.
    stream = _stream(cameras, [200], camera_ids=(0, 1), noise_px=0.05)
    stream += _stream(cameras, range(203, 203 + config.tracker.coast_after_misses + 1),
                      camera_ids=(0, 1), noise_px=0.05,
                      offset=lambda f: np.array([150.0, -100.0, 30.0]))
    run = run_stream(stream, cameras, config, session_uuid="f7b", tracker=tracker)
    flash_tracks = [tid for tid, s in tracker.statuses().items() if s == TrackState.DELETED]
    assert flash_tracks, "the unpersistent tentative should be deleted"
    published_ids = {t.track_id for _, t in run.published}
    assert set(flash_tracks) & published_ids == set()


def test_f7_suppressed_consumes_and_blocks_rebirth(cameras, config):
    tracker = Tracker(config, session_uuid="f7c")
    stream_a = _stream(cameras, range(200, 204), noise_px=0.05)
    run_stream(stream_a, cameras, config, session_uuid="f7c", tracker=tracker)
    (track_id, status), = tracker.statuses().items()
    assert status == TrackState.CONFIRMED
    tracker.suppress(track_id)

    records_before = len(tracker.records)
    stream_b = _stream(cameras, range(204, 210), noise_px=0.05)
    run_b = run_stream(stream_b, cameras, config, session_uuid="f7c", tracker=tracker)
    # Publishes nothing...
    assert run_b.published == []
    # ...but keeps consuming its observations (audit records exist; the
    # shared tracker's record list is cumulative, so count only the new ones).
    consumed = [r for r in tracker.records[records_before:] if r.track_id == track_id]
    assert len(consumed) == 6
    # ...and no new track was born from those observations.
    assert set(tracker.statuses()) == {track_id}


def test_f8_three_camera_route_confirms_in_one_batch(cameras, config):
    tracker = Tracker(config, session_uuid="f8a")
    stream = _stream(cameras, [200], noise_px=0.05)
    run = run_stream(stream, cameras, config, session_uuid="f8a", tracker=tracker)
    assert [t.status for _, t in run.published] == [TrackState.CONFIRMED]


def test_f8_two_camera_route_needs_the_full_streak(cameras, config):
    needed = config.tracker.confirm_two_cam_batches
    # One batch short of the streak: must NOT confirm (the gate can fail).
    tracker_short = Tracker(config, session_uuid="f8b")
    stream_short = _stream(cameras, range(200, 200 + needed - 1), camera_ids=(0, 1),
                           noise_px=0.05)
    run_short = run_stream(stream_short, cameras, config, session_uuid="f8b",
                           tracker=tracker_short)
    assert run_short.published == []
    assert all(s == TrackState.TENTATIVE for s in tracker_short.statuses().values())

    # The full streak confirms.
    tracker_full = Tracker(config, session_uuid="f8c")
    stream_full = _stream(cameras, range(200, 200 + needed), camera_ids=(0, 1),
                          noise_px=0.05)
    run_full = run_stream(stream_full, cameras, config, session_uuid="f8c",
                          tracker=tracker_full)
    assert any(t.status == TrackState.CONFIRMED for _, t in run_full.published)


def test_f8_broken_streak_resets(cameras, config):
    needed = config.tracker.confirm_two_cam_batches
    tracker = Tracker(config, session_uuid="f8d")
    # needed-1 hits, one miss, needed-1 hits: never confirms.
    frames = list(range(200, 200 + needed - 1)) \
        + list(range(200 + needed, 200 + 2 * needed - 1))
    stream = _stream(cameras, frames, camera_ids=(0, 1), noise_px=0.05)
    run = run_stream(stream, cameras, config, session_uuid="f8d", tracker=tracker)
    assert run.published == []
