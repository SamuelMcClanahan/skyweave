"""E4: the fabricated PTS is declared, and the declaration reaches the wire.

C1 has no capture clock. Every ``capture_ts_ns`` on this path is scene time
plus a perturbation the harness invented, and the only thing that keeps that
from being a lie is that the perturbation's magnitude rides in
``time_sync_error_ms`` on every single frame, untouched, all the way to the
Jetson.

So this file tests the whole chain rather than the formula:

  profile -> fabricated timestamp -> injection stream -> daemon -> datagram

at each step asking the same question: is the declaration still there, and
is it still the right number?

The known-lie half matters as much. A test that only asserts "the field is
populated" passes just as well when the field is a constant, so the
harness's ``declared_error_ms_override`` is exercised too — and the stream
that carries it is required to FLAG itself, because a fixture that lies
quietly eventually gets mistaken for evidence.
"""

from __future__ import annotations

import pytest

from skyweave2.contracts import ClockDomain
from skyweave2.edge import daemon
from skyweave2.edge.injection import (
    FLAG_DECLARATION_OVERRIDDEN,
    InjectionStreamError,
    PtsProfile,
    build_injection_stream,
    fabricate_pts,
)
from skyweave2.transport import codec
from skyweave2.transport.wire import unframe
from tests.edge.conftest import load_config
from tests.edge.test_e3_injection_determinism import read_stream

SESSION = "e4-honesty-00000000-0000-00000000"


def _scene_ts_ns(frame_seq: int, fps: float) -> int:
    """Scene time exactly as `runner.detect_clip` computes it."""
    return int(round(frame_seq / fps * 1e9))


# ---------------------------------------------------------------------------
# The formula: the declaration IS the perturbation
# ---------------------------------------------------------------------------


def test_a_pure_offset_is_declared_exactly():
    profile = PtsProfile(offset_ms=2.5)
    ts, declared = fabricate_pts(1_000_000_000, profile, camera_id=0, frame_seq=0)
    assert ts == 1_002_500_000
    assert declared == pytest.approx(2.5)


def test_drift_grows_with_scene_time_and_is_declared_as_it_grows():
    """A constant declaration would be a lie that only shows up an hour in."""
    profile = PtsProfile(drift_ppm=100.0)
    early_ts, early = fabricate_pts(1_000_000_000, profile, 0, 30)
    late_ts, late = fabricate_pts(60_000_000_000, profile, 0, 1800)
    assert early_ts - 1_000_000_000 == pytest.approx(100_000, rel=1e-9)
    assert late_ts - 60_000_000_000 == pytest.approx(6_000_000, rel=1e-9)
    assert late > early * 50


def test_jitter_is_declared_per_frame_not_as_a_sigma():
    """The D6 convention: the honest declaration is THIS frame's shift, not
    a distribution parameter. A sigma would understate half the frames."""
    profile = PtsProfile(jitter_ms_sigma=1.0, seed=7)
    declarations = [
        fabricate_pts(_scene_ts_ns(seq, 30.0), profile, 0, seq)[1] for seq in range(50)
    ]
    assert len(set(declarations)) > 40, "the declaration is not varying per frame"
    assert all(value >= 0.0 for value in declarations)


def test_the_declaration_equals_the_shift_it_applied():
    """The whole honesty rule in one assertion, over all three knobs at once."""
    profile = PtsProfile(offset_ms=-1.25, drift_ppm=40.0, jitter_ms_sigma=0.3, seed=3)
    for seq in range(0, 200, 7):
        scene = _scene_ts_ns(seq, 30.0)
        ts, declared = fabricate_pts(scene, profile, camera_id=1, frame_seq=seq)
        assert declared == pytest.approx(abs(ts - scene) / 1e6, abs=1e-6)


def test_a_zero_profile_declares_zero_and_moves_nothing():
    """The honest null case: no fabrication, no claim of fabrication."""
    ts, declared = fabricate_pts(123_456_789, PtsProfile(), 0, 5)
    assert ts == 123_456_789
    assert declared == 0.0


# ---------------------------------------------------------------------------
# The stream: every frame carries its own declaration
# ---------------------------------------------------------------------------


