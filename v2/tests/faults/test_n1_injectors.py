"""N1: each injector produces EXACTLY its declared fault.

Every test here is a can-fail fixture: it asserts the fault is measurably
present in the output (an injector that silently did nothing fails), and
that the ledger it reports matches what it actually did (an injector that
lied about its plants fails).
"""

from __future__ import annotations

import numpy as np

from skyweave2.faults import injectors as inj
from skyweave2.fusion.aligner import obs_key
from tests.faults.conftest import FPS, clean_stream


def test_n1_false_blobs_are_added_and_ledgered(cameras):
    stream = clean_stream(cameras, frames=range(0, 30))
    faulted, ledger = inj.inject_false_blobs(stream, 0.5, seed=7, cameras=cameras)
    assert len(faulted) > len(stream), "no false blobs were added"
    # Plants must arrive in capture-time order, not appended after the clip
    # (appending made the aligner kill them all as "late").
    times = [o.envelope.capture_ts_ns for o in faulted]
    assert times == sorted(times), "planted blobs are not in arrival order"
    assert len(ledger.planted) == len(faulted) - len(stream)
    planted_keys = set(ledger.planted)
    original_keys = {obs_key(o) for o in stream}
    # Ledgered plants are genuinely NEW observations, not relabelled truth.
    assert planted_keys.isdisjoint(original_keys)
    # And every planted key really exists in the output.
    assert planted_keys <= {obs_key(o) for o in faulted}
    # Zero rate must plant nothing (can-fail in the other direction).
    none, empty = inj.inject_false_blobs(stream, 0.0, seed=7, cameras=cameras)
    assert len(none) == len(stream) and empty.planted == {}


def test_n1_deleted_detections_remove_the_declared_fraction(cameras):
    stream = clean_stream(cameras, frames=range(0, 60))
    faulted, ledger = inj.inject_deleted_detections(stream, 20.0, seed=7)
    removed = len(stream) - len(faulted)
    assert removed > 0, "nothing was deleted"
    assert removed == len(ledger.deleted)
    # ~20% within sampling tolerance.
    assert 0.10 <= removed / len(stream) <= 0.32
    # Deleted keys really are absent from the output.
    remaining = {obs_key(o) for o in faulted}
    assert remaining.isdisjoint(set(ledger.deleted))


def test_n1_split_blobs_produce_two_detections_astride_the_original(cameras):
    stream = clean_stream(cameras, frames=range(0, 40))
    faulted, ledger = inj.inject_split_blobs(stream, 100.0, seed=7, split_px=6.0)
    assert len(faulted) == 2 * len(stream), "every detection should split"
    assert len(ledger.planted) == len(faulted)
    by_frame = {}
    for obs in faulted:
        by_frame.setdefault((obs.envelope.camera_id, obs.envelope.frame_seq), []).append(obs)
    pair = next(iter(by_frame.values()))
    assert len(pair) == 2
    assert abs(abs(pair[0].u - pair[1].u) - 6.0) < 1e-9, "split separation wrong"
    original = next(o for o in stream
                    if o.envelope.camera_id == pair[0].envelope.camera_id
                    and o.envelope.frame_seq == pair[0].envelope.frame_seq)
    assert abs((pair[0].u + pair[1].u) / 2.0 - original.u) < 1e-9


def test_n1_merged_blobs_shift_the_centroid_and_grow_area(cameras):
    stream = clean_stream(cameras, frames=range(0, 40))
    faulted, ledger = inj.inject_merged_blobs(stream, 100.0, seed=7, merge_px=10.0)
    assert len(faulted) == len(stream)
    assert len(ledger.perturbed) == len(stream)
    for original, merged in zip(stream, faulted, strict=True):
        assert abs((merged.u - original.u) - 5.0) < 1e-9
        assert merged.area_px == original.area_px * 2


def test_n1_centroid_bias_constant_and_slow_varying(cameras):
    stream = clean_stream(cameras, frames=range(0, 150))
    const, ledger = inj.inject_centroid_bias(stream, 2.0, 0.0, FPS, 5.0)
    for original, biased in zip(stream, const, strict=True):
        assert abs((biased.u - original.u) - 2.0) < 1e-9
    assert len(ledger.perturbed) == len(stream)

    slow, _ = inj.inject_centroid_bias(stream, 0.0, 2.0, FPS, 5.0)
    shifts = np.array([b.u - o.u for o, b in zip(stream, slow, strict=True)])
    # A sinusoid of amplitude 2 px, period 5 s, over 5 s of frames.
    assert abs(shifts.max() - 2.0) < 0.15 and abs(shifts.min() + 2.0) < 0.15
    assert abs(float(shifts.mean())) < 0.2  # zero-mean by construction
    # Zero magnitudes plant nothing.
    none, empty = inj.inject_centroid_bias(stream, 0.0, 0.0, FPS, 5.0)
    assert empty.perturbed == {} and len(none) == len(stream)


