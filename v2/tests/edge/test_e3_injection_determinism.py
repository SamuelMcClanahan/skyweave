"""E3: the injection harness is deterministic.

The claim: same clip + same seed + same declared PTS parameters -> identical
daemon input stream. Byte-identical, not equivalent — the daemon's whole
behaviour downstream is a function of these bytes, so anything short of
byte-identity leaves a replay that cannot be reproduced.

Three levels, because determinism fails at three different depths:

1. within one process (a stray dict order, an unseeded draw);
2. across processes (``hash()`` salting — the exact defect that silently
   broke a D6 campaign's cross-run reproducibility);
3. across machines and days (the committed ``injection_sha256``).

And the can-fail side: a harness that produced constant bytes regardless of
its inputs would pass every determinism test ever written, so each knob is
also shown to CHANGE the stream.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys

import pytest

from skyweave2.contracts import ClockDomain
from skyweave2.edge import fixtures
from skyweave2.edge.injection import (
    InjectionStreamError,
    PtsProfile,
    build_injection_stream,
    iter_injection_frames,
    read_injection_session,
)
from tests.edge.conftest import load_config, load_stats

SESSION = "e3-determinism-0000-0000-00000000"


def _stream(clip, config, profile=None, camera_id=0) -> bytes:
    return build_injection_stream(
        clip, config, SESSION, camera_id=camera_id, profile=profile
    )


# ---------------------------------------------------------------------------
# Level 1: within one process
# ---------------------------------------------------------------------------


def test_the_same_inputs_produce_the_same_bytes(scene_clips):
    config = load_config("clutter")
    profile = PtsProfile(offset_ms=1.5, drift_ppm=25.0, jitter_ms_sigma=0.2, seed=7)
    first = _stream(scene_clips["clutter"], config, profile)
    second = _stream(scene_clips["clutter"], config, profile)
    assert first == second
    assert len(first) > 0


def read_stream(data: bytes):
    """(session, frames) from a whole injection stream."""
    stream = io.BytesIO(data)
    session = read_injection_session(stream)
    return session, list(iter_injection_frames(stream))


def test_jitter_does_not_depend_on_how_many_frames_came_before(scene_clips):
    """Per-frame RNG keyed by (seed, camera, frame), not drawn from a running
    stream: a partial replay must fabricate the same clock as a full one, or
    a truncated fixture and a whole one describe different experiments."""
    config = load_config("clutter")
    profile = PtsProfile(jitter_ms_sigma=0.5, seed=11)
    _, whole_frames = read_stream(_stream(scene_clips["clutter"], config, profile))
    partial_session, partial_frames = read_stream(
        build_injection_stream(
            scene_clips["clutter"], config, SESSION, profile=profile, frame_limit=10
        )
    )
    assert partial_session.frame_count == 10
    assert len(partial_frames) == 10
    assert len(whole_frames) > 10
    for a, b in zip(whole_frames[:10], partial_frames, strict=True):
        assert a.frame_seq == b.frame_seq
        assert a.capture_ts_ns == b.capture_ts_ns
        assert a.time_sync_error_ms == b.time_sync_error_ms


# ---------------------------------------------------------------------------
# Level 2: across processes
# ---------------------------------------------------------------------------


def test_the_stream_is_identical_in_a_separate_process(scene_clips, tmp_path):
    """PYTHONHASHSEED-salted `hash()` looks deterministic inside one process
    and is not across two; that defect reached a D6 campaign before its own
    byte-identity gate caught it."""
    clip = scene_clips["sparse"]
    script = (
        "from skyweave2.edge.fixtures import scene_config\n"
        "from skyweave2.edge.injection import PtsProfile, build_injection_stream\n"
        "import hashlib, sys\n"
        "profile = PtsProfile(offset_ms=1.5, drift_ppm=25.0, jitter_ms_sigma=0.2, seed=7)\n"
        f"data = build_injection_stream({str(clip)!r}, scene_config('sparse'), "
        f"{SESSION!r}, profile=profile)\n"
        "sys.stdout.write(hashlib.sha256(data).hexdigest())\n"
    )
    path = tmp_path / "child.py"
    path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(path)], capture_output=True, text=True, check=False,
        env={"PYTHONHASHSEED": "1", "PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    profile = PtsProfile(offset_ms=1.5, drift_ppm=25.0, jitter_ms_sigma=0.2, seed=7)
    here = hashlib.sha256(
        _stream(clip, fixtures.scene_config("sparse"), profile)
    ).hexdigest()
    assert completed.stdout.strip() == here


# ---------------------------------------------------------------------------
# Level 3: across machines, via the committed digest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(fixtures.SYNTHETIC_FIXTURES))
def test_the_stream_matches_the_committed_digest(scene_clips, name):
    """The fixture's `injection_sha256` was recorded when the fixture was
    built. It pins the harness AND the clip generator together: either one
    drifting shows up here rather than as a mysterious replay difference."""
    config = load_config(name)
    session = load_stats(name)["session_uuid"]
    data = build_injection_stream(scene_clips[name], config, session)
    assert hashlib.sha256(data).hexdigest() == load_stats(name)["injection_sha256"]


# ---------------------------------------------------------------------------
# Can-fail: the knobs actually reach the bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile",
    [
        PtsProfile(offset_ms=1.0),
        PtsProfile(drift_ppm=50.0),
        PtsProfile(jitter_ms_sigma=0.3, seed=7),
        PtsProfile(jitter_ms_sigma=0.3, seed=8),
    ],
)
def test_every_declared_knob_changes_the_stream(scene_clips, profile):
    """Without this, a harness that ignored its parameters would pass every
    determinism assertion above."""
    config = load_config("sparse")
    baseline = _stream(scene_clips["sparse"], config, PtsProfile())
    assert _stream(scene_clips["sparse"], config, profile) != baseline


def test_the_camera_id_changes_the_stream(scene_clips):
    config = load_config("sparse")
    assert _stream(scene_clips["sparse"], config, camera_id=0) != _stream(
        scene_clips["sparse"], config, camera_id=1
    )


def test_the_jitter_key_uses_the_seed_and_the_camera(scene_clips):
    """Each INGREDIENT of the RNG key, isolated.

    Comparing a jittered profile against an UNJITTERED baseline proves only
    that jitter exists: deleting the seed from the key, or the camera id,
    leaves every such comparison passing. Both mutations were shown to
    survive the rest of this file, so each ingredient is now varied on its
    own, with everything else held fixed.
    """
    config = load_config("sparse")
    clip = scene_clips["sparse"]
    jitter = dict(offset_ms=0.0, drift_ppm=0.0, jitter_ms_sigma=0.4)

    seed_a = _stream(clip, config, PtsProfile(seed=7, **jitter))
    seed_b = _stream(clip, config, PtsProfile(seed=8, **jitter))
    assert seed_a != seed_b, "the seed does not reach the fabricated clock"

    camera_a = _stream(clip, config, PtsProfile(seed=7, **jitter), camera_id=0)
    camera_b = _stream(clip, config, PtsProfile(seed=7, **jitter), camera_id=1)
    # The camera id also rides in the session header, so compare the CLOCK
    # rather than the bytes: two nodes on one seed must not share a jitter
    # sequence, or an injected "three-camera" run would move all three
    # clocks in lockstep and hide every alignment problem it exists to find.
    clocks_a = [f.capture_ts_ns for f in read_stream(camera_a)[1]]
    clocks_b = [f.capture_ts_ns for f in read_stream(camera_b)[1]]
    assert clocks_a != clocks_b, "the camera id does not reach the fabricated clock"


# ---------------------------------------------------------------------------
# The stream is self-describing, and a broken one fails loudly
# ---------------------------------------------------------------------------


def test_the_session_header_declares_what_the_daemon_needs(scene_clips):
    config = load_config("clutter")
    stream = io.BytesIO(_stream(scene_clips["clutter"], config))
    session = read_injection_session(stream)
    assert session.session_uuid == SESSION
    assert (session.proc_width, session.proc_height) == (
        config.proc_width, config.proc_height
    )
    # The full grid differs from the proc grid on purpose: the D0 scaling law
    # has to be exercised, and a fixture where scale == 1 would let a daemon
    # that forgot it pass.
    assert session.scale_x == 2.0 and session.scale_y == 2.0
    assert session.clock_domain is ClockDomain.SYNTHETIC
    assert not session.declaration_overridden
    frames = list(iter_injection_frames(stream))
    assert len(frames) == session.frame_count
    assert all(
        frame.luma.shape == (session.proc_height, session.proc_width) for frame in frames
    )


def test_a_truncated_stream_is_an_error_not_a_short_clip(scene_clips):
    """Stopping at EOF would make a cut stream indistinguishable from a short
    one: the daemon would detect on what it got and report a clean run."""
    config = load_config("sparse")
    data = _stream(scene_clips["sparse"], config)
    stream = io.BytesIO(data[: len(data) // 2])
    read_injection_session(stream)
    with pytest.raises(InjectionStreamError):
        list(iter_injection_frames(stream))


def test_a_stream_missing_its_trailer_is_rejected(scene_clips):
    config = load_config("sparse")
    data = _stream(scene_clips["sparse"], config)
    stream = io.BytesIO(data[:-8])  # drop the trailer only
    read_injection_session(stream)
    with pytest.raises(InjectionStreamError):
        list(iter_injection_frames(stream))


def test_a_foreign_stream_is_rejected_at_the_magic():
    with pytest.raises(InjectionStreamError, match="magic"):
        read_injection_session(io.BytesIO(b"NOPE" + bytes(200)))


def test_the_daemon_reads_the_same_stream_the_harness_wrote(
    scene_clips, edge_build_dir, tmp_path
):
    """One format, one parser on each side. A file replay and a wire replay
    cannot diverge if they are the same bytes."""
    from skyweave2.edge import daemon

    config = load_config("sparse")
    path = tmp_path / "sparse.swij"
    path.write_bytes(_stream(scene_clips["sparse"], config))
    run = daemon.run_daemon_on_stream(path, config, tmp_path / "run",
                                      build_dir=edge_build_dir)
    assert run.returncode == 0, run.stderr
    session = read_injection_session(io.BytesIO(path.read_bytes()))
    assert run.stats["frames_in"] == session.frame_count
    assert run.stats["proc_width"] == session.proc_width
    assert run.stats["proc_height"] == session.proc_height


def test_ethernet_and_storage_replay_produce_the_same_datagrams(
    scene_clips, edge_build_dir, tmp_path
):
    """The brief's C1: "over Ethernet OR from local storage".

    One format and one parser, so the two must be indistinguishable
    downstream. Tested rather than asserted in a docstring: the TCP path has
    its own accept, its own partial-read behaviour and its own end-of-stream
    condition, and any of the three could produce a different frame sequence
    while the file path stayed correct.
    """
    import socket
    import subprocess
    import time

    from skyweave2.edge import daemon

    config = load_config("sparse")
    data = _stream(scene_clips["sparse"], config)

    from_file = daemon.run_daemon_on_stream(
        _write(tmp_path / "sparse.swij", data), config, tmp_path / "file",
        build_dir=edge_build_dir,
    )
    assert from_file.returncode == 0, from_file.stderr

    # An ephemeral port, taken and released, so a busy machine cannot make
    # this test flaky on a fixed number.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    log = tmp_path / "tcp-packets.hex"
    stats = tmp_path / "tcp-stats.json"
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    argv = [
        str(daemon.daemon_path(edge_build_dir)),
        "--inject-listen", str(port),
        "--packet-log", str(log),
        "--stats", str(stats),
        "--measurement-port", str(sink.getsockname()[1]),
        "--control-port", "0",
    ] + daemon.daemon_args(config)
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 20.0
        connection = None
        while time.monotonic() < deadline:
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=5.0)
                break
            except OSError:
                time.sleep(0.05)
        assert connection is not None, "the daemon never accepted an injection connection"
        with connection:
            connection.sendall(data)
            connection.shutdown(socket.SHUT_WR)
        _, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
    finally:
        sink.close()
        if process.poll() is None:
            process.kill()

    over_ethernet = [
        bytes.fromhex(line) for line in log.read_text(encoding="utf-8").split() if line
    ]
    assert over_ethernet == from_file.packets, (
        "the same stream produced different datagrams over TCP and from a file"
    )


def _write(path, data):
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# The RAM-loop source: a third way into the same parser
# ---------------------------------------------------------------------------
#
# Replay determinism is E3's subject, so the RAM loop's two determinism claims
# live here rather than in the runbook item's own file. The rest of that source
# — its refusals, its budget, its pacing — is in test_d81_ram_loop_source.py.

RAM_CLIP_FRAMES = 6
RAM_STRIDE_NS = 200_000_000  # 6 frames at 30 fps, exactly
RAM_SESSION = "e3-ram-loop-00000000-0000-0000001"


def _ram_plan(frames: int = RAM_CLIP_FRAMES, warmup_frames: int = 0):
    from skyweave2.edge import benchmark

    return benchmark.BenchmarkPlan(frames=frames, warmup_frames=warmup_frames)


def test_one_pass_of_the_ram_loop_is_byte_identical_to_the_file_source(
    edge_build_dir, tmp_path
):
    """One pass through the arena must be indistinguishable from reading the file.

    This is what makes the SECOND pass's arithmetic trustworthy. The preload
    drives the UNMODIFIED file path — ``ram_active`` is still false while it
    runs — so the clip goes through exactly one parser and every existing
    validation applies verbatim; if that were not true, a RAM run and a file run
    could disagree about the bytes before any wrap arithmetic entered the
    picture, and the wrap tests would be measuring the wrong difference.
    """
    from skyweave2.edge import benchmark, daemon

    clip_plan = _ram_plan()
    width, height = 288, 162
    clip = tmp_path / "clip.swij"
    benchmark.write_benchmark_stream(
        clip, clip_plan, width, height, session_uuid=RAM_SESSION,
        frame_count=RAM_CLIP_FRAMES,
    )
    config = benchmark.benchmark_config(width, height, _ram_plan(warmup_frames=2))

    from_file = daemon.run_daemon_on_stream(
        clip, config, tmp_path / "file", build_dir=edge_build_dir
    )
    assert from_file.returncode == 0, from_file.stderr
    from_ram = daemon.run_daemon_on_stream(
        clip, config, tmp_path / "ram", build_dir=edge_build_dir,
        ram_loop=daemon.RamLoopDeclaration(
            clip_frames=RAM_CLIP_FRAMES, total_frames=RAM_CLIP_FRAMES,
            pts_stride_ns=RAM_STRIDE_NS,
        ),
    )
    assert from_ram.returncode == 0, from_ram.stderr

    assert from_ram.packets, "the RAM loop sent nothing, so identity proves nothing"
    assert from_ram.packets == from_file.packets, (
        "one pass of the preloaded arena produced different datagrams from "
        "reading the same file"
    )
    assert from_file.stats["frames_in"] == from_ram.stats["frames_in"] == (
        RAM_CLIP_FRAMES
    )


def test_a_ram_loop_emits_the_datagrams_the_harnesss_own_looped_feed_emits(
    edge_build_dir, tmp_path
):
    """The headline claim about the RAM loop, gated against a NON-CIRCULAR oracle.

    The harness already loops: ``_feed_stream_over_tcp`` sends
    ``scene[frame_seq % len(scene)]`` with a CONTINUOUS frame_seq and a PTS from
    ``frame_seq / fps``, and that path is already gated by the Ethernet-vs-storage
    test above. So the claim "the daemon reproduces the harness's own looping
    rule locally" can be checked against the harness rather than against a
    restatement of the daemon's own arithmetic.

    Run A is that existing TCP feed, 6 scene frames stretched over 24. Run B is
    the same 6 frames preloaded and looped by the daemon under a DECLARED
    per-pass stride. If the daemon replayed the clip's stored PTS, renumbered
    frame_seq per pass, or got the stride wrong by a nanosecond, the two packet
    logs would differ.
    """
    import socket
    import subprocess

    from skyweave2.edge import benchmark, daemon

    clip_plan = _ram_plan()
    width, height = 288, 162
    profile = PtsProfile(offset_ms=2.5)
    run_config = benchmark.benchmark_config(width, height, _ram_plan(warmup_frames=2))
    total_frames = 24

    # Run A: the harness's own looping rule, over TCP, into the daemon.
    port = benchmark._free_port()
    tcp_log = tmp_path / "tcp-packets.hex"
    tcp_stats = tmp_path / "tcp-stats.json"
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    argv = [
        str(daemon.daemon_path(edge_build_dir)),
        "--inject-listen", str(port),
        "--packet-log", str(tcp_log),
        "--stats", str(tcp_stats),
        "--measurement-port", str(sink.getsockname()[1]),
        "--control-port", "0",
    ] + daemon.daemon_args(run_config, detector="soft")
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    feeder = benchmark._Feeder(
        port=port, plan=clip_plan, proc_width=width, proc_height=height,
        session_uuid=RAM_SESSION, frame_count=total_frames, profile=profile,
        paced_fps=None,
    )
    try:
        feeder.start()
        _, stderr = process.communicate(timeout=180)
        assert process.returncode == 0, stderr
    finally:
        feeder.join()
        sink.close()
        if process.poll() is None:
            process.kill()
    assert feeder.error is None, feeder.error
    over_tcp = [
        bytes.fromhex(line) for line in tcp_log.read_text(encoding="utf-8").split()
        if line
    ]

    # Run B: the same six frames, preloaded and looped by the daemon.
    clip = tmp_path / "clip.swij"
    benchmark.write_benchmark_stream(
        clip, clip_plan, width, height, session_uuid=RAM_SESSION, profile=profile,
        frame_count=RAM_CLIP_FRAMES,
    )
    from_ram = daemon.run_daemon_on_stream(
        clip, run_config, tmp_path / "ram", build_dir=edge_build_dir,
        ram_loop=daemon.RamLoopDeclaration(
            clip_frames=RAM_CLIP_FRAMES, total_frames=total_frames,
            pts_stride_ns=RAM_STRIDE_NS,
        ),
    )
    assert from_ram.returncode == 0, from_ram.stderr

    import json

    assert json.loads(tcp_stats.read_text())["frames_in"] == total_frames
    assert from_ram.stats["frames_in"] == total_frames
    assert over_tcp, "the TCP feed logged nothing to compare against"
    assert from_ram.packets == over_tcp, (
        "the daemon's RAM loop did not reproduce the harness's own looped feed"
    )
