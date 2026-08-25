"""Provisioning a node, exercised against the HOST-built daemon.

The amendment asks for "a node provisioning script (push daemon, start, collect
results)" and for it to be exercised in tests. What can be exercised without a
node is the whole sequence except the network and the node's BusyBox:
``LocalTransport`` copies real files, runs real commands and starts a real
detached process, so every step here is the step ``SshTransport`` will run — it
is the same code above a different transport.

What is NOT covered, stated plainly: ssh itself, scp itself, and the assumption
that a Buildroot rootfs has ``sha256sum``. Those are the first three things to
check at the bench, and they are cheap to check there.

The refusals get as much attention as the happy path, because they are what
stands between a bench session and a morning spent measuring half a binary.
"""

from __future__ import annotations

import shlex

import pytest

from skyweave2.detector.config import Backend, DetectorConfig
from skyweave2.edge import benchmark, daemon, provision


def _spec(tmp_path, **overrides) -> provision.NodeSpec:
    base = {
        "name": "local-node",
        "camera_id": 0,
        "remote_dir": "run",
        "jetson_host": "127.0.0.1",
    }
    base.update(overrides)
    return provision.NodeSpec(**base)


def _stream(tmp_path, frames: int = 40, width: int = 192, height: int = 108):
    plan = benchmark.BenchmarkPlan(frames=frames, warmup_frames=8)
    path = tmp_path / "inject.swij"
    benchmark.write_benchmark_stream(
        path, plan, width, height, session_uuid="d8prov-0000-0001"
    )
    return plan, path


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------


def test_the_daemon_arrives_verified_runs_and_its_results_come_back(
    edge_build_dir, tmp_path
):
    """Push, hash-check on the far side, start, wait, collect. All of it."""
    node_root = tmp_path / "node"
    plan, stream = _stream(tmp_path)
    spec = _spec(tmp_path, measurement_port=5601)
    transport = provision.LocalTransport(root=node_root)
    result = provision.provision_node(
        transport,
        daemon.daemon_path(edge_build_dir),
        benchmark.benchmark_config(192, 108, plan),
        spec,
        out_dir=tmp_path / "collected",
        stream_local=stream,
        detector="soft",
        wait_s=60.0,
    )
    assert result.binary_verified
    assert result.local_sha256 == result.remote_sha256
    assert result.source_local_sha256 == result.source_remote_sha256
    assert result.source_verified
    assert result.wall_s > 0.0
    assert result.pid and result.pid > 0
    assert result.completed_before_deadline
    assert result.daemon_stopped
    assert result.exit_status == 0
    assert "stats.json" in result.collected and "run.log" in result.collected
    assert "exit.status" in result.collected
    assert result.stats["frames_in"] == plan.frames
    assert result.stats["warmup_frames"] == plan.warmup_frames
    assert result.stats["capture_events"] > 0
    # The binary really is on the "node", and it is executable there.
    pushed = node_root / "run" / "skyweave-edge"
    assert pushed.exists() and pushed.stat().st_mode & 0o111


def test_a_binary_that_did_not_arrive_intact_is_refused(edge_build_dir, tmp_path):
    """The refusal that matters most.

    A truncated transfer leaves a file that exists, and a benchmark run against
    half a binary is a confusing morning. The hash is computed on the far side
    for exactly this, and a mismatch stops the sequence before the daemon is
    ever started.
    """

    class TruncatingTransport(provision.LocalTransport):
        def push(self, local, remote):
            super().push(local, remote)
            target = self._resolve(remote)
            target.write_bytes(target.read_bytes()[: 1024])

    plan, _ = _stream(tmp_path)
    transport = TruncatingTransport(root=tmp_path / "node")
    with pytest.raises(provision.ProvisionError, match="does not hash"):
        provision.provision_node(
            transport,
            daemon.daemon_path(edge_build_dir),
            benchmark.benchmark_config(192, 108, plan),
            _spec(tmp_path),
            out_dir=tmp_path / "collected",
            inject_port=5700,
            detector="soft",
        )
    # And nothing was started: a refusal that still spawns is not a refusal.
    assert not (tmp_path / "node" / "run" / "run.log").exists()


