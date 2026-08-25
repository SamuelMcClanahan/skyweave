# C-004 tournament annex: five-team hillclimb to 30 fps

**Status:** adopted 2026-08-25, approved by Samuel; roster and board
model finalized 2026-08-26. This annex extends
`C-004-ive-throughput.md`; the campaign file's OPEN RULES (frozen
boundary + truth floors), anti-cheating policy, Phase 0, and stop
rules bind every team identically. Protocol amendments apply.

## Teams — five, one board each

| Team | Harness | Model | Board |
| --- | --- | --- | --- |
| T1 | Claude Code | Opus 5 (Opus 5 subagents) | assigned at kickoff |
| T2 | Azure Codex | GPT 5.6 Sol Ultra (Sol subagents) | " |
| T3 | Opencode (OpenRouter) | Ox Alpha (stealth) | " |
| T4 | Opencode (Fireworks) | Qwen3.8-2.4T-A95B | " |
| T5 | Opencode (OpenRouter) | Muse Spark 1.2 Contributor | " |

Board assignments (physical position, MAC) are recorded at kickoff;
prerequisite HANDS: .236 reflashed onto a genuine card, cameras
optional per the profiling policy. Each team works in its own branch
(`c004/t1`..`t5`) from the frozen base. Each Opencode/Codex model gets
a 30-minute orchestration smoke test before its VM is provisioned; a
model that cannot drive its harness is benched and recorded, not
babysat.

## Scoring — own-board, baseline-normalized

There is no dedicated referee board. Instead:

1. The referee keeps a FROZEN BASELINE BINARY (the Phase 0 fixed
   daemon). Daily, and before any scoring session, it runs the
   baseline on each team's board (3 runs): that board's
   `baseline_fps`.
2. An official submission is scored ON THE SUBMITTING TEAM'S BOARD:
   referee-built from source in the pinned container, referee-deployed,
   referee-run on unseen probe clips, floors checked against truth.
3. The leaderboard ranks `speedup = submission_fps / baseline_fps`
   (same board, same day). Board silicon divides out to first order.
   Absolute fps is displayed beside it; nobody optimizes a ratio
   without the real number in view.
4. Scoring runs preempt the team's inner loop on that board (~25 min);
   the referee schedules them, the team waits for its own runs only.
5. **The absolute verdict stays absolute:** the 30 fps win condition is
   measured directly, and a would-be winner is confirmed on a SECOND
   team's board (briefly borrowed) plus the full soak before
   ratification. Baseline drift on any board (>E8 bounds day-over-day)
   is a finding and freezes that board's scores until re-measured.

**The referee is an automated judge service** — submit, get scored,
get the verdict back, no human and no polling: floor verdicts return
in ~3 minutes with full diagnostics, board verdicts in ~10, pushed to
the submitting team over CCP the moment they exist, leaderboard
updated automatically. Five boards score in parallel; teams pipeline
freely.

**Scoring ladder (fast verdicts, declared before use):**

- **Tier 0 — host floor pre-check, no board (~3 min):** every
  submission must keep a host-buildable arm (the oracle pattern; this
  is a standing OPEN RULES requirement for novel detectors too). The
  referee scores recall/false/centroid against truth on the VM; a
  floor failure is rejected here at zero board cost. The board only
  ever measures the one thing hosts cannot: speed.
- **Tier 1 — short board verdict (~8-10 min end to end):** single run,
  steady-state fps window (~1,500 frames, first 300 dropped), on the
  team's own board. Legitimate only under Phase 0 determinism (one run
  measures what three did); if Phase 0 leaves residual nondeterminism,
  Tier 1 uses the re-declared bounded comparator instead.
- **Tier 2 — full protocol (three runs, full length, board-confirmed
  floors):** required for top-3 leaderboard positions, the daily best,
  and anything heading to ratification. Cheap heats, expensive finals.
- **Validation before use:** Phase 0 runs Tier 1 and Tier 2 side by
  side and verifies agreement within E8 bounds; short-run bias is
  measured before any leaderboard exists, and the declared correction
  (if any) travels with every Tier 1 score.
- **Top-3 re-verification (decided 2026-08-26):** whenever the top 3
  changes, the judge runs Tier 2 on the new entrant within 30 minutes,
  on fresh probe clips. An unchanged top 3 is not re-reviewed — the
  verdict is cached by submission hash. A re-verification that lands
  outside E8 bounds of the original score is a finding and suspends
  that entry pending diagnosis.

Daily cap: 6 Tier-2-equivalents per team; Tier 0/1 verdicts are cheap
enough to be effectively uncapped within the team's own board time.
Inner-loop use of your own board is otherwise uncapped.

## The auditor (Fable 5 High — no board, no scores)

