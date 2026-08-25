# Campaign C-002: clean sweep + soak at 1152x648

**Status:** opened 2026-08-23. Authorized by Samuel in the direct
implementation request, per the protocol campaign map row "C-002".
Protocol: `CAMPAIGN_PROTOCOL.md`, including the 2026-08-23 amendments.
C-001 ended the same day in a terminal `contract_change_required` re-scope
STOP (`f7abb8d4cce1435e...`): the objective scalar measured 0.0 with frozen
default knobs and zero CCL label failures, but the recall gates failed on
the wrap-repeating probe. C-002 does not depend on that re-scope decision:
it freezes the same default configuration — the C-001-final config by
measurement — and measures throughput and stability, not detection quality.
C-001 ratification remains pending with planning; this file does not claim
it.

This is a MEASUREMENT campaign. The knob whitelist is EMPTY. Nothing here
tunes the detector; the "climb" named in the campaign map is fixing
whatever breaks the soak, and any such fix is BUG-class, ledgered, and
never a quality knob.

## Objective

Maximize one scalar, computable by the harness from the retained soak-pair
comparison artifact:

`soak_e8_pass ∈ {0, 1}`

`soak_e8_pass = 1` iff the two paced soak runs compare with E8 verdict
`pass` under the pre-declared section 8.1 bounds AND both soak runs are
individually clean under the health criteria below. Anything else,
including E8 `incomplete`, is 0.

The campaign also delivers one mandated Measured number that is not the
objective: **sustained fps at 1152x648**, the unpaced throughput ceiling,
from a two-run E8-passed sweep pair. It feeds the C4 resolution row of
`D8_EDGE_REPORT.md` section 8.

## Frozen configuration (C-001-final, not knobs)

Every field below is frozen for every run. There is no whitelist; a run
with any deviation is invalid and cannot be recorded.

- processing 1152x648, detector `ive-gmm2`, warm-up 30 frames;
- `min_area_px` 2, `max_area_px` 10000, `morph_open` 1 (open radius 1);
- `gmm2_match_sigmas` 2.5, `gmm2_var_min` 25.0;
- cap 7, persistence 2 frames with the 12.0-processing-pixel gate;
- scene noise 2.0 DN, 6 movers, benchmark scene generator;
- board daemon: the frozen Shift 4 ARM binary, SHA256
  `eaa9d178d1c0e5d408ff7260e1c34ae6917c3a8f5aa09c5720de718d9169ba94`;
- board runtime `/oem/usr/lib/librve.so` must hash
  `2446c5b1720c083b89338c33cdf3f289c8fc94b29386a0404481881a06cc3455`
  before and after every score-bearing run;
- RAM-loop source (`--inject-ram`), budget 160 MB decimal, 36-frame clip
  at 1152x648 (derived arithmetic, heap-bound), PTS stride
  1,200,000,000 ns per pass.

## Run matrix (declared before any run)

| # | Kind | Pacing | Total frames | Declared duration | Purpose |
| --- | --- | --- | --- | --- | --- |
| S1 | sweep | unpaced (`period_ns=0`) | 6,300 | by throughput (~4-5 min) | throughput ceiling, run 1 |
| S2 | sweep | unpaced | 6,300 | same config, second run | E8 pair for S1 |
| K1 | soak | paced 30.0 fps (`period_ns=33,333,333`) | 108,000 | 1 h declared at 30 fps | stability, run 1 |
| K2 | soak | paced 30.0 fps | 108,000 | 1 h declared | E8 pair for K1 |

Rules:

- Both runs of a pair use byte-identical clip, declaration, and command;
  the E8 exact counters demand it.
- A run ends by serving its declared frame budget and exiting naturally
  with status zero. SIGTERM or any other early termination invalidates
  the run (E8 would be `incomplete` by construction) and is a finding.
- If the S-pair ceiling measures below 30 fps, K1/K2 still run at the
  declared 30.0 fps pace; the daemon runs late and `pace_late_frames`
  records it. A duty cycle below 30 fps is a finding about the node for
  the C4 decision, never a licence to relax anything, and does not by
  itself fail the soak.
