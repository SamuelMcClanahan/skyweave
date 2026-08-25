# Campaign protocol: autonomous hillclimbing with guardrails

**Status:** adopted 2026-08-20, approved by Samuel. Parameters marked
Provisional are adjustable at any campaign boundary.

## Why

Ideas live in theory until the rig disagrees. Experiments are now cheap
(deterministic harness, scored JSON, SSH to every board, remote PoE
power-cycle), so the bottleneck is no longer running experiments, it is
deciding them one at a time through a human. A campaign lets an agent
iterate toward a goal for hours, across context windows, without
weakening the standing rules. The risk inverts under autonomy: the
danger stops being too little data and becomes overfitting to the
bench. Every guardrail below exists for that reason.

## The shape of a campaign

One campaign = one file in `v2/docs/campaigns/` with exactly these
sections. An agent may not start a campaign whose file does not exist
(same rule as phase briefs).

1. **Objective.** ONE scalar to minimize or maximize, computable by the
   harness from a scored artifact. No prose objectives.
2. **Subject-to.** The constraints that make climbing safe. Always
   includes: full suite green on the gate platform; fenced paths
   untouched (v1/, golden/, contracts except entries the campaign file
   itself sanctions, proto/); no gate-scene input to any decision;
   host-board parity within its declared tolerance where applicable.
3. **Knob whitelist.** The exact config fields the agent may vary, each
   with an allowed range. Anything not listed is frozen. The whitelist
   is enforced by the campaign runner, not by agent memory.
4. **Probe inputs.** The clips/scenes experiments may score. Gate and
   acceptance scenes are NEVER probe inputs. If a probe clip is
   generated, its manifest and seed are recorded like any artifact.
5. **Budget.** Max experiments per shift, max wall-minutes per
   experiment, max board power-cycles per shift. Defaults (Provisional):
   40 experiments, 20 minutes, 6 power-cycles.
6. **Stop rules.** Hard conditions that end the shift with a ledger
   summary instead of continuing: budget exhausted; board unreachable
   after 2 PoE cycles; objective regressed for N=8 consecutive
   experiments; any subject-to violation; any surprise that would
   require a contract change (stop and report, never improvise).
7. **Win condition.** The objective threshold that ends the campaign,
   plus the confirmation requirement (below).

## The ledger (memory across agents)

`v2/docs/campaigns/<id>/ledger.jsonl`, one line per experiment:

```json
{"n": 17, "ts": "...", "hypothesis": "area floor scaled 16x cuts
sub-cap failures", "knobs": {"min_area_px": 32}, "seed": 1017,
"board": ".104", "result": {"fail_rate": 0.21, "fps": 24.8},
"verdict": "improved", "note": "threshold-runaway mode unchanged"}
```

Rules: append-only; every experiment seeded; the scored JSON artifact
is kept beside the ledger; a successor agent reads the ledger and the
campaign file, never a predecessor's transcript. Shift change = write a
`SHIFT.md` (current best, active hypotheses, dead ends with reasons,
next three experiments) and stop. The next agent starts from those two
files. Dead ends with reasons are mandatory: ruled-out theories are
the ones that otherwise get re-investigated at midnight.

## Anti-overfitting (non-negotiable)

- **Confirmation runs.** No result is a win until reproduced with a
  fresh seed, and (if board-dependent) on a second board. An agent
  running dozens of experiments WILL find spurious wins; confirmation
  is the filter.
- **Declared before run.** Any tolerance or threshold a campaign
  result will be judged against is written in the campaign file before
  the first experiment.
- **Gate scenes stay dark.** The campaign runner refuses to load
  acceptance manifests. D9 is a one-shot gate, not a campaign; nothing
  ever hillclimbs on it.
- **Winner ratification.** A campaign's winning config becomes real
  only when the planning session verifies the ledger and appends the
  decision to the D0 log. Until then it is Provisional.

## Board recovery (closing the physical loop)

The Sirivision switch exposes per-port PoE control (`poe.cgi`, see
`D8_1_RIG_DEBUG_LOG.md`). The campaign runner gains: cycle port, wait
for boot, re-verify board identity (MAC + image marker) before
continuing. A board that fails identity after recovery is excluded from
the shift and reported, never silently substituted. Hands stay needed
only for: cards, cables, supplier issues, and anything with money.

## Amendments (2026-08-23, from the C-001 alignment audit)