A standing audit agent with READ-ONLY access to every team branch,
the referee's ledger and artifacts, and the forum. Periodically (each
epoch boundary, and daily on the top 3) it deep-reads winning patches
for what mechanical checks cannot see: semantic floor-gaming, probe
distribution overfitting, causality tricks in buffer management,
collusion patterns across teams, and referee-blind exploits. It
publishes audit reports to the planning session and (in the open
epoch) the forum. It has NO scoring power and NO write access to
anything but its own reports; action on an audit flag belongs to
Samuel/planning. The auditor is also the standing check ON the
referee: it re-derives a sample of scores from raw artifacts.

## Cameras

Available on team boards as a PROFILING source (true ISP DDR traffic).
Camera runs are diagnostics, never scored. Any team bringing up the
stubbed VI capture path must merge it to the shared base at epoch-open;
until merged it may not be used in any scored run.

## Claims feed vs Official board (decided 2026-08-26)

Teams may post self-measured numbers to a LIVE CLAIMS FEED on CCP at
any time — unofficial by construction, displayed separately, useful
for pace and morale. The OFFICIAL leaderboard is written only by the
referee through the scoring ladder. Full self-reported scoring was
considered and rejected: it returns the scoring clips to team custody
(killing the unseen-probe defense), forks the ruler into five
implementations, and hands optimizers write access to their own
objective. The auditor computes CLAIM INFLATION automatically (claims
persistently above official scores) as a standing flag; chronic
inflation is a finding, not a score.

## Forum: CCP

CCP server (github.com/Squid-Proxy-Lovers/ccp) on the always-on relay
VM, tailnet-only, one authenticated identity per team plus referee,
auditor, and planning. Leaderboard posted by the referee only. Fallback
if CCP integration stalls: the append-only posts directory in the
evidence store — the tournament does not wait on transport.

## Epochs

- **Epoch 1 — sealed (short):** teams see leaderboard scores only.
  Ends at the FIRST of: 48 hours, or every team holding >= 2 official
  scores. Diversity first.
- **Epoch 2 — open:** all posts, diffs, findings visible to all teams;
  idea provenance recorded. Capture-path code merges to base here.

## Agent hosting

Each team agent runs UNRESTRICTED (root, no permission prompts) on its
own disposable x86_64 Azure Ubuntu VM. Containment is walls, not
flags:

- **Tailscale ACLs are the enforcement layer.** Each team VM routes to
  exactly: its own board (via the Jetson network hop), CCP, and the
  submission endpoint. No route to other boards, the referee VM, the
  auditor, or the evidence store.
- Each VM carries only its team's API key and checkout; a trashed VM
  rebuilds from image.
- Sizes: team VMs `D4s_v5` (4 vCPU/16 GB, ~128 GB SSD); referee
  `D8s_v5` (8 vCPU/32 GB — it builds and scores for everyone and must
  never be the queue); auditor `D2s_v5`. Roughly $35/day on-demand
  all-in; spot or school-hours deallocation halves it.
- The smoke test's pass bar includes pace: a model must complete one
  full iteration (read state, produce a patch, build) inside one
  board-run window (~25 min) in its actual harness, or it is benched —
  it would spend the tournament reacting to stale verdicts.
- The VM doubles as the team's gate-equivalent platform (x86, pinned
  container native, rmem sysctls).
- The REFEREE runs on its own x86 VM with routes to ALL five boards
  (it scores on each). The AUDITOR runs beside planning or on a small
  VM with read-only credentials. CCP lives on the ARM relay VM.
- The Jetson's only role is network door; its recurring outages
  (F-C2-4/5) are a recorded SPOF — hands-on diagnosis recommended
  before the first overnight epoch.
- Agent CLIs run under a supervisor (tmux/systemd restart); SHIFT.md
  handoffs carry state across restarts per the protocol.

## Fairness and safety rules

1. Identical frozen base, identical scored protocol, identical probe
   distribution, per-board baselines measured by the same referee.
2. No team modifies the referee, auditor, harness, campaign files,
   this annex, or fenced paths — submissions live in the firmware
   detector path per the campaign's diff jail.
3. Teams may PoE-cycle their OWN board within the campaign budget;
   wedge-class regressions (F-C1-5) stop that team's loop pending
   diagnosis and are findings.
4. Baseline runs are sacrosanct: interfering with a referee baseline
   or scoring run on your board (load, network floods, daemon
   squatting) voids the day's scores for that team.
5. Samuel may pause any team at any time. The tournament ends when the
   campaign's win condition or its two-flat-shifts stop rule fires.

## Win

The campaign file's win condition, unchanged: >= 30 fps sustained,
floors held, E8 bounds passing, soak at pace — measured absolutely,
confirmed cross-board, audited, then ratified through planning review.
The leaderboard is bragging rights; the ledger is the record.
