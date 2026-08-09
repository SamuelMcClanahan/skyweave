# Phase D7 brief: wire freeze and loopback replay

**Status:** finalized 2026-08-08, approved by Samuel.
**Read first:** `/CLAUDE.md`, `v2/docs/DETECTION_CONTRACTS_D0.md` (reserved
field numbers in the contract docstrings + all closure entries),
`v2/docs/PHASE_D6_BRIEF.md` (Tier IV faults, re-run here over real sockets).

## Goal

Freeze the wire. Prove the unchanged D5/D6 engine behaves identically when
observations arrive over real UDP sockets, including under stream faults.
The measurement path carries protobuf observations, never video; the vendor
RTSP stays as an optional human debug stream only (no H.264 on the
measurement path, ever).

## Scope

1. **Schema** (`v2/proto/skyweave.proto`, proto3): `ObservationPacket`
   (FrameEnvelope + repeated Observation2D), `HealthPacket` (fps, drops,
   time_sync_error_ms, IMU quaternion), control messages (calibration
   revision, config, start/stop). Field numbers EXACTLY the D0 reservations.
   nanopb-friendly: bounded repeated fields, no maps, no recursive types
   (the D8 C daemon encodes with nanopb). Golden byte fixtures pin encoding;
   unknown-field tolerance tested in both directions; parity tests against
   the pydantic models.
2. **Datagram discipline:** one capture event per UDP datagram; 1200-byte
   provisional ceiling verified against the worst case (max components);
   measurement data never splits; patches/masks on a separate droppable
   channel with expiry (evidence-drop rule); control plane over TCP, same
   protobuf, length-prefixed.
3. **Transport modules** (`v2/src/skyweave2/transport/`): encoder/decoder,
   UDP sender/receiver, TCP control channel, socket ingest adapter feeding
   the UNCHANGED fusion engine (no engine or threshold edits).
4. **Replay pacing:** `RecordedYFrameSource`-style observation replay in the
   two frozen modes: deterministic (synthetic capture times carried) and
   wall-clock (scheduled against local clocks, real packet age measured).
5. **Loopback rig:** three edge-simulator processes, each replaying one
   camera of the golden gate clip observations, real UDP via loopback into
   the engine process. This is the D9 topology minus boards.
6. **Tier IV re-run at socket level:** loss, reorder, duplication, lateness,
   node restart, camera dropout, injected on the real sockets. Policies
   must match D6's stream-level results; divergences are findings.
   Determinism per EXP-001: same delivered-event stream, identical
   decisions, byte-compared.

## Tests (W-series)

W1 proto round-trip + pydantic parity + golden bytes; W2 datagram ceiling
worst-case (a too-big packet FAILS loudly, never truncates silently);
W3 evidence-channel drop never blocks geometry; W4 socket vs local replay
parity on the clean gate clip (identical acceptance table); W5 Tier IV
socket faults reproduce D6 policies; W6 wall-clock mode reports real packet
age and stays within declared jitter on loopback; W7 delivered-event
determinism cross-process; W8 session restart on the wire (new UUID mid-
stream) handled per D0. All prior suites + ruff green; fenced paths
untouched. Protobuf runtime joins deps via lockfile; nanopb NOT required
this phase (schema compatibility only).

## Report

`D7_WIRE_REPORT.md` (seeded, byte-identical twice): measured datagram size
distribution vs ceiling, loopback latency stats, W4 parity table, Tier IV
verdicts. Host-side loopback numbers labeled Measured; everything else
keeps its labels. NOTE: loopback latency is not network latency; D9
measures the real switch.

## Done when

W1-W8 pass; report reproduces; the same acceptance table emerges from file
replay and socket replay; hand-back lists datagram sizes, any D6-policy
divergence, and surprises with shipped-consequence statements.
