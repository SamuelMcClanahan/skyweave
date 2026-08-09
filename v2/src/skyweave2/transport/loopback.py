"""Three-process loopback rig: the D9 topology minus boards (D7 scope 5).

One engine process and three edge-simulator processes, each replaying one
camera's recorded observations as real UDP datagrams over 127.0.0.1, with a
real TCP control channel carrying START/STOP.

Port handshake: the ENGINE binds ephemeral UDP and TCP ports first and writes
them to a JSON file; the parent waits for that file and only then spawns the
edges. Guessing a "probably free" port is a race that shows up as a flaky
suite months later.

Everything crossing a process boundary is JSON on disk, so the child entry
points stay picklable under the ``spawn`` start method (the default on macOS)
and the rig behaves the same on every platform.

Determinism contract (EXP-001): decisions are reproducible given the SAME
DELIVERED-EVENT STREAM. Arrival interleaving across three independent senders
is not something a UDP rig can promise, so the rig reports the delivered
stream explicitly and W7 compares decisions conditioned on it.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import select
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from skyweave2.contracts import CameraModel, Observation2D
from skyweave2.faults.honesty import decisions_fingerprint
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream
from skyweave2.fusion.metrics import Truth, evaluate_scene
from skyweave2.transport import control as ctl
from skyweave2.transport.adapter import SocketIngestAdapter
from skyweave2.transport.codec import encode_observation_packet
from skyweave2.transport.netfaults import FaultyDatagramSender, NetworkFaultPolicy
from skyweave2.transport.replay import ObservationReplaySource, PacingMode
from skyweave2.transport.udp import UdpReceiver, UdpSender

HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# On-disk interchange
# ---------------------------------------------------------------------------


def write_observations(path: Path, observations: list[Observation2D]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for obs in observations:
            fh.write(json.dumps(obs.model_dump(mode="json"), sort_keys=True) + "\n")


def read_observations(path: Path) -> list[Observation2D]:
    with open(path, encoding="utf-8") as fh:
        return [Observation2D.model_validate(json.loads(line)) for line in fh if line.strip()]


def _write_atomic(path: Path, payload: dict) -> None:
    """Write JSON so a reader never sees a half-written file."""
    handle, temporary = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, sort_keys=True)
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Edge simulator (child process)
# ---------------------------------------------------------------------------


@dataclass
class EdgeSpec:
    camera_id: int
    observations_path: str
    result_path: str
    measurement_port: int
    control_port: int
    mode: str = PacingMode.DETERMINISTIC.value
    speed: float = 1.0
    session_uuid: str = ""
    fault_policy: dict | None = None
    # Shared PTP-style time origin; see ObservationReplaySource.replay.
    start_epoch_ns: int | None = None


def run_edge_node(spec_dict: dict) -> None:
    """Child entry point: replay one camera onto the real measurement socket."""
    spec = EdgeSpec(**spec_dict)
    observations = read_observations(Path(spec.observations_path))
    source = ObservationReplaySource(
        observations, mode=PacingMode(spec.mode), speed=spec.speed
    )
    session = spec.session_uuid or (
        observations[0].envelope.session_uuid if observations else ""
    )

    sender = UdpSender(HOST, spec.measurement_port)
    faulty = (
        FaultyDatagramSender(sender.send, NetworkFaultPolicy(**spec.fault_policy))
        if spec.fault_policy
        else None
    )
    control = ctl.connect(HOST, spec.control_port)
    encode_failures: list[dict] = []
    try:
        ctl.send_control(control, ctl.start_message(spec.camera_id, session))

        def emit(event) -> None:
            try:
                datagram = encode_observation_packet(
                    event.envelope, list(event.observations)
                )
            except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                # An unsendable capture event is a LOUD, recorded failure, not
                # a silently shortened packet. The rig asserts this list is
                # empty on a clean run and the report prints it when not.
                encode_failures.append(
                    {
                        "camera_id": event.envelope.camera_id,
                        "frame_seq": event.envelope.frame_seq,
                        "observations": len(event.observations),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                return
            if faulty is not None:
                faulty.offer(
                    datagram, event.envelope.camera_id, event.envelope.frame_seq
                )
            else:
                sender.send(datagram)

        stats = source.replay(emit, start_epoch_ns=spec.start_epoch_ns)
        if faulty is not None:
            faulty.flush()
        ctl.send_control(control, ctl.stop_message(spec.camera_id, session))
        result = {
            "camera_id": spec.camera_id,
            "events": stats.events,
            "observations_offered": stats.observations,
            "datagrams_sent": sender.counters.datagrams,
            "bytes_sent": sender.counters.bytes_total,
            "sizes": sender.counters.sizes,
            "max_schedule_error_ns": stats.max_schedule_error_ns,
            "schedule_error_ns": stats.schedule_error_ns,
            "tx_monotonic_ns": {
                f"{cam}|{sess}|{seq}": value
                for (cam, sess, seq), value in stats.tx_monotonic_ns.items()
            },
            "due_monotonic_ns": {
                f"{cam}|{sess}|{seq}": value
                for (cam, sess, seq), value in stats.due_monotonic_ns.items()
            },
            "encode_failures": encode_failures,
            "fault_ledger": asdict(faulty.ledger) if faulty is not None else None,
        }
        _write_atomic(Path(spec.result_path), result)
    finally:
        control.close()
        sender.close()


# ---------------------------------------------------------------------------
# Engine process (child process)
# ---------------------------------------------------------------------------


@dataclass
class EngineSpec:
    ports_path: str
    result_path: str
    delivered_path: str
    cameras_path: str
    truth_path: str
    session_uuid: str
    expected_nodes: int = 3
    idle_timeout_s: float = 0.2
    max_idle_polls: int = 8
    deadline_s: float = 180.0
    velocity_convergence_window_batches: int = 0
    velocity_gate_mps: float = float("inf")


def _accept_control_nodes(server: ctl.ControlServer, expected: int,
                          deadline_ns: int) -> list:
    """Accept one control connection per edge node before replay begins."""
    connections = []
    while len(connections) < expected and time.monotonic_ns() < deadline_ns:
        connection = server.accept(timeout_s=0.5)
        if connection is not None:
            connections.append(connection)
    return connections


def run_engine(spec_dict: dict) -> None:
    """Child entry point: bind, ingest, run the UNCHANGED fusion engine, score."""
    spec = EngineSpec(**spec_dict)
    receiver = UdpReceiver(HOST, 0)
    server = ctl.ControlServer(HOST, 0)
    _write_atomic(
        Path(spec.ports_path),
        {"measurement_port": receiver.port, "control_port": server.port},
    )

    deadline_ns = time.monotonic_ns() + int(spec.deadline_s * 1e9)
    connections = _accept_control_nodes(server, spec.expected_nodes, deadline_ns)

    stopped: set[int] = set()
    closed: set[int] = set()

    def poll_control() -> None:
        """Read any control frame that is fully readable, without desyncing.

        ``select`` first, then a SHORT BLOCKING read. Reading the stream in
        non-blocking mode would let a frame's length prefix be consumed while
        its body was still in flight; the partial read would raise, the
        already-consumed prefix would be gone, and every later frame on that
        connection would be misparsed. Framing bugs of that shape look like
        random control-plane loss, so the ordering here is load-bearing.
        """
        for index, connection in enumerate(connections):
            if index in closed:
                continue
            ready, _, _ = select.select([connection], [], [], 0)
            if not ready:
                continue
            connection.settimeout(1.0)
            try:
                message = ctl.recv_control(connection)
            except ctl.ControlChannelClosed:
                closed.add(index)
                continue
            except Exception:  # noqa: BLE001 - recorded by the caller's counters
                closed.add(index)
                continue
            if message.type == ctl.pb.CONTROL_TYPE_STOP:
                stopped.add(message.camera_id)

    def every_node_stopped() -> bool:
        poll_control()
        return len(stopped) >= spec.expected_nodes

    adapter = SocketIngestAdapter(receiver)
    observations = adapter.drain(
        idle_timeout_s=spec.idle_timeout_s,
        max_idle_polls=spec.max_idle_polls,
        stop_when=every_node_stopped,
        poll_evidence=False,
        deadline_s=spec.deadline_s,
    )

    cameras = {
        int(key): CameraModel.model_validate(value)
        for key, value in json.loads(Path(spec.cameras_path).read_text()).items()
    }
    truth_raw = json.loads(Path(spec.truth_path).read_text())
    truth = Truth(
        positions={int(k): np.asarray(v) for k, v in truth_raw["positions"].items()},
        velocity=np.asarray(truth_raw["velocity"]),
        entry_s=truth_raw["entry_s"],
        fps=truth_raw["fps"],
    )

    config = FusionConfig()
    run = run_stream(observations, cameras, config, session_uuid=spec.session_uuid)
    scene = evaluate_scene(
        "socket", observations, cameras, truth, config,
        velocity_convergence_window_batches=spec.velocity_convergence_window_batches,
        velocity_gate_mps=spec.velocity_gate_mps,
    )

    write_observations(Path(spec.delivered_path), observations)
    result = {
        "datagrams": adapter.stats.datagrams,
        "observations": adapter.stats.observations,
        "bytes_total": adapter.stats.bytes_total,
        "sizes": adapter.stats.sizes,
        "rejected": adapter.stats.rejected,
        "deadline_exceeded": adapter.deadline_exceeded,
        "stopped_nodes": sorted(stopped),
        "delivered_fingerprint": adapter.delivered_fingerprint(),
        "decisions_fingerprint": decisions_fingerprint(run),
        "receipts": [
            {
                "camera_id": r.camera_id,
                "session_uuid": r.session_uuid,
                "frame_seq": r.frame_seq,
                "capture_ts_ns": r.capture_ts_ns,
                "observation_count": r.observation_count,
                "datagram_bytes": r.datagram_bytes,
                "rx_monotonic_ns": r.rx_monotonic_ns,
                "rx_clock_domain": r.rx_clock_domain.value,
            }
            for r in adapter.receipts
        ],
        "scene": _scene_payload(scene),
        "rcvbuf_bytes": receiver.rcvbuf_bytes,
    }
    _write_atomic(Path(spec.result_path), result)
    for connection in connections:
        connection.close()
    server.close()
    receiver.close()


def _scene_payload(scene) -> dict:
    """The acceptance-relevant SceneEval fields, JSON-safe."""
    return {
        "range_p95_m": scene.range_p95_m,
        "cross_p95_m": scene.cross_p95_m,
        "cross_p95_random_m": scene.cross_p95_random_m,
        "velocity_rmse_mps": scene.velocity_rmse_mps,
        "velocity_rmse_random_mps": scene.velocity_rmse_random_mps,
        "velocity_rmse_settled_mps": scene.velocity_rmse_settled_mps,
        "velocity_rmse_transient_mps": scene.velocity_rmse_transient_mps,
        "velocity_settling_s": scene.velocity_settling_s,
        "velocity_settled_samples": scene.velocity_settled_samples,
        "velocity_convergence_window_batches":
            scene.velocity_convergence_window_batches,
        "bias_mean_m": scene.bias_mean_m,
        "bias_max_m": scene.bias_max_m,
        "bias_axes_m": list(scene.bias_axes_m),
        "bias_window_batches": scene.bias_window_batches,
        "acquisition_s": scene.acquisition_s,
        "duplicate_confirmed": scene.duplicate_confirmed,
        "published_samples": scene.published_samples,
        "batches": scene.batches,
        "coverage": list(scene.coverage),
        "final_statuses": scene.final_statuses,
        "rejections": scene.rejections,
        "nis_rejects": scene.nis_rejects,
        "excluded": scene.excluded,
    }


# ---------------------------------------------------------------------------
# Parent orchestration
# ---------------------------------------------------------------------------


@dataclass
class LoopbackResult:
    engine: dict
    edges: list[dict]
    delivered: list[Observation2D] = field(default_factory=list)

    @property
    def datagrams_sent(self) -> int:
        return sum(edge["datagrams_sent"] for edge in self.edges)

    @property
    def encode_failures(self) -> list[dict]:
        return [failure for edge in self.edges for failure in edge["encode_failures"]]

    @property
    def datagram_sizes(self) -> list[int]:
        return [size for edge in self.edges for size in edge["sizes"]]

    def _joined(self, reference: str) -> list[int]:
        """rx minus a per-identity send-side reference, in nanoseconds.

        ``time.monotonic_ns`` is a host-wide clock on both Linux and macOS, so
        the two processes' readings are directly comparable. Only the FIRST
        receipt of an identity is joined: a duplicate arrives after its
        original by construction and would skew the statistic.
        """
        send_side: dict[str, int] = {}
        for edge in self.edges:
            send_side.update(edge.get(reference) or {})
        seen: set[str] = set()
        out: list[int] = []
        for receipt in self.engine["receipts"]:
            key = (
                f"{receipt['camera_id']}|{receipt['session_uuid']}"
                f"|{receipt['frame_seq']}"
            )
            if key in seen or key not in send_side:
                continue
            seen.add(key)
            out.append(receipt["rx_monotonic_ns"] - send_side[key])
        return out

    def transit_ns(self) -> list[int]:
        """Pure loopback transit: receive time minus actual send time."""
        return self._joined("tx_monotonic_ns")

    def packet_age_ns(self) -> list[int]:
        """REAL packet age: receive time minus the SCHEDULED capture instant.

        Measured against the due time rather than the send time, so the
        pacer's own scheduling error is inside the age it helped cause.
        WALL_CLOCK mode only; empty in DETERMINISTIC mode, where no wall-clock
        capture instant exists and claiming an age would be an invention.
        """
        return self._joined("due_monotonic_ns")

    def schedule_error_ns(self) -> list[int]:
        return [
            value for edge in self.edges for value in edge.get("schedule_error_ns") or []
        ]


def run_loopback(
    per_camera: dict[int, list[Observation2D]],
    cameras: dict[int, CameraModel],
    truth: Truth,
    session_uuid: str,
    workdir: str | Path,
    mode: PacingMode = PacingMode.DETERMINISTIC,
    speed: float = 1.0,
    fault_policy: NetworkFaultPolicy | None = None,
    velocity_convergence_window_batches: int = 0,
    velocity_gate_mps: float = float("inf"),
    startup_timeout_s: float = 30.0,
    spawn_margin_s: float = 2.0,
) -> LoopbackResult:
    """Run the rig once and return both sides' results.

    ``spawn_margin_s`` is the head start every edge waits out before the
    shared replay epoch, so process spawn cost lands BEFORE the clip starts
    rather than inside it as per-camera skew.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    ports_path = workdir / "ports.json"
    if ports_path.exists():
        ports_path.unlink()

    (workdir / "cameras.json").write_text(
        json.dumps(
            {str(k): v.model_dump(mode="json") for k, v in cameras.items()},
            sort_keys=True,
        )
    )
    (workdir / "truth.json").write_text(
        json.dumps(
            {
                "positions": {
                    str(k): [float(x) for x in v] for k, v in truth.positions.items()
                },
                "velocity": [float(x) for x in truth.velocity],
                "entry_s": truth.entry_s,
                "fps": truth.fps,
            },
            sort_keys=True,
        )
    )

    context = mp.get_context("spawn")
    engine_spec = EngineSpec(
        ports_path=str(ports_path),
        result_path=str(workdir / "engine.json"),
        delivered_path=str(workdir / "delivered.jsonl"),
        cameras_path=str(workdir / "cameras.json"),
        truth_path=str(workdir / "truth.json"),
        session_uuid=session_uuid,
        expected_nodes=len(per_camera),
        velocity_convergence_window_batches=velocity_convergence_window_batches,
        velocity_gate_mps=velocity_gate_mps,
    )
    engine = context.Process(target=run_engine, args=(asdict(engine_spec),))
    engine.start()

    deadline = time.monotonic() + startup_timeout_s
    ports = None
    while time.monotonic() < deadline:
        if ports_path.exists():
            try:
                ports = json.loads(ports_path.read_text())
                break
            except json.JSONDecodeError:  # pragma: no cover - atomic write guards
                pass
        if not engine.is_alive():
            # Report why it died instead of blaming the handshake. Under the
            # `spawn` start method the child re-imports the parent's __main__,
            # so an unguarded caller script is the usual cause.
            raise RuntimeError(
                f"engine process exited with code {engine.exitcode} before "
                "publishing its ports. Under the 'spawn' start method the "
                "child re-imports the caller's __main__ module: call "
                "run_loopback() from inside an `if __name__ == \"__main__\":` "
                "guard, or from an imported module (pytest is fine)."
            )
        time.sleep(0.02)
    if ports is None:
        engine.terminate()
        engine.join()
        raise RuntimeError(
            f"engine process never published its ports within {startup_timeout_s}s"
        )

    # One epoch for every camera: PTP-disciplined nodes capture frame N at the
    # same instant, and the aligner reads capture time, not arrival time.
    start_epoch_ns = time.monotonic_ns() + int(
        spawn_margin_s * len(per_camera) * 1e9
    )
    edge_processes = []
    for camera_id, observations in sorted(per_camera.items()):
        observations_path = workdir / f"cam{camera_id}.jsonl"
        write_observations(observations_path, observations)
        spec = EdgeSpec(
            camera_id=camera_id,
            observations_path=str(observations_path),
            result_path=str(workdir / f"edge{camera_id}.json"),
            measurement_port=ports["measurement_port"],
            control_port=ports["control_port"],
            mode=mode.value,
            speed=speed,
            fault_policy=asdict(fault_policy) if fault_policy else None,
            start_epoch_ns=start_epoch_ns,
        )
        process = context.Process(target=run_edge_node, args=(asdict(spec),))
        process.start()
        edge_processes.append(process)

    for process in edge_processes:
        process.join(timeout=startup_timeout_s + 240.0)
    engine.join(timeout=startup_timeout_s + 240.0)
    for process in [*edge_processes, engine]:
        if process.is_alive():  # pragma: no cover - only on a hung run
            process.terminate()
            process.join()
        if process.exitcode != 0:
            raise RuntimeError(
                f"loopback child exited with code {process.exitcode}"
            )

    engine_result = json.loads((workdir / "engine.json").read_text())
    edges = [
        json.loads((workdir / f"edge{camera_id}.json").read_text())
        for camera_id in sorted(per_camera)
    ]
    delivered = read_observations(workdir / "delivered.jsonl")
    return LoopbackResult(engine=engine_result, edges=edges, delivered=delivered)