def test_a_replay_source_that_did_not_arrive_intact_is_refused(
    edge_build_dir, tmp_path
):
    class TruncateSecondPush(provision.LocalTransport):
        pushes = 0

        def push(self, local, remote):
            super().push(local, remote)
            self.pushes += 1
            if self.pushes == 2:
                target = self._resolve(remote)
                target.write_bytes(target.read_bytes()[:-1])

    plan, stream = _stream(tmp_path)
    transport = TruncateSecondPush(root=tmp_path / "node")
    with pytest.raises(provision.ProvisionError, match="injection stream.*does not hash"):
        provision.provision_node(
            transport,
            daemon.daemon_path(edge_build_dir),
            benchmark.benchmark_config(192, 108, plan),
            _spec(tmp_path),
            out_dir=tmp_path / "collected",
            stream_local=stream,
            detector="soft",
        )
    assert not (tmp_path / "node" / "run" / "run.log").exists()


def test_stopping_a_running_daemon_leaves_its_counters_behind(
    edge_build_dir, tmp_path
):
    """SIGTERM, not SIGKILL, and the difference is the whole run.

    ``main.c`` handles SIGTERM by leaving the frame loop and calling
    ``write_stats``. A killed daemon writes nothing, and a soak that ends in
    SIGKILL is an hour with no counters. This starts a paced feed, stops the
    daemon mid-stream, and requires the stats file to be there with a partial
    count in it.
    """
    node_root = tmp_path / "node"
    plan = benchmark.BenchmarkPlan(frames=20, warmup_frames=5)
    spec = _spec(tmp_path, measurement_port=5601)
    transport = provision.LocalTransport(root=node_root)
    remote, local_hash, remote_hash = provision.push_daemon(
        transport, daemon.daemon_path(edge_build_dir), spec
    )
    assert local_hash == remote_hash
    port = benchmark._free_port()
    command = provision.daemon_command(
        remote, benchmark.benchmark_config(192, 108, plan), spec,
        inject_port=port, stats_remote="run/stats.json", detector="soft",
    )
    pid = transport.spawn(command, "run/run.log")
    # The harness's own feeder, not a bare thread: stopping the daemon
    # mid-stream breaks the pipe on purpose, and `_Feeder` is the thing that
    # records that rather than raising it into a thread nobody is catching
    # from.
    feeder = benchmark._Feeder(
        port=port, plan=plan, proc_width=192, proc_height=108,
        session_uuid="d8prov-stop", frame_count=600,
        profile=benchmark.PtsProfile(), paced_fps=30.0,
    )
    feeder.start()
    # Long enough that the daemon is inside its loop with frames behind it,
    # short enough that the paced feed is nowhere near its 600th frame.
    import time

    time.sleep(1.0)
    assert provision.stop_daemon(transport, pid, grace_s=10.0), "it did not stop"
    stats_path = node_root / "run" / "stats.json"
    assert stats_path.exists(), "SIGTERM left no counters behind"
    import json

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert 0 < stats["frames_in"] < 600


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_the_command_line_is_built_through_the_shared_detector_mapping():
    """Every detector knob reaches the node through `daemon_args`.

    That function is TOTAL over `DetectorConfig`'s fields and raises on
    anything unmapped, so a knob added on the host and forgotten here fails a
    test instead of producing a node that quietly disagrees with the oracle.
    """
    config = DetectorConfig(backend=Backend.IVE_APPROX, proc_width=1152,
                            proc_height=648)
    command = provision.daemon_command(
        "/root/skyweave/skyweave-edge", config, provision.NodeSpec(),
        stream_remote="/root/skyweave/inject.swij",
    )
    for expected in (
        "--proc 1152x648",
        "--cap 7",
        "--warmup 90",
        "--morph-open 1",
        "--min-area-px 2",
        "--gmm2-match-sigmas 2.5",
        "--gmm2-var-min 25.0",
        "--detector ive",
        "--inject-file",
        "--stats",
    ):
        assert expected in command, expected


def test_c001_whitelisted_detector_knobs_reach_the_daemon_argv():
    """The four campaign knobs are declarations, not compiled-in guesses."""
    base = DetectorConfig(backend=Backend.IVE_APPROX)
    config = base.model_copy(
        update={
            "open_radius_px": 0,
            "min_area_px": 64,
            "ive_approx": base.ive_approx.model_copy(
                update={"match_sigmas": 4.0, "var_min": 100.0}
            ),
        }
    )
    args = daemon.daemon_args(config, detector="ive")
    joined = " ".join(args)
    for expected in (
        "--morph-open 0",
        "--min-area-px 64",
        "--gmm2-match-sigmas 4.0",
        "--gmm2-var-min 100.0",
    ):
        assert expected in joined


def test_c001_frozen_detector_knob_cannot_diverge_silently():
    config = DetectorConfig(
        backend=Backend.IVE_APPROX,
        persistence_frames=3,
    )
    with pytest.raises(AssertionError, match="C-001 freezes"):
        daemon.daemon_args(config, detector="ive")


