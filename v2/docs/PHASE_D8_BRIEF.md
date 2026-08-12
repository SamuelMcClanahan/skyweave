# Phase D8 brief: real RV1106 in the loop

**Status:** finalized 2026-08-09, approved by Samuel.
**Read first:** `/CLAUDE.md`, `v2/docs/DETECTION_CONTRACTS_D0.md` (all
closure entries, especially "D8 opening (2026-08-08)"),
`v1/docs/RV1106_EDGE_NODE.md` (the node design and checklist),
`v2/docs/SYNTHETIC_PIPELINE_DESIGN.md` section 6 (the C1 injection stage),
`v2/docs/D4_DETECTOR_REPORT.md` (the three-resolution scorecards),
`v2/docs/D7_WIRE_REPORT.md` (wire freeze and findings).

## Goal

Put a real RV1106 node on the wire. The C edge daemon detects on injected
Y frames with hardware IVE GMM2/CCL, encodes observations with nanopb per
the frozen `v2/proto/skyweave.proto`, and the unchanged v2 host stack
decodes and scores them. The board benchmark settles the deployment
resolution. The scored path this phase is C1 app-layer Y injection;
real CSI capture gets a smoke test only.

## Sub-phases and gates

| Sub-phase | Needs | Delivers |
| --- | --- | --- |
| D8.0 host-side | No hardware. Docker build container on the Mac (Linux PC runs the same container as a mirror; the container image tag is recorded in the report and is the reproducible build) | Capacity implementation, daemon source cross-compiled, injection harness, golden fixtures |
| D8.1 board bring-up | One flashed node (record the exact image), PoE switch, Jetson | Daemon on board, C1 injection running, benchmark + soak, deployment resolution Chosen |
| D8.2 board validation | D8.1 complete | Fixture replay through the board, parity + toleranced scorecard, health, cap behavior |

An agent must not start D8.1 until Samuel confirms the flashed node.

## Scope

1. **Capacity decision implementation** (per the recorded D8 opening
   entry — this is the sanctioned contract touch): datagram ceiling
   1200 -> 1472 B; `ObservationPacket.observations` max_count 5 -> 7 in
   `proto/skyweave.options`; detector per-frame component cap 7, keeping
   the top components by descending confidence, in the shared
   `DetectorConfig` (host oracle and edge daemon read the same value);
   dropped components counted in detector stats and the health path,
   never silent. Invariant, tested: wire max_count >= detector cap.
   Existing golden byte fixtures must NOT change (max_count is an
   allocation bound, not a wire value).
2. **The C edge daemon** (`firmware/rv1106/`): one statically-or-minimally
   linked C daemon per the `RV1106_EDGE_NODE.md` checklist. Buildroot
   rootfs, boots into the daemon; no Python, no OpenCV, no systemd.
   RKMPI VI capture path present but behind the injection source this
   phase; IVE GMM2 + CCL as the detector; RGA for crops; nanopb encoding
   of `skyweave.proto` (bounds from `proto/skyweave.options`, verbatim);
   UDP measurement plane + 1 Hz health packet + TCP control plane per D7.
   Cross-compiled in the pinned container with the Luckfox SDK toolchain.
3. **C1 injection harness** (host side + daemon input source): push U8
   luma frames into the daemon over Ethernet or from local storage.
   PTS is fabricated by the harness with configurable and DECLARED
   offset, drift, and jitter — stamped honestly into
   `time_sync_error_ms`, never presented as a real capture clock.
4. **Golden frame-to-packet fixtures:** known Y clips (golden gate clip
   Y planes + a cluttered synthetic clip that exceeds the cap) through
   the host `ive_approx` oracle produce expected observations and
   expected packet bytes. Fixture policy is SPLIT:
   - **Wire: exact.** For identical observation inputs, nanopb encoding
     must be byte-identical to the host codec. This gate is absolute.
   - **Detector: toleranced.** Hardware GMM2 output is compared to
     `ive_approx` with tolerances DECLARED IN THIS BRIEF'S REPORT BEFORE
     the first board run (anti-tuning rule); the divergence itself is a
     Measured result, not a failure, unless it exceeds the declared
     bounds.
5. **The board benchmark** (D8.1): GMM2 sweep at the three D4
   resolutions (2304x1296, 1536x864, 1152x648): sustained fps, memory,
   DDR bandwidth, A7 utilization, thermals, plus a one-hour soak at the
   surviving resolution. All three resolutions pass the D4 host gate, so
   the board ceiling picks the deployment resolution; the intersection
   argument and the choice are recorded as a decisions-log entry
   (label: Chosen, benchmark numbers Measured).
