"""Putting the daemon on a node, starting it, and collecting what it produced.

Three steps, and the middle one is the only one that is interesting:

1. **push** the binary and verify it by SHA-256 ON THE NODE. A truncated scp
   produces a file that exists, and a benchmark run against half a binary is a
   confusing morning. The hash is computed at both ends and compared, and a
   mismatch is a refusal, not a warning.
2. **start** it, detached, with the argv built from a ``DetectorConfig`` by
   ``daemon.daemon_args`` — the same function the fixture replay uses, so a
   detector knob cannot reach the board through a path the oracle never saw.
3. **collect** the stats file, the packet log if one was asked for, and the
   daemon's own stderr. Stopping is SIGTERM, which the daemon handles by
   leaving its loop and WRITING ITS STATS; killing it with SIGKILL would leave
   the run with no counters, which is the one thing a benchmark cannot lose.

Transport is an interface with two implementations. ``SshTransport`` is what a
bench session uses. ``LocalTransport`` runs the same three steps against a
directory on this machine, which is how the D8.1-prep tests exercise all of it
against the host-built daemon: the brief asks for the tooling to be debugged
before the board session, and a provisioning path that has never been run is
not tooling, it is a plan.

There is no Python on the node and there never will be (RV1106_EDGE_NODE.md
section 5). Everything here runs on the HOST and speaks to the node with ssh,
scp and the BusyBox utilities a Buildroot rootfs actually has.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from skyweave2.detector.config import DetectorConfig
from skyweave2.edge import daemon


class ProvisionError(RuntimeError):
    """A step that must not be papered over. Every raise site says which."""


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    def check(self, what: str) -> CommandResult:
        if self.returncode != 0:
            raise ProvisionError(
                f"{what} failed ({self.returncode}): {' '.join(self.argv)}\n"
                f"{self.stderr.strip() or self.stdout.strip()}"
            )
        return self


@dataclass(frozen=True)
class NodeSpec:
    """One node, as the host addresses it."""

    name: str = "node0"
    camera_id: int = 0
    #: Where the Jetson is, from the NODE's point of view. The daemon does no
    #: name resolution on purpose, so this is a dotted quad or it is a refusal.
    jetson_host: str = "127.0.0.1"
    measurement_port: int = 5601
    control_port: int = 5602
    health_period_ms: int = 1000
    remote_dir: str = "/root/skyweave"
    #: ssh only
    ssh_host: str = ""
    ssh_user: str = "root"
    ssh_port: int = 22
    #: The node's uClibc loader has no ldconfig and no ld.so.cache, so the
    #: media libraries under /oem/usr/lib (librve, librockit, librga,
    #: librkaiq) resolve only if the launch environment says where they are
    #: (F-C1-1). Empty = no env prefix, which is what a bare host run wants.
    ld_library_path: str = "/oem/usr/lib"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "camera_id": self.camera_id,
            "jetson_host": self.jetson_host,
            "measurement_port": self.measurement_port,
            "control_port": self.control_port,
            "health_period_ms": self.health_period_ms,
            "remote_dir": self.remote_dir,
            "ssh_host": self.ssh_host,
            "ssh_user": self.ssh_user,
            "ssh_port": self.ssh_port,
            "ld_library_path": self.ld_library_path,
        }


class NodeTransport(Protocol):
    """The four things provisioning needs from a machine it is not on."""

    name: str

    def push(self, local: Path, remote: str) -> None: ...

    def fetch(self, remote: str, local: Path) -> None: ...

    def run(self, command: str, timeout_s: float = 60.0) -> CommandResult: ...

    def spawn(self, command: str, log_remote: str) -> int: ...

    def terminate(self, pid: int) -> None: ...

    def alive(self, pid: int) -> bool: ...


@dataclass
class LocalTransport:
    """The same three steps, against a directory on this machine.

    Not a mock: it copies real files, runs real commands and starts a real
    detached process. What it does NOT exercise is the network and the node's
    BusyBox, which is exactly the part a bench session is for.
    """

    root: Path
    name: str = "local"
    #: Log files and child processes this transport started. Held because the
    #: child writes into the fd for its whole life: closing the handle when
    #: `spawn` returns would close the file the daemon is logging to.
    _logs: list = field(default_factory=list)
    _processes: dict = field(default_factory=dict)

    def _resolve(self, remote: str) -> Path:
        # A "remote" path is interpreted under `root`, so a test cannot write
        # to an absolute path on the developer's machine by accident.
        return self.root / remote.lstrip("/")

    def push(self, local: Path, remote: str) -> None:
        target = self._resolve(remote)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(local).read_bytes())
        target.chmod(0o755)

    def fetch(self, remote: str, local: Path) -> None:
        source = self._resolve(remote)
        if not source.exists():
            raise ProvisionError(f"nothing to fetch at {source}")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(source.read_bytes())

    def _translate(self, command: str) -> str:
        """Rewrite node paths in a command so they land under ``root``.

        Without this, ``push`` writes to ``<root>/root/skyweave/skyweave-edge``
        while ``run``/``spawn`` execute the command verbatim — which names
        ``/root/skyweave/skyweave-edge``, a path outside the sandbox that on a
        Linux developer machine is a real directory. Every command this
        transport runs was built for the NODE's filesystem, so translating is
        not a convenience: an untranslated one either fails confusingly or
        writes somewhere it was never meant to.

        The command is tokenised with ``shlex`` and re-quoted rather than split
        on spaces: the commands this receives are already shell-quoted, so a
        remote directory containing a space arrives as ONE quoted token, and a
        naive split would tear it in half and rewrite the halves. Shell
        operators are refused rather than mangled — nothing in this module
        sends any, and a silent corruption would be worse than a refusal.
        """
        for operator in ("&&", "||", "|", ";", ">", "<", "$("):
            if operator in command:
                raise ProvisionError(
                    f"LocalTransport cannot translate node paths through a "
                    f"shell operator ({operator!r}): {command}"
                )
        root = str(self.root.resolve())
        out = []
        for token in shlex.split(command):
            if token.startswith("/") and not token.startswith(root):
                token = f"{root}/{token.lstrip('/')}"
            out.append(shlex.quote(token))
        return " ".join(out)

    def run(self, command: str, timeout_s: float = 60.0) -> CommandResult:
        translated = self._translate(command)
        completed = subprocess.run(
            ["sh", "-c", translated], cwd=str(self.root), capture_output=True,
            text=True, timeout=timeout_s, check=False,
        )
        return CommandResult(["sh", "-c", translated], completed.returncode,
                             completed.stdout, completed.stderr)

    def spawn(self, command: str, log_remote: str) -> int:
        command = self._translate(command)
        log_path = self._resolve(log_remote)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("wb")
        process = subprocess.Popen(
            ["sh", "-c", command], cwd=str(self.root), stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        self._logs.append(handle)
        self._processes[process.pid] = process
        return process.pid

    def terminate(self, pid: int) -> None:
        """SIGTERM the whole process GROUP, not just the pid.

        ``spawn`` uses ``start_new_session``, so the child is a group leader.
        Signalling the pid alone reaches whatever ``sh -c`` turned into — and
        if that shell did not exec the daemon, the daemon is a grandchild and
        never hears it. The node's ssh path does not have this problem
        (``setsid nohup cmd`` execs down to the daemon, so ``$!`` IS the
        daemon), which is exactly why it was invisible until a test ran the
        local one.
        """
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def alive(self, pid: int) -> bool:
        """Whether it is still running — and REAPED if it is not.

        ``os.kill(pid, 0)`` says yes for a zombie, and a zombie is what an
        unreaped child becomes the moment it exits. Polling the Popen collects
        it, so "it did not stop" means it did not stop.
        """
        process = self._processes.get(pid)
        if process is not None:
            return process.poll() is None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


@dataclass
class SshTransport:
    """scp/ssh against a real node.

    ``BatchMode=yes`` throughout: a provisioning step that stops to ask for a
    password in the middle of a bench session is a hang, not a prompt.
    Host-key policy is a PARAMETER with the strict default — accepting a new
    key silently is a decision about trust, and the person at the bench should
    make it on purpose the first time each node is flashed.
    """

    spec: NodeSpec
    name: str = "ssh"
    strict_host_key_checking: str = "yes"
    identity: str | None = None

    def _ssh_argv(self) -> list[str]:
        argv = ["ssh", "-o", "BatchMode=yes",
                "-o", f"StrictHostKeyChecking={self.strict_host_key_checking}",
                "-p", str(self.spec.ssh_port)]
        if self.identity:
            argv += ["-i", self.identity]
        return argv + [f"{self.spec.ssh_user}@{self.spec.ssh_host}"]

    def _scp_argv(self) -> list[str]:
        argv = ["scp", "-o", "BatchMode=yes",
                "-o", f"StrictHostKeyChecking={self.strict_host_key_checking}",
                "-P", str(self.spec.ssh_port)]
        if self.identity:
            argv += ["-i", self.identity]
        return argv

    def push(self, local: Path, remote: str) -> None:
        target = f"{self.spec.ssh_user}@{self.spec.ssh_host}:{remote}"
        self.run(f"mkdir -p {shlex.quote(str(Path(remote).parent))}").check("mkdir")
        argv = self._scp_argv() + [str(local), target]
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        CommandResult(argv, completed.returncode, completed.stdout,
                      completed.stderr).check("scp push")
        self.run(f"chmod +x {shlex.quote(remote)}").check("chmod")

    def fetch(self, remote: str, local: Path) -> None:
        source = f"{self.spec.ssh_user}@{self.spec.ssh_host}:{remote}"
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        argv = self._scp_argv() + [source, str(local)]
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        CommandResult(argv, completed.returncode, completed.stdout,
                      completed.stderr).check("scp fetch")

    def run(self, command: str, timeout_s: float = 60.0) -> CommandResult:
        argv = self._ssh_argv() + [command]
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=timeout_s, check=False)
        return CommandResult(argv, completed.returncode, completed.stdout,
                             completed.stderr)

    def spawn(self, command: str, log_remote: str) -> int:
        # setsid + nohup so the daemon survives the ssh session closing, and
        # the pid comes back on stdout because that is what stops it later.
        wrapped = (
            f"mkdir -p {shlex.quote(str(Path(log_remote).parent))}; "
            f"setsid nohup {command} > {shlex.quote(log_remote)} 2>&1 & echo $!"
        )
        result = self.run(wrapped).check("spawn")
        text = result.stdout.strip().splitlines()
        if not text or not text[-1].strip().isdigit():
            raise ProvisionError(f"the node did not return a pid: {result.stdout!r}")
        return int(text[-1].strip())

    def terminate(self, pid: int) -> None:
        self.run(f"kill -TERM {pid}")

    def alive(self, pid: int) -> bool:
        probe = self.run(f"kill -0 {pid} 2>/dev/null && echo alive || echo gone")
        return "gone" not in probe.stdout


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ProvisionResult:
    node: NodeSpec
    transport: str
    remote_binary: str
    local_sha256: str
    remote_sha256: str
    pid: int | None = None
    argv: str = ""
    stats: dict = field(default_factory=dict)
    collected: list[str] = field(default_factory=list)
    log_tail: str = ""

    @property
    def binary_verified(self) -> bool:
        return bool(self.local_sha256) and self.local_sha256 == self.remote_sha256

    def as_dict(self) -> dict:
        return {
            "schema": "d8-provision/1",
            "node": self.node.as_dict(),
            "transport": self.transport,
            "remote_binary": self.remote_binary,
            "local_sha256": self.local_sha256,
            "remote_sha256": self.remote_sha256,
            "binary_verified": self.binary_verified,
            "pid": self.pid,
            "argv": self.argv,
            "stats": self.stats,
            "collected": self.collected,
        }


def push_daemon(
    transport: NodeTransport, binary: str | Path, spec: NodeSpec
) -> tuple[str, str, str]:
    """Copy the daemon over and verify it by hash on the far side.

    Returns (remote path, local sha256, remote sha256). The caller decides what
    a mismatch means; :func:`provision_node` decides it means stop.
    """
    binary = Path(binary)
    remote = f"{spec.remote_dir.rstrip('/')}/skyweave-edge"
    transport.push(binary, remote)
    local_hash = sha256_of(binary)
    probe = transport.run(f"sha256sum {shlex.quote(remote)}")
    remote_hash = ""
    if probe.returncode == 0 and probe.stdout.strip():
        remote_hash = probe.stdout.split()[0].strip()
    return remote, local_hash, remote_hash


def daemon_command(
    remote_binary: str,
    config: DetectorConfig,
    spec: NodeSpec,
    stream_remote: str | None = None,
    inject_port: int | None = None,
    stats_remote: str = "stats.json",
    packet_log_remote: str | None = None,
    detector: str = "ive",
    ram_clip_remote: str | None = None,
    ram_loop: daemon.RamLoopDeclaration | None = None,
) -> str:
    """The exact command line the node will run.

    Built through ``daemon.daemon_args`` so every detector knob the host oracle
    knows about reaches the node, and any knob it does not know about raises
    here rather than diverging silently. ``--detector`` defaults to ``ive``
    because on a node that is the point; a host-side exercise passes ``soft``
    and the artifact records which one ran.
    """
    # Counted rather than XORed, because there are three sources now and a
    # two-way XOR extended by hand is how "exactly one" quietly becomes "at
    # least one". The C side counts the same way: every source flag bumps
    # `sources`, and `sources > 1` refuses with the same words.
    chosen = [stream_remote is not None, inject_port is not None,
              ram_clip_remote is not None]
    if sum(chosen) != 1:
        raise ProvisionError(
            "choose exactly one source: a stream file on the node, an "
            "injection port for the host to connect to, or a RAM-loop clip "
            "on the node"
        )
    if ram_clip_remote is not None and ram_loop is None:
        raise ProvisionError(
            "a RAM-loop source needs its declaration (clip frames, total "
            "frames, PTS stride, budget): a loop has no trailer, and a run "
            "bounded by a signal cannot be compared exactly"
        )
    argv = []
    if spec.ld_library_path:
        # F-C1-1: the node's loader finds the media libraries only through
        # the environment, and spawn()'s `setsid nohup` wrapper preserves a
        # prefix but sets nothing itself — so the launch command carries it.
        argv += ["env", f"LD_LIBRARY_PATH={spec.ld_library_path}"]
    argv.append(remote_binary)
    if ram_clip_remote is not None:
        # Spelled by the single builder in daemon.py, never retyped here.
        argv += ram_loop.as_daemon_args(ram_clip_remote)
    elif stream_remote is not None:
        argv += ["--inject-file", stream_remote]
    else:
        argv += ["--inject-listen", str(inject_port)]
    argv += [
        "--jetson", spec.jetson_host,
        "--measurement-port", str(spec.measurement_port),
        "--control-port", str(spec.control_port),
        "--health-period-ms", str(spec.health_period_ms),
        "--camera-id", str(spec.camera_id),
        "--stats", stats_remote,
    ]
    if packet_log_remote:
        argv += ["--packet-log", packet_log_remote]
    argv += daemon.daemon_args(config, detector=detector)
    return " ".join(shlex.quote(part) for part in argv)


def stop_daemon(transport: NodeTransport, pid: int, grace_s: float = 10.0) -> bool:
    """SIGTERM, then wait for it to write its stats. Returns True if it left.

    Not SIGKILL. ``main.c`` handles SIGTERM by dropping out of the frame loop
    and running ``write_stats``; a killed daemon leaves no counters, and a soak
    with no counters is an hour spent for nothing.
    """
    transport.terminate(pid)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not transport.alive(pid):
            return True
        time.sleep(0.1)
    return False


def collect(
    transport: NodeTransport, spec: NodeSpec, names: list[str], out_dir: str | Path
) -> list[str]:
    """Fetch named artifacts out of the node's run directory."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    for name in names:
        remote = f"{spec.remote_dir.rstrip('/')}/{name}"
        try:
            transport.fetch(remote, out_dir / Path(name).name)
        except ProvisionError:
            # A missing packet log is normal (it is optional); a missing stats
            # file is a finding. Which is which is the caller's business, so
            # this records what arrived and says nothing about what did not.
            continue
        got.append(name)
    return got