def test_every_frame_in_the_stream_carries_its_own_declaration(scene_clips):
    config = load_config("sparse")
    profile = PtsProfile(offset_ms=3.0, drift_ppm=50.0, jitter_ms_sigma=0.25, seed=5)
    session, frames = read_stream(
        build_injection_stream(scene_clips["sparse"], config, SESSION, profile=profile)
    )
    assert session.clock_domain is ClockDomain.SYNTHETIC
    assert not session.declaration_overridden
    fps = session.fps
    for frame in frames:
        scene = _scene_ts_ns(frame.frame_seq, fps)
        assert frame.capture_ts_ns != scene, "the fabrication did not reach the stream"
        # float32 on the wire, so compare at binary32 precision.
        assert frame.time_sync_error_ms == pytest.approx(
            abs(frame.capture_ts_ns - scene) / 1e6, rel=1e-6
        )
    # Non-zero everywhere: a declaration that is zero on most frames would be
    # a declaration in name only.
    assert all(frame.time_sync_error_ms > 0.0 for frame in frames)


def test_the_declaration_is_never_zeroed_by_the_pipeline(scene_clips):
    """The brief's words: 'never zeroed'. Checked against a profile whose
    every frame must declare something."""
    config = load_config("sparse")
    profile = PtsProfile(offset_ms=5.0)
    _, frames = read_stream(
        build_injection_stream(scene_clips["sparse"], config, SESSION, profile=profile)
    )
    assert frames
    assert min(frame.time_sync_error_ms for frame in frames) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# The daemon carries it to the wire, untouched
# ---------------------------------------------------------------------------


def test_the_daemon_puts_the_declaration_on_the_wire(scene_clips, edge_build_dir,
                                                     tmp_path):
    """The daemon must COPY the harness's timestamp and declaration, never
    recompute either. If it recomputed, the honesty the harness built in
    would end at the daemon's front door."""
    config = load_config("sparse")
    profile = PtsProfile(offset_ms=2.0, drift_ppm=30.0, jitter_ms_sigma=0.1, seed=9)
    path = tmp_path / "honest.swij"
    path.write_bytes(
        build_injection_stream(scene_clips["sparse"], config, SESSION, profile=profile)
    )
    session, frames = read_stream(path.read_bytes())
    by_seq = {frame.frame_seq: frame for frame in frames}

    run = daemon.run_daemon_on_stream(path, config, tmp_path / "run",
                                      build_dir=edge_build_dir)
    assert run.returncode == 0, run.stderr
    assert run.packets, "the daemon sent nothing to compare"

    for packet in run.packets:
        envelope, observations = codec.decode_observation_packet(unframe(packet)[1])
        source = by_seq[envelope.frame_seq]
        assert envelope.capture_ts_ns == source.capture_ts_ns
        assert envelope.time_sync_error_ms == pytest.approx(
            source.time_sync_error_ms, rel=1e-6
        )
        assert envelope.time_sync_error_ms > 0.0
        # And the domain is still the harness's declaration, not a promotion.
        assert envelope.clock_domain is ClockDomain.SYNTHETIC
        assert observations


# ---------------------------------------------------------------------------
# A fabricated clock may not wear a real clock's name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain", [ClockDomain.NODE_MONO, ClockDomain.NODE_PTP, ClockDomain.JETSON_RX]
)
def test_the_harness_refuses_to_claim_a_board_clock(scene_clips, domain):
    """C1 fabricates the PTS from scene time and has no board clock at all.
    Claiming NODE_PTP would present a fabricated timestamp as a real capture
    clock — the one thing this stage must not do."""
    config = load_config("sparse")
    with pytest.raises(InjectionStreamError, match="may not claim clock domain"):
        build_injection_stream(
            scene_clips["sparse"], config, SESSION,
            profile=PtsProfile(clock_domain=domain),
        )


def test_the_daemon_refuses_a_stream_claiming_a_board_clock(scene_clips,
                                                            edge_build_dir, tmp_path):
    """Both ends enforce it. A guard only the writer honours is not a guard:
    a hand-built stream must not be able to talk the daemon into stamping
    NODE_PTP on an injected frame."""
    config = load_config("sparse")
    path = tmp_path / "liar.swij"
    data = bytearray(build_injection_stream(scene_clips["sparse"], config, SESSION))
    data[52] = 3  # CLOCK_DOMAIN_NODE_PTP, straight into the session header
    path.write_bytes(bytes(data))
    run = daemon.run_daemon_on_stream(path, config, tmp_path / "run",
                                      build_dir=edge_build_dir)
    assert run.returncode != 0
    assert "may only declare SYNTHETIC" in run.stderr


# ---------------------------------------------------------------------------
# The known-lie path, which is what makes the honest path non-vacuous
# ---------------------------------------------------------------------------


