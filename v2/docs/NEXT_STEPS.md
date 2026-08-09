# Next steps (written 2026-08-08, end of the D0-D7 session)

State: the detection rewrite is complete and verified through the wire
(D0-D7 all green). Remaining, per the original plan: D8 (real RV1106 in
the loop) and D9 (wired synthetic acceptance), the declared stopping point
before any full IRL test.

## For Samuel (hands, in order)

1. **Git snapshot.** Commit D5-D7 (nothing is committed since the last
   snapshot). Suggested message: `v2: D5-D7 fusion, faults, wire + reports`.
2. **Bench session** (procedure in `PHASE_D3_BRIEF.md`, results into the
   Measured section of `D3_SENSOR_NOTES.md`):
   - conversion-gain PTC on the SC3336 (anchors every Modeled noise number);
   - CFA pattern check against the driver (de-Provisionalizes the mosaic);
   - several minutes of sky out a window (first honest false-proposal data,
     activates hybrid clips).
3. **Hardware for D8/D9:** flash 3 RV1106 nodes (record the exact image),
   verify SC3336 capture on one; PoE switch powered; Jetson flashed
   (JetPack, Python 3.10); cables. The D9 topology is 3 nodes -> switch ->
   Jetson.
4. Start a **new Cowork session in the Skyweave project** and point it at
   this file.

## For the new session (planner/verifier)

1. **Re-establish trust first:** stage the repo, run the full suite
   (expect ~325 pass + clip-dependent skips), ruff, golden hash manifest.
   This independently verifies the D7 hand-back, which the previous
   session closed on review only.
2. **Resolve the open D7 decision** during D8 planning: the 1200 B
   datagram admits max 5 observations/event and the detector has no
   per-frame cap (levers listed in the D7 closure entry of
   `DETECTION_CONTRACTS_D0.md`; it interacts with the edge byte governor
   in `RV1106_EDGE_NODE.md` section 8).
3. **Plan D8** with Samuel, per the phase discipline (discuss, finalize,
   brief, hand off to local Claude Code, verify): the C edge daemon per
   the `RV1106_EDGE_NODE.md` checklist (Buildroot, RKMPI, IVE GMM2, RGA,
   nanopb speaking `v2/proto/skyweave.proto`), C1 app-layer Y injection,
   the board benchmark that settles GMM2 sustained resolution (intersect
   with the D4 three-resolution scorecards), golden frame->packet fixtures
   against the host detector oracle, fabricated PTS with declared
   offset/drift until real CSI capture.
4. **When bench data lands:** promote the Measured entries, re-check the
   0.75 px tripwire against anchored noise, and re-run the affected
   Modeled tables (D1 budget noise rows, D6 sensitivity curves).
5. **Then D9:** the wired synthetic acceptance run, deterministic then
   wall-clock, one report: the original plan's finish line.

## Standing rules (unchanged)

Label discipline (nothing is Measured without a recorded bench result);
contracts and goldens fenced; anti-tuning on gate scenes; every phase ends
with a decisions-log entry; briefs are the hand-off boundary; adversarial
review before every hand-back.
