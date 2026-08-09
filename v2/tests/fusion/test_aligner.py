"""F1: event-time batching, reorder handling, lateness, session anomalies."""

from __future__ import annotations

from skyweave2.fusion.aligner import EventAligner
from tests.fusion.conftest import FRAME_NS, make_observation


def test_f1_batches_by_capture_time_not_arrival(cameras, config):
    aligner = EventAligner(config)
    cam0, cam1 = cameras[0], cameras[1]
    # Arrival order deliberately scrambled across frames and cameras.
    arrivals = [
        make_observation(cam0, 100.0, 100.0, frame_seq=2),
        make_observation(cam1, 200.0, 200.0, frame_seq=1),
        make_observation(cam0, 100.0, 100.0, frame_seq=1),
        make_observation(cam1, 200.0, 200.0, frame_seq=2),
        make_observation(cam0, 100.0, 100.0, frame_seq=3),
    ]
    for obs in arrivals:
        aligner.offer(obs)
    batches = aligner.ready_batches() + aligner.flush()
    indices = [b.batch_index for b in batches]
    assert indices == sorted(indices)
    by_index = {b.batch_index: b for b in batches}
    assert len(by_index[1].observations) == 2  # both cameras' frame 1 together
    assert len(by_index[2].observations) == 2
    assert len(by_index[3].observations) == 1
    assert aligner.excluded == []


def test_f1_late_observation_recorded_never_solved(cameras, config):
    aligner = EventAligner(config)
    cam0 = cameras[0]
    aligner.offer(make_observation(cam0, 1.0, 1.0, frame_seq=30))
    # A frame-2 observation arriving while the stream is at frame 30 is far
    # beyond the lateness window (100 ms): excluded with a reason.
    late = make_observation(cam0, 2.0, 2.0, frame_seq=2, session="lagging-session")
    aligner.offer(late)
    batches = aligner.ready_batches() + aligner.flush()
    solved = [o for b in batches for o in b.observations]
    assert late not in solved
    assert [e.reason for e in aligner.excluded] == ["late"]
    assert aligner.excluded[0].obs is late


def test_f1_emitted_batch_never_reopened(cameras, config):
    """The reopen guard specifically: the straggler is INSIDE the lateness
    window (so the lateness gate alone cannot catch it — deleting the
    reopen guard makes this test fail by re-emitting batch 1)."""
    aligner = EventAligner(config)
    cam0, cam1 = cameras[0], cameras[1]
    aligner.offer(make_observation(cam0, 1.0, 1.0, frame_seq=1))
    aligner.offer(make_observation(cam0, 1.0, 1.0, frame_seq=4))
    emitted = aligner.ready_batches()
    assert any(b.batch_index == 1 for b in emitted)
    straggler_ts = 1 * FRAME_NS + FRAME_NS // 2  # inside batch 1's window
    assert straggler_ts >= 4 * FRAME_NS - config.aligner.lateness_ns, (
        "fixture broken: straggler must NOT be late by the lateness gate"
    )
    straggler = make_observation(cam1, 2.0, 2.0, frame_seq=1, ts_ns=straggler_ts)
    aligner.offer(straggler)
    rest = aligner.ready_batches() + aligner.flush()
    assert all(b.batch_index != 1 for b in rest)
    assert [e.reason for e in aligner.excluded] == ["late"]
    assert aligner.excluded[0].obs is straggler


def test_f1_session_anomalies_quarantined(cameras, config):
    aligner = EventAligner(config)
    cam0 = cameras[0]
    aligner.offer(make_observation(cam0, 1.0, 1.0, frame_seq=5))
    duplicate = make_observation(cam0, 1.0, 1.0, frame_seq=5)
    aligner.offer(duplicate)
    regression = make_observation(cam0, 1.0, 1.0, frame_seq=3)
    aligner.offer(regression)
    # A reboot (new session uuid, sequence reset) is accepted cleanly.
    reboot = make_observation(cam0, 1.0, 1.0, frame_seq=0, session="rebooted-node",
                              ts_ns=6 * FRAME_NS)
    aligner.offer(reboot)
    batches = aligner.ready_batches() + aligner.flush()
    solved = [o for b in batches for o in b.observations]
    # Exactly ONE copy of frame 5 is solved (identity check: pydantic value
    # equality would hide a duplicate slipping through).
    frame5 = [o for o in solved if o.envelope.frame_seq == 5
              and o.envelope.session_uuid != "rebooted-node"]
    assert len(frame5) == 1
    # An unseen lower sequence is transport REORDER (§6): solved, not lost.
    assert any(o.envelope.frame_seq == 3 for o in solved)
    assert any(o.envelope.session_uuid == "rebooted-node" for o in solved)
    assert [e.reason for e in aligner.excluded] == ["stale_or_duplicate"]
    assert aligner.excluded[0].obs is duplicate
    assert regression is not None  # fixture sanity
