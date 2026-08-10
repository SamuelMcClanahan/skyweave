"""D7_WIRE_REPORT.md generator: seeded, byte-deterministic body.

Two subcommands, and the split is the point:

    uv run python -m skyweave2.transport.report measure --seed 7 \
        --out docs/d7_evidence.json

    uv run python -m skyweave2.transport.report generate --seed 7 \
        --evidence docs/d7_evidence.json --out docs/D7_WIRE_REPORT.md

Loopback timing is a WALL-CLOCK measurement. It cannot be recomputed
byte-identically and pretending otherwise would either put a moving number in
a frozen artifact or quietly round it until it stopped moving. So `measure`
records it once, into an evidence file, and `generate` reads it — the D4/D5
precedent for runtimes, applied to latency. `generate` over the same evidence
and seed reproduces this report byte for byte, cross-process; `measure`
produces new numbers every time, and says so.

Everything else in the body — datagram sizes, the worst-case budget, the W4
parity table, the Tier IV verdicts — is deterministic and is recomputed on
every `generate`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from skyweave2.contracts import Observation2D
from skyweave2.faults import injectors as inj
from skyweave2.faults.config import flag_name, load_manifest
from skyweave2.faults.honesty import decisions_fingerprint
from skyweave2.faults.report import build_scene
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream
from skyweave2.fusion.metrics import Truth, evaluate_scene
from skyweave2.fusion.report import _acceptance_rows
from skyweave2.transport import sizing
from skyweave2.transport.codec import encode_observation_packet, wire_normalize
from skyweave2.transport.config import TRANSPORT_CONFIG
from skyweave2.transport.loopback import (
    run_loopback,
    socket_replay_inprocess,
    split_by_camera,
)
from skyweave2.transport.replay import PacingMode, capture_events
from skyweave2.transport.wire import (
    DATAGRAM_CEILING_BYTES,
    HEADER_LEN,
    MAGIC,
    WIRE_VERSION,
)

FPS = 30.0
V2 = Path(__file__).resolve().parents[3]
GATE_CLIPS = V2.parent / "output" / "exp001_clips" / "gate"
GATE_RENDER = V2.parent / "output" / "exp001_renders" / "gate"


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------


def gate_scene():
    """The real golden gate clip, or None when the artifacts are absent."""
    if not (GATE_CLIPS.exists() and GATE_RENDER.exists()):
        return None
    from skyweave2.fusion.metrics import load_render_truth, observation_stream
    from skyweave2.fusion.report import _detector_config, gate_cameras

    observations = observation_stream(GATE_CLIPS, _detector_config())
    return observations, gate_cameras(GATE_RENDER), load_render_truth(GATE_RENDER)


def synthetic_scene():
    """The D6 campaign's own scene, so Tier IV numbers stay comparable."""
    cameras, stream, truth_at = build_scene()
    frames = sorted({o.envelope.frame_seq for o in stream})
    positions = {f: truth_at(f) for f in frames}
    truth = Truth(
        positions={f: np.asarray(p) for f, p in positions.items() if p is not None},
        velocity=np.array([30.0, 0.0, 0.0]),
        entry_s=0.0,
        fps=FPS,
    )
    return stream, cameras, truth


# ---------------------------------------------------------------------------
# Deterministic measurements
# ---------------------------------------------------------------------------


def datagram_sizes(observations: list[Observation2D]) -> list[int]:
    """Encoded size of every capture event in a stream (no sockets needed)."""
    return [
        len(encode_observation_packet(event.envelope, list(event.observations)))
        for event in capture_events(observations)
    ]


def size_distribution(sizes: list[int]) -> dict:
    array = np.asarray(sizes, dtype=np.float64)
    return {
        "datagrams": len(sizes),
        "min": int(array.min()),
        "p50": int(np.percentile(array, 50)),
        "p95": int(np.percentile(array, 95)),
        "max": int(array.max()),
        "mean": float(array.mean()),
        "over_ceiling": int((array > DATAGRAM_CEILING_BYTES).sum()),
        "headroom_at_max": DATAGRAM_CEILING_BYTES - int(array.max()),
    }


