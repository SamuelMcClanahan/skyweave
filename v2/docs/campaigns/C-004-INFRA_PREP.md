# C-004 infrastructure prep: everything before the overnight hillclimb

**Status:** authorized 2026-08-26 by Samuel. One agent shift (or two)
builds ALL of this; the tournament does not start until the dry run at
the end passes. Governing docs: `C-004-ive-throughput.md` (rules,
floors, anti-cheat, Phase 0), `C-004-TOURNAMENT.md` (teams, boards,
scoring ladder, hosting), `../CAMPAIGN_PROTOCOL.md` (all amendments).

**The core loop this infra serves, in one line: iterate on X with a
profiler and a dataset until we hit Y.** X = the detector pipeline,
the profiler = the intra-frame profiler on real boards, the dataset =
generated probe clips with truth, Y = 30 fps with floors held. Every
piece below either speeds the loop or guards the ruler; anything that
does neither is out of scope.

## Build order

### 1. Phase 0 (first, on one board — nothing else is trustworthy
until this lands)

Per the campaign file: determinism fix (memset fg/bg/match planes,
explicit NULL factor), three-run exactness check, re-baseline, AND the
Tier 1 vs Tier 2 agreement validation (short-run bias measured before
any leaderboard exists). STOP semantics per the campaign file if
divergence survives the fix.

### 2. The judge service (referee VM, D8s_v5)

A daemon, not a script. Components:

- **Intake:** authenticated patch submission per team; diff jail
  (firmware detector path only; fenced paths, harness, campaign docs
  rejected mechanically); submission hash recorded.
- **Tier 0:** pinned-container host build + floor scoring against
  truth on fresh probe clips (private seed). Verdict with per-floor
  diagnostics pushed back over CCP in ~3 min.
- **Tier 1:** cross-build, deploy to the SUBMITTING team's board
  (hash-verified push), single deterministic steady-state run (~1,500
  frames, first 300 dropped), fps + floors verdict in ~10 min.
- **Tier 2:** full protocol (3 runs, full length, board-confirmed
  floors). Triggered by: top-3 entry, daily best, ratification path.
- **Top-3 re-verification:** on any top-3 change, Tier 2 the new
  entrant within 30 min on fresh clips; cache by submission hash;
  unchanged top-3 never re-run; out-of-bounds re-verification suspends
  the entry as a finding.
- **Baselines:** frozen Phase-0 binary run on every board daily and
  before scoring sessions; leaderboard ranks speedup vs same-board
  baseline; baseline drift beyond E8 freezes that board's scores.
- **Leaderboard + verdict push:** referee-only posts to CCP; ledger
  rows + raw artifacts to the private evidence store.
- **Claim-inflation tracker:** compares claims-feed posts to official
  scores per team; chronic inflation auto-flagged to the auditor.

### 3. Probe-clip generator

The existing synthetic pipeline wrapped: public generator + declared
parameter ranges published to teams (with sample clips); private
scoring seeds held ONLY on the referee VM (mode 600, never in the
repo, never in CCP). Fresh clips per scoring run; every clip's
manifest+seed retained in the evidence store for replay.

### 4. CCP forum (relay VM)

Build and deploy the CCP server (github.com/Squid-Proxy-Lovers/ccp),
tailnet-only on the always-on relay VM. Identities: t1..t5, referee,
auditor, planning, samuel. Channels: leaderboard (referee-only write),
claims feed (teams write), forum (epoch-gated visibility), findings.
Fallback: append-only posts directory in the evidence store if any
harness cannot speak CCP — do not block the tournament on transport.

### 5. VMs and network

- Provision: 5x `D4s_v5` (teams), 1x `D8s_v5` (referee), 1x `D2s_v5`
  (auditor). Ubuntu 24.04, 128 GB SSD, rmem sysctls, Docker + pinned
  container image, repo checkout at the frozen base revision,
  supervisor (systemd unit or tmux with auto-restart) for the agent
  CLI.
- Tailscale ACLs exactly per the annex: each team VM -> own board (via
  Jetson hop) + CCP + submission endpoint, nothing else; referee -> all
  boards + evidence store; auditor -> read-only everything, write
  nothing but its reports.
- API keys are installed by SAMUEL (hands), one team's key per VM;
  slots and instructions prepared by this shift.

### 6. Team harnesses + smoke tests

T1 Claude Code (Opus 5, Opus 5 subagents), T2 Azure Codex (Sol Ultra,
Sol subagents), T3-T5 Opencode (Ox Alpha via OpenRouter, Qwen3.8 via
Fireworks, Muse Spark via OpenRouter). Each gets the 30-minute
orchestration smoke test: read campaign state, produce a trivial legal
patch, build it, submit it, receive the verdict. PACE BAR: one full
iteration inside one board-run window or the model is benched and
recorded. Seed every team workspace with: the campaign file, the
tournament annex, the seed-ideas list, the SHIFT protocol, and the
profiler usage notes.

### 7. The auditor

Fable 5 High on the auditor VM: read-only credentials to all branches,
referee ledger/artifacts, forum. Standing jobs: daily deep-read of
top-3 patches; epoch-boundary full review; claim-inflation triage;
sample re-derivation of referee scores from raw artifacts. Output: one
audit report per cycle to planning + (open epoch) the forum. No other
write access; no scoring power.

### 8. Overnight-readiness dry run (the gate for kickoff)

All of these pass before epoch 1 opens:

1. A scripted dummy submission from EACH team VM traverses the full
   ladder and returns verdicts (Tier 0 reject case AND Tier 1 pass
   case per team).
2. Baselines measured on all five boards; speedups compute to 1.00
   within E8 on the frozen binary.
3. PoE wedge-recovery exercised once per board (cycle port, reboot,
   identity re-verified).
4. Top-3 re-verification fires correctly on a synthetic leaderboard
   change and correctly skips an unchanged one.
5. Kill switch works: a single command from Samuel (or planning)
   pauses all intake and idles the judge; a second resumes.
6. Jetson watchdog: alert (CCP post + log) if the rig door drops;
   agents' board work degrades to host-only instead of erroring loops.
7. Evidence store receives ledger rows from the dry run; public repo
   shows NOTHING from the runtime (gitignore verified).

### HANDS (Samuel, before or during the shift)

- .236 reflashed onto a genuine card; five boards assigned to teams
  and recorded (MAC -> team).
- API keys onto each team VM; Azure quota check for 7 VMs.
- Jetson hands-on diagnosis (the recurring outage) — strongly
  recommended before the first overnight.
- Flip the kill switch once yourself so you trust it.

## Out of scope for this shift

No climbing, no detector changes beyond Phase 0's sanctioned fix, no
epoch-1 start (Samuel opens it explicitly), no CCP feature work beyond
what the dry run needs. Log edge cases; do not build around them.
