"""W4: file replay and socket replay produce the IDENTICAL acceptance table.

The table compared here is the real one — ``fusion.report._acceptance_rows``
rendered against the frozen manifest's acceptance block, the same function
that writes the D5 report. Comparing a hand-rolled summary instead would let
the two paths differ in exactly the places the table does not look.

Two socket paths, because they answer different questions:

- ``socket_replay_inprocess``: one sender pushing the ALREADY-MERGED arrival
  order through real UDP. Any difference can only come from the wire, the
  codec, or the adapter, so this is the sharp test.
- the three-process loopback rig: three independent senders, real process
  boundaries, the D9 topology minus boards. This additionally proves that
  multi-source interleaving does not move the table.

The file-replay side is the ORIGINAL recorded stream, not a wire-normalised
copy. If the D0-declared ``float`` narrowing were ever large enough to move a
decision, this test would fail — which is the point.
"""

from __future__ import annotations

import numpy as np

from skyweave2.faults.honesty import decisions_fingerprint
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream
from skyweave2.fusion.metrics import Truth, evaluate_scene
from skyweave2.fusion.report import _acceptance_rows
from skyweave2.transport.config import TRANSPORT_CONFIG
from skyweave2.transport.loopback import (
    run_loopback,
    socket_replay_inprocess,
    split_by_camera,
)
from skyweave2.transport.replay import PacingMode
from tests.fusion.conftest import target_position
from tests.transport.conftest import as_wire, dump_observations, requires_gate_clip

SYNTHETIC_FRAMES = range(0, 120)


def synthetic_truth_object() -> Truth:
    positions = {f: target_position(f) for f in SYNTHETIC_FRAMES}
    return Truth(
        positions=positions,
        velocity=np.array([30.0, 0.0, 0.0]),
        entry_s=0.0,
        fps=30.0,
    )


def render_acceptance_table(scene_eval, acceptance: dict) -> str:
    """The acceptance table exactly as the report renders it."""
    rows = _acceptance_rows(scene_eval, acceptance)
    return "\n".join(
        f"| {name} | {gate} | {value} | {'PASS' if ok else 'FAIL'} |"
        for name, gate, value, ok in rows
    )


def score(observations, cameras, truth, acceptance, scene="w4"):
    config = FusionConfig()
    evaluation = evaluate_scene(
        scene, observations, cameras, truth, config,
        velocity_convergence_window_batches=int(
            acceptance["velocity_convergence_window_batches"]
        ),
        velocity_gate_mps=float(acceptance["velocity_rmse_max_mps"]),
    )
    return render_acceptance_table(evaluation, acceptance), evaluation


# ---------------------------------------------------------------------------
# Synthetic stream — always runs, so W4 can never pass by being skipped
# ---------------------------------------------------------------------------


def test_inprocess_socket_replay_matches_file_replay(cameras, stream,
                                                     acceptance_manifest):
    truth = synthetic_truth_object()
    file_table, file_eval = score(stream, cameras, truth, acceptance_manifest)

    result = socket_replay_inprocess(stream)
    assert result.encode_failures == []
    assert result.adapter.stats.rejected == {}
    assert result.adapter.stats.observations == len(stream)
    socket_table, socket_eval = score(
        result.delivered, cameras, truth, acceptance_manifest
    )

    assert socket_table == file_table
    assert socket_eval.published_samples == file_eval.published_samples
    assert socket_eval.batches == file_eval.batches
    assert socket_eval.final_statuses == file_eval.final_statuses
    assert socket_eval.rejections == file_eval.rejections
    assert socket_eval.excluded == file_eval.excluded


def test_inprocess_socket_replay_matches_file_replay_decision_for_decision(
    cameras, stream
):
    """Stronger than the table: every published state, record and rejection."""
    config = FusionConfig()
    result = socket_replay_inprocess(stream)
    file_fingerprint = decisions_fingerprint(
        run_stream(stream, cameras, config, session_uuid="w4")
    )
    socket_fingerprint = decisions_fingerprint(
        run_stream(result.delivered, cameras, config, session_uuid="w4")
    )
    assert socket_fingerprint == file_fingerprint


