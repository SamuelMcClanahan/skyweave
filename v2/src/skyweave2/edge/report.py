"""D8 edge report generator.

Two subcommands, the same split as the D7 generator:

    uv run python -m skyweave2.edge.report measure --out docs/d8_evidence.json
    uv run python -m skyweave2.edge.report generate \\
        --evidence docs/d8_evidence.json --out docs/D8_EDGE_REPORT.md

``measure`` replays every regenerable fixture through an ALREADY-BUILT
daemon (`--build-dir`, default `firmware/rv1106/build-host`), runs the
suites, and records what came back — including the SHA-256 of the binary it
measured, so the evidence names the thing it is evidence about.

``generate`` writes the document from the committed fixtures, the DECLARED
tolerances and that evidence file, and is byte-identical for identical
inputs — no wall clock, no environment, nothing that changes between two
runs of the same evidence.

The separation is not tidiness. The tolerance declarations must be committed
BEFORE the first board scorecard run (the brief's anti-tuning rule), so the
generator must be able to write the whole declaration half of this document
with no measurements in hand at all. Run it with no evidence file and it
does exactly that, marking every measured row PENDING.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from skyweave2.detector import cap
from skyweave2.detector.config import DetectorConfig
from skyweave2.edge import daemon, fixtures, tolerance
from skyweave2.edge.injection import PtsProfile, build_injection_stream
from skyweave2.edge.obsfixture import decode_fixture
from skyweave2.edge.tolerance import DetectorTolerance, compare_to_oracle
from skyweave2.transport import codec, sizing
from skyweave2.transport.wire import DATAGRAM_CEILING_BYTES, WIRE_LIMITS, unframe

V2_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVIDENCE = V2_ROOT / "docs" / "d8_evidence.json"
DEFAULT_REPORT = V2_ROOT / "docs" / "D8_EDGE_REPORT.md"

# The three D4 sweep resolutions. The board benchmark (D8.1) fills the
# numbers; the rows exist here now so the report shows what is PENDING
# instead of omitting it.
D4_RESOLUTIONS = ((2304, 1296), (1536, 864), (1152, 648))


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------


def _load_fixture_observations(name: str):
    import io

    raw = (fixtures.FIXTURE_ROOT / name / "observations.swob").read_bytes()
    return [obs for _, event in decode_fixture(io.BytesIO(raw)) for obs in event]


def _load_fixture_packets(name: str) -> list[bytes]:
    text = (fixtures.FIXTURE_ROOT / name / "packets.hex").read_text(encoding="utf-8")
    return [bytes.fromhex(line) for line in text.split() if line]


def _wire_identity(build_dir: Path | None) -> dict:
    """Encode every committed fixture with the C tool; compare to the host's.

    Recorded rather than asserted in prose. The report's central claim is
    that nanopb and the host codec agree byte for byte, and a hardcoded
    "yes" in the generator would say so whether or not they did.
    """
    out: dict = {}
    for name in ("sparse", "clutter", "gate"):
        fixture = fixtures.FIXTURE_ROOT / name
        if not (fixture / "observations.swob").exists():
            continue
        try:
            tool = daemon.fixture_tool_path(build_dir)
        except daemon.DaemonUnavailable:
            return {}
        completed = subprocess.run(
            [str(tool), "encode", str(fixture / "observations.swob")],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            out[name] = {"identical": False, "detail": completed.stderr.strip()[:400]}
            continue
        produced = [bytes.fromhex(line) for line in completed.stdout.split() if line]
        expected = _load_fixture_packets(name)
        out[name] = {
            "identical": produced == expected,
            "datagrams": len(produced),
            "expected_datagrams": len(expected),
        }
    return out


def _run_suite(selector: str) -> dict:
    """Run a pytest selection and record what it actually did."""
    completed = subprocess.run(
        ["uv", "run", "pytest", selector, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(V2_ROOT), capture_output=True, text=True, check=False,
    )
    tail = [line for line in completed.stdout.splitlines() if line.strip()]
    summary = tail[-1] if tail else ""
    counts: dict = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    import re

    for key in counts:
        match = re.search(rf"(\d+) {key}", summary)
        if match:
            counts[key] = int(match.group(1))
    return {
        "selector": selector,
        "returncode": completed.returncode,
        "summary": re.sub(r"\x1b\[[0-9;]*m", "", summary),
        **counts,
    }


def measure(work_dir: Path, build_dir: Path | None = None,
            run_suites: bool = True, seed: int = fixtures.SCENE_SEED) -> dict:
    """Replay every regenerable fixture through the daemon; record the result."""
    import hashlib

    work_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict = {"schema": "d8-evidence/3", "seed": seed, "fixtures": {}}
    try:
        binary = daemon.daemon_path(build_dir)
        evidence["daemon"] = {
            "path": str(binary),
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
    except daemon.DaemonUnavailable as exc:
        raise SystemExit(
            f"{exc}\n\n`measure` does not build the daemon; build it first so the "
            "evidence can record exactly which binary produced these numbers."
        ) from exc

    provenance = _container_provenance()
    if provenance:
        evidence["container"] = provenance

    identity = _wire_identity(build_dir)
    if identity:
        evidence["wire_identity"] = identity

    for name, blobs in fixtures.SYNTHETIC_FIXTURES.items():
        config = DetectorConfig.model_validate(
            json.loads((fixtures.FIXTURE_ROOT / name / "config.json").read_text())
        )
        session = json.loads(
            (fixtures.FIXTURE_ROOT / name / "stats.json").read_text()
        )["session_uuid"]
        clip = fixtures.build_scene_clip(work_dir / "clips" / name, blobs, seed=seed)
        stream = work_dir / f"{name}.swij"
        stream.write_bytes(
            build_injection_stream(clip, config, session, profile=PtsProfile())
        )
        run = daemon.run_daemon_on_stream(
            stream, config, work_dir / name, build_dir=build_dir
        )
        if run.returncode != 0:
            raise SystemExit(f"{name}: the daemon failed\n{run.stderr}")
        observations = []
        for packet in run.packets:
            observations += codec.decode_observation_packet(unframe(packet)[1])[1]
        divergence = compare_to_oracle(
            _load_fixture_observations(name), observations,
            tolerance.HOST_SOFT_TOLERANCE,
        )
        stats = dict(run.stats)
        # `fps` is a WALL-CLOCK rate over a file replay that is not paced.
        # It says nothing about the node and would put a number that changes
        # every run into a committed artifact, so it is dropped here rather
        # than reported and then explained away.
        stats.pop("fps", None)
        evidence["fixtures"][name] = {
            "detector": "soft",
            "stats": stats,
            "divergence": divergence.as_dict(),
            "breaches": divergence.breaches(tolerance.HOST_SOFT_TOLERANCE),
            "datagrams_byte_identical_to_oracle": run.packets == _load_fixture_packets(name),
        }

    if run_suites:
        evidence["suites"] = {
            "edge": _run_suite("tests/edge"),
            "full": _run_suite("tests"),
        }
    return evidence


def _container_provenance() -> dict | None:
    """Ask the pinned image what it is, if it exists on this machine."""
    completed = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/amd64",
         "skyweave-edge-build:d8.0", "cat", "/etc/skyweave-build-provenance"],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        return None
    out = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out or None


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _tolerance_rows(name: str, declared: DetectorTolerance, measured: dict | None):
    fields = (
        ("match radius (full-res px)", "match_radius_px", None),
        ("centroid mean (full-res px)", "centroid_mean_px", "centroid_mean_px"),
        ("centroid p95 (full-res px)", "centroid_p95_px", "centroid_p95_px"),
        ("missed fraction", "missed_fraction", "missed_fraction"),
        ("extra per capture event", "extra_per_event", "extra_per_event"),
        ("count-mismatch fraction", "count_mismatch_fraction", "count_mismatch_fraction"),
    )
    rows = []
    for label, declared_field, measured_field in fields:
        bound = getattr(declared, declared_field)
        if measured is None or measured_field is None:
            value = "—" if measured_field is None else "PENDING"
        else:
            value = f"{measured[measured_field]:.4f}"
        rows.append((label, f"{bound:g}", value))
    return rows


def generate(evidence: dict | None) -> str:
    lines: list[str] = []

    def a(text: str = "") -> None:
        lines.append(text)

    fixture_stats = {
        name: json.loads((fixtures.FIXTURE_ROOT / name / "stats.json").read_text())
        for name in ("sparse", "clutter", "gate")
        if (fixtures.FIXTURE_ROOT / name / "stats.json").exists()
    }
    measured = (evidence or {}).get("fixtures", {})
    container = (evidence or {}).get("container")

    # Read out of the committed fixtures, not typed in. Section 9's D8-F7
    # makes claims about these artifacts ("all N observations", "none of them
    # saturates"), and review finding 8 is what happens to a claim that is a
    # literal in this generator: it keeps asserting itself after the thing it
    # describes has moved.
    committed = [
        observation
        for name in fixture_stats
        if (fixtures.FIXTURE_ROOT / name / "observations.swob").exists()
        for observation in _load_fixture_observations(name)
    ]
    committed_count = len(committed)
    committed_max_area = max((obs.area_px for obs in committed), default=0)
    committed_saturated = sum(
        1
        for obs in committed
        if obs.area_px >= cap.CONFIDENCE_SATURATION_AREA_PX
    )

    a("# D8 edge report — real RV1106 in the loop")
    a("")
    a("**Labels.** Everything on the host path — the capacity arithmetic, the")
    a("wire byte-identity verdict, the fixture counters and the host-replay")
    a("divergence — is **Measured** on this host and recomputed by this")
    a("generator on every run. Everything about the BOARD is **Pending**:")
    a("no RV1106 has run this daemon yet, so no benchmark, no soak and no")
    a("deployment-resolution choice is claimed. Numbers carried in from")
    a("earlier phases keep the label they had.")
    a("")
    a("**The tolerance declarations in section 6 were committed BEFORE any")
    a("board scorecard run**, which is what the anti-tuning rule requires. The")
    a("host bounds were also committed before the host replay existed, and")
    a("section 9 records what happened when the first measurement broke one.")
    a("")
    a("Generated by `uv run python -m skyweave2.edge.report generate` —")
    a("byte-identical for identical fixtures, declarations and evidence file.")
    a("")

    # ---------------------------------------------------------------- 1
    a("## 1. What this phase delivered")
    a("")
    a("| Sub-phase | Status |")
    a("| --- | --- |")
    a("| D8.0 host-side: capacity, daemon, injection harness, fixtures, E1-E5 | "
      "**complete on this host** |")
    a("| D8.1 board bring-up: benchmark, soak, deployment resolution | "
      "**not started — needs a flashed node** |")
    a("| D8.2 board validation: fixture replay, toleranced scorecard, health | "
      "**not started — gated on D8.1** |")
    a("")
    a("The brief bars an agent from starting D8.1 until Samuel confirms the")
    a("flashed node. Nothing below claims a board number.")
    a("")

    # ---------------------------------------------------------------- 2
    a("## 2. Build provenance")
    a("")
    a("| Item | Value |")
    a("| --- | --- |")
    if container:
        a("| Container image tag | `skyweave-edge-build:d8.0` |")
        for key in ("base", "luckfox_repo", "luckfox_commit", "toolchain", "gcc",
                    "cmake", "git", "debian"):
            if key in container:
                a(f"| {key.replace('_', ' ').capitalize()} | `{container[key]}` |")
    else:
        a("| Container image tag | `skyweave-edge-build:d8.0` (not present on the "
          "machine that generated this report) |")
    a("| nanopb | 0.4.9, vendored under `firmware/rv1106/third_party/nanopb/` |")
    a("| Generated protobuf sources | `firmware/rv1106/proto/skyweave.pb.{c,h}`, "
      "checked in, bounds from `proto/skyweave.options` verbatim |")
    a("| Board image | **Pending** — recorded in D8.1 when a node is flashed |")
    a("")
    a("The image is `linux/amd64` because the Luckfox toolchain binaries are")
    a("x86_64 ELF. On Apple Silicon it runs under emulation; the Linux PC runs")
    a("the same image natively as the mirror the brief calls for. The SDK is")
    a("pinned by COMMIT, not by branch — a branch would make \"the reproducible")
    a("build\" a statement about the day it ran.")
    a("")
    a("**Cross-compile verdict: CLEAN.** `./scripts/build-board.sh` inside that")
    a("image produces `skyweave-edge` and `sw-fixture-tool` as ELF 32-bit ARM")
    a("EABI5 binaries against `/lib/ld-uClibc.so.0`, linking `librve`, `libivs`,")
    a("`librga` and `librockit` — the real IVE, RGA and RKMPI paths, compiled,")
    a("not stubbed. The binaries carry no RPATH, so the node's own loader")
    a("resolves those libraries instead of a build-container path that does not")
    a("exist on it.")
    a("")
    a("Two build notes, both recorded because the next person will hit them:")
    a("")
    a("- `librockit.so` itself needs `libdrm` and `librockchip_mpp`, which live")
    a("  on the BOARD's rootfs and are not in the SDK's sparse checkout, so the")
    a("  link passes `-Wl,--allow-shlib-undefined`. That relaxes undefined")
    a("  symbols in a SHARED LIBRARY only; an undefined symbol in this daemon's")
    a("  own objects is still a link error, which is the check that matters.")
    a("- Under linux/amd64 EMULATION on Apple Silicon, `cc1` and `as` segfault")
    a("  intermittently — different files each time, the same file compiling")
    a("  cleanly on the next attempt. `build-board.sh` retries a bounded number")
    a("  of times and announces every retry. On the native Linux mirror the")
    a("  first attempt succeeds. It is an emulation artifact, not a compiler bug")
    a("  and not a property of this source.")
    a("")

    # ---------------------------------------------------------------- 3
    a("## 3. The recorded decisions, implemented")
    a("")
    a("Two entries in the D0 decisions log are implemented by this phase and")
    a("nothing else is: the \"D8 opening\" capacity decision, and the \"D8.0")
    a("amendment\" that says what the `confidence` field carries. Both are")
    a("sanctioned in writing before the code moved; neither is an edit.")
    a("")
    a("### 3.1 Capacity (the D8 opening entry)")
    a("")
    budget = sizing.worst_case_budget()
    a("| Item | Before (D7) | After (D8) |")
    a("| --- | --- | --- |")
    a(f"| Datagram ceiling | 1200 B (Provisional) | {DATAGRAM_CEILING_BYTES} B "
      "= 1500 MTU - 20 IPv4 - 8 UDP (Chosen) |")
    a(f"| `ObservationPacket.observations` max_count | 5 | "
      f"{WIRE_LIMITS.observations_max_count} |")
    a(f"| Detector per-frame component cap | none | "
      f"{DetectorConfig().max_components_per_frame} |")
    a("")
    a("Worst case the SCHEMA permits, measured by encoding rather than")
    a("estimated (`transport/sizing.py`): every string at its nanopb")
    a("`max_size`, every varint at its widest, a negative `capture_ts_ns`,")
    a("every optional present.")
    a("")
    a("| Component | Bytes |")
    a("| --- | --- |")
    a(f"| Header | {budget.header_bytes} |")
    a(f"| FrameEnvelope | {budget.envelope_bytes} |")
    a(f"| Each Observation2D | {budget.per_observation_bytes} |")
    a(f"| Declared observation bound | {budget.observation_count} |")
    a(f"| Worst-case datagram | {budget.total_bytes} |")
    a(f"| Headroom | {budget.headroom_bytes} |")
    a("")
    a("The D8 opening entry predicted 251 + 7x163 = 1392 B with 80 B of")
    a(f"headroom. The encoder says {budget.total_bytes} B with "
      f"{budget.headroom_bytes} B, because the planning arithmetic omits the")
    a("repeated field's tag and length overhead, which widens as the packet")
    a("grows. The conclusion is unchanged — 7 fits, 8 does not — and the")
    a("difference is recorded rather than smoothed over: `sizing.py` derives")
    a("the bound by encoding for exactly this reason.")
    a("")
    a("**Invariant, tested (E1): wire `max_count` >= detector cap.** The wire")
    a("bound is what the peer's nanopb ALLOCATES from, so a cap above it would")
    a("build capture events this host encodes happily and the board cannot")
    a("decode. The daemon refuses such a configuration at startup rather than")
    a("discovering it on a cluttered frame.")
    a("")
    a("**Golden byte fixtures: unchanged.** `max_count` is an allocation")
    a("bound, not a wire value, so raising it moved no encoded byte. The D7")
    a("`wire_golden/*.hex` files are untouched and still gate W1.")
    a("")
    a("### 3.2 Wire confidence (the D8.0 amendment)")
    a("")
    a("The D8.0 hand-back surfaced three components of the system putting")
    a("different numbers in one wire field (finding D8-F6). The amendment")
    a("settles which number:")
    a("")
    a(f"    confidence = min(1.0, area_px / "
      f"{cap.CONFIDENCE_SATURATION_AREA_PX!r})   # area_px at PROC resolution")
    a("")
    a("| Item | Value |")
    a("| --- | --- |")
    a("| Definition | `detector/cap.py::component_confidence`, once |")
    a("| Reported by | `detector/runner.py`, by CALLING it |")
    a("| Ranked by | `detector/cap.py::rank_key`, level 1 |")
    a("| Mirrored in | `firmware/rv1106/src/sw_pipeline.c`, integer-safe |")
    a(f"| Saturates at | {cap.CONFIDENCE_SATURATION_AREA_PX:g} px, proc "
      "resolution |")
    a("")
    a("It is not a probability and the Jetson must not read it as one. A")
    a("background model plus connected components has no appearance model; area")
    a("is the only evidence it holds, so this is a monotone restatement of how")
    a("much of the frame said something happened. It is also resolution-")
    a("dependent by construction — the same object at 1536x864 and at 1152x648")
    a("does not get the same number — which is honest for a per-node quantity")
    a("and one more reason not to calibrate anything on it. The NPU appearance")
    a("gate (node design section 9, phase 2) is what would make it real.")
    a("")
    a("`persistence_count` is deliberately absent from the formula: it is")
    a("already its own wire field, and folding it in would double-count the")
    a("same evidence inside a value the fusion side may later weight by.")
    a("")
    a("**Integer-safe** in `sw_pipeline.c` means three specific things, because")
    a("this value sits inside an ABSOLUTE byte gate and one ULP is a failure:")
    a("the saturation test is an integer comparison (`area_px >= 50`), which no")
    a("rounding mode or evaluation width can reach; `(double)area_px` is exact")
    a("for every `uint32_t`; and the division happens in double and is narrowed")
    a("to binary32 exactly once, at the wire field — the same two roundings, in")
    a("the same order, as Python's double division followed by protobuf's")
    a("`float` store.")
    a("")
    a("Only the middle one is load-bearing TODAY, and the report says so rather")
    a("than implying three necessities. Checked exhaustively: an all-float form")
    a("agrees bit-for-bit with this one over every area the clamp admits, and")
    a("the two first diverge at 2^24 + 1 — where it is the operand conversion")
    a("that loses, which the clamp puts out of reach. The shape above is")
    a("correctness by construction, not a measured difference, and that is the")
    a("point: the divisor and the saturation area are decisions, and decisions")
    a("move. The adversarial review caught the first draft of this passage")
    a("asserting the stronger claim (10c, finding 4).")
    a("")
    a("**Fixtures regenerated**, with the log entry as the recorded reason: the")
    a(f"field's VALUE changed on {committed_count - committed_saturated} of the "
      f"{committed_count} committed observations —")
    a("everything below the saturation area, which after the amendment is")
    a("where the value stops being 1.0. The `golden/` manifests are not")
    a("affected — this touches D8 fixtures only — and the byte-identity gate")
    a("re-applies AFTER regeneration, unchanged and still absolute (section 4).")
    a("")

    # ---------------------------------------------------------------- 4
    a("## 4. Wire byte-identity (the absolute gate)")
    a("")
    a("For identical observation inputs, the daemon's nanopb encoding must be")
    a("byte-identical to the host codec. Both encoders are handed the same")
    a("`observations.swob` bytes — a JSON fixture would have let a reader's")
    a("rounding masquerade as an encoder's disagreement.")
    a("")
    a("These are the POST-AMENDMENT fixtures (section 3.2). The gate is the")
    a("same gate: regenerating the inputs does not soften it, and the max")
    a("datagram column below is unchanged from before the regeneration because")
    a("`confidence` is a fixed-width `float` — a different value in it costs")
    a("the same five bytes, so the capacity arithmetic in section 3.1 is")
    a("untouched by the amendment.")
    a("")
    a("| Fixture | Capture events | Observations | Max obs/event | Max datagram B | "
      "nanopb == host codec |")
    a("| --- | --- | --- | --- | --- | --- |")
    identity = (evidence or {}).get("wire_identity", {})
    for name in ("sparse", "clutter", "gate"):
        stats = fixture_stats.get(name)
        if stats is None:
            continue
        # MEASURED, never asserted. A hardcoded "yes" here would say the
        # encoders agree whether or not they do, in the one document whose
        # job is to say whether they do.
        row = identity.get(name)
        if row is None:
            verdict = "PENDING (run `report measure`)"
        elif row.get("identical"):
            verdict = f"**yes** ({row.get('datagrams', '?')} datagrams)"
        else:
            verdict = "**NO — investigate**"
        a(f"| {name} | {stats['capture_events']} | {stats['observations']} | "
          f"{stats['max_observations_per_event']} | {stats['packet_bytes_max']} | "
          f"{verdict} |")
    a("")
    a("Plus an adversarial fixture no clip produces: an all-zero bbox origin")
    a("(a PRESENT but empty submessage), `local_blob_id` absent versus 0,")
    a("`evidence_ref` absent versus the empty string, a negative")
    a("`capture_ts_ns` (a full 10-byte varint), negative bbox origins through")
    a("the zigzag path, every string at its declared bound, and a full")
    a(f"{WIRE_LIMITS.observations_max_count}-observation event. Byte-identical "
      "on all of them, in both")
    a("directions, with unknown-field tolerance checked at the C boundary.")
    a("")
    if identity:
        agreed = all(row.get("identical") for row in identity.values())
        if agreed:
            a("**Verdict: EXACT**, measured over "
              f"{sum(row.get('datagrams', 0) for row in identity.values())} datagrams. "
              "No tolerance was")
            a("needed and none is permitted here.")
        else:
            broken = sorted(k for k, v in identity.items() if not v.get("identical"))
            a(f"**Verdict: DIVERGED on {', '.join(broken)}.** The wire gate is "
              "absolute; this is a")
            a("hand-back blocker, not a tolerance question.")
    else:
        a("**Verdict: PENDING** — no C toolchain on the machine that generated")
        a("this report, so nothing here measured the encoders against each other.")
    a("")

    # ---------------------------------------------------------------- 5
    a("## 5. Host fixture replay (E5)")
    a("")
    if measured:
        a("The C daemon replays each injection stream end to end — injection")
        a("reader, GMM2, opening, CCL, persistence, cap, nanopb, framing — and")
        a("its datagrams are compared against the host oracle's.")
        a("")
        a("| Fixture | Frames | Capture events | Observations | Offered | "
          "Dropped by cap | Frames at cap | Datagrams == oracle |")
        a("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for name in sorted(measured):
            stats = measured[name]["stats"]
            identical = "**yes**" if measured[name][
                "datagrams_byte_identical_to_oracle"] else "no"
            a(f"| {name} | {stats['frames_in']} | {stats['capture_events']} | "
              f"{stats['observations_sent']} | {stats['components_offered']} | "
              f"{stats['components_dropped_over_cap']} | {stats['frames_at_cap']} | "
              f"{identical} |")
        a("")
        exact = all(
            row["datagrams_byte_identical_to_oracle"] for row in measured.values()
        )
        if exact:
            a("**The portable detector is byte-exact against the oracle on this")
            a("host.** That is stronger than the declared tolerance in section 6,")
            a("which remains the contract. It holds because `sw_detect_soft.c` is")
            a("a faithful transcription of the `ive_approx` backend — same")
            a("float32 state, same numpy promotion points, same structuring")
            a("element — and because the build pins `-ffp-contract=off` so no")
            a("compiler fuses a multiply-add the oracle rounds twice.")
            a("")
            a("It says nothing about the board. The hardware GMM2 is a different")
            a("detector with four structural divergences named in section 6, and")
            a("D8.2 measures it against the bounds declared there.")
    else:
        a("**PENDING** — run `python -m skyweave2.edge.report measure` on a")
        a("machine with a C toolchain to fill this section.")
    a("")

    # ---------------------------------------------------------------- 6
    a("## 6. Detector tolerances — DECLARED BEFORE THE BOARD RUNS")
    a("")
    a("The D8 fixture policy is split: the wire is exact, the detector is")
    a("toleranced. These bounds live in `skyweave2/edge/tolerance.py`, test E5")
    a("enforces them, and test E5 also checks that this document quotes the")
    a("same numbers — a declaration that only exists in code is not a")
    a("declaration.")
    a("")
    a("### 6.1 Portable C GMM2 vs the `ive_approx` oracle (host)")
    a("")
    a("| Axis | Declared bound | Measured |")
    a("| --- | --- | --- |")
    host_measured = None
    if measured:
        # The sparse fixture is where the DETECTOR is measured: the cap is
        # inert there, so a difference is a detection difference.
        host_measured = measured.get("sparse", {}).get("divergence")
    for label, bound, value in _tolerance_rows(
        "soft", tolerance.HOST_SOFT_TOLERANCE, host_measured
    ):
        a(f"| {label} | {bound} | {value} |")
    a("")
    a("Measured on the **sparse** fixture, where the cap never bites. That")
    a("choice is section 9's finding D8-F3: on the clutter fixture the cap")
    a("turns a sub-pixel detector difference into a different SUBSET of")
    a("measurements, and mixing the two would report a selection effect as a")
    a("detection error.")
    a("")
    a("### 6.2 Hardware RK_MPI_IVE_GMM2 + IVE_CCL vs the same oracle (board)")
    a("")
    a("| Axis | Declared bound | Measured |")
    a("| --- | --- | --- |")
    for label, bound, _pending in _tolerance_rows(
        "ive", tolerance.BOARD_IVE_TOLERANCE, None
    ):
        a(f"| {label} | {bound} | PENDING (D8.2) |")
    a("")
    a("Wider than the host bounds on every axis because four structural")
    a("divergences are known IN ADVANCE, read out of the SDK headers rather")
    a("than guessed. They are properties of the hardware, not defects, and")
    a("each is named in `firmware/rv1106/src/sw_detect_ive.c`:")
    a("")
    a("| # | Divergence | Why it moves a number |")
    a("| --- | --- | --- |")
    a("| 1 | `rk_mpi_ive.h` documents IVE_CCL as \"Only 8-Connected method is "
      "supported\"; the host oracle uses cv2 connectivity 4 | blobs touching "
      "only diagonally are ONE component on the board and TWO on the host |")
    a("| 2 | `IVE_REGION_S` carries area and the four bbox edges — no centroid | "
      "the centroid is a first moment the A7 takes over the label image, so "
      "the area is the hardware's and the centroid is ours, and they can "
      "disagree about a boundary pixel |")
    a("| 3 | `IVE_CCL_CTRL_S` has `u16InitAreaThr`/`u16Step` and the hardware "
      "RAISES its area threshold until the region count fits 254 | the "
      "effective `min_area_px` MOVES on crowded frames; the daemon reads "
      "`u32CurAreaThr` back every frame and counts the frames it rose |")
    a("| 4 | GMM2's controls are u8q2/u10q0 fixed point | the host's float "
      "learn rate, background ratio and weights are QUANTISED on the way in; "
      "the daemon logs what it actually programmed |")
    a("")
    a("D8.2 reports the measured divergence against these bounds. Exceeding")
    a("one is a finding to investigate and record, not automatically a")
    a("failure — but the bound is what decides which conversation happens,")
    a("and it exists before the data.")
    a("")

    # ---------------------------------------------------------------- 7
    a("## 7. Cap and health behaviour")
    a("")
    clutter = fixture_stats.get("clutter", {}).get("detector", {})
    if clutter:
        a("| Quantity | clutter fixture |")
        a("| --- | --- |")
        a(f"| Scored frames | {clutter['frames']} |")
        a(f"| Frames at the cap | {clutter['frames_at_cap']} |")
        a(f"| Components offered | {clutter['components_offered']} |")
        a(f"| Components emitted | {clutter['components_emitted']} |")
        a(f"| Components dropped by the cap | "
          f"{clutter['components_dropped_over_cap']} |")
        a(f"| Busiest frame offered | {clutter['max_components_offered']} |")
        a("")
    a("Every dropped component is counted in three places: the detector's own")
    a("stats, the daemon's `--stats` JSON, and the 1 Hz health packet's")
    a("`drops`. No capture event was unencodable on any fixture — which is the")
    a("entire point of the cap, since before it a cluttered frame was a loud")
    a("failure and a lost measurement (D7-F1).")
    a("")
    a("`HealthPacket.drops` is a TOTAL — frames the detector failed, plus")
    a("components the cap removed, plus events that could not be encoded — and")
    a("finding D8-F2 records why it is not a breakdown.")
    a("")

    # ---------------------------------------------------------------- 8
    a("## 8. Board benchmark and deployment resolution — PENDING (D8.1)")
    a("")
    a("The benchmark sweeps GMM2 at the three D4 resolutions and the surviving")
    a("one gets a one-hour soak. All three pass the D4 HOST gate, so the board")
    a("ceiling is what picks the deployment resolution; the intersection")
    a("argument and the choice become a decisions-log entry (label: Chosen,")
    a("benchmark numbers Measured).")
    a("")
    a("| Resolution | Sustained fps | Peak RSS | DDR bandwidth | A7 utilisation | "
      "Thermals | Verdict |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    for width, height in D4_RESOLUTIONS:
        a(f"| {width}x{height} | PENDING | PENDING | PENDING | PENDING | PENDING | "
          "PENDING |")
    a("")
    a("| Soak | Value |")
    a("| --- | --- |")
    a("| Resolution | PENDING |")
    a("| Duration | 1 h (declared) |")
    a("| Frames | PENDING |")
    a("| Drops | PENDING |")
    a("| Thermal drift | PENDING |")
    a("")
    a("The intersection argument, stated now so the choice cannot be made by")
    a("whichever number is prettiest later: the deployment resolution is the")
    a("HIGHEST of the three that (a) sustains 30 fps on the board with margin,")
    a("(b) fits the node's memory and DDR budgets, and (c) keeps the D4")
    a("centroid tripwire clear at 0.75 px full-res sigma. D4 measured all")
    a("three clear on the host (worst-axis 0.141 px, Modeled), so only (a) and")
    a("(b) can bind — and if none of the three satisfies (a), that is a")
    a("finding about the node, not a licence to relax the gate.")
    a("")
    a("E8 (benchmark reproducibility: two runs, same config, within declared")
    a("run-to-run bounds) is board-gated and runs with this section.")
    a("")

    # ---------------------------------------------------------------- 9
    a("## 9. Findings")
    a("")
    a("### D8-F1 — the cap ranks by confidence, and confidence is area")
    a("")
    a("The D8 opening says the cap keeps \"the top components by descending")
    a("confidence\". After the D8.0 amendment (section 3.2) there IS a")
    a("confidence to rank by — but it is `min(1.0, area_px / 50.0)`, a")
    a("monotone non-decreasing function of area, so \"top by confidence\" and")
    a("\"top by area\" are the same selection. The finding survives the")
    a("amendment; only its wording changed.")
    a("")
    a("Two consequences follow from the monotonicity, and both are load-")
    a("bearing. Every component at or above 50 px ties at 1.0, so the")
    a("confidence sort is a PARTIAL order and the key needs its lower levels:")
    a("`-confidence`, then `-area_px`, then raster order (`detector/cap.py`).")
    a("Area is what separates the saturated components and it is the right")
    a("thing to separate them by — a larger foreground region is a better-")
    a("conditioned centroid, and it matches the edge byte governor's intent as")
    a("closely as a model-free detector can. Raster order exists only to make")
    a("the choice TOTAL, so the C daemon does not have to reproduce an accident")
    a("of Python's sort. And because levels 1 and 2 can tie but never")
    a("disagree, the daemon's separate component-shedding order")
    a("(`sw_detect_soft.c::rank_worse_than`, area and raster only, used when a")
    a("frame offers more than 254 regions) is the same ranking; a confidence")
    a("model that stops being a function of area has to revisit that too.")
    a("")
    a("Consequence if shipped: the cap's selection is deterministic and")
    a("area-driven, and it will stay that way until the NPU appearance gate")
    a("(phase 2 of the node design) gives `confidence` an independent meaning.")
    a("Anyone reading \"top by confidence\" in the decisions log should read")
    a("this finding first, and anyone tempted to read the wire field as a")
    a("detection probability should read section 3.2.")
    a("")
    a("### D8-F2 — the health plane has one drop counter and three causes")
    a("")
    a("The brief requires dropped components to be counted \"in detector stats")
    a("and the health path, never silent\". `HealthPacket` has exactly one")
    a("counter field, `drops`, and adding a second would be a wire change this")
    a("phase is not sanctioned to make — the D8 opening sanctions the capacity")
    a("touch and nothing else.")
    a("")
    a("So `drops` is defined as the TOTAL of measurement items the node did")
    a("not send: frames the detector failed, components the cap removed, and")
    a("capture events that could not be encoded. The daemon keeps the three")
    a("separated in `--stats` and in its log; the Jetson sees the sum.")
    a("")
    a("Consequence if shipped: a node that is dropping measurements says so")
    a("every second, and an operator cannot tell WHY from the wire alone —")
    a("they have to ask the node. Splitting the counter is a D9 lever and a")
    a("one-field wire decision; it is recorded here rather than taken.")
    a("")
    a("### D8-F3 — the cap amplifies a sub-pixel divergence into a different")
    a("subset of measurements")
    a("")
    a("Found by running the first host replay. With ten movers of nearly equal")
    a("area against a cap of seven, two detectors that agreed on every")
    a("centroid to a fraction of a pixel still sent 24% DIFFERENT")
    a("observations: a one-pixel area difference flips which components")
    a("survive the ranking, and the survivors are then disjoint.")
    a("")
    a("That is why the detector tolerance is measured on the **sparse**")
    a("fixture, where the cap is inert, and why the clutter fixture is used")
    a("for the cap, the counters and the health path instead. Reporting them")
    a("together would have logged a selection effect as a detection error.")
    a("")
    a("Consequence if shipped: on a crowded frame the host oracle can predict")
    a("that the board will send seven good measurements, and cannot predict")
    a("WHICH seven. Three nodes looking at the same crowded sky may each")
    a("forward a different subset, which is a fusion-association question for")
    a("D9, not a detector question. It is bounded — the cap keeps the largest")
    a("components, so the target is never the one dropped while decoys survive")
    a("unless the target is genuinely smaller than seven decoys — but it is")
    a("real and it is new.")
    a("")
    a("### D8-F4 — OpenCV's 3x3 \"ellipse\" structuring element is a cross")
    a("")
    a("`cv2.getStructuringElement(MORPH_ELLIPSE, (3, 3))` returns")
    a("`[[0,1,0],[1,1,1],[0,1,0]]`, not a filled 3x3 square. The daemon's")
    a("first transcription assumed the square, which eroded one extra ring off")
    a("every blob: a systematic ~1 px area deficit on every component, a")
    a("centroid shift of up to 1.1 full-res px, and — through D8-F3 — a")
    a("24% divergence in which components survived the cap.")
    a("")
    a("The DECLARED tolerance is what caught it. The bound (0.5 px mean) was")
    a("committed before the first replay; the first measurement came in at")
    a("0.5414 px and breached it. After the fix the divergence is exactly")
    a("zero. A bound chosen after seeing the data would have been set at 0.6")
    a("and the bug would have shipped.")
    a("")
    a("Consequence if shipped: none, because it did not ship. The general")
    a("lesson is the one the fixture policy is built on — a structuring")
    a("element is a detector parameter, not an implementation detail, and")
    a("\"transcribe the oracle\" means transcribing the library's choices too.")
    a("")
    a("### D8-F5 — the daemon's socket counter is not its measurement counter")
    a("")
    a("Measurement datagrams and the 1 Hz health packet leave through the same")
    a("socket, so the sender's `datagrams_sent` is a socket total and answered")
    a("the wrong question: a replay reported one more datagram than it had")
    a("capture events. Caught by the E5 cross-check between the packet log and")
    a("the counters, which exists precisely because the packet log is the")
    a("evidence the rest of E5 rests on.")
    a("")
    a("Consequence if shipped: a D9 operator reconciling \"datagrams sent\"")
    a("against \"observations received\" would have found a permanent")
    a("off-by-N-health-packets discrepancy and gone looking for a lost")
    a("measurement. The stats now report measurement and socket totals")
    a("separately.")
    a("")
    a("### D8-F6 — the detector reported one confidence and the cap ranked by")
    a("another, and nothing in the suite could tell")
    a("")
    a("Late in the phase `runner.py` was found emitting")
    a("`confidence=min(1.0, area_px / 50.0)` — an area-derived confidence —")
    a("directly beneath a comment stating that the value is constant, while")
    a("`cap.py::component_confidence`, the C daemon's `sw_pipeline.c` and every")
    a("committed fixture still used 1.0.")
    a("")
    a("The gap that let it hide is the interesting part. E2 and E5 both feed on")
    a("COMMITTED fixture bytes, so both stayed green: the daemon still agreed")
    a("with the fixtures, and the fixtures no longer agreed with the detector.")
    a("A fresh oracle run reproduced none of the committed packets, and the")
    a("host and the edge were putting different numbers in the same wire field")
    a("— the exact host/edge divergence this phase exists to eliminate.")
    a("")
    a("Two tests now close it: one asserting that the confidence the runner")
    a("REPORTS is the value `component_confidence` returns (the cap's ranked")
    a("quantity and the reported quantity are one quantity), and one asserting")
    a("that a fresh oracle run still reproduces the committed packet bytes. The")
    a("runner now calls `component_confidence` rather than restating a literal,")
    a("so there is one place a real confidence model can ever land.")
    a("")
    a("Consequence if shipped: the cap would have selected on a constant while")
    a("the Jetson received a variable, both looking reasonable in isolation,")
    a("and every committed fixture would have been quietly stale. If an")
    a("area-derived confidence IS wanted, it is a four-part change — "
      "`cap.py`,")
    a("the C daemon, a fixture regeneration with a recorded reason, and D8-F1 —")
    a("and a decision, not an edit.")
    a("")
    a("**Closed by the D8.0 amendment.** It was wanted, the decision was taken")
    a("and recorded, and the four-part change is exactly what shipped: section")
    a("3.2 for the formula and the mirror, this section's D8-F1 for the")
    a("ranking, and regenerated fixtures with the log entry as the reason. The")
    a("interim constant is Rejected in the log rather than deleted from it. The")
    a("two tests named above are unchanged and still the gate.")
    a("")
    a("### D8-F7 — the amendment moved a wire value, and added a branch")
    a("")
    a("Three things worth writing down about implementing D8.0a, because two")
    a("of them are the sort of thing a green suite hides.")
    a("")
    a("**The selection did not change.** Confidence is monotone in area")
    a("(D8-F1), so the components the cap keeps are exactly the ones it kept")
    a("before. What the D8.0a regeneration moved was the `confidence` field on")
    a(f"the {committed_count - committed_saturated} committed observations "
      "below the saturation area — above it")
    a("the value was 1.0 before the amendment and is 1.0 after — and nothing")
    a("else: same components, same `obs_id`s, same centroids, same datagram")
    a("sizes, same counters. That is a claim, so it is a test: E1 pins that")
    a("the cap's survivors are the ones an area-only ranking would pick, and")
    a("it fails the day a confidence model stops being a function of area —")
    a("which is the day the cap starts selecting differently and the fusion")
    a("side needs to hear about it.")
    a("")
    if committed_saturated == 0:
        a("**No committed fixture reaches the saturated branch.** The busiest")
        a(f"component in any of the three is {committed_max_area} px against a "
          f"{cap.CONFIDENCE_SATURATION_AREA_PX:g} px")
        a(f"saturation point, so every one of those {committed_count} "
          "observations exercises the")
        a("division and none exercises the clamp. Below saturation the host and")
        a("the daemon run the same double division; AT and above it they run")
        a("different expressions — Python's `min()` against an integer short-")
        a("circuit. An untested branch inside an absolute byte gate is the shape")
        a("of D8-F6 again, so E5 grew two clips of fat movers, and asserts on")
        a("both that the daemon's datagrams are byte-identical to the oracle's:")
        a("")
        a("- one that STRADDLES the saturation point (components from 38 px to")
        a("  91 px, including 49, 50 and 51), so the threshold constant is")
        a("  pinned rather than merely exceeded. The first version of this test")
        a("  produced 70-90 px only, which left the daemon's `50` free to be")
        a("  any value in 41..69 with the whole suite green — the review caught")
        a("  it, and the boundary assertions are now part of the test;")
        a("- one CROWDED with saturated movers, so more components than the cap")
        a("  admits all tie at confidence 1.0 and the ranking's `-area_px` level")
        a("  is what decides. Nothing else in the suite reaches that: the")
        a("  clutter fixture exceeds the cap but never saturates, and a single")
        a("  fat mover saturates but never exceeds the cap, so the C")
        a("  comparator's second level had no test that could fail.")
        a("")
        a("Neither is a committed fixture. They cover branches, and a fourth and")
        a("fifth fixture would add tolerance-table rows nothing declares.")
    else:
        a(f"**{committed_saturated} of {committed_count} committed observations "
          "now reach the saturated**")
        a(f"**branch** (busiest component {committed_max_area} px against a "
          f"{cap.CONFIDENCE_SATURATION_AREA_PX:g} px saturation")
        a("point). When this report was first written none did, and E5's")
        a("dedicated saturation clip was added for exactly that reason; it")
        a("stays, because a fixture that happens to cover a branch today is not")
        a("a test that it is covered.")
    a("")
    a("**The regeneration surfaced a stale honesty wart.** The committed")
    a("fixtures predated the `--seed` work (review finding 13 below), so")
    a("regenerating them added a `seed` field to every `stats.json` — including")
    a("the gate fixture's, whose pixels are a machine-local render artifact that")
    a("no seed of ours produced. `\"seed\": 7` there would have been a")
    a("determinism claim with nothing behind it, so the gate fixture records")
    a("`null` and `FixtureBuild.seed` is now `int | None`. Small, but it is the")
    a("exact failure mode the label discipline exists to prevent, and it was")
    a("introduced BY this regeneration rather than found in it.")
    a("")

    # ---------------------------------------------------------------- 10
    a("## 10. E-series status")
    a("")
    suites = (evidence or {}).get("suites", {})
    edge_suite = suites.get("edge")
    # ONE status for E1-E5, taken from an actual run of the E-series rather
    # than five hardcoded PASS cells. Per-test verdicts would be five more
    # literals nothing could contradict; the suite either went green or it
    # did not, and that is the claim worth making.
    if edge_suite is None:
        e_status = "PENDING (run `report measure`)"
    elif edge_suite["returncode"] == 0 and edge_suite["failed"] == 0:
        e_status = f"PASS ({edge_suite['passed']} tests)"
    else:
        e_status = f"**FAIL** — {edge_suite['summary']}"
    a(f"E1-E5 run as one suite (`tests/edge`): **{e_status}**")
    a("")
    a("| Test | Content | Status |")
    a("| --- | --- | --- |")
    a("| E1 | cap + max_count + ceiling agree; invariant holds in Python AND at "
      "daemon startup; cluttered frame emits the top 7 with drops counted; a "
      "full event is encodable at the schema worst case; goldens unmoved; the "
      "wire confidence is the declared area formula, is the one the cap ranks "
      "by, and selects what area alone would select | "
      f"{e_status} |")
    a("| E2 | nanopb byte-identity against the host codec on every fixture and "
      "on the adversarial cases, both directions, unknown-field tolerance; "
      "the committed stats files match the schema the writer writes and the "
      f"counters this report publishes | {e_status} |")
    a("| E3 | injection determinism within a process, across processes, and "
      "against the committed digest; Ethernet and storage replay produce the "
      f"same datagrams | {e_status} |")
    a("| E4 | fabricated-PTS honesty end to end; a board clock domain refused "
      f"at both ends; the known-lie path flagged in the artifact | {e_status} |")
    a("| E5 | host fixture replay under the DECLARED tolerance, plus the "
      "byte-exactness result, cap/health behaviour, health decode on the "
      "unchanged host stack, the packet-log cross-check, host/edge identity "
      "across the saturation boundary and with saturated components "
      f"competing for the cap, and report freshness | {e_status} |")
    a("| E6 | board fixture replay through the unchanged v2 stack, toleranced "
      "scorecard | board-gated (D8.2) |")
    a("| E7 | health packets at 1 Hz with real drop counters, on the node | "
      "board-gated (D8.2) |")
    a("| E8 | benchmark reproducibility: two runs, same config, within declared "
      "run-to-run bounds | board-gated (D8.1) |")
    a("")
    a("E1-E5 are the D8.0 hand-back. E6-E8 need hardware and are not claimed.")
    a("")
    a("### Full-suite status")
    a("")
    a("Every prior suite plus the E-series, and `ruff check src tests`, on the")
    a("machine that generated this report:")
    a("")
    full_suite = suites.get("full")
    a("| Suite | Result |")
    a("| --- | --- |")
    a(f"| E-series (`tests/edge`) alone | "
      f"{edge_suite['summary'] if edge_suite else 'PENDING'} |")
    a(f"| Whole suite (`tests`) | "
      f"{full_suite['summary'] if full_suite else 'PENDING'} |")
    a("| Whole suite before D8 (recorded at the start of this phase) | "
      "325 passed, 1 skipped |")
    a("")
    a("Both rows are the tail of an actual `uv run pytest -q`, recorded by")
    a("`report measure` into the evidence file — not a number typed into this")
    a("generator, which nothing could contradict.")
    a("")
    a("The skip is the D7 precedent: a gate-clip test whose machine-local")
    a("render artifacts are absent, with a synthetic variant that still runs.")
    a("")
    a("### A host-load observation on W6, recorded because it cost time")
    a("")
    a("Earlier in this phase, with the machine carrying a sustained load")
    a("average near 4 from macOS background indexing (`photoanalysisd`,")
    a("`mediaanalysisd`, `FPCKService`),")
    a("`test_w6_wallclock.py::test_loopback_packet_age_is_measured_and_within"
      "_the_declared_budget` failed repeatedly and reproducibly at a loopback")
    a("scheduling-jitter p95 of ~10.03 ms against its 10.0 ms budget. It")
    a("passes on the same machine once quiet, and the run recorded above is a")
    a("clean one.")
    a("")
    a("It was checked against the PRE-D8 tree — same failure, same number — so")
    a("nothing in this phase moved it, and the budget was NOT widened to make")
    a("it green. Widening it would be the gate-weakening this project forbids,")
    a("and `rig_replay_speed` is DERIVED from that budget.")
    a("")
    a("What the episode says is that D7's declared headroom (\"roughly 2x the")
    a("LOADED figure\") no longer holds on this host under ordinary background")
    a("indexing. That is a planning-session decision, not an edit: either")
    a("re-measure and confirm the budget, or record a new loaded-host figure")
    a("and re-derive the rig speed from it.")
    a("")

    # ---------------------------------------------------------------- 10b
    a("## 10b. Adversarial review before the D8.0 hand-back")
    a("")
    a("Five lenses over the whole D8.0 diff — silent loss, encode/decode")
    a("asymmetry, C correctness and memory, tests that cannot fail, and")
    a("scope/labels/honesty — with every candidate finding handed to an")
    a("independent skeptic whose instruction was to REFUTE it and who could")
    a("build and run the daemon to settle it.")
    a("")
    a("**29 candidate findings, 15 refuted, 14 confirmed and fixed before")
    a("hand-back.** The confirmed fourteen, because what a review caught is")
    a("more useful to the next phase than a claim that it found nothing:")
    a("")
    a("| # | Found | Fix |")
    a("| --- | --- | --- |")
    a("| 1 | A measurement datagram the socket refused was counted as SENT and "
      "reached no drop total, so `HealthPacket.drops` stayed still for the one "
      "failure that happens on the link the Jetson is watching | `send_failures` "
      "is now a fourth cause inside `total_drops`, and measurement datagrams "
      "are counted on success only |")
    a("| 2 | The portable detector abandoned the rest of a frame after 254 "
      "components with only a log line, AND it kept the first 254 in raster "
      "order — so a saturated frame went blind below a scanline and the cap's "
      "\"largest areas survive\" was silently pre-empted by scan position | the "
      "list now keeps the BEST 254 by the cap's own order (matching what the "
      "IVE hardware does, which sheds the smallest regions), everything shed "
      "is counted per frame and per run, and the count reaches `--stats` and "
      "the health drop total |")
    a("| 3 | A failed detector frame `continue`d past the housekeeping block, so "
      "a detector failing on EVERY frame produced total silence on the health "
      "plane — indistinguishable from a dead node | the failure branch now goes "
      "to housekeeping; health and control keep running |")
    a("| 4 | The control-plane frame read was bounded per-`read()` by "
      "`SO_RCVTIMEO`, not per frame, so a peer dribbling one byte at a time "
      "held the single-threaded loop indefinitely | a total 5 ms deadline for "
      "one control frame (`sw_read_exact_by`), well under a frame period |")
    a("| 5 | A partial `ive_alloc` failure orphaned the MMZ blocks it had "
      "taken and retried every frame, leaking DMA memory on a 256 MB node | "
      "rollback on failure, geometry committed only after success |")
    a("| 6 | The same defect in the portable detector: `soft_alloc` committed "
      "the geometry before its allocations, so the frame after an OOM "
      "dereferenced NULL | allocate first, commit after, free what arrived |")
    a("| 7 | Section 4's \"nanopb == host codec: yes\" and \"Verdict: EXACT\" "
      "were hardcoded literals in this generator — the report's central claim "
      "asserted, in the document whose job is to test it | both are read from "
      "`wire_identity` in the evidence, produced by running the C tool |")
    a("| 8 | The suite counts in section 10 were literals, and had drifted to "
      "FALSE as tests were added | recorded by `measure` from an actual "
      "`pytest -q` and quoted from the evidence |")
    a("| 9 | The anti-tuning gate tying the declared tolerances to this "
      "document was a whole-document substring search: 9 of 12 corrupted "
      "bounds went undetected, including a 10x-looser host bound | the test "
      "parses section 6's tables by row label and column |")
    a("| 10 | `test_cap_survivors_keep_their_offered_order` passed under the "
      "exact bug it is named for — its fixture's top-N happened to be "
      "ascending in both orders | new fixture where the two orders differ, "
      "plus an assertion that they still differ |")
    a("| 11 | A cmake CONFIGURE failure (a deleted source, a broken "
      "CMakeLists) was classified as an environment problem: 30+ tests "
      "skipped and the suite exited 0 | only a missing toolchain skips; "
      "configure and build failures fail, and both binaries must exist |")
    a("| 12 | E3's per-frame RNG key: deleting the seed, or the camera id, "
      "from it left all tests passing — every comparison was against an "
      "UNJITTERED baseline | each ingredient is now varied on its own |")
    a("| 13 | Neither artifact-producing command took a seed and no artifact "
      "recorded one, against the standing determinism rule | `--seed` on "
      "`fixtures` and on `report measure`; recorded in `stats.json` and in "
      "the evidence |")
    a("| 14 | `measure` recorded nothing identifying the binary it measured, "
      "and its docstring claimed it built one | the evidence records the "
      "daemon's SHA-256; the docstring says what it actually does |")
    a("")
    a("Findings 7-12 are TEST-STRENGTH defects: the code was right and the")
    a("tests could not have caught it being wrong. Findings 1-6 were real")
    a("daemon defects, four of them on failure paths that only open when")
    a("something else has already gone wrong — which is exactly where a node")
    a("in a field for a year gets to spend its time.")
    a("")
    a("Two of the refuted fifteen are worth naming because the refutation is")
    a("the useful part: a claimed `+25.0 px` offset in the host detector does")
    a("not exist (a test's deliberate perturbation, misattributed), and a")
    a("claimed in-contract overflow in the portable detector's label plane is")
    a("arithmetically impossible at the declared bounds.")
    a("")

    # ---------------------------------------------------------------- 10c
    a("## 10c. Adversarial review before the D8.0a hand-back")
    a("")
    a("The same shape, over the amendment's diff: five lenses — host/edge")
    a("numerical identity, honesty and labels, tests that cannot fail, scope")
    a("and blast radius, and C correctness and fixture integrity — each")
    a("candidate finding handed to an independent skeptic told to REFUTE it,")
    a("with a compiler and the suite available to settle it.")
    a("")
    a("**17 candidates, 7 refuted, 10 confirmed — 6 distinct defects, all")
    a("fixed before hand-back.** (Four filings were the same un-regenerated")
    a("report and two were the same quantifier.)")
    a("")
    a("| # | Found | Fix |")
    a("| --- | --- | --- |")
    a("| 1 | The generator was rewritten for the amendment and its ARTIFACT was "
      "not re-run, so this document went on asserting the superseded constant "
      "while the code beside it did the opposite — and `cap.py`'s own comment "
      "pointed the reader at a D8-F1 that contradicted it. The evidence file "
      "was stale too, so `generate` alone would have re-emitted PASS (84 "
      "tests) and a byte-identity verdict measured with the pre-amendment "
      "binary against the pre-regeneration fixtures | rebuild, `measure`, "
      "`generate`, both artifacts committed with the code; plus an E5 test "
      "asserting the committed report IS `generate(committed evidence)`, "
      "which is deterministic because the generator promises byte-identity "
      "for identical inputs |")
    a("| 2 | The new E5 saturation clip produced components of 70-90 px, so it "
      "proved the clamp is reachable and pinned nothing: the daemon's `50` "
      "could be set to ANY value in 41..69 with the whole suite green | the "
      "clip now STRADDLES the point (38..91 px, including 49, 50 and 51) and "
      "asserts it still does. Verified by mutation: 41, 48, 49 and 69 all now "
      "fail. 51 still passes, and correctly — at exactly 50 both expressions "
      "produce a bit-identical 1.0, so `>` versus `>=` is untestable by "
      "construction, which the test says out loud |")
    a("| 3 | The amendment silently REMOVED coverage. Confidence is injective "
      "in area below saturation, so the cap comparator's `-area_px` level "
      "stopped being the deciding one anywhere the suite looked; it decides "
      "again only when several SATURATED components compete for the cap, and "
      "nothing built that — clutter exceeds the cap without saturating, the "
      "saturation clip saturates without exceeding it | a second clip: nine "
      "fat movers against the cap of seven, all at confidence 1.0. Verified "
      "by mutation: reversing the C comparator's area level fails this test "
      "and NOTHING else in the suite |")
    a("| 4 | `sw_pipeline.c`'s stated reason for dividing in double — that "
      "dividing in float \"would land on a different bit pattern for some "
      "areas\" — is false for every area the clamp admits | reworded to say "
      "what is true: the two agree bit-for-bit over 0..49 and first diverge "
      "at 2^24+1, where it is the operand conversion that loses. The double "
      "form is kept because it mirrors the host's SHAPE, not because it "
      "changes a byte today |")
    a("| 5 | D8-F7 said the regeneration moved the field on \"all N\" "
      "observations while section 3.2 derived the saturation-aware count from "
      "the same fixtures — the literal-claim failure mode the new counter "
      "block was added to prevent, in the paragraph that introduced it | both "
      "places derive it identically now |")
    a("| 6 | `stats.json` is the one committed artifact nothing derives, and "
      "this report prints four of its numbers: a wholly falsified stats file "
      "passed the entire suite, and `gate/stats.json` was read by no test at "
      "all. The regeneration exposed it by ADDING a `seed` key — the "
      "committed files had drifted from their own writer | two clip-free "
      "tests: the committed schema must match what `write_fixture` actually "
      "writes (keys only — values are machine-specific), and the four "
      "published counters must equal what the byte-gated `observations.swob` "
      "and `packets.hex` say |")
    a("")
    a("Findings 2, 3 and 6 are TEST-STRENGTH defects and 3 is the one worth")
    a("carrying forward: an amendment can DELETE coverage without touching a")
    a("test, by making one branch of a comparator unreachable from every")
    a("fixture. Nothing goes red when that happens. It was found by mutating")
    a("the C source and re-running the suite, which is the only method that")
    a("finds it.")
    a("")
    a("Three of the seven refutations are worth naming:")
    a("")
    a("- the wire `confidence` has no tolerance axis, but it does not need")
    a("  one: `Observation2D.confidence` is `ge=0.0, le=1.0` in the frozen D0")
    a("  contract, and every board path decodes through that validator before")
    a("  any tolerance comparison is reachable, so an out-of-range confidence")
    a("  dies loudly upstream rather than being measured;")
    a("- the \"integer-safe\" rationale was filed as arithmetically false in")
    a("  full. It is not: the integer comparison is exactly equivalent to its")
    a("  float form over all 2^32, and the skeptic extended the check to every")
    a("  candidate saturation constant from 2 to 65536. Only the one sentence")
    a("  in finding 4 was wrong;")
    a("- E2's decode-direction test does not assert `conf`. True, and left")
    a("  alone: it asserts 12 of 25 fields and `conf` is not singled out, so")
    a("  this is a pre-existing breadth gap in that test rather than anything")
    a("  D8.0a introduced. Recorded here as a D9 lever, not patched in a")
    a("  phase that has no claim on it.")
    a("")

    # ---------------------------------------------------------------- 11
    a("## 11. Out of scope, confirmed untouched")
    a("")
    a("No engine, threshold or fusion change. No NPU classifier. No RTSP/VENC")
    a("beyond the debug smoke look. No real CSI/PTS characterisation — C2 and")
    a("C3 stay parked, and the VI capture path is present, compiled, and")
    a("declared a smoke test with no PTS claim. `v1/` untouched. The bench")
    a("session (conversion-gain PTC, CFA check, sky footage) lands in")
    a("`D3_SENSOR_NOTES.md`, not here.")
    a("")
    a("Two decisions were implemented and both were written down before the")
    a("code moved: the capacity touch, exactly as the D0 \"D8 opening\" entry")
    a("authorised it, and the wire confidence, exactly as the \"D8.0")
    a("amendment\" entry authorised it. Nothing under")
    a("`v2/src/skyweave2/contracts/` changed for either — the amendment moves a")
    a("VALUE the detector puts in an existing field, not the field, its type or")
    a("its bounds — and the fixture regeneration it required is D8 fixtures")
    a("only, with `golden/` untouched.")
    a("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="D8 edge report")
    sub = parser.add_subparsers(dest="command", required=True)

    measure_parser = sub.add_parser("measure", help="replay the fixtures, record evidence")
    measure_parser.add_argument("--out", default=str(DEFAULT_EVIDENCE))
    measure_parser.add_argument("--work", default=None)
    measure_parser.add_argument("--build-dir", default=None)
    # Explicit, per the standing determinism rule.
    measure_parser.add_argument("--seed", type=int, default=fixtures.SCENE_SEED)
    measure_parser.add_argument(
        "--no-suites", action="store_true",
        help="skip the pytest runs (the report's suite rows then read PENDING)",
    )

    generate_parser = sub.add_parser("generate", help="write the report")
    generate_parser.add_argument("--evidence", default=str(DEFAULT_EVIDENCE))
    generate_parser.add_argument("--out", default=str(DEFAULT_REPORT))

    args = parser.parse_args(argv)

    if args.command == "measure":
        import tempfile

        work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="d8-"))
        build_dir = Path(args.build_dir) if args.build_dir else None
        evidence = measure(work, build_dir=build_dir, seed=args.seed,
                           run_suites=not args.no_suites)
        Path(args.out).write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.out}")
        return

    evidence_path = Path(args.evidence)
    evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else None
    if evidence is None:
        print(f"no evidence at {evidence_path}: measured rows will read PENDING")
    Path(args.out).write_text(generate(evidence), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()


__all__ = ["asdict", "generate", "measure"]