1. **Memory durability without public exposure.** Campaign memory
   (ledger, SHIFT.md, findings, progress logs) stays OUT of the public
   repo by design: it carries rig topology, addresses, and session
   detail. But it must not live as a single untracked copy on one
   machine — that is how the D0 C3-opening entry was nearly lost. Rule:
   campaign memory lives in a separate PRIVATE versioned store (a
   private repo, or a local repo synced off-device). A shift may not
   end without committing its ledger and SHIFT there. The planning
   session reads that store at campaign boundaries.
2. **History rewrites.** No public-repo history rewrite without a
   planning-session check of the decisions log immediately after, and
   a copy of `DETECTION_CONTRACTS_D0.md` in the private store first.
3. **Attestation proportionality.** Full identity/attestation binding
   applies to SCORE-BEARING evidence (anything a ledger verdict or a
   ratification will rest on). Diagnostic channels get lightweight
   treatment: recorded, hashed once, not fortress-staged. A shift that
   spends more effort on evidence machinery than on experiments should
   say so in SHIFT.md and the boundary review decides whether to trim.

## Human checkpoints

Campaign start: Samuel + planning session write the objective,
subject-to, whitelist, and budget together. Campaign end: planning
session verifies the ledger, spot-reruns the winner from its seed, and
ratifies or rejects. Mid-campaign the agent is autonomous inside the
file. Contract changes, gate scenes, procurement, and physical work
never happen inside a campaign.

## What makes a problem climbable (the five properties)

1. ONE scalar to improve.
2. A machine scores it cheaply (minutes, not hours).
3. Scoring is deterministic, or its noise is measured and bounded.
4. Floors protect everything that must not get worse.
5. Probe inputs can be generated fresh, so nothing can be memorized.

A problem missing one property is not unclimbable — the missing
property is the first work item (Phase 0 of C-004 is the worked
example: determinism was missing, so determinism was built first).

## Hill registry (candidate future campaigns, 2026-08-26)

| Hill | Scalar | Floors | Judge | Ready when |
| --- | --- | --- | --- | --- |
| Node fps (C-004) | sustained fps | recall/false/centroid vs truth | board + host | RUNNING |
| Detection quality on real sky | false alarms/hour at fixed recall | recall, centroid error | scored real footage | bench-session sky data exists (needs labeling strategy) |
| Fusion tracking accuracy | range/cross-range p95 vs truth on probe scenes | coverage honesty (covariance calibration), no gate scenes | synthetic scenes + scorer (exists) | now — probe scene generator distinct from gate scenes |
| Jetson many-camera throughput | cameras sustained at 30 Hz fusion | decision-stream byte-parity vs reference | replay + wall clock | now — the THROUGHPUT doc's three bottlenecks are the seed ideas |
| End-to-end latency | frame-capture to track-update p95 | no accuracy floor broken | wired rig timestamps | after D9 (needs the real wire) |
| Suite speed (meta-hill) | fast-tier wall time | zero tests weakened or deleted | CI timer | now |
| Soak reliability | hours between faults | honest fault reporting (no suppression) | long-run harness | slow hill; after C-002 soak pair |
| Drone intercept (future repo) | p95 miss distance in SITL | safety envelope, causality, link-fault tolerance | SITL + seeded scenarios | after drone contracts + SITL harness — the BEST hill in the program: fast, deterministic, truth-rich |

NEVER hills: D9 acceptance (one-shot gate, by design), contract
decisions, label promotions, gate scenes, procurement. A hill whose
judge would need a human in the loop per-iteration is not a hill yet;
build the judge first.

| Id | Objective (scalar) | Gate it unblocks | Status |
| --- | --- | --- | --- |
| C-001 | Minimize CCL detector-fail rate at 1152x648 on probe clips | Clean C3 sweep | Ready to start |
| C-002 | Measure sustained fps + soak stability at 1152x648 (measurement campaign: the "climb" is fixing whatever breaks the soak, objective = soak passes with E8 bounds) | C4 fps number; C5-C8 close D8.1 | Blocked on C-001 |
| C-003 | Board-vs-host scorecard divergence within declared tolerance (D8.2 fixture replay; knobs are BUG-class fixes only, never quality tuning) | D8.2, closes D8 | Blocked on C-002 |
| C-004 | Maximize sustained fps at 1152x648 (implementation levers only, detection semantics frozen, quality-parity guarded). Phase 0: the sanctioned determinism fix + re-baseline | The D8.1 fps number and the deployment cadence | Open 2026-08-24 |
| (not a campaign) | Bench session: conversion-gain PTC, CFA check, sky footage | Measured promotions, tripwire re-check | HANDS, parallel |
| (not a campaign) | D9 wired synthetic acceptance | The finish line | One-shot gate. Never iterated |
