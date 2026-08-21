# SkyWeave: agent entry point

SkyWeave detects and tracks flying objects with multiple cheap fixed cameras.
RV1106 edge nodes find motion and send compact observations. A Jetson fuses
them: triangulation first, sparse voxel evidence as the proposal stage, then
an EKF track. This repo holds v1 (the reference MVP) and v2 (the rewrite).

## Read these before writing code

1. `v2/docs/DETECTION_ARCHITECTURE_WORKING_PLAN.md` — the architecture.
2. `v2/docs/DETECTION_CONTRACTS_D0.md` — frozen contracts and the decisions
   log. Section 10 lists decisions made after the original freeze.
3. `golden/REGENERATION.md` — golden policy and environment requirements.
4. The current phase brief: `v2/docs/PHASE_D8_BRIEF.md` and its runbook
   `v2/docs/D8_1_BOARD_RUNBOOK.md` (a phase brief is created when the
   phase is finalized; if it does not exist, the phase is not ready and
   you should not start it).
5. `v2/docs/CAMPAIGN_PROTOCOL.md` — autonomous iteration happens ONLY
   inside a campaign file under `v2/docs/campaigns/`. No campaign file,
   no climbing. The ledger and SHIFT.md are the memory between agents;
   read those, not predecessor transcripts.

## Standing rules

- **v1 is read-only.** It is the reference implementation and golden
  generator. Never edit, reformat, or "fix" anything under `v1/`.
- **Contracts are frozen.** Anything in `v2/src/skyweave2/contracts/` changes
  only with a recorded decision appended to the D0 spec. If a task seems to
  need a contract change, stop and report instead.
- **Goldens regenerate only with a recorded reason** in
  `golden/REGENERATION.md`, with OpenCV installed (finding F-D0-8).
- **Never tune on gate scenes.** Acceptance manifests are held out.
- **Determinism:** every artifact-producing command takes an explicit seed;
  no wall-clock values in scored outputs; dataset/golden IDs are SHA-256
  hashes over manifest + versions + seed + git revision.
- **Label discipline:** claims are Chosen, Provisional, Measured, Deferred,
  or Rejected. Nothing in this project is Measured yet unless a recorded
  bench result exists. Do not promote labels in documents.
- **Two error channels:** conditional covariance (random, filter input) and
  labeled systematic bounds (bias, report-only). Never fold a bias into a
  covariance.

## Environment and commands

Python 3.10 pinned (matches the Jetson image). uv manages everything.

```bash
cd v2
uv sync --extra dev
uv run pytest                      # D0 suite: T1-T5, T7 (27 tests)
PYTHONPATH=../v1/src uv run pytest # also runs the v1 convention audit
uv run ruff check src tests
```

All 27 tests and ruff must pass before any hand-back. New phase code adds
tests; it never deletes or weakens existing ones.

## Phase roadmap (detection rewrite)

| Phase | Scope | Status |
| --- | --- | --- |
| D0 | Contracts, tests T1-T7, goldens | Done 2026-08-05 |
| D1 | Geometry: projection, initializer, refinement, covariance, Monte Carlo budget | Done 2026-08-05, verified |
| D2 | Freeze the named EXP-001 scene | Frozen: `v2/configs/exp001_scene.yaml` |
| D3 | Sensor model, scorecard, hybrid clips, Blender | Done 2026-08-06, verified |
| D4 | Host reference detector, 3-resolution sweep | Done 2026-08-07, verified. Tripwire CLEAR (0.141 px, Modeled) |
| D5 | Association, refinement, EKF track lifecycle | Done 2026-08-08, verified through D5.3. All 8 acceptance lines PASS |
| D6 | Noise and fault injection | Done 2026-08-08, verified incl. D6.1. Honest-declaration overconfidence: 0 |
| D7 | Protobuf/UDP wire freeze, replay | Done 2026-08-08. Software complete through the wire |
| D8 | Real RV1106 in the loop | In progress. D8.0 + D8.0a done, verified. D8.1: Phases A/J/B/C0 done (image is a bootable SD card per D8-F15; 5 nodes up, boards run from cards); C1 in progress — see `v2/docs/D8_1_BOARD_RUNBOOK.md`. Observation-cap decision closed (D0 log, D8 opening). Bench session still pending, parallel |
| D9 | Wired synthetic acceptance (3 nodes + switch + Jetson) | Stopping point |

Planning and verification happen in the Cowork "Skyweave" project sessions;
this file and the phase briefs are the hand-off boundary. Keep work inside
the current brief's scope.
