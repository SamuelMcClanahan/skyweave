"""W7: delivered-event determinism, cross-process.

EXP-001's determinism contract is conditional and stays conditional here:
given the SAME DELIVERED-EVENT STREAM, decisions are byte-identical. A UDP rig
with three independent senders cannot promise a fixed interleaving, so the
delivered stream is reported explicitly and the claim is made against it
rather than around it.

Three separate things are checked, because passing one does not imply the
others:

1. the rig delivers the same SET of events every run (nothing is lost);
2. decisions computed in the ENGINE PROCESS are byte-identical across runs;
3. the delivered stream, re-run through fusion in a DIFFERENT process, gives
   byte-identical decisions — the actual cross-process claim.
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

from skyweave2.contracts import CameraModel
from skyweave2.faults.honesty import decisions_fingerprint
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream
from skyweave2.transport.config import TRANSPORT_CONFIG
from skyweave2.transport.loopback import (
    read_observations,
    run_loopback,
    socket_replay_inprocess,
    split_by_camera,
)
from skyweave2.transport.replay import PacingMode
from tests.transport.conftest import dump_observations
from tests.transport.test_w4_parity import synthetic_truth_object


def _fingerprint_in_child(args: tuple[str, str, str]) -> None:
    """Child entry point: fuse a delivered stream and write its fingerprint."""
    delivered_path, cameras_path, out_path = args
    observations = read_observations(Path(delivered_path))
    cameras = {
        int(key): CameraModel.model_validate(value)
        for key, value in json.loads(Path(cameras_path).read_text()).items()
    }
    run = run_stream(observations, cameras, FusionConfig(), session_uuid="w7-child")
    Path(out_path).write_text(decisions_fingerprint(run))


def test_the_rig_is_deterministic_across_two_runs(cameras, stream, tmp_path):
    truth = synthetic_truth_object()
    first = run_loopback(
        split_by_camera(stream), cameras, truth, "w7", tmp_path / "run1",
        mode=PacingMode.WALL_CLOCK, speed=TRANSPORT_CONFIG.rig_replay_speed,
        spawn_margin_s=1.0,
    )
    second = run_loopback(
        split_by_camera(stream), cameras, truth, "w7", tmp_path / "run2",
        mode=PacingMode.WALL_CLOCK, speed=TRANSPORT_CONFIG.rig_replay_speed,
        spawn_margin_s=1.0,
    )

    for result in (first, second):
        assert result.engine["rejected"] == {}
        assert result.engine["deadline_exceeded"] is False
        assert result.engine["observations"] == len(stream)

    # 1. Same set of delivered events, both runs. Order may differ (three
    #    independent senders); membership may not.
    first_events = sorted(json.loads(first.engine["delivered_fingerprint"]))
    second_events = sorted(json.loads(second.engine["delivered_fingerprint"]))
    assert first_events == second_events

    # 2. Same decisions, computed inside the engine process both times.
    assert (
        first.engine["decisions_fingerprint"]
        == second.engine["decisions_fingerprint"]
    )
    # The engine's own acceptance-relevant numbers must match too.
    assert first.engine["scene"] == second.engine["scene"]


def test_decisions_are_reproducible_in_a_different_process(cameras, stream, tmp_path):
    """The cross-process claim: same delivered stream, same decisions."""
    truth = synthetic_truth_object()
    result = run_loopback(
        split_by_camera(stream), cameras, truth, "w7-cross", tmp_path / "cross",
        mode=PacingMode.WALL_CLOCK, speed=TRANSPORT_CONFIG.rig_replay_speed,
        spawn_margin_s=1.0,
    )
    engine_fingerprint = result.engine["decisions_fingerprint"]

    # Re-run the SAME delivered stream in this process...
    parent = decisions_fingerprint(
        run_stream(result.delivered, cameras, FusionConfig(), session_uuid="w7-cross")
    )
    assert parent == engine_fingerprint

    # ...and in a third, freshly spawned one.
    out_path = tmp_path / "child_fingerprint.txt"
    context = mp.get_context("spawn")
    child = context.Process(
        target=_fingerprint_in_child,
        args=(
            (
                str(tmp_path / "cross" / "delivered.jsonl"),
                str(tmp_path / "cross" / "cameras.json"),
                str(out_path),
            ),
        ),
    )
    child.start()
    child.join(timeout=180)
    assert child.exitcode == 0
    child_fingerprint = out_path.read_text()
    assert child_fingerprint == decisions_fingerprint(
        run_stream(result.delivered, cameras, FusionConfig(), session_uuid="w7-child")
    )


def test_the_determinism_check_can_fail(cameras, stream, tmp_path):
    """Can-fail guard: a different delivered stream must fingerprint differently."""
    truth = synthetic_truth_object()
    result = run_loopback(
        split_by_camera(stream), cameras, truth, "w7-canfail", tmp_path / "canfail",
        mode=PacingMode.WALL_CLOCK, speed=TRANSPORT_CONFIG.rig_replay_speed,
        spawn_margin_s=1.0,
    )
    dropped = [
        o for o in result.delivered
        if not (o.envelope.camera_id == 1 and 40 <= o.envelope.frame_seq < 60)
    ]
    assert len(dropped) < len(result.delivered)
    assert decisions_fingerprint(
        run_stream(dropped, cameras, FusionConfig(), session_uuid="w7-canfail")
    ) != result.engine["decisions_fingerprint"]


def test_inprocess_socket_replay_is_bit_for_bit_repeatable(cameras, stream):
    """The single-source path has no interleaving freedom at all."""
    first = socket_replay_inprocess(stream)
    second = socket_replay_inprocess(stream)
    assert dump_observations(first.delivered) == dump_observations(second.delivered)
    assert first.adapter.delivered_fingerprint() == second.adapter.delivered_fingerprint()
    assert decisions_fingerprint(
        run_stream(first.delivered, cameras, FusionConfig(), session_uuid="w7-in")
    ) == decisions_fingerprint(
        run_stream(second.delivered, cameras, FusionConfig(), session_uuid="w7-in")
    )


def test_encoding_the_same_event_twice_gives_the_same_bytes(stream):
    """Byte-level determinism under the freeze, not just value equality."""
    from skyweave2.transport.codec import encode_observation_packet
    from skyweave2.transport.replay import capture_events

    events = capture_events(stream)
    first = [encode_observation_packet(e.envelope, list(e.observations)) for e in events]
    second = [encode_observation_packet(e.envelope, list(e.observations)) for e in events]
    assert first == second