def test_c001_diagnostics_are_explicit_bounded_daemon_arguments():
    config = DetectorConfig(backend=Backend.IVE_APPROX)
    command = provision.daemon_command(
        "/root/skyweave/skyweave-edge",
        config,
        provision.NodeSpec(),
        stream_remote="/root/skyweave/inject.swij",
        ccl_log_remote="/root/skyweave/ccl.jsonl",
        fg_mask_log_remote="/root/skyweave/fg masks.swfm",
        fg_mask_limit=10,
    )
    parts = shlex.split(command)
    assert parts[parts.index("--ccl-log") + 1] == "/root/skyweave/ccl.jsonl"
    assert parts[parts.index("--fg-mask-log") + 1] == "/root/skyweave/fg masks.swfm"
    assert parts[parts.index("--fg-mask-limit") + 1] == "10"


def test_c001_foreground_mask_capture_cannot_be_unbounded_or_orphaned():
    config = DetectorConfig(backend=Backend.IVE_APPROX)
    common = {
        "remote_binary": "/root/skyweave/skyweave-edge",
        "config": config,
        "spec": provision.NodeSpec(),
        "stream_remote": "/root/skyweave/inject.swij",
    }
    with pytest.raises(provision.ProvisionError, match="positive capture limit"):
        provision.daemon_command(
            **common,
            fg_mask_log_remote="/root/skyweave/fg-masks.swfm",
        )
    with pytest.raises(provision.ProvisionError, match="no foreground-mask log"):
        provision.daemon_command(**common, fg_mask_limit=10)
    with pytest.raises(provision.ProvisionError, match="hard-bounded to 16"):
        provision.daemon_command(
            **common,
            fg_mask_log_remote="/root/skyweave/fg-masks.swfm",
            fg_mask_limit=17,
        )


def test_a_config_the_oracle_did_not_produce_is_refused():
    """`mog2` on a node would put a backend difference in the tolerance table,
    and there is no OpenCV on the node to run it with anyway."""
    with pytest.raises(AssertionError, match="ive_approx"):
        provision.daemon_command(
            "/root/skyweave/skyweave-edge",
            DetectorConfig(backend=Backend.MOG2),
            provision.NodeSpec(),
            stream_remote="/root/skyweave/inject.swij",
        )


def test_exactly_one_source_or_it_is_a_refusal():
    config = DetectorConfig(backend=Backend.IVE_APPROX)
    for kwargs in ({}, {"stream_remote": "a.swij", "inject_port": 5600}):
        with pytest.raises(provision.ProvisionError, match="exactly one source"):
            provision.daemon_command(
                "/root/skyweave/skyweave-edge", config, provision.NodeSpec(), **kwargs
            )


def test_exactly_one_source_of_the_three_or_it_is_a_refusal():
    """The three-way version of the refusal above, now that a third source exists.

    A two-way XOR extended by hand is how "exactly one" quietly becomes "at
    least one", so the check counts. Every wrong combination is enumerated —
    none, and each of the four ways to name more than one — because the failure
    this stands against is a bench command line that names two sources and runs
    against whichever one the daemon happened to prefer.

    The ``exactly one source`` substring is preserved from the two-way version
    deliberately: it is what both refusals bind on, and changing the words while
    adding a source would leave the older gate matching nothing.
    """
    config = DetectorConfig(backend=Backend.IVE_APPROX)
    combinations = (
        {},
        {"stream_remote": "a.swij", "inject_port": 5600},
        {"stream_remote": "a.swij", "ram_clip_remote": "b.swij"},
        {"inject_port": 5600, "ram_clip_remote": "b.swij"},
        {"stream_remote": "a.swij", "inject_port": 5600, "ram_clip_remote": "b.swij"},
    )
    for kwargs in combinations:
        with pytest.raises(provision.ProvisionError, match="exactly one source"):
            provision.daemon_command(
                "/root/skyweave/skyweave-edge", config, provision.NodeSpec(), **kwargs
            )