def split_by_camera(
    observations: list[Observation2D],
) -> dict[int, list[Observation2D]]:
    """One replay list per camera, arrival order within a camera preserved."""
    out: dict[int, list[Observation2D]] = {}
    for obs in observations:
        out.setdefault(obs.envelope.camera_id, []).append(obs)
    return out


@dataclass
class InProcessResult:
    """Result of a single-sender socket replay."""

    delivered: list[Observation2D]
    adapter: SocketIngestAdapter
    sender: UdpSender
    encode_failures: list[dict] = field(default_factory=list)
    fault_ledger: object | None = None


def socket_replay_inprocess(
    observations: list[Observation2D],
    fault_policy: NetworkFaultPolicy | None = None,
    raise_on_reject: bool = True,
    mode: PacingMode = PacingMode.DETERMINISTIC,
    rcvbuf_bytes: int = 8 * 1024 * 1024,
) -> InProcessResult:
    """Push one ALREADY-MERGED arrival order through real UDP, then drain.

    This is the single-source form of the socket path: one sender emitting the
    exact arrival order that file replay would have consumed, so a difference
    in the result can only come from the wire, the codec, or the adapter. The
    three-process rig covers the multi-source case, where interleaving is a
    property of the deployment rather than of the recording.

    Send-then-drain is safe here because the kernel receive buffer is sized
    far above the whole clip; the rig is where concurrent send/receive is
    exercised.
    """
    receiver = UdpReceiver(HOST, 0, rcvbuf_bytes=rcvbuf_bytes)
    sender = UdpSender(HOST, receiver.port)
    faulty = (
        FaultyDatagramSender(sender.send, fault_policy)
        if fault_policy is not None
        else None
    )
    encode_failures: list[dict] = []
    source = ObservationReplaySource(observations, mode=mode)
    try:
        def emit(event) -> None:
            try:
                datagram = encode_observation_packet(
                    event.envelope, list(event.observations)
                )
            except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                encode_failures.append(
                    {
                        "camera_id": event.envelope.camera_id,
                        "frame_seq": event.envelope.frame_seq,
                        "observations": len(event.observations),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                return
            if faulty is not None:
                faulty.offer(
                    datagram, event.envelope.camera_id, event.envelope.frame_seq
                )
            else:
                sender.send(datagram)

        source.replay(emit)
        if faulty is not None:
            faulty.flush()
        adapter = SocketIngestAdapter(receiver, raise_on_reject=raise_on_reject)
        delivered = adapter.drain(
            idle_timeout_s=0.2, max_idle_polls=3, poll_evidence=False
        )
        return InProcessResult(
            delivered=delivered,
            adapter=adapter,
            sender=sender,
            encode_failures=encode_failures,
            fault_ledger=faulty.ledger if faulty is not None else None,
        )
    finally:
        sender.close()
        receiver.close()
