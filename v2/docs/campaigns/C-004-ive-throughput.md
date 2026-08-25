# Campaign C-004: IVE detection throughput at 1152x648

**Status:** open 2026-08-24, approved by Samuel. Protocol:
`../CAMPAIGN_PROTOCOL.md` including all amendments. Authority:
D0 log "C-002 closure decisions (2026-08-24)". Baseline fact: the
clean unpaced ceiling is 14.47 fps (Measured, board-104, three runs,
spread 0.02%), A7 at 0.106 core, thermals flat — the wall is the IVE
engine/DMA path (F-C2-2).

## Phase 0 — prerequisites, before any climbing (measurements, not
experiments)

0.1 **Determinism fix (sanctioned in the D0 entry):** zero-initialize
    the fg/bg/match planes at engine init; make the NULL factor-image
    argument explicit. Rebuild in the pinned container; rerun the
    three-run determinism check on the byte-identical 36-frame clip.
    Record the outcome in the ledger:
    - exact agreement -> E8 exact-counter rule STANDS; proceed;
    - divergence persists -> STOP, report to planning: nondeterminism
      becomes a Measured hardware property and the E8 rule is
      re-declared there before this campaign continues.
0.2 Re-baseline the ceiling with the fixed binary (three runs). This
    number, not 14.47, is the campaign's starting scalar if it moved.
0.3 The soak pair (blocked on HANDS: switch/cable/PSU inspection and
    card integrity checks after the F-C2-5 outage) may complete in
    parallel; it is C-002's deliverable, not this campaign's.

## Objective

Maximize `sustained_fps` at 1152x648: the daemon's own health-packet
fps figure, unpaced, over >= 6,300 frames on a rig board, RAM-loop
source, frozen detection knobs.

## Win condition

sustained_fps >= 30.0 with the E8 run-to-run bounds passing, detection
quality parity holding (below), and a soak at the achieved pace whose
re-declared thermal criterion passes. If the engine's hard ceiling
proves lower than 30, the campaign's honest output is that Measured
ceiling plus the best achieved number; the deployment-cadence decision
returns to the planning session either way. A throughput "win" that
detects less is a loss.

## OPEN RULES (amendment 2026-08-25, approved by Samuel — supersedes
the original parity guard and knob whitelist)

The detection APPROACH is free. GMM2 is not sacred; neither is the
IVE engine, morphology, or any DetectorConfig value. Weird ideas are
the point: NEON on the idle A7, hybrid engine+CPU splits, model-store
diets, staggered background updates, coarse-detect/fine-localize
pyramids — anything inside the box. Two things bound the box:

**The frozen boundary (what "inside the box" means).** Inputs: full
1152x648 Y frames, every frame, from the scored source. Outputs: the
frozen wire contract (Observation2D semantics, proto encoding,
capacity, honest envelope fields) — fenced paths stay fenced. The
pool-aware memory budget holds. Causality holds: frame N's
observations may use only frames <= N, enforced as a wire-latency
bound (frame N's packet emitted before frame N+2 is consumed,
referee-measured). Board environment is stock: no clock, voltage,
kernel, or /sys changes.

**The quality floors (truth-based, declared here, before any
submission).** On referee probe clips with ground truth, at 1152x648,
post-warm-up:

| Floor | Value | Provenance |
| --- | --- | --- |
| Detection recall vs truth | >= 0.95 | the D4 acceptance gate |
| False proposals per frame | <= 0.05 | D4 measured 0.003; margin is not permission to spend it |
| Centroid error p95 (full-res px) | <= 1.5 | D4 measured 0.73-0.81; same note |
| Warm-up | <= 90 frames to floor-compliance | D4 schedule |

A submission that breaks any floor scores as a LOSS at any fps. These
floors replace oracle parity: a novel detector answers to truth, not
to `ive_approx`.

## Anti-cheating policy (mechanical, not aspirational)

1. **Unseen probe clips.** Teams receive SAMPLE clips from the
   declared scene distribution (the probe generator and its parameter
   ranges are public). The referee scores every submission on FRESH
   clips from a private seed, generated per scoring run. Memorization
   has nothing to memorize. Gate/acceptance scenes remain excluded
   from everything.
   **Seeking the private seed is cheating.** Teams must not attempt to
   locate, infer, brute-force, or reconstruct the referee's private
   scoring seeds or generated scoring clips — not by searching the
   referee host, the evidence store, forum metadata, run artifacts,
   memory, or timing side channels, and not by statistical inference
   over repeated submissions. Stumbling onto an access path is a
   mandatory rule-6 report (uncredited); using one is score-voiding,
   the same as any silent exploit.