def test_an_overridden_declaration_is_representable_and_flagged(scene_clips):
    """Mirrors the D6 dishonest-declaration design: the lie is a first-class,
    LABELLED case, and the artifact carrying it says so about itself."""
    config = load_config("sparse")
    profile = PtsProfile(offset_ms=25.0, declared_error_ms_override=0.0)
    assert not profile.is_honest
    assert "known lie" in profile.describe()
    data = build_injection_stream(
        scene_clips["sparse"], config, SESSION, profile=profile
    )
    assert data[5] & FLAG_DECLARATION_OVERRIDDEN
    session, frames = read_stream(data)
    assert session.declaration_overridden
    fps = session.fps
    for frame in frames:
        scene = _scene_ts_ns(frame.frame_seq, fps)
        assert frame.capture_ts_ns - scene == 25_000_000  # the shift is real
        assert frame.time_sync_error_ms == 0.0  # ...and the declaration is not


def test_the_daemon_announces_a_known_lie_stream(scene_clips, edge_build_dir, tmp_path):
    """A known-lie fixture that ran quietly would eventually be mistaken for
    evidence, so the daemon says so at startup, every time."""
    config = load_config("sparse")
    path = tmp_path / "lie.swij"
    path.write_bytes(
        build_injection_stream(
            scene_clips["sparse"], config, SESSION,
            profile=PtsProfile(offset_ms=25.0, declared_error_ms_override=0.0),
        )
    )
    run = daemon.run_daemon_on_stream(path, config, tmp_path / "run",
                                      build_dir=edge_build_dir)
    assert run.returncode == 0, run.stderr
    assert "OVERRIDDEN" in run.stderr
    assert "Known-lie" in run.stderr


def test_the_honest_path_is_not_vacuous(scene_clips):
    """Can-fail: an honest stream and a lying one must differ in the field
    under test, or 'the declaration is present' proves nothing."""
    config = load_config("sparse")
    honest = read_stream(
        build_injection_stream(scene_clips["sparse"], config, SESSION,
                               profile=PtsProfile(offset_ms=25.0))
    )[1]
    lying = read_stream(
        build_injection_stream(
            scene_clips["sparse"], config, SESSION,
            profile=PtsProfile(offset_ms=25.0, declared_error_ms_override=0.0),
        )
    )[1]
    assert [f.capture_ts_ns for f in honest] == [f.capture_ts_ns for f in lying]
    assert all(f.time_sync_error_ms == pytest.approx(25.0) for f in honest)
    assert all(f.time_sync_error_ms == 0.0 for f in lying)


# ---------------------------------------------------------------------------
# The RAM loop: an advance is not an invention, and the declaration survives it
# ---------------------------------------------------------------------------
#
# PTS honesty is E4's subject, so the RAM loop's two honesty claims live here.
# The source's own behaviour — its budget, its refusals, its pacing — is in
# test_d81_ram_loop_source.py.

RAM_CLIP_FRAMES = 6
RAM_STRIDE_NS = 200_000_000  # 6 frames at 30 fps, exactly
RAM_SESSION = "e4-ram-loop-00000000-0000-0000001"