def test_n1_camera_perturbation_moves_the_estimator_not_the_truth(cameras):
    faulted, _ = inj.perturb_cameras(cameras, seed=7, rotation_err_deg=1.0)
    for camera_id, camera in cameras.items():
        original = np.array(camera.t_world_cam)
        moved = np.array(faulted[camera_id].t_world_cam)
        # Rotation changed, position did not.
        assert not np.allclose(original[:3, :3], moved[:3, :3])
        assert np.allclose(original[:3, 3], moved[:3, 3])
        # The rotation magnitude is the declared one.
        r_rel = original[:3, :3].T @ moved[:3, :3]
        angle = np.degrees(np.arccos(np.clip((np.trace(r_rel) - 1) / 2, -1, 1)))
        assert abs(angle - 1.0) < 1e-6
    # Zero perturbation is a genuine no-op.
    same, _ = inj.perturb_cameras(cameras, seed=7)
    for camera_id in cameras:
        assert np.allclose(np.array(same[camera_id].t_world_cam),
                           np.array(cameras[camera_id].t_world_cam))


def test_n1_focal_and_principal_point_perturbations(cameras):
    faulted, _ = inj.perturb_cameras(cameras, seed=7, focal_err_pct=0.5,
                                     principal_point_err_px=2.0)
    for camera_id, camera in cameras.items():
        k0 = np.array(camera.k)
        k1 = np.array(faulted[camera_id].k)
        assert abs(k1[0, 0] / k0[0, 0] - 1.005) < 1e-9
        offset = np.hypot(k1[0, 2] - k0[0, 2], k1[1, 2] - k0[1, 2])
        assert abs(offset - 2.0) < 1e-6


def test_n1_rolling_shutter_shifts_capture_time_by_row(cameras):
    stream = clean_stream(cameras, frames=range(0, 30))
    faulted, ledger = inj.inject_rolling_shutter(stream, 30.0)
    assert len(ledger.perturbed) == len(stream)
    for original, shifted in zip(stream, faulted, strict=True):
        row_fraction = original.v / (original.envelope.full_height - 1)
        expected = int(round(row_fraction * 30.0 * 1e6))
        actual = shifted.envelope.capture_ts_ns - original.envelope.capture_ts_ns
        assert actual == expected
        assert shifted.envelope.line_readout_us > 0.0
    none, empty = inj.inject_rolling_shutter(stream, 0.0)
    assert empty.perturbed == {}
    assert all(a.envelope.capture_ts_ns == b.envelope.capture_ts_ns
               for a, b in zip(stream, none, strict=True))


def test_n1_clock_error_shifts_time_and_declares_it(cameras):
    stream = clean_stream(cameras, frames=range(0, 30))
    honest, ledger = inj.inject_clock_error(stream, 10.0, 0.0, 0.0, seed=7,
                                            honest=True, dishonest_claim_ms=0.5)
    for original, faulted in zip(stream, honest, strict=True):
        assert faulted.envelope.capture_ts_ns - original.envelope.capture_ts_ns == 10_000_000
        assert abs(faulted.envelope.time_sync_error_ms - 10.0) < 1e-6
    assert len(ledger.perturbed) == len(stream)

    dishonest, _ = inj.inject_clock_error(stream, 10.0, 0.0, 0.0, seed=7,
                                          honest=False, dishonest_claim_ms=0.5)
    for faulted in dishonest:
        assert abs(faulted.envelope.time_sync_error_ms - 0.5) < 1e-9
    # Same physical shift, different declaration: that is the honesty axis.
    assert [o.envelope.capture_ts_ns for o in honest] == \
           [o.envelope.capture_ts_ns for o in dishonest]


def test_n1_drift_and_jitter_are_time_dependent_and_random(cameras):
    stream = clean_stream(cameras, frames=range(0, 60))
    drifted, _ = inj.inject_clock_error(stream, 0.0, 100.0, 0.0, seed=7,
                                        honest=True, dishonest_claim_ms=0.5)
    shifts = [f.envelope.capture_ts_ns - o.envelope.capture_ts_ns
              for o, f in zip(stream, drifted, strict=True)]
    assert shifts[-1] > shifts[0], "drift must grow with time"
    jittered, _ = inj.inject_clock_error(stream, 0.0, 0.0, 1.0, seed=7,
                                         honest=True, dishonest_claim_ms=0.5)
    jshifts = np.array([f.envelope.capture_ts_ns - o.envelope.capture_ts_ns
                        for o, f in zip(stream, jittered, strict=True)], dtype=float)
    assert 0.4e6 < jshifts.std() < 2.0e6  # ~1 ms sigma


def test_n1_observation_loss_random_and_burst(cameras):
    stream = clean_stream(cameras, frames=range(0, 90))
    random_loss, r_ledger = inj.inject_observation_loss(stream, 20.0, "random", seed=7)
    assert len(r_ledger.deleted) == len(stream) - len(random_loss) > 0
    burst_loss, b_ledger = inj.inject_observation_loss(stream, 20.0, "burst", seed=7)
    assert len(b_ledger.deleted) > 0
    # Burst losses are contiguous: the dropped frames of one camera cluster.
    dropped_frames = sorted({int(k.split(":")[2]) for k in b_ledger.deleted
                             if k.startswith("0:")})
    if len(dropped_frames) > 2:
        gaps = np.diff(dropped_frames)
        assert (gaps == 1).sum() >= len(gaps) // 2, "burst losses not contiguous"