def test_a_ram_loop_command_carries_every_declaration():
    """A RAM clip on a node without its declaration is an endless run.

    A loop has no trailer, so the frame budget is the only thing that ends it —
    and a run bounded by a signal instead cannot be compared exactly, which is
    the whole reason D2 chose a frame count. So the clip and the declaration
    arrive together or not at all.

    The flags themselves are spelled by ``RamLoopDeclaration.as_daemon_args``
    and never retyped here. Three argv builders reach this daemon — the test
    driver, the benchmark runner and this one — and three spellings of
    ``--ram-loop-frames`` are three ways a board command line diverges from what
    a host test exercised.
    """
    config = DetectorConfig(backend=Backend.IVE_APPROX, proc_width=1152,
                            proc_height=648)
    command = provision.daemon_command(
        "/root/skyweave/skyweave-edge", config, provision.NodeSpec(),
        ram_clip_remote="/root/skyweave/ram.swij",
        ram_loop=daemon.RamLoopDeclaration(
            clip_frames=6, total_frames=24, pts_stride_ns=200_000_000
        ),
    )
    for expected in (
        "--inject-ram", "/root/skyweave/ram.swij",
        "--ram-loop-frames 24", "--ram-loop-pts-stride-ns 200000000",
        "--ram-budget-mb 160", "--ram-loop-period-ns 0",
    ):
        assert expected in command, expected
    # Exactly one source really did reach the argv.
    assert "--inject-file" not in command
    assert "--inject-listen" not in command
    # ...and the detector mapping still ran, so a RAM row is the same
    # experiment as a file row apart from where the bytes came from.
    for expected in ("--proc 1152x648", "--cap 7", "--warmup 90", "--detector ive"):
        assert expected in command, expected

    with pytest.raises(provision.ProvisionError, match="declaration"):
        provision.daemon_command(
            "/root/skyweave/skyweave-edge", config, provision.NodeSpec(),
            ram_clip_remote="/root/skyweave/ram.swij",
        )


def test_a_ram_clip_path_with_a_space_survives_the_shell():
    """Same hazard as the stream path, on the new flag.

    An unquoted path with a space becomes two arguments, and the daemon refuses
    an unknown flag — at the bench, at the worst moment.
    """
    spec = provision.NodeSpec(remote_dir="/root/sky weave")
    command = provision.daemon_command(
        "/root/sky weave/skyweave-edge",
        DetectorConfig(backend=Backend.IVE_APPROX),
        spec,
        ram_clip_remote="/root/sky weave/ram.swij",
        ram_loop=daemon.RamLoopDeclaration(
            clip_frames=6, total_frames=24, pts_stride_ns=200_000_000
        ),
    )
    parts = shlex.split(command)
    # The F-C1-1 env prefix leads; the spaced binary path must survive after it.
    assert parts[0] == "env"
    assert parts[2] == "/root/sky weave/skyweave-edge"
    assert "/root/sky weave/ram.swij" in parts
    assert parts[parts.index("--inject-ram") + 1] == "/root/sky weave/ram.swij"


def test_paths_with_spaces_survive_the_shell():
    """The command is a shell string on the far side, so quoting is not
    cosmetic: an unquoted path with a space becomes two arguments and the
    daemon refuses an unknown flag — at the bench, at the worst moment."""
    spec = provision.NodeSpec(remote_dir="/root/sky weave")
    command = provision.daemon_command(
        "/root/sky weave/skyweave-edge",
        DetectorConfig(backend=Backend.IVE_APPROX),
        spec,
        stream_remote="/root/sky weave/inject.swij",
    )
    parts = shlex.split(command)
    # The F-C1-1 env prefix leads; the spaced binary path must survive after it.
    assert parts[0] == "env"
    assert parts[2] == "/root/sky weave/skyweave-edge"
    assert "/root/sky weave/inject.swij" in parts


def test_the_local_transport_translates_node_paths_including_spaced_ones(tmp_path):
    """The sandbox translation, on the shape that breaks a naive one.

    `LocalTransport` runs commands built for the NODE's filesystem, so it has
    to rewrite absolute paths into its own root — and the first version of that
    split on spaces, which tore a quoted `/root/sky weave/...` in half and
    rewrote both halves. Tokenising with shlex is what makes it safe, and this
    is the case that proves it.
    """
    transport = provision.LocalTransport(root=tmp_path / "node")
    root = str((tmp_path / "node").resolve())
    spaced = provision.NodeSpec(remote_dir="/root/sky weave")
    command = provision.daemon_command(
        "/root/sky weave/skyweave-edge",
        DetectorConfig(backend=Backend.IVE_APPROX),
        spaced,
        stream_remote="/root/sky weave/inject.swij",
    )
    parts = shlex.split(transport._translate(command))
    # The env prefix is not a path: translation must leave `env` and the
    # LD_LIBRARY_PATH= token alone while rewriting the absolute paths after it.
    assert parts[0] == "env"
    assert parts[1] == "LD_LIBRARY_PATH=/oem/usr/lib"
    assert parts[2] == f"{root}/root/sky weave/skyweave-edge"
    assert f"{root}/root/sky weave/inject.swij" in parts
    # Relative paths and non-path arguments are left alone.
    assert "--detector" in parts and "ive" in parts
    # A shell operator is a refusal, not a silent mangling.
    with pytest.raises(provision.ProvisionError, match="shell operator"):
        transport._translate("kill -0 5 && echo alive")