def test_the_ram_loop_carries_a_constant_offset_declaration_on_every_pass(
    edge_build_dir, tmp_path
):
    """A constant offset is HONEST under a constant per-pass stride.

    This is the case an over-strict design would have refused. With a pure
    offset, ``capture_ts(seq) = scene_ts(seq) + offset``, so
    ``clip[i].capture_ts + pass*stride`` is exactly what an unrolled feed would
    have written for virtual frame ``pass*N + i`` — the offset rides along
    unchanged and the declaration stays true on pass 4 as much as on pass 1.
    What actually breaks under a loop is drift (the error grows with scene
    time) and jitter (keyed by frame_seq, so a repeated slot carries the wrong
    frame's draw); both are refused at the host, and that is the test below.

    Note the ``by_seq`` idiom the file-source test above uses does NOT apply
    here: a looped run's frame_seq spans the whole budget from an N-frame clip,
    so there is no stored frame to look up. The arithmetic is asserted instead.
    """
    from skyweave2.edge import benchmark

    width, height = 288, 162
    plan = benchmark.BenchmarkPlan(frames=RAM_CLIP_FRAMES, warmup_frames=0)
    profile = PtsProfile(offset_ms=2.5)
    clip = tmp_path / "clip.swij"
    benchmark.write_benchmark_stream(
        clip, plan, width, height, session_uuid=RAM_SESSION, profile=profile,
        frame_count=RAM_CLIP_FRAMES,
    )
    total_frames = 24
    config = benchmark.benchmark_config(
        width, height, benchmark.BenchmarkPlan(frames=total_frames, warmup_frames=2)
    )
    run = daemon.run_daemon_on_stream(
        clip, config, tmp_path / "run", build_dir=edge_build_dir,
        ram_loop=daemon.RamLoopDeclaration(
            clip_frames=RAM_CLIP_FRAMES, total_frames=total_frames,
            pts_stride_ns=RAM_STRIDE_NS,
        ),
    )
    assert run.returncode == 0, run.stderr
    assert run.packets, "the daemon sent nothing to compare"

    _, clip_frames = read_stream(clip.read_bytes())
    assert len(clip_frames) == RAM_CLIP_FRAMES
    seen_passes = set()
    for packet in run.packets:
        envelope, observations = codec.decode_observation_packet(unframe(packet)[1])
        passes, slot = divmod(envelope.frame_seq, RAM_CLIP_FRAMES)
        seen_passes.add(passes)
        source = clip_frames[slot]
        # The declaration is the CLIP's, copied byte for byte and never
        # recomputed — on pass 4 exactly as on pass 0.
        assert envelope.time_sync_error_ms == pytest.approx(
            source.time_sync_error_ms, rel=1e-6
        )
        assert envelope.time_sync_error_ms == pytest.approx(2.5, rel=1e-6)
        # ...and the timestamp is what an UNROLLED feed would have carried for
        # this virtual frame_seq. Exactly, not approximately: the whole point of
        # a declared integer stride is that no float enters the daemon's path.
        unrolled, declared = fabricate_pts(
            _scene_ts_ns(envelope.frame_seq, 30.0), profile, 0, envelope.frame_seq
        )
        assert envelope.capture_ts_ns == unrolled, envelope.frame_seq
        assert declared == pytest.approx(envelope.time_sync_error_ms, rel=1e-6)
        # An advanced synthetic timestamp is still a synthetic timestamp.
        assert envelope.clock_domain is ClockDomain.SYNTHETIC
        assert observations
    assert seen_passes >= {0, 1, 2, 3}, (
        f"only passes {sorted(seen_passes)} reached the wire; the claim is about "
        "the later ones"
    )


def test_a_drifting_or_jittering_profile_is_refused_at_the_host():
    """The two profiles a constant per-pass stride cannot carry, each with its
    own reason rather than one shared "unsupported".

    Drift makes the declared error grow with SCENE time, so from pass 2 the
    looped timestamp is one no unrolled feed would have written. Jitter is drawn
    from a key that includes frame_seq, so a repeated clip slot carries the
    perturbation belonging to a different frame. Neither is a limitation of the
    daemon: they are conditions under which the harness's own looping rule stops
    being reproducible, which is why they are refused where the clip is BUILT.

    A clip length whose per-pass stride is not a whole number of nanoseconds is
    refused for the same underlying reason and is checked here beside them.
    """
    from skyweave2.edge import benchmark

    plan = benchmark.BenchmarkPlan(
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM, ram_clip_frames=6
    )
    with pytest.raises(ValueError, match="drift"):
        benchmark.ram_loop_declaration(
            plan, 288, 162, total_frames=24, profile=PtsProfile(drift_ppm=30.0)
        )
    with pytest.raises(ValueError, match="jitter"):
        benchmark.ram_loop_declaration(
            plan, 288, 162, total_frames=24, profile=PtsProfile(jitter_ms_sigma=0.1)
        )
    with pytest.raises(ValueError, match="known-lie"):
        benchmark.ram_loop_declaration(
            plan, 288, 162, total_frames=24,
            profile=PtsProfile(offset_ms=25.0, declared_error_ms_override=0.0),
        )
    # ...and the honest constant offset the test above exercises is NOT refused.
    assert benchmark.ram_loop_declaration(
        plan, 288, 162, total_frames=24, profile=PtsProfile(offset_ms=2.5)
    ).pts_stride_ns == RAM_STRIDE_NS

    odd = benchmark.BenchmarkPlan(
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM, ram_clip_frames=13
    )
    with pytest.raises(ValueError, match="whole number of nanoseconds"):
        benchmark.ram_loop_declaration(odd, 288, 162, total_frames=24)


def test_unknown_injection_flags_are_rejected(scene_clips):
    """A future flag this build has never heard of must stop the stream, not
    be ignored: an ignored flag is an undeclared change in what the bytes
    mean."""
    config = load_config("sparse")
    data = bytearray(build_injection_stream(scene_clips["sparse"], config, SESSION))
    data[5] = 0x80
    with pytest.raises(InjectionStreamError, match="unknown injection flags"):
        read_stream(bytes(data))