def test_n1_reorder_preserves_content_but_changes_arrival(cameras):
    stream = clean_stream(cameras, frames=range(0, 40))
    reordered, _ = inj.inject_reorder(stream, seed=7, window=12)
    assert len(reordered) == len(stream)
    assert {obs_key(o) for o in reordered} == {obs_key(o) for o in stream}
    assert [obs_key(o) for o in reordered] != [obs_key(o) for o in stream]
    # Event times are untouched by reorder (that is the point).
    assert sorted(o.envelope.capture_ts_ns for o in reordered) == \
           sorted(o.envelope.capture_ts_ns for o in stream)


def test_n1_duplication_and_lateness(cameras):
    stream = clean_stream(cameras, frames=range(0, 60))
    duped, d_ledger = inj.inject_duplication(stream, seed=7, pct=10.0)
    assert len(duped) > len(stream)
    # A verbatim duplicate SHARES the original's obs_key, so it must never
    # be booked as a plant (that would brand the true original false and
    # invert the false-accept rate). It is counted separately.
    assert len(duped) - len(stream) == len(d_ledger.duplicated)
    assert d_ledger.planted == {}
    assert set(d_ledger.duplicated) <= {obs_key(o) for o in stream}

    late, l_ledger = inj.inject_lateness(stream, seed=7, pct=10.0)
    assert len(late) == len(stream)
    # Delayed TRUE observations are perturbed, never planted.
    assert l_ledger.perturbed, "no observations were made late"
    assert l_ledger.planted == {}
    tail_keys = {obs_key(o) for o in late[-len(l_ledger.perturbed):]}
    assert tail_keys == set(l_ledger.perturbed)


def test_n1_node_restart_resets_identity_not_physics(cameras):
    stream = clean_stream(cameras, frames=range(0, 90))
    faulted, ledger = inj.inject_node_restart(stream, 1.0, FPS, camera_id=0)
    restart_frame = int(ledger.notes["restart_frame"])
    # Split on the ORIGINAL stream's integer frame ticks: comparing a float
    # seconds threshold against integer capture ticks puts the boundary
    # frame on the wrong side (30 * 33_333_333 ns < 1.0 s).
    restart_ts = min(o.envelope.capture_ts_ns for o in stream
                     if o.envelope.camera_id == 0
                     and o.envelope.frame_seq >= restart_frame)
    before = [o for o in faulted if o.envelope.camera_id == 0
              and o.envelope.capture_ts_ns < restart_ts]
    after = [o for o in faulted if o.envelope.camera_id == 0
             and o.envelope.capture_ts_ns >= restart_ts]
    assert before and after
    assert len({o.envelope.session_uuid for o in before}) == 1
    assert len({o.envelope.session_uuid for o in after}) == 1
    assert before[0].envelope.session_uuid != after[0].envelope.session_uuid
    assert min(o.envelope.frame_seq for o in after) == 0  # seq reset
    # Event times are NOT rewound.
    assert min(o.envelope.capture_ts_ns for o in after) > \
           max(o.envelope.capture_ts_ns for o in before)
    # Other cameras untouched.
    assert len({o.envelope.session_uuid for o in faulted
                if o.envelope.camera_id == 1}) == 1


def test_n1_camera_dropout_removes_only_the_named_camera(cameras):
    stream = clean_stream(cameras, frames=range(0, 90))
    faulted, ledger = inj.inject_camera_dropout(stream, camera_id=1, from_s=1.5, fps=FPS)
    cutoff = int(ledger.notes["dropout_from_frame"])
    assert not [o for o in faulted if o.envelope.camera_id == 1
                and o.envelope.frame_seq >= cutoff]
    assert [o for o in faulted if o.envelope.camera_id == 1
            and o.envelope.frame_seq < cutoff]
    for other in (0, 2):
        assert len([o for o in faulted if o.envelope.camera_id == other]) == \
               len([o for o in stream if o.envelope.camera_id == other])
    assert len(ledger.deleted) == len(stream) - len(faulted)


def test_n1_injectors_are_deterministic(cameras):
    stream = clean_stream(cameras, frames=range(0, 40))
    a, la = inj.inject_false_blobs(stream, 0.5, seed=7, cameras=cameras)
    b, lb = inj.inject_false_blobs(stream, 0.5, seed=7, cameras=cameras)
    assert [obs_key(o) for o in a] == [obs_key(o) for o in b]
    assert la.planted == lb.planted
    c, _ = inj.inject_false_blobs(stream, 0.5, seed=8, cameras=cameras)
    assert [obs_key(o) for o in a] != [obs_key(o) for o in c]