2. **Source-only submissions, referee-built.** Teams submit patches;
   the referee builds in the pinned container and deploys. No team
   binary ever runs on the reference board.
3. **Referee-measured speed.** fps comes from the referee's own
   receive-side timestamps and frame accounting, cross-checked against
   the daemon's health figure; a daemon that lies about its own rate
   is caught by the wire.
4. **Diff jail.** A submission may touch only the firmware detector
   path (`firmware/rv1106/src`, detector-scoped) — the referee rejects
   diffs touching the harness, scorer, campaign files, proto, wire,
   provisioning, or any fenced path. Enforced by path check on the
   patch, same machinery as the fenced-path attestation.
5. **Reproducibility.** Scored protocol stays: multiple runs, fresh
   seeds, E8 bounds (as amended by the Phase-0 determinism outcome);
   a leader confirms on a second board. One-off lucky runs do not
   lead.
6. **Loophole procedure.** Hunting for exploits is not a scoring path
   and earns nothing. But a team that STUMBLES onto one has exactly one
   legal move: post it to the forum so the rule gets patched. Reporting
   is neutral (no reward, no penalty); using it silently voids the
   team's scores for the epoch. All compute belongs on the hill.

## Seed ideas (public to all teams, from the C-002/C-004 profiling)

Published identically to every team for fairness; diversity comes from
the models, not from information asymmetry.

1. **Engine-path mechanics** (the 69 ms/frame): pipeline/async the
   strictly-serial copy -> GMM2 -> erode -> dilate -> CCL chain
   (engine idles during CPU steps and vice versa; double-buffer frame
   N+1's copy under frame N's engine work). Reclaim the cached source
   buffer + explicit flush instead of the uncacheable 746 KB/frame
   memcpy (post-wedge safety trade; keep the phys-guard). Eliminate
   the `mask_preserve` full-frame copy by role-swapping planes.
   Dropping morphology (`open_radius 0`) removes two of four engine
   ops — now legal, floors judge it.
2. **Memory bandwidth** (the likely true ceiling): the GMM2 model
   store streams ~12 B/model/pixel through DDR both ways every frame;
   `model_num` 3 -> 2 cuts a third — legal now, floors judge it. DDR
   bandwidth is NOT-MEASURED; instrument before believing any
   bandwidth theory.
3. **Profile first.** The intra-frame profiler exists; nobody knows
   how the 69 ms splits across copy/GMM2/morph/CCL. Submissions
   without profiling evidence are guesses wearing confidence.
4. **Hot-loop hygiene:** rate-limit the per-slot stderr WARN/DEBUG
   inside the region-scan span; an anomalous frame currently pays
   console I/O in the frame loop.
5. **Operating point** stays planning-scope: accepting ~15 fps,
   procurement of 256 MB G3 silicon, and resolution changes are
   Samuel's decisions, not submission levers.

## Probe inputs

The retained C-001/C-002 probe clips and their manifests, fresh seeds
per batch. Gate and acceptance scenes are forbidden inputs; the runner
refuses them.

## Budget and stop rules

Protocol defaults (40 experiments/shift, 20 min/experiment, 6 PoE
cycles). Additional: any wedge recurrence (F-C1-5 class) stops the
shift immediately; two consecutive shifts with no fps improvement
above the E8 noise floor end the campaign at the measured ceiling;
infrastructure loss (Jetson or switch, F-C2-4/5 class) stops the shift
with a diagnostic note, never a scored claim.

## Evidence weight

Per the attestation-proportionality amendment: full identity binding
for scored fps results and the Phase-0 determinism verdict; lightweight
(hashed once) for profiling and diagnostics. Ledger and SHIFT.md go to
the private store.

## Deliverables

Ledger + SHIFT chain; the Phase-0 determinism verdict; the fixed-binary
baseline; the best sustained fps with confirmation (fresh seed, second
board); profiling evidence for where the remaining wall is; a drafted
deployment-cadence recommendation for planning ratification. D8.1
closure consumes this campaign's final fps number.