def provision_node(
    transport: NodeTransport,
    binary: str | Path,
    config: DetectorConfig,
    spec: NodeSpec,
    out_dir: str | Path,
    stream_local: str | Path | None = None,
    inject_port: int | None = None,
    ram_clip_local: str | Path | None = None,
    ram_loop: daemon.RamLoopDeclaration | None = None,
    detector: str = "ive",
    run_seconds: float | None = None,
    wait_s: float = 120.0,
    packet_log: bool = False,
) -> ProvisionResult:
    """Push, start, wait or stop, collect. The whole bench-session sequence.

    ``run_seconds`` is how long to let it run before SIGTERM. Left None the
    daemon is expected to end on its own — which it does when an injection
    stream reaches its trailer — and this waits up to ``wait_s`` for that.
    """
    out_dir = Path(out_dir)
    remote_binary, local_hash, remote_hash = push_daemon(transport, binary, spec)
    result = ProvisionResult(
        node=spec, transport=transport.name, remote_binary=remote_binary,
        local_sha256=local_hash, remote_sha256=remote_hash,
    )
    if not result.binary_verified:
        raise ProvisionError(
            f"the daemon on {spec.name} does not hash to what was sent: "
            f"local {local_hash} vs node {remote_hash or '(no sha256sum output)'}. "
            "Refusing to start it — a partially transferred binary would "
            "produce numbers about nothing."
        )

    stream_remote = None
    if stream_local is not None:
        stream_remote = f"{spec.remote_dir.rstrip('/')}/inject.swij"
        transport.push(Path(stream_local), stream_remote)

    ram_clip_remote = None
    if ram_clip_local is not None:
        # A separate name from inject.swij on purpose: the two are read by
        # different sources, and one name would make a collected node
        # directory ambiguous about which run produced it.
        ram_clip_remote = f"{spec.remote_dir.rstrip('/')}/ram.swij"
        transport.push(Path(ram_clip_local), ram_clip_remote)

    command = daemon_command(
        remote_binary, config, spec,
        stream_remote=stream_remote, inject_port=inject_port,
        ram_clip_remote=ram_clip_remote, ram_loop=ram_loop,
        stats_remote=f"{spec.remote_dir.rstrip('/')}/stats.json",
        packet_log_remote=(
            f"{spec.remote_dir.rstrip('/')}/packets.hex" if packet_log else None
        ),
        detector=detector,
    )
    result.argv = command
    log_remote = f"{spec.remote_dir.rstrip('/')}/run.log"
    result.pid = transport.spawn(command, log_remote)

    if run_seconds is not None:
        time.sleep(run_seconds)
        stop_daemon(transport, result.pid)
    else:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if not transport.alive(result.pid):
                break
            time.sleep(0.2)
        else:
            # It outlived the wait. Stop it the way that keeps the counters.
            stop_daemon(transport, result.pid)

    wanted = ["stats.json", "run.log"] + (["packets.hex"] if packet_log else [])
    result.collected = collect(transport, spec, wanted, out_dir)
    stats_path = out_dir / "stats.json"
    if stats_path.exists():
        result.stats = json.loads(stats_path.read_text(encoding="utf-8"))
    log_path = out_dir / "run.log"
    if log_path.exists():
        result.log_tail = "\n".join(
            log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Push the edge daemon to a node, run it, collect the results"
    )
    parser.add_argument("--host", required=True, help="node address (or a local dir with --local)")
    parser.add_argument("--local", action="store_true",
                        help="treat --host as a directory on this machine")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--identity", default=None)
    parser.add_argument("--accept-new-host-key", action="store_true",
                        help="first contact with a freshly flashed node")
    parser.add_argument("--binary", default=None, help="defaults to the board build")
    parser.add_argument("--stream", default=None, help="SWIJ stream to push and replay")
    parser.add_argument("--inject-port", type=int, default=None)
    parser.add_argument("--jetson", default="127.0.0.1")
    parser.add_argument("--camera-id", type=int, default=0)
    parser.add_argument("--measurement-port", type=int, default=5601)
    parser.add_argument("--control-port", type=int, default=5602)
    parser.add_argument("--remote-dir", default="/root/skyweave")
    parser.add_argument("--detector", choices=("soft", "ive"), default="ive")
    parser.add_argument("--proc", default=None, help="WxH; defaults to the config's")
    parser.add_argument("--run-seconds", type=float, default=None)
    parser.add_argument("--packet-log", action="store_true")
    parser.add_argument("--out", required=True, help="where collected artifacts land")
    args = parser.parse_args(argv)

    spec = NodeSpec(
        name=args.host, camera_id=args.camera_id, jetson_host=args.jetson,
        measurement_port=args.measurement_port, control_port=args.control_port,
        remote_dir=args.remote_dir, ssh_host="" if args.local else args.host,
        ssh_user=args.user, ssh_port=args.ssh_port,
    )
    transport: NodeTransport
    if args.local:
        transport = LocalTransport(root=Path(args.host))
    else:
        transport = SshTransport(
            spec=spec,
            strict_host_key_checking="accept-new" if args.accept_new_host_key else "yes",
            identity=args.identity,
        )
    config = DetectorConfig(backend="ive_approx")
    if args.proc:
        width, _, height = args.proc.partition("x")
        config = config.model_copy(
            update={"proc_width": int(width), "proc_height": int(height)}
        )
    # The BOARD build by default: provisioning a node with a host binary is a
    # mistake that only shows up as "Exec format error" three steps later.
    default_binary = daemon.FIRMWARE_ROOT / "build-board" / "skyweave-edge"
    binary = Path(args.binary) if args.binary else default_binary
    result = provision_node(
        transport, binary, config, spec, out_dir=args.out,
        stream_local=args.stream, inject_port=args.inject_port,
        detector=args.detector, run_seconds=args.run_seconds,
        packet_log=args.packet_log,
    )
    Path(args.out, "provision.json").write_text(
        json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "CommandResult",
    "LocalTransport",
    "NodeSpec",
    "NodeTransport",
    "ProvisionError",
    "ProvisionResult",
    "SshTransport",
    "collect",
    "daemon_command",
    "provision_node",
    "push_daemon",
    "sha256_of",
    "stop_daemon",
]