# ---------------------------------------------------------------------------
# The ssh transport, as far as it can be checked without a node
# ---------------------------------------------------------------------------


def test_the_ssh_transport_never_prompts_and_defaults_to_strict_host_keys():
    """Two properties worth pinning before a bench session.

    BatchMode because a provisioning step that stops for a password is a hang,
    not a prompt. Strict host keys by default because accepting a new key is a
    decision about trust and belongs to the person flashing the node, once, on
    purpose.
    """
    spec = provision.NodeSpec(ssh_host="192.168.1.21", ssh_user="root", ssh_port=22)
    transport = provision.SshTransport(spec=spec)
    argv = transport._ssh_argv()
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert argv[-1] == "root@192.168.1.21"
    relaxed = provision.SshTransport(spec=spec, strict_host_key_checking="accept-new")
    assert "StrictHostKeyChecking=accept-new" in relaxed._ssh_argv()
    jumped = provision.SshTransport(spec=spec, jump_host="jetson-ts")
    for jumped_argv in (jumped._ssh_argv(), jumped._scp_argv()):
        jump_index = jumped_argv.index("-J")
        assert jumped_argv[jump_index + 1] == "jetson-ts"
        assert "StrictHostKeyChecking=yes" in jumped_argv


def test_ssh_spawn_records_exit_status_and_liveness_targets_the_process_group():
    class RecordingTransport(provision.SshTransport):
        commands: list[str]

        def __init__(self, spec):
            super().__init__(spec=spec)
            self.commands = []
            self.group_alive = True

        def run(self, command, timeout_s=60.0):
            del timeout_s
            self.commands.append(command)
            if "setsid nohup" in command:
                stdout = "321\n"
            elif command == "kill -TERM -321":
                self.group_alive = False
                stdout = ""
            elif command == "kill -0 -321 2>/dev/null && echo alive || echo gone":
                stdout = "alive\n" if self.group_alive else "gone\n"
            else:  # pragma: no cover - makes an unexpected shell form obvious
                raise AssertionError(command)
            return provision.CommandResult(["ssh", command], 0, stdout, "")

    transport = RecordingTransport(provision.NodeSpec(ssh_host="192.0.2.1"))
    assert transport.spawn(
        "/run/skyweave-edge --example", "/run/run.log", "/run/exit.status"
    ) == 321
    spawn = transport.commands[-1]
    assert "setsid nohup sh -c" in spawn
    assert "/run/exit.status" in spawn
    assert transport.alive(321) is True
    assert transport.commands[-1] == (
        "kill -0 -321 2>/dev/null && echo alive || echo gone"
    )
    transport.terminate(321)
    assert transport.commands[-1] == "kill -TERM -321"
    assert transport.alive(321) is False
    assert transport.commands[-1] == (
        "kill -0 -321 2>/dev/null && echo alive || echo gone"
    )


# ---------------------------------------------------------------------------
# The loader path (F-C1-1)
# ---------------------------------------------------------------------------


def test_the_daemon_launch_sets_the_loader_path_the_node_needs(tmp_path):
    """F-C1-1: /oem/usr/lib is not on the node's uClibc loader path.

    There is no ldconfig and no ld.so.cache on the image, so librve and the
    rockit stack resolve only through LD_LIBRARY_PATH. The launch command
    carries it — spawn()'s ``setsid nohup`` wrapper preserves a prefix but
    sets nothing itself — because a daemon that dies at the dynamic linker
    looks exactly like a crash from the host side.
    """
    plan, _ = _stream(tmp_path)
    config = benchmark.benchmark_config(192, 108, plan)
    command = provision.daemon_command(
        "/root/skyweave/skyweave-edge", config, _spec(tmp_path),
        stream_remote="/root/skyweave/inject.swij",
    )
    assert command.startswith(
        "env LD_LIBRARY_PATH=/oem/usr/lib /root/skyweave/skyweave-edge "
    )


def test_an_empty_loader_path_launches_with_no_env_prefix(tmp_path):
    """A bare host exercise says so with an empty string, and gets no prefix."""
    plan, _ = _stream(tmp_path)
    config = benchmark.benchmark_config(192, 108, plan)
    command = provision.daemon_command(
        "/root/skyweave/skyweave-edge", config,
        _spec(tmp_path, ld_library_path=""),
        stream_remote="/root/skyweave/inject.swij",
    )
    assert command.startswith("/root/skyweave/skyweave-edge ")
    assert "LD_LIBRARY_PATH" not in command