- The soak scene LOOPS (3,000 passes of 36 frames). Both declared RAM-loop
  systematics (DDR profile, wrap-repeating scene) travel with every
  artifact and are folded into no bound. C-001's terminal finding — GMM2
  absorbs wrap-repeating movers — is expected to depress component counts
  here too; C-002 measures throughput and stability, for which the loaded
  detector path still executes every frame, so this is recorded as a
  labeled systematic on any component-count observation, and no
  detection-quality claim is made from these runs.

## E8 comparison (pre-declared, section 8.1)

Bounds are `tolerance.BENCHMARK_RUN_TO_RUN`, exactly as declared 2026-08-10:
sustained fps relative 0.10; peak RSS relative 0.05; A7 utilisation
relative 0.25. The ten `benchmark.EXACT_COUNTERS` must match exactly.
Verdict is three-valued; `incomplete` is not a pass; `fail` outranks
`incomplete`.

Board measurement sources (identical mechanism and `source` string on both
runs of a pair, else the comparison is a config mismatch):

- sustained fps: the daemon's own fps from its LAST health packet
  (`sustained_fps_daemon`) — the E8-bounded fps axis. The capture must
  reconcile with the daemon's `health_sent` counter (at most 2 packets of
  UDP loss tolerated) or the axis is NOT-MEASURED: with packets missing,
  the last captured packet is not the daemon's last;
- peak RSS: board `/proc/<pid>/status` `VmHWM`, KiB read, reported
  decimal MB, sampled by a board-side sampler for the daemon's own pid.
  Valid only when the sampler outlives the daemon (its `end` marker is
  present); a sampler prefix is NOT-MEASURED, never a partial peak;
- A7 utilisation: board `/proc/<pid>/stat` `utime+stime` tick deltas over
  `CLK_TCK`, divided by the board's own `/proc/uptime` span between the
  sampler's first and last readings — both quantities on the board's
  clock, so the harness's clock never enters the ratio. The sampler
  starts just after spawn, so the daemon's first seconds sit outside the
  span; the source string records this. Same `end`-marker requirement;
- DDR bandwidth: NOT-MEASURED (stub), exactly as on the host;
- thermals: board `/sys/class/thermal/thermal_zone*/temp`, max across
  zones, sampled every 5 s for the run's whole life; first, last, drift,
  and the full curve are retained (the curve is retained because a
  runaway verdict needs it; the host harness keeps only endpoints).

`sustained_fps_wall` (harness wall clock over `frames_in`) is retained
beside it, unbounded, as on the host.

## Health criteria (a soak run is clean iff all hold)