6. **CSI smoke test only:** SC3336 frames flow through VI on one node,
   eyeballed via the optional debug stream. Not scored, no PTS claims.
   Full CSI/ISP/PTS characterization is deferred (C2/C3 remain parked).

## D8.0a amendment (2026-08-09, after the D8.0 hand-back)

Adopt the area-derived wire confidence per the "D8.0 amendment" entry in
the D0 decisions log: `min(1.0, area_px / 50.0)`, defined once in
`component_confidence()`, mirrored integer-safe in `sw_pipeline.c`,
D8 fixtures regenerated from the host oracle (the log entry is the
recorded reason), E2/E5 byte-identity re-applied after regeneration.
The two hand-back tests (reported == ranked; fresh oracle reproduces
committed bytes) must pass unchanged in form. Full suite + ruff green,
adversarial review before hand-back, as always.

## D8.1-prep amendment (2026-08-10): board-free D8.1 work, sanctioned now

Three D8.1 items have no board dependency and may be done immediately,
before the flashed-node gate; the gate still holds for everything else.

1. **Flashable image build:** full Buildroot image set (boot, kernel,
   rootfs) from the pinned SDK commit in the pinned container. Record
   defconfig, SDK commit, and SHA-256 of every produced image file in
   the report's build-provenance section. The daemon does NOT need to be
   baked in; hand-start is acceptable until D8.2.
2. **Benchmark + provisioning harness:** the resolution sweep runner
   (three D4 resolutions, sustained fps/memory/DDR/A7/thermal
   collection, soak orchestration, E8 run-to-run bounds), a node
   provisioning script (push daemon, start, collect results), and the
   health-packet listener. All exercised against the HOST-built daemon
   in tests so the first board session debugs the board, not the
   tooling. Board-only collectors (DDR counters, thermals) may stub
   with a loud NOT-MEASURED marker, never a fake number.
3. **Declared tolerances:** the D8.2 detector tolerance bounds and the
   benchmark run-to-run bounds written into the report skeleton and
   committed BEFORE any board run, per the anti-tuning rule already in
   this brief.

## Tests (E-series)

E1 capacity: cap + max_count + ceiling constants agree, invariant holds,
cluttered frame emits top-7 with drops counted, event always encodable;
E2 nanopb byte-identity against host codec on fixture observations,
both directions, unknown-field tolerance; E3 injection harness
determinism (same clip + seed + declared PTS params -> identical daemon
input stream); E4 fabricated-PTS honesty (declared offset/drift appears
in the envelope, never zeroed); E5 fixture replay host-side (C daemon
compiled for host or run under QEMU where feasible; otherwise E5 runs in
D8.2 on the board); E6 board fixture replay decodes on the unchanged v2
stack and passes the toleranced scorecard; E7 health packets at 1 Hz
with real drop counters; E8 benchmark reproducibility (two runs, same
config, stats within declared run-to-run bounds). E1-E4 are D8.0;
E5-E8 are board-gated. All prior suites (T, W, everything) + ruff stay
green; fenced paths untouched; goldens regenerate only with a recorded
reason (none is expected this phase).

## Report

`D8_EDGE_REPORT.md`: build provenance (container image tag, SDK version,
board image), benchmark tables per resolution, soak result, the
deployment-resolution decision and its intersection argument, wire
byte-identity verdict, detector tolerance table (declared bounds vs
measured divergence), health/cap behavior, and surprises with
shipped-consequence statements. Board numbers are Measured; everything
else keeps its labels. The report's tolerance declarations must be
committed BEFORE the first board scorecard run.

## Out of scope

NPU classifier (phase 2 of the node design); RTSP/VENC beyond the debug
smoke look; real CSI/PTS characterization (C2/C3 parked); any engine,
threshold, or fusion change; the bench session (conversion-gain PTC, CFA
check, sky footage) runs in parallel and lands in `D3_SENSOR_NOTES.md`,
not here.

## Done when

E1-E4 pass in CI-equivalent host runs and the daemon cross-compiles
clean in the pinned container (D8.0 hand-back); board boots into the
daemon, benchmark + soak recorded, resolution Chosen (D8.1); E5-E8 pass
on the board, the toleranced scorecard holds, and the hand-back lists
divergences and surprises (D8.2). Each sub-phase ends with its own
adversarial review before hand-back, per the standing rules.