def test_the_parity_test_can_fail(cameras, stream):
    """Can-fail guard: perturb one delivered centroid, parity must break."""
    config = FusionConfig()
    result = socket_replay_inprocess(stream)
    tampered = list(result.delivered)
    tampered[len(tampered) // 2] = tampered[len(tampered) // 2].model_copy(
        update={"u": tampered[len(tampered) // 2].u + 5.0}
    )
    baseline = decisions_fingerprint(
        run_stream(stream, cameras, config, session_uuid="w4")
    )
    perturbed = decisions_fingerprint(
        run_stream(tampered, cameras, config, session_uuid="w4")
    )
    assert perturbed != baseline


def test_the_delivered_stream_is_the_wire_representable_input(cameras, stream):
    """Exactly what changed on the wire, and nothing else."""
    result = socket_replay_inprocess(stream)
    assert dump_observations(result.delivered) == dump_observations(as_wire(stream))


def test_three_process_rig_matches_file_replay(cameras, stream, acceptance_manifest,
                                               tmp_path):
    truth = synthetic_truth_object()
    file_table, file_eval = score(stream, cameras, truth, acceptance_manifest)

    result = run_loopback(
        split_by_camera(stream), cameras, truth, "w4-rig", tmp_path / "rig",
        mode=PacingMode.WALL_CLOCK, speed=TRANSPORT_CONFIG.rig_replay_speed,
        velocity_convergence_window_batches=int(
            acceptance_manifest["velocity_convergence_window_batches"]
        ),
        velocity_gate_mps=float(acceptance_manifest["velocity_rmse_max_mps"]),
        spawn_margin_s=1.0,
    )
    engine = result.engine
    assert engine["rejected"] == {}, engine["rejected"]
    assert engine["deadline_exceeded"] is False
    assert result.encode_failures == []
    assert engine["observations"] == len(stream), (
        "the rig lost measurements on loopback"
    )
    assert engine["scene"]["excluded"] == {}, (
        "the aligner excluded observations the file path kept — pacing or "
        "epoch skew, not a codec problem"
    )

    rig_table, _ = score(result.delivered, cameras, truth, acceptance_manifest)
    assert rig_table == file_table
    assert engine["scene"]["published_samples"] == file_eval.published_samples


# ---------------------------------------------------------------------------
# The real golden gate clip
# ---------------------------------------------------------------------------


@requires_gate_clip
def test_gate_clip_socket_replay_matches_file_replay(
    gate_observations, gate_cameras_fixture, gate_truth, acceptance_manifest
):
    file_table, file_eval = score(
        gate_observations, gate_cameras_fixture, gate_truth, acceptance_manifest,
        scene="gate",
    )
    result = socket_replay_inprocess(gate_observations)
    assert result.encode_failures == [], (
        "a gate-clip capture event could not be sent under the frozen bounds"
    )
    assert result.adapter.stats.rejected == {}
    assert result.adapter.stats.observations == len(gate_observations)

    socket_table, socket_eval = score(
        result.delivered, gate_cameras_fixture, gate_truth, acceptance_manifest,
        scene="gate",
    )
    assert socket_table == file_table
    assert socket_eval.published_samples == file_eval.published_samples
    assert socket_eval.excluded == file_eval.excluded == {}


@requires_gate_clip
def test_gate_clip_datagrams_are_far_under_the_ceiling(gate_observations):
    """The measured distribution, asserted rather than only reported."""
    from skyweave2.transport.report import datagram_sizes
    from skyweave2.transport.wire import DATAGRAM_CEILING_BYTES

    result = socket_replay_inprocess(gate_observations)
    sizes = result.adapter.stats.sizes
    assert sizes and len(sizes) == len(result.adapter.receipts)
    # Cross-check the RECEIVED byte accounting against the independent encoder:
    # asserting only "<= ceiling" left `sizes.append(1)` passing, so nothing
    # tied the recorded number to the datagram that actually arrived.
    assert sizes == datagram_sizes(gate_observations)
    # And "far under" has to mean something: half the ceiling, not just under it.
    assert max(sizes) <= DATAGRAM_CEILING_BYTES // 2


@requires_gate_clip
def test_gate_clip_decisions_match_decision_for_decision(
    gate_observations, gate_cameras_fixture
):
    config = FusionConfig()
    result = socket_replay_inprocess(gate_observations)
    assert decisions_fingerprint(
        run_stream(result.delivered, gate_cameras_fixture, config, session_uuid="w4-gate")
    ) == decisions_fingerprint(
        run_stream(gate_observations, gate_cameras_fixture, config,
                   session_uuid="w4-gate")
    )


# ---------------------------------------------------------------------------
# The pacing finding, pinned as a test rather than left as prose
# ---------------------------------------------------------------------------


def test_unpaced_multiprocess_replay_is_not_a_valid_multi_camera_mode(
    cameras, stream, tmp_path
):
    """DETERMINISTIC pacing across independent processes breaks alignment.

    Three unpaced senders race: one can transmit its whole clip before another
    starts, so the aligner sees the trailing cameras' capture times far behind
    the newest and correctly rejects them as LATE. The engine is not at fault
    and nothing here is tuned to hide it — the mode is simply single-source
    only, and the rig uses WALL_CLOCK with a shared epoch.

    Pinned as a test so the constraint cannot be quietly forgotten.
    """
    truth = synthetic_truth_object()
    result = run_loopback(
        split_by_camera(stream), cameras, truth, "w4-unpaced", tmp_path / "unpaced",
        mode=PacingMode.DETERMINISTIC, spawn_margin_s=0.5,
    )
    engine = result.engine
    assert engine["observations"] == len(stream), "datagrams were lost, not excluded"
    excluded = engine["scene"]["excluded"]
    assert excluded.get("late", 0) > 0, (
        "unpaced multi-process replay unexpectedly aligned cleanly; if this "
        "holds on other machines the finding needs restating, not deleting"
    )


def test_wall_clock_rig_keeps_every_camera_inside_the_lateness_window(
    cameras, stream, tmp_path
):
    """The counterpart: with a shared epoch, nothing is late."""
    truth = synthetic_truth_object()
    result = run_loopback(
        split_by_camera(stream), cameras, truth, "w4-paced", tmp_path / "paced",
        mode=PacingMode.WALL_CLOCK, speed=TRANSPORT_CONFIG.rig_replay_speed,
        spawn_margin_s=1.0,
    )
    assert result.engine["scene"]["excluded"] == {}
    assert result.engine["observations"] == len(stream)


def test_the_rig_replay_speed_leaves_room_for_host_jitter():
    """The rig speed is DERIVED from the aligner's own window, not chosen.

    At speed S a D-millisecond host hiccup reads as D*S milliseconds of
    capture-time lag, and the aligner's window is `lateness_ns`. Running the
    rig faster than this bound makes the aligner correctly reject the trailing
    cameras, and the failure then reads as a parity break when it is really a
    harness artifact — which is exactly how this was first observed.
    """
    from skyweave2.fusion.config import AlignerConfig

    lateness_ms = AlignerConfig().lateness_ns / 1e6
    worst_jitter_ms = TRANSPORT_CONFIG.loopback_schedule_jitter_p95_ms
    safe_speed = lateness_ms / (worst_jitter_ms * TRANSPORT_CONFIG.rig_speed_safety_factor)
    assert TRANSPORT_CONFIG.rig_replay_speed <= safe_speed, (
        f"rig replay speed {TRANSPORT_CONFIG.rig_replay_speed} exceeds the "
        f"{safe_speed:.2f} bound implied by a {lateness_ms:.0f} ms lateness "
        f"window and {worst_jitter_ms} ms of host jitter"
    )