Health rides the measurement socket at 1 Hz to the Jetson
(the Jetson's rig-LAN address and the declared measurement port (both operator-supplied at invocation; recorded in the private rig log)); node-to-home-LAN UDP is not routed, so the
capture listener runs ON the Jetson and every datagram is retained with
its arrival timestamp for local replay through the real
`skyweave2.edge.health` stack.

1. **No restart:** exactly one `session_uuid` across the run's health
   packets from this board, and zero `drop_counter_regressions`. The
   capture records every datagram's SENDER; packets from any other sender
   are counted foreign and never scored, because 5601 is the rig-wide
   measurement port and a stale daemon's stream must neither fill this
   run's cadence gaps nor fail this run with a foreign session.
2. **No health gap:** the filtered capture reconciles with the daemon's
   `health_sent` counter (completeness first — a listener that died
   mid-run leaves a clean-looking prefix and only the reconciliation
   exposes it), AND cadence `max_period_s < 2.0 x` the declared 1 s
   health period (the E7 missed-send criterion). A merely late packet
   under that line is recorded, not failed.
3. **No thermal runaway**, declared now, before any board thermal number
   for this campaign exists: (a) no zone reading ever reaches 100.0 C,
   and (b) the max-zone temperature plateaus — over the final 15 minutes
   of the soak, no 5-minute window rises more than 0.5 C. Windows SLIDE
   at the sampler's own 5 s cadence (a ramp straddling a fixed boundary
   is still one window's rise); each window needs at least half its
   expected samples to be evaluable; and the tail is anchored at the
   sampler's `end` marker, so a sampler that died mid-run fails the
   criterion outright — the unobserved interval is exactly where a
   runaway would be. A soak failing (a) or (b), or whose curve is not
   evaluable, fails the objective and is a finding.
4. **Natural completion:** `source_frames_served == source_frames_planned
   == 108000`, exit status zero, `stats.json` written by the daemon's own
   exit path.

Any restart, gap, runaway, or non-natural end fails that soak run, sets
the objective to 0 for the pair, and is written up as a finding. Fixing
whatever broke it is BUG-class work: ledgered, re-run from scratch, never
a knob.

## Subject-to

Per the attestation-proportionality amendment: FULL binding for
score-bearing runs (S1/S2/K1/K2 and their comparison artifacts — anything
a ledger verdict rests on); lightweight treatment (recorded, hashed once)
for diagnostics such as preflight probes, capture-staging checks, and
board sampler smoke tests. No staged-tree replay fortress is rebuilt for
C-002; that machinery was proportionate to C-001's adversarial replay
problem, not to a measurement campaign, and this trade is declared here
per the amendment.

Every score-bearing ledger row binds:

1. full suite green on the Linux x86_64 gate platform for the exact source
   tree that produced the run artifacts: transcript retained, zero skips,
   zero selection/ignore/maxfail overrides, nonempty stderr refused. The
   C-002 driver is new phase code with its own tests, so this gate runs
   AFTER the driver lands and BEFORE the first score-bearing board run;
2. fenced paths untouched BY CAMPAIGN WORK, proven by the same scoped
   `git status --porcelain --untracked-files=all` list C-001 used (`v1`,
   `:(glob)**/golden/**`, `v2/docs/DETECTION_CONTRACTS_D0.md`,
   `v2/src/skyweave2/contracts`, `v2/tests/contracts`, `v2/proto`,
   `v2/tests/edge/fixtures/gate`), transcript hashed and retained. One
   expected line is declared: ` M v2/docs/DETECTION_CONTRACTS_D0.md` —
   the planning session's 2026-08-23 verbatim restoration of the lost
   "D8.1 C3 opening" entry (17 appended lines; the entry itself says so),
   which predates every C-002 run. The fence evidence binds that file's
   exact SHA256 at freeze time; any OTHER line in the scoped status, or
   any further change to the D0 file, fails the fence;
3. no gate or acceptance scene as input anywhere: the only probe is the
   benchmark-generated clip declared below;
4. board identity: MAC plus image marker verified over SSH before each
   run; NO stale `skyweave-edge` process may already be running (a leaked
   daemon shares the measurement port; the driver refuses rather than
   killing it silently, because the leak is itself a finding); the
   deployed binary and the remote clip hash-verified after push;
   `librve.so` hashed before and after; a mismatch excludes the board and
   stops, never substitutes silently;
5. per-run isolation: every run gets a fresh run id, a fresh remote
   directory on the board, and a fresh capture path on the Jetson whose
   listener must prove itself alive (live pid plus an on-disk sentinel)
   before the daemon starts — stale evidence from an earlier run can
   never be collected as this run's;
6. artifacts: stats.json, run.log, exit.status, the Jetson health capture,
   the board sampler log, provision record, clip manifest, and every
   comparison artifact retained under `C-002/artifacts/` with SHA256s in
   the ledger row. An E8 pair must be two DISTINCT run ids, indexes
   {1, 2}, one board, one MAC, one probe hash; an invalidated run (wall
   cap, non-zero exit, short frame budget, or multiple sessions from this
   board) is refused by the comparison and by the ledger.

## Probe inputs

One immutable probe, generated before S1 and reused byte-identically by
all four runs:

- `skyweave2.edge.benchmark` scene, plan seed **20260824**, 6 movers,
  warm-up 30, noise 2.0 DN, fps 30.0, `source_mode=inject-ram`;
- clip: exactly 36 frames at 1152x648, written with `frame_count=36` (the
  shadow-plan rule keeps movers present from frame 0), 26,873,856 bytes;
- manifest with clip SHA256, plan echo, and generator versions retained
  under `C-002/probes/`; gate/acceptance manifests are refused by
  construction (the generator is the only source).

Seed note, declared before any run: the protocol's fresh-seed confirmation
is deliberately replaced here by the E8 same-config two-run comparison —
E8's exact counters REQUIRE identical clip bytes, and reproducibility of a
measurement is exactly what E8 was declared to test. A fresh-seed clip
would change the deterministic counters and make the comparison
impossible. Board-dependence is handled by declaring the result
board-specific: every number this campaign produces is Measured **for
board-104** (MAC `recorded in the private rig log`) and labeled so; no fleet claim is
made. Board `.102` (`recorded in the private rig log`) is the declared fallback if
board-104 fails identity or dies; a fallback switch restarts the affected
pair from S1/K1 of that pair and is a finding.

## Driver (declared deviation from the host harness)

`benchmark.run_sweep`/`run_soak` are host-local by design (local Popen,
loopback health listener, local /proc). C-002 adds
`v2/src/skyweave2/edge/campaign_c002.py`: a board driver in the
`campaign_c001_run` mould — `provision.SshTransport` with an explicit
jump host, hash-verified push, natural-exit collection — plus the board
sampler, the Jetson-side health capture, and record construction that
feeds `benchmark.compare_runs` unchanged. E8 bounds and exact counters are
read from `tolerance.py`/`benchmark.py`, never retyped. New phase code
adds host-exercised tests; no existing test is deleted or weakened.

## Budget

- maximum 12 experiments this shift (each board run, each abandoned or
  invalid attempt, and each comparison recorded as a row counts);
- per-run wall caps, measured from provision start to collection:
  sweep 20 minutes, soak 150 minutes. A run over its cap is abandoned,
  counted, and is a finding; two overruns of the same kind stop the shift.
  *Amendment, 2026-08-23, before any soak ran:* the soak cap was first
  declared 120 minutes against the stale C3 throughput numbers (~21-26
  fps, which counted CCL label-failure frames that skip detector work).
  S1 measured the clean ceiling at 14.47 fps, making the declared
  108,000-frame paced soak ~124 minutes by arithmetic — this file's own
  "K1/K2 still run at a sub-30 ceiling" clause and the 120-minute cap
  contradicted each other. Re-derived: `108000 / measured S-pair ceiling
  x 1.15 + provisioning margin`, rounded to 150 minutes (covers any duty
  cycle down to ~12.6 fps). A wall cap bounds cost, not truth: no E8
  bound, health criterion, or verdict moves with it. Flagged for the
  boundary review;
- maximum 6 PoE cycles per shift, at most 2 for one unreachable board.

## Stop rules

- board unreachable after 2 recovery cycles;
- identity, binary, or `librve.so` hash mismatch (terminal for that board);
- one experiment wedges a board twice;
- two wall-cap overruns of the same run kind;
- any subject-to violation;
- any surprise requiring a contract change: stop and report, never
  improvise. A daemon crash, restart, or thermal runaway inside a soak is
  a finding and fails the objective but is NOT automatically a stop; the
  shift may attempt the BUG-class fix inside budget and re-run the pair.

## Win condition

`soak_e8_pass = 1`, with:

- the S-pair E8 verdict `pass` and its sustained-fps ceiling published as
  Measured (board-104) beside the C4 resolution row;
- the K-pair E8 verdict `pass`, both runs individually clean under all
  four health criteria;
- all four runs naturally complete with full frame budgets.

Reproduction is the E8 pair itself, as declared above. The result stays
Provisional until the planning session verifies the ledger and appends the
C4/C5 outcome to the decisions log; the runner never edits D0. Deliverables:
`C-002/ledger.jsonl`, `C-002/SHIFT.md`, the four run artifacts, both
comparison artifacts, the health captures, thermal curves, and the updated
sweep/soak tables in `D8_EDGE_REPORT.md` section 8 (regenerated in the
D8.1 close, not hand-edited).

## Ledger

`v2/docs/campaigns/C-002/ledger.jsonl`, protocol format, append-only, one
row per experiment with hypothesis, seed, board, artifact SHA256s, verdict
(`measurement` for every valid run and comparison), and note. Campaign
memory (ledger, SHIFT.md, findings, artifacts) lives in the private store
`~/skyweave-evidence`; a shift may not end without committing it there. It
is never committed to the public repo.

## Public-copy sanitization note (2026-08-25)

This tracked copy replaces board MAC and rig-LAN address literals with
references: the repo's standing policy keeps rig facts out of the public
tree (the .gitignore's own words), and every identity this campaign
bound is operator-supplied at invocation and preserved VERBATIM, hash-
bound, in the private evidence store's copy of this file and in every
retained run artifact. No declared criterion, bound, or procedure
differs between the two copies.