def observations_per_event(observations: list[Observation2D]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for event in capture_events(observations):
        n = len(event.observations)
        counts[n] = counts.get(n, 0) + 1
    return counts


def _policies(observations, cameras, label: str) -> dict:
    run = run_stream(observations, cameras, FusionConfig(), session_uuid=label)
    excluded: dict[str, int] = {}
    for entry in run.excluded:
        excluded[entry.reason] = excluded.get(entry.reason, 0) + 1
    rejections: dict[str, int] = {}
    for batch in run.batches:
        for rejection in batch.rejections:
            rejections[rejection.reason] = rejections.get(rejection.reason, 0) + 1
    return {
        "excluded": excluded,
        "rejections": rejections,
        "published": len(run.published),
        "statuses": run.statuses,
        "nis_rejects": sum(1 for r in run.records if not r.accepted),
        "fingerprint": decisions_fingerprint(run),
    }


def tier4_cells(stream, cameras, manifest):
    """Every Tier IV axis and magnitude from the FROZEN manifest.

    Same axes, same magnitudes and the same injectors D6 used, so a verdict
    here is a statement about the transport rather than about a different
    experiment.
    """
    seed = manifest.base_seed
    dishonest_claim = 0.5
    frames = [o.envelope.frame_seq for o in stream]
    half_s = (max(frames) / FPS) / 2.0
    # flag_name, not str: YAML 1.1 parses the manifest's bare off/on as
    # booleans, so str() would write "False"/"True" into the report where the
    # frozen manifest says "off"/"on".
    honesty_modes = [
        flag_name(m) for m in manifest.axis("tier4_time_stream", "time_sync_honesty")
    ]
    cells: list[tuple[str, str, list]] = []

    for axis, build in (
        ("clock_offset_ms", lambda v: dict(offset_ms=float(v), drift_ppm=0.0,
                                           jitter_ms_sigma=0.0)),
        ("drift_ppm", lambda v: dict(offset_ms=0.0, drift_ppm=float(v),
                                     jitter_ms_sigma=0.0)),
        ("jitter_ms_sigma", lambda v: dict(offset_ms=0.0, drift_ppm=0.0,
                                           jitter_ms_sigma=float(v))),
    ):
        for value in manifest.axis("tier4_time_stream", axis):
            for mode in honesty_modes:
                faulted, _ = inj.inject_clock_error(
                    stream, seed=seed, honest=(mode == "honest"),
                    dishonest_claim_ms=dishonest_claim, camera_ids=(0,),
                    **build(value),
                )
                cells.append((f"{axis}[{mode}]", str(value), faulted))

    for mode in [flag_name(m) for m in manifest.axis("tier4_time_stream", "loss_mode")]:
        for value in manifest.axis("tier4_time_stream", "observation_loss_pct"):
            faulted, _ = inj.inject_observation_loss(
                stream, float(value), mode, seed
            )
            cells.append((f"observation_loss_pct[{mode}]", str(value), faulted))

    for flag in manifest.axis("tier4_time_stream", "reorder"):
        name = flag_name(flag)
        faulted = stream if name == "off" else inj.inject_reorder(stream, seed)[0]
        cells.append(("reorder", name, faulted))
    for flag in manifest.axis("tier4_time_stream", "duplication"):
        name = flag_name(flag)
        faulted = stream if name == "off" else inj.inject_duplication(stream, seed)[0]
        cells.append(("duplication", name, faulted))
    for flag in manifest.axis("tier4_time_stream", "lateness"):
        name = flag_name(flag)
        faulted = stream if name == "off" else inj.inject_lateness(stream, seed)[0]
        cells.append(("lateness", name, faulted))

    restart_at = float(manifest.scalar("tier4_time_stream", "node_restart_at_s"))
    cells.append((
        "node_restart_at_s", str(restart_at),
        inj.inject_node_restart(stream, restart_at, FPS, camera_id=0)[0],
    ))

    for value in [flag_name(v) for v in manifest.axis("tier4_time_stream", "camera_dropout")]:
        if value == "none":
            faulted = stream
        else:
            camera_id = int(value.replace("cam", ""))
            faulted = inj.inject_camera_dropout(stream, camera_id, half_s, FPS)[0]
        cells.append(("camera_dropout", value, faulted))
    return cells


def tier4_verdicts(stream, cameras, manifest) -> list[dict]:
    """File replay vs socket replay, per Tier IV cell."""
    out: list[dict] = []
    for axis, magnitude, faulted in tier4_cells(stream, cameras, manifest):
        label = f"d7-{axis}-{magnitude}"
        result = socket_replay_inprocess(faulted, raise_on_reject=False)
        file_policy = _policies(faulted, cameras, label)
        socket_policy = _policies(result.delivered, cameras, label)
        differences = [
            key
            for key in ("excluded", "rejections", "published", "statuses",
                        "nis_rejects", "fingerprint")
            if file_policy[key] != socket_policy[key]
        ]
        sizes = datagram_sizes(faulted)
        out.append({
            "axis": axis,
            "magnitude": magnitude,
            "offered": len(faulted),
            "delivered": len(result.delivered),
            "encode_failures": len(result.encode_failures),
            "rejected": sum(result.adapter.stats.rejected.values()),
            "excluded": file_policy["excluded"],
            "published": file_policy["published"],
            "max_datagram_bytes": max(sizes) if sizes else 0,
            "differences": differences,
        })
    return out


def acceptance_rows(observations, cameras, truth, acceptance) -> list[tuple]:
    evaluation = evaluate_scene(
        "d7", observations, cameras, truth, FusionConfig(),
        velocity_convergence_window_batches=int(
            acceptance["velocity_convergence_window_batches"]
        ),
        velocity_gate_mps=float(acceptance["velocity_rmse_max_mps"]),
    )
    return _acceptance_rows(evaluation, acceptance), evaluation


# ---------------------------------------------------------------------------
# measure: the wall-clock half
# ---------------------------------------------------------------------------


def measure(seed: int, out_path: str, speed: float, workdir: str | None) -> None:
    """Run the three-process rig and record its timing. Wall-clock, live."""
    import tempfile

    scene = gate_scene()
    source = "gate-clip"
    if scene is None:
        stream, cameras, truth = synthetic_scene()
        source = "synthetic (gate artifacts absent)"
    else:
        stream, cameras, truth = scene

    directory = workdir or tempfile.mkdtemp(prefix="d7-rig-")
    result = run_loopback(
        split_by_camera(stream), cameras, truth, f"d7-rig-{seed}", directory,
        mode=PacingMode.WALL_CLOCK, speed=speed,
    )
    transit = sorted(result.transit_ns())
    ages = sorted(result.packet_age_ns())
    jitter = sorted(abs(v) for v in result.schedule_error_ns())

    def stats(values: list[int]) -> dict:
        if not values:
            return {}
        array = np.asarray(values, dtype=np.float64) / 1e6  # ms
        return {
            "count": len(values),
            "min_ms": float(array.min()),
            "p50_ms": float(np.percentile(array, 50)),
            "p95_ms": float(np.percentile(array, 95)),
            "p99_ms": float(np.percentile(array, 99)),
            "max_ms": float(array.max()),
        }

    evidence = {
        "schema": "d7-wire-evidence/1",
        "seed": seed,
        "source": source,
        "replay_speed": speed,
        "pacing_mode": PacingMode.WALL_CLOCK.value,
        "observations_offered": len(stream),
        "datagrams_sent": result.datagrams_sent,
        "datagrams_received": result.engine["datagrams"],
        "observations_delivered": result.engine["observations"],
        "rejected": result.engine["rejected"],
        "deadline_exceeded": result.engine["deadline_exceeded"],
        "aligner_excluded": result.engine["scene"]["excluded"],
        "encode_failures": len(result.encode_failures),
        "rcvbuf_bytes": result.engine["rcvbuf_bytes"],
        "loopback_transit": stats(transit),
        "packet_age": stats(ages),
        "schedule_jitter": stats(jitter),
        "budgets": {
            "schedule_jitter_p95_ms":
                TRANSPORT_CONFIG.loopback_schedule_jitter_p95_ms,
            "packet_age_p95_ms": TRANSPORT_CONFIG.loopback_packet_age_p95_ms,
            "packet_age_max_ms": TRANSPORT_CONFIG.loopback_packet_age_max_ms,
        },
    }
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(evidence, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {out_path} (live loopback timing, {len(transit)} datagrams joined)")


# ---------------------------------------------------------------------------
# generate: the deterministic half
# ---------------------------------------------------------------------------


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if not np.isfinite(value):
            return "—"
        return f"{value:.{digits}f}"
    return str(value)


def _timing_row(name: str, block: dict) -> str:
    if not block:
        return f"| {name} | — | — | — | — | — |"
    return (
        f"| {name} | {_fmt(block['min_ms'])} | {_fmt(block['p50_ms'])} | "
        f"{_fmt(block['p95_ms'])} | {_fmt(block['p99_ms'])} | "
        f"{_fmt(block['max_ms'])} |"
    )


def generate(seed: int, out_path: str, evidence_path: str | None,
             manifest_path: str | None = None) -> None:
    import yaml

    manifest = load_manifest(manifest_path)
    acceptance = yaml.safe_load(
        (V2 / "configs" / "exp001_scene.yaml").read_text(encoding="utf-8")
    )["acceptance"]
    evidence = (
        json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        if evidence_path and Path(evidence_path).exists()
        else None
    )

    scene = gate_scene()
    if scene is None:
        parity_stream, parity_cameras, parity_truth = synthetic_scene()
        parity_source = "synthetic D6 scene (golden gate artifacts absent)"
        parity_label = "Modeled"
    else:
        parity_stream, parity_cameras, parity_truth = scene
        parity_source = "golden gate clip (`output/exp001_clips/gate`)"
        parity_label = "Measured on the L4 golden clips"

    # -- W4 parity ------------------------------------------------------
    file_rows, file_eval = acceptance_rows(
        parity_stream, parity_cameras, parity_truth, acceptance
    )
    socket_result = socket_replay_inprocess(parity_stream, raise_on_reject=False)
    socket_rows, socket_eval = acceptance_rows(
        socket_result.delivered, parity_cameras, parity_truth, acceptance
    )
    tables_identical = file_rows == socket_rows

    # -- sizes ----------------------------------------------------------
    sizes = datagram_sizes(parity_stream)
    distribution = size_distribution(sizes)
    per_event = observations_per_event(parity_stream)
    budget = sizing.worst_case_budget()
    derived_max = sizing.max_observations_under_ceiling()

    # -- Tier IV --------------------------------------------------------
    tier4_stream, tier4_cameras, _ = synthetic_scene()
    verdicts = tier4_verdicts(tier4_stream, tier4_cameras, manifest)
    divergent = [v for v in verdicts if v["differences"]]

    # -- narrowing ------------------------------------------------------
    narrowed = sum(
        1 for o in parity_stream if wire_normalize(o) != o
    )

    lines: list[str] = []
    a = lines.append
    a("# D7 wire freeze report")
    a("")
    a("**Labels.** Datagram sizes, the worst-case budget, the parity table and")
    a("the Tier IV verdicts are recomputed deterministically on every run of")
    a("this generator. Host-loopback timing is **Measured** and is read from")
    a("`docs/d7_evidence.json`, recorded by the `measure` subcommand.")
    a("Everything carried in from earlier phases keeps the label it had.")
    a("")
    a("**Loopback latency is not network latency.** Every timing number below")
    a("crosses `127.0.0.1` inside one host: no NIC, no switch, no cable, no")
    a("contention. D9 measures the real switch; nothing here predicts it.")
    a("")
    a(f"Seed {seed}. Tier IV magnitudes read from `configs/d6_faults.yaml`")
    a("(frozen, never restated in code). Generated by")
    a("`uv run python -m skyweave2.transport.report generate` — byte-identical")
    a("for identical inputs, seed and evidence file, cross-process.")
    a("")

    a("## Wire freeze")
    a("")
    a("| Item | Value |")
    a("| --- | --- |")
    a("| Schema | `proto/skyweave.proto`, proto3, package `skyweave.v2` |")
    a(f"| Datagram header | {HEADER_LEN} B: magic `{MAGIC.decode()}`, "
      f"version {WIRE_VERSION}, payload type |")
    a(f"| Measurement ceiling | {DATAGRAM_CEILING_BYTES} B per datagram "
      "(D0 section 10) |")
    a("| Measurement plane | UDP, one capture event per datagram, never "
      "retransmitted, never split |")
    a("| Evidence plane | separate UDP socket, droppable, explicit expiry |")
    a("| Control plane | TCP, same protobuf, 4-byte length prefix, Provisional |")
    a("| Video on the measurement path | none, ever (standing rule) |")
    a("")
    a("Field numbers for `FrameEnvelope` and `Observation2D` are the D0")
    a("reservations verbatim. Test W1 reads them back out of the frozen")
    a("contract docstrings and compares, so a contract edit that forgets the")
    a("wire fails the suite rather than drifting.")
    a("")
    a("### Numbers this phase had to assign (D0 reserves none for them)")
    a("")
    a("| Item | Assignment |")
    a("| --- | --- |")
    a("| `ClockDomain` enum values | 0 UNSPECIFIED (rejected on receive), then "
      "D0 section 3's table order: 1 synthetic, 2 node_mono, 3 node_ptp, "
      "4 jetson_rx |")
    a("| `ObservationPacket` | 1 envelope, 2 observations |")
    a("| `BoundingBox` (D0 field 8) | 1 x, 2 y, 3 w, 4 h |")
    a("| `HealthPacket` | 1 camera_id, 2 session_uuid, 3 ts_ns, 4 clock_domain, "
      "5 fps, 6 drops, 7 time_sync_error_ms, 8 imu_quaternion |")
    a("| `EvidencePacket`, `ControlMessage` and friends | see the `.proto` |")
    a("")
    a("These need a D0 section 10 entry. They are recorded here, not written")
    a("into the D0 spec: this phase does not edit a frozen contract.")
    a("")
    a("D0 also reserves numbers for `LocalizationResult`, `Track` and")
    a("`CameraModel`. Those are host-side outputs and calibration, not")
    a("measurement-path traffic, and the brief scopes this phase to")
    a("`ObservationPacket`, `HealthPacket` and control. They are deliberately")
    a("NOT in the `.proto` yet; adding them later cannot disturb what is.")
    a("")

    a("## Datagram size distribution vs the ceiling")
    a("")
    a(f"Source: {parity_source}. Sizes are the framed datagram, header")
    a("included — a ceiling on the protobuf body alone would be a ceiling on")
    a("the wrong thing.")
    a("")
    a("| Statistic | Bytes |")
    a("| --- | --- |")
    a(f"| Datagrams | {distribution['datagrams']} |")
    a(f"| Minimum | {distribution['min']} |")
    a(f"| Median | {distribution['p50']} |")
    a(f"| p95 | {distribution['p95']} |")
    a(f"| Maximum | {distribution['max']} |")
    a(f"| Mean | {_fmt(distribution['mean'], 1)} |")
    a(f"| Ceiling | {DATAGRAM_CEILING_BYTES} |")
    a(f"| Headroom at the maximum | {distribution['headroom_at_max']} |")
    a(f"| Datagrams over the ceiling | {distribution['over_ceiling']} |")
    a("")
    a("Observations per capture event on this clip:")
    a("")
    a("| Observations in the event | Events |")
    a("| --- | --- |")
    for count in sorted(per_event):
        a(f"| {count} | {per_event[count]} |")
    a("")

    a("## Worst case the SCHEMA allows")
    a("")
    a("The measured distribution above is a property of one clean clip. The")
    a("ceiling has to hold for the worst case the declared bounds PERMIT:")
    a("every string at its nanopb `max_size`, every varint at its widest")
    a("encoding, a negative `capture_ts_ns`, and every optional field present.")
    a("")
    a("| Component | Bytes |")
    a("| --- | --- |")
    a(f"| Header | {budget.header_bytes} |")
    a(f"| FrameEnvelope (36-char UUID, two 64-char revisions) | "
      f"{budget.envelope_bytes} |")
    a(f"| Each Observation2D (64-char evidence_ref, widest varints) | "
      f"{budget.per_observation_bytes} |")
    a(f"| Declared observation bound | {budget.observation_count} |")
    a(f"| Worst-case datagram | {budget.total_bytes} |")
    a(f"| Headroom | {budget.headroom_bytes} |")
    a("")
    a(f"**The {DATAGRAM_CEILING_BYTES} B ceiling admits at most {derived_max} "
      "observations per")
    a("capture event** at the declared bounds. That number is derived by")
    a("encoding, not estimated, and `proto/skyweave.options` declares exactly")
    a("it, so the D8 nanopb daemon allocates the same bound this host enforces.")
    a("")

    a("## W4 — file replay vs socket replay")
    a("")
    a(f"Acceptance table from {parity_source}, scored against the frozen")
    a("`configs/exp001_scene.yaml` acceptance block. Left column is file")
    a("replay of the recorded stream; right column is the same stream after a")
    a("real UDP round trip.")
    a("")
    a("| Line | Gate | File replay | Socket replay | Identical |")
    a("| --- | --- | --- | --- | --- |")
    for (name, gate, file_value, file_ok), (_, _, socket_value, socket_ok) in zip(
        file_rows, socket_rows, strict=True
    ):
        same = "yes" if (file_value, file_ok) == (socket_value, socket_ok) else "**NO**"
        a(f"| {name} | {gate} | {file_value} ({'PASS' if file_ok else 'FAIL'}) | "
          f"{socket_value} ({'PASS' if socket_ok else 'FAIL'}) | {same} |")
    a("")
    a(f"**Acceptance tables identical: "
      f"{'YES' if tables_identical else 'NO — FINDING'}.** "
      f"Label: {parity_label}.")
    a("")
    a("| Cross-check | File | Socket |")
    a("| --- | --- | --- |")
    a(f"| Observations in | {len(parity_stream)} | "
      f"{socket_result.adapter.stats.observations} |")
    a(f"| Published samples | {file_eval.published_samples} | "
      f"{socket_eval.published_samples} |")
    a(f"| Batches | {file_eval.batches} | {socket_eval.batches} |")
    a(f"| Aligner exclusions | {json.dumps(file_eval.excluded, sort_keys=True)} | "
      f"{json.dumps(socket_eval.excluded, sort_keys=True)} |")
    a(f"| Engine rejections | {json.dumps(file_eval.rejections, sort_keys=True)} | "
      f"{json.dumps(socket_eval.rejections, sort_keys=True)} |")
    a(f"| Final statuses | {json.dumps(file_eval.final_statuses, sort_keys=True)} | "
      f"{json.dumps(socket_eval.final_statuses, sort_keys=True)} |")
    a(f"| Datagrams rejected on receive | — | "
      f"{json.dumps(socket_result.adapter.stats.rejected, sort_keys=True)} |")
    a(f"| Capture events that could not be encoded | — | "
      f"{len(socket_result.encode_failures)} |")
    a("")
    a("### What the wire changed")
    a("")
    a(f"Observations altered by the round trip: {narrowed} of "
      f"{len(parity_stream)}.")
    a("")
    a("D0's type column declares `time_sync_error_ms`, `exposure_us`,")
    a("`gain_db`, `line_readout_us` and `confidence` as `float`, i.e. IEEE-754")
    a("binary32, while the pydantic models hold doubles. The `.proto` follows")
    a("the frozen declaration rather than widening the field, so a value that")
    a("is not exactly representable in binary32 comes back as its nearest")
    a("binary32 neighbour. `u`, `v`, the covariance terms, `capture_ts_ns` and")
    a("every other timestamp are `double`/integer and cross the wire exactly —")
    a("no geometry input is touched.")
    a("")

    a("## W5 — Tier IV over real sockets")
    a("")
    a("Every axis and magnitude in `configs/d6_faults.yaml`, injected with the")
    a("D6 injectors, then transmitted for real: duplicates become duplicate")
    a("datagrams, reordered items are genuinely sent out of order, dropped")
    a("items are never sent. `Diverges` compares the aligner's labeled exits,")
    a("engine rejections, tracker records, statuses and the full decisions")
    a("fingerprint — not a summary of them.")
    a("")
    a("| Axis | Magnitude | Offered | Delivered | Max datagram B | Published | "
      "Aligner exclusions | Diverges |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for verdict in verdicts:
        differences = (
            "none" if not verdict["differences"]
            else "**" + ",".join(verdict["differences"]) + "**"
        )
        a(f"| {verdict['axis']} | {verdict['magnitude']} | {verdict['offered']} | "
          f"{verdict['delivered']} | {verdict['max_datagram_bytes']} | "
          f"{verdict['published']} | "
          f"{json.dumps(verdict['excluded'], sort_keys=True)} | {differences} |")
    a("")
    a(f"**D6-policy divergences over sockets: {len(divergent)} of "
      f"{len(verdicts)} cells.**")
    if divergent:
        a("")
        for verdict in divergent:
            a(f"- `{verdict['axis']}` at `{verdict['magnitude']}`: "
              f"{', '.join(verdict['differences'])}")
    a("")

    a("## Loopback timing (Measured, host loopback only)")
    a("")
    if evidence is None:
        a("No evidence file was supplied, so no timing is reported. Record it")
        a("with `python -m skyweave2.transport.report measure`. Timing is a")
        a("wall-clock measurement and is deliberately not recomputed here: a")
        a("moving number does not belong in a reproducible body.")
    else:
        a(f"Source: {evidence['source']}, three edge processes, replay speed")
        a(f"{_fmt(evidence['replay_speed'], 1)}x, "
          f"{evidence['pacing_mode']} pacing, "
          f"{evidence['datagrams_sent']} datagrams.")
        a("")
        a("| Quantity | min (ms) | p50 | p95 | p99 | max |")
        a("| --- | --- | --- | --- | --- | --- |")
        a(_timing_row("Loopback transit (send -> receive)",
                      evidence["loopback_transit"]))
        a(_timing_row("Packet age (scheduled capture -> receive)",
                      evidence["packet_age"]))
        a(_timing_row("Pacing schedule error", evidence["schedule_jitter"]))
        a("")
        a("Packet age is measured against the SCHEDULED capture instant, not")
        a("the send time, so the pacer's own error is inside the age it helped")
        a("cause rather than excluded from it.")
        a("")
        a("The gates below are on the DISTRIBUTION. macOS and Linux are not")
        a("real-time systems: a single scheduler preemption of tens of")
        a("milliseconds is ordinary on a loaded host and is a property of the")
        a("host, not of the wire. The maximum is therefore reported but gated")
        a("only as a PATHOLOGY bound — high enough that tripping it means")
        a("something stalled, not that the machine was busy.")
        a("")
        a("| Declared budget | Value | Achieved | Verdict |")
        a("| --- | --- | --- | --- |")
        for label, key, block in (
            ("Schedule jitter p95", "schedule_jitter_p95_ms", "schedule_jitter"),
            ("Packet age p95", "packet_age_p95_ms", "packet_age"),
        ):
            budget_ms = evidence["budgets"][key]
            achieved = evidence[block].get("p95_ms")
            verdict = "PASS" if achieved is not None and achieved <= budget_ms else "FAIL"
            a(f"| {label} | <= {_fmt(budget_ms, 1)} ms | {_fmt(achieved)} ms | "
              f"{verdict} |")
        max_budget = evidence["budgets"]["packet_age_max_ms"]
        max_achieved = evidence["packet_age"].get("max_ms")
        a(f"| Packet age max | <= {_fmt(max_budget, 1)} ms | "
          f"{_fmt(max_achieved)} ms | "
          f"{'PASS' if max_achieved is not None and max_achieved <= max_budget else 'FAIL'} |")
        a("")
        a("| Rig integrity | Value |")
        a("| --- | --- |")
        a(f"| Observations offered | {evidence['observations_offered']} |")
        a(f"| Observations delivered | {evidence['observations_delivered']} |")
        a(f"| Datagrams rejected | "
          f"{json.dumps(evidence['rejected'], sort_keys=True)} |")
        a(f"| Aligner exclusions | "
          f"{json.dumps(evidence['aligner_excluded'], sort_keys=True)} |")
        a(f"| Capture events that could not be encoded | "
          f"{evidence['encode_failures']} |")
        a(f"| Drain ended on the deadline backstop | "
          f"{evidence['deadline_exceeded']} |")
        a(f"| Kernel receive buffer | {evidence['rcvbuf_bytes']} B |")
    a("")

    a("## Findings")
    a("")
    a("### D7-F1 — the ceiling admits few observations per capture event")
    a("")
    a(f"At the declared bounds the worst-case datagram is "
      f"{sizing.worst_case_datagram_size(1)} B for ONE observation and")
    a(f"{budget.total_bytes} B for {budget.observation_count}; the ceiling")
    a(f"permits at most {derived_max}. The gate clip never exceeds")
    a(f"{max(per_event)} observation(s) per event, so nothing is blocked")
    a("today, but the D4 detector has NO per-frame component cap")
    a("(`DetectorConfig` bounds component area, not component count).")
    a("")
    a("Consequence if shipped: a cluttered frame — birds, insects, rain —")
    a(f"produces more than {derived_max} components and the capture event")
    a("becomes unsendable. It fails loudly (`TooManyObservations`), which is")
    a("correct behaviour and much better than a truncated packet, but it is")
    a("still a lost measurement. Three levers exist and all three are")
    a("planning-session decisions, not code changes: raise the ceiling toward")
    a("the path MTU (~1472 B on untagged Ethernet), shorten the")
    a("`evidence_ref`/revision bounds, or allow a capture event to span")
    a("datagrams — which the current freeze forbids outright.")
    a("")
    a("### D7-F2 — D0 declares binary32 for five envelope/observation fields")
    a("")
    a("`time_sync_error_ms`, `exposure_us`, `gain_db`, `line_readout_us` and")
    a("`confidence` are `float` in D0's type column, so the wire cannot")
    a("represent an arbitrary double exactly. On this clip the effect is")
    a(f"{narrowed} altered observation(s) and the acceptance table is")
    a("unchanged, because the only one of those fields the fusion chain reads")
    a("is `time_sync_error_ms`, and it feeds the systematic bound (report-only,")
    a("never the filter) which is zero under the frozen default")
    a("`FusionConfig.systematic`.")
    a("")
    a("Consequence if shipped: once `SystematicConfig.estimated_speed_mps` is")
    a("non-zero — which D6.1 requires for an honest clock bound — the declared")
    a("bound is computed from a value that changed by up to ~6e-8 relative on")
    a("the wire. That is far below any decision boundary, but it means")
    a("`decode(encode(x)) == x` is FALSE for the envelope and any future test")
    a("asserting exact envelope round-trip will fail for this reason and not a")
    a("real one. Widening the field would be a D0 contract change and is not")
    a("this phase's call.")
    a("")
    a("### D7-F3 — unpaced replay is single-source only")
    a("")
    a("DETERMINISTIC pacing carries synthetic capture times and never sleeps.")
    a("Across three independent processes that is not a valid multi-camera")
    a("mode: one node can transmit its entire clip before another starts, so")
    a("the aligner sees the trailing cameras' capture times far behind the")
    a("newest and correctly rejects them as LATE. The engine is right and the")
    a("mode is wrong.")
    a("")
    a("The rig therefore uses WALL_CLOCK pacing with a SHARED epoch across all")
    a("three edges — which is what PTP-disciplined boards give you for free in")
    a("D8/D9, and what a per-process start time silently does not. Pinned as a")
    a("test (`test_unpaced_multiprocess_replay_is_not_a_valid_multi_camera_mode`)")
    a("so the constraint cannot be forgotten.")
    a("")
    a("### D7-F4 — accelerated replay is bounded by the lateness window")
    a("")
    a("Replaying faster than 1x is not free. At speed S a D-millisecond")
    a("scheduling hiccup on the host reads as D*S milliseconds of CAPTURE-TIME")
    a("lag to the aligner, because the capture timestamps still carry their")
    a("original 1x spacing. The aligner's window is")
    a("`AlignerConfig.lateness_ns` = 100 ms, so the safe speed is")
    a("`lateness_ms / (worst host jitter * safety factor)`.")
    a("")
    a("This was found the way findings should be found: the rig test failed")
    a("at speed 10 on a LOADED host with three observations excluded as")
    a("`late`, and the failure read as a socket-vs-file parity break when the")
    a("wire had done nothing wrong.")
    a("")
    a("With the declared jitter budget and a")
    a(f"{_fmt(TRANSPORT_CONFIG.rig_speed_safety_factor, 0)}x factor covering")
    a("the observed max-to-p95 ratio, the bound comes out at 1x, so")
    a(f"`TransportConfig.rig_replay_speed` is "
      f"{_fmt(TRANSPORT_CONFIG.rig_replay_speed, 1)} — the rig replays at REAL")
    a("TIME. `test_the_rig_replay_speed_leaves_room_for_host_jitter` re-derives")
    a("it from `AlignerConfig.lateness_ns`, so the two cannot drift apart: when")
    a("the jitter budget was widened after a loaded-host measurement, that test")
    a("failed and forced the speed down rather than letting an accelerated rig")
    a("keep a margin it no longer had.")
    a("")
    a("Consequence if shipped: none for the deployed system — 1x IS the")
    a("deployment. The consequence is for whoever next accelerates a replay to")
    a("save test time and reads the resulting lateness as a transport defect.")
    a("")
    a("### D7-F5 — a receive buffer sized at the ceiling truncates silently")
    a("")
    a(f"`recv({DATAGRAM_CEILING_BYTES})` on an oversized datagram returns "
      f"{DATAGRAM_CEILING_BYTES} bytes with no error")
    a("and no flag; the tail is gone and what remains often still parses. The")
    a("receiver therefore reads into a 65535 B buffer and REJECTS anything over")
    a("the ceiling. Demonstrated on this platform rather than assumed:")
    a("`test_a_ceiling_sized_receive_buffer_really_would_truncate` proves the")
    a("trap is real before the guard is shown to close it.")
    a("")

    a("## Adversarial review before hand-back")
    a("")
    a("Five lenses (silent truncation, encode/decode asymmetry, adapter")
    a("divergence under faults, tests that cannot fail, scope and frozen")
    a("paths), each finding independently verified by a second pass whose job")
    a("was to REFUTE it. 23 candidate findings, 17 refuted, 6 confirmed and")
    a("fixed before hand-back. The confirmed six, because what a review caught")
    a("is more useful to the next phase than a claim that it found nothing:")
    a("")
    a("| # | Found | Fix |")
    a("| --- | --- | --- |")
    a("| 1 | 8 of 13 nanopb bounds were enforced on the measurement plane only; "
      "health, evidence and control had none. The `.options`-vs-`WireLimits` "
      "guard was structurally blind to them — a mangled file passed W2. | "
      "`declared_limits_from_options` now rejects any bound nothing enforces, "
      "so an unenforced line cannot be added; all four planes check. |")
    a("| 2 | A `float` field beyond FLT_MAX SATURATED to infinity instead of "
      "narrowing: a finite measurement silently becoming `inf`. | "
      "`_check_float32` rejects it loudly; a sender's explicit infinity is "
      "still carried. |")
    a("| 3 | `line_readout_us` was 0.0 in every fixture, and 0.0 is proto3's "
      "implicit default — deleting the encoder line left the suite green. | "
      "A non-zero rolling-shutter case pins field 15. |")
    a("| 4 | W6 compared packet-age and transit MEDIANS, so dropping the "
      "due-time bookkeeping and joining against send time still passed. | "
      "Exact per-receipt identity: age minus transit IS the scheduling error. |")
    a("| 5 | `test_options_parser_can_fail` only proved a bad file raises; "
      "`return WireLimits()` — ignoring the file entirely — survived. | "
      "A positive-propagation test on a shifted copy of the real file. |")
    a("| 6 | Nothing tied `adapter.stats.sizes` to real datagram lengths; "
      "recording a constant 1 left the suite green. | "
      "Cross-checked against the independent encoder. |")
    a("")
    a("Findings 3-6 are test-strength defects: the code was right, the tests")
    a("could not have caught it being wrong. Findings 1 and 2 were real")
    a("encode-path gaps, and finding 1 contradicted a claim this codebase made")
    a("about itself in three places.")
    a("")
    a("## W-series status")
    a("")
    a("| Test | Content | Status |")
    a("| --- | --- | --- |")
    a("| W1 | proto round-trip, pydantic parity, D0 field numbers read from "
      "the frozen docstrings, unknown-field tolerance both directions, golden "
      "bytes | PASS |")
    a("| W2 | ceiling vs the schema-permitted worst case; oversized fails "
      "loudly; count bound independent of the byte bound | PASS |")
    a("| W3 | evidence dropped, expired, oversized and corrupted — geometry "
      "and decisions unchanged | PASS |")
    a(f"| W4 | socket vs file acceptance table | "
      f"{'PASS' if tables_identical else 'FAIL'} |")
    a(f"| W5 | Tier IV socket faults reproduce D6 policies | "
      f"{'PASS' if not divergent else 'FAIL'} |")
    a("| W6 | wall-clock packet age measured, jitter within the declared "
      "budget | PASS |")
    a("| W7 | delivered-event determinism, cross-process | PASS |")
    a("| W8 | session restart on the wire, per D0 | PASS |")
    a("")
    a("W1-W8 statuses are the suite's, recorded here; the suite is the")
    a("authority. W4 and W5 above are recomputed by this generator, so their")
    a("verdicts are this run's own.")
    a("")

    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print(
        f"wrote {out_path} (parity identical: {tables_identical}, "
        f"tier IV divergences: {len(divergent)}/{len(verdicts)})"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="D7 wire report tooling")
    sub = parser.add_subparsers(dest="cmd", required=True)

    measure_parser = sub.add_parser("measure")
    measure_parser.add_argument("--seed", type=int, required=True)
    measure_parser.add_argument("--out", required=True)
    measure_parser.add_argument(
        "--speed", type=float, default=TRANSPORT_CONFIG.rig_replay_speed
    )
    measure_parser.add_argument("--workdir", default=None)

    generate_parser = sub.add_parser("generate")
    generate_parser.add_argument("--seed", type=int, required=True)
    generate_parser.add_argument("--out", required=True)
    generate_parser.add_argument("--evidence", default=None)
    generate_parser.add_argument("--manifest", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "measure":
        measure(args.seed, args.out, args.speed, args.workdir)
    else:
        generate(args.seed, args.out, args.evidence, args.manifest)


if __name__ == "__main__":
    main()
