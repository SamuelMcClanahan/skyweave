# Campaign C-001: CCL detector-fail rate at 1152x648

**Status:** Shift 3 stopped on 2026-08-22 UTC after physical attempt 3 exposed
that board uClibc's `%.17g` CCL diagnostic formatting does not always
round-trip the original binary64 centroid used by the protobuf path. An
explicitly authorized Shift 4 preserves all three predecessors and adds a
lossless centroid evidence channel before collecting fresh physical evidence.
Protocol: `../CAMPAIGN_PROTOCOL.md`.
The original campaign cited a D0 "D8.1 C3 opening" / F-C1-6 entry, but the
checked-in `DETECTION_CONTRACTS_D0.md` does not currently contain that entry.
The direct implementation request authorizes this work; it does not silently
repair that governance gap or ratify a result. BUG A/B remain prerequisites,
not campaign knobs.

## Objective

Minimize one scalar:

`detector_fail_rate = ccl_label_failures / ccl_attempts`

Both values are explicit daemon counters. A score may not infer the denominator
from `frames_in`, missing log rows, or empty packets. A run has at least 630
total frames and 600 post-warm-up CCL attempts. Any `ccl_api_failures > 0`
invalidates it.

Every completed CCL attempt with `s8LabelStatus != 0` is classified exactly:

- `threshold_runaway` iff `u8RegionNum == 0`;
- `sub_cap` iff `0 < u8RegionNum < 254`;
- `other` otherwise.

The three counters sum exactly to `ccl_label_failures`. These are retained
historical, field-defined labels rather than proof of the vendor library's
internal cause: `u8RegionNum` is opaque telemetry. `other` lies outside the
predeclared two-bucket model, so a nonzero value requires a contract-change
stop and cannot win.

For a completed call with `s8LabelStatus == 0`, the exhaustive scan of all 254
`astRegion` records with `u32Area > 0` is authoritative for component
extraction. `u8RegionNum` remains verbatim opaque SDK telemetry and is never a
scan bound or asserted cardinality on that success path. Each row carries
`region_count_mismatch == (u8_region_num != nonzero_region_slots)`, and stats
carry the exactly reconciled `ccl_region_count_mismatch_frames`. A mismatch is
diagnostic, not a detector failure. The failure subtypes above continue to use
the reported `u8RegionNum` only when `s8LabelStatus != 0`.

## Subject-to

Every ledger row carries explicit evidence for these constraints:

1. the full suite is green on Linux x86_64/amd64 with both
   `net.core.rmem_max` and `net.core.rmem_default` at least 4,194,304 bytes.
   The retained command uses the evidence producer's own `sys.executable`
   followed by exactly `-m pytest -q`; test selection, ignores, max-fail, expected-failure
   overrides, skips, xfails, xpasses, deselection, warnings, and nonempty
   pytest stderr are refused;
2. fenced paths remain untouched. The retained scoped
   `git status --porcelain --untracked-files=all`
   covers `v1`, `:(glob)**/golden/**`, `v2/docs/DETECTION_CONTRACTS_D0.md`,
   `v2/src/skyweave2/contracts`, `v2/tests/contracts`, `v2/proto`, and
   `v2/tests/edge/fixtures/gate` exactly;
   root `.gitignore` must byte/mode-match HEAD, private/global exclude rules
   are refused/isolated, and physical non-generated members under `v1`, every
   `golden` directory, and the critical v2 contract/proto/gate roots are
   checked so ignored bytes cannot disappear from this claim;
3. only a retained C-001 probe is used. Gate/acceptance roles, path tokens,
   symlinks, `..`, and out-of-directory members are refused;
4. processing is exactly 1152x648; warm-up 30; scene noise 2.0 DN; cap 7;
   persistence 2 frames with a 12.0-processing-pixel gate; seed explicit;
5. board stats, provision argv, measured provision wall time, deployed binary,
   the exact `/oem/usr/lib/librve.so` SHA256 before and after execution,
   remote source SHA256, raw logs, probe manifest, base git revision, and a
   source-tree/build-input SHA256 bind to one identity and run;
6. climbs require all four ordered Phase-1 artifacts and authorization derived
   from the retained evidence, not caller booleans;
7. any surprise requiring a contract change stops for planning review.

Gate and fenced evidence include hash-bound raw stdout, structured PASS
summaries, source revision/tree digest, exact commands, and platform facts.
The full gate and BUG E2/E5 pytest children run only from a fresh temporary
tree materialized from those exact manifest members; the live checkout is
never their working directory or import root, and both staged and original
declared inputs are rehashed after execution. Ignored live `conftest.py`,
sourceless bytecode, caches, and other unmanifested inputs cannot participate.
The excluded C-001 runtime/output directory is created as an empty canonical
mount point inside the stage so production CLI path guards remain testable;
anything written there is still excluded from source inputs and discarded with
the temporary stage.
The gate pins `PYTHONPATH=src:../v1/src`: `src` is the exact staged v2 package,
never an editable install or unbound wheel. The v1 tree is regular-file-only,
byte/mode-identical to HEAD, authenticated inside the source-tree digest, and
embedded in the normalized source bundle; detached support roots cannot supply
Python. A second retained manifest binds exact per-file sizes and SHA256s for
`output/exp001_clips/gate`, `output/exp001_renders/gate`,
`output/exp001_multiblob`, and the ignored
`v2/firmware/rv1106/image` payloads. Those large external members are mapped
one file at a time and rehashed before and after the gate; their manifest
path/hash/tree digest are retained in the gate transcript.
They remain local producer/operator attestations rather than cryptographic
signatures from an independent authority; campaign review must inspect their
producer and raw transcript. The runner does not overclaim that boundary.

The source digest covers the exact tracked plus untracked, non-ignored `v2`
source/build-recipe tree and its exact HEAD-derived v1 support block. Both the
live `v2/docs/campaigns/C-001/` runtime and immutable
`v2/docs/campaigns/C-001-shifts/` history are excluded from discovery,
staging, and source manifests. Neither may contribute evidence bytes to a new
source identity. The
approved ARM SHA256 is an operator-provided
trust anchor: it is co-bound to that tree and reverified after deployment, not
cryptographic proof that the binary was derived from it. Retain the pinned
Docker image digest and build log for review.
Likewise, the board `librve.so` digest is measured provenance, not proof that
the deployed file equals an SDK-container library. Phase 1.1 establishes that
runtime anchor; Phase 1.4 and all later scores must match it and prove the file
did not change during their own run.

### Phase 1 before climbing

Phase 1 uses frozen default knobs, occupies ledger n=1..4 in order, and every
verdict is `measurement`:

1. A `skyweave-c001-bug-verification/2` artifact retains nonempty BUG A board,
   BUG B board, E2, and E5 transcripts, their SHA256s, exact commands, pinned
   toolchain, git SHA, operator build log, exact build command, pinned Docker
   image digest, bound output-binary SHA256, and the exact IVE runtime-library
   SHA256 before and after both board self-tests. Booleans without these
   durable attachments cannot satisfy the step.
   Legacy `/1` artifacts remain replayable only inside immutable predecessor
   archives; they cannot be recorded into the live Shift 4 ledger.
2. The exact loop runs through host `ive_approx`, retaining every accepted
   post-morphology/area-gate, pre-persistence component and count. It is paired
   to every actual board label-failure frame from the same CCL log. A
   connectivity divergence on any individual failure frame takes precedence
   over symmetric evidence from every other frame; a mixture containing a
   clean host failure frame also re-scopes rather than climbs.
3. Board post-morph masks for the first
   `min(10, ccl_label_failures)` failure frames are diffed against identical
   host frame sequences. Fewer than ten failures require every available mask.
   Zero failures require zero masks and the explicit
   `not_applicable_no_board_label_failures` branch; an empty file is never
   accepted for a run with failures. Parsed record count must equal
   `fg_masks_written`, mask-write failures must be zero, and step 2/3/4 must
   share board, seed, manifest, clip, truth, and CCL-log digests.
4. One complete API-clean board score reconciles every CCL row, aggregate
   classifier counter, raw component centroid/bbox, emitted packet, mask
   count, and objective. Packet replay checks the exact RAM-loop envelope and
   every Observation2D centroid, covariance, bbox, area, persistence count,
   confidence, observation order/id, local blob id, and absent evidence ref
   against the frozen nearest-pair persistence plus cap pipeline. Successful
   rows reconstruct authoritative centroids from retained IEEE-754 binary64 bit
   strings, recompute their mismatch flag, retain every accepted component,
   require `accepted_components <= nonzero_region_slots`, and reconcile the
   aggregate mismatch counter; step 4 refuses `other` failures.

The board CCL is 8-connected while the authoritative host oracle remains
4-connected. Phase 1 therefore also computes a diagnostic 8-connected view of
the same host mask. A host frame is clean only when both views recall every
visible truth mover with zero unexplained components. Four-connected extras
that disappear in the 8-connected view are connectivity divergence and force
re-scope. Missing truth in the 8-connected view is also a re-scope, never
symmetry.

The predeclared mask-parity tolerance is conservative: **zero differing binary
occupancy pixels** after normalizing all nonzero SWFM bytes to foreground.
Only unexplained 8-connected host components plus the required, exactly equal
paired masks authorize symmetric-host evidence. Mere mask presence never
authorizes a climb. This exact choice may stop on a legitimate host/board
approximation difference; changing it after seeing data requires planning
review.

If the complete default run has zero label failures, no climb is necessary or
allowed. After all four measurements, that score may enter only the default-
knob candidate/confirmation path.

## Knob whitelist

Valid only after Phase 1 authorizes a climb:

| Campaign knob | Actual field | Range |
| --- | --- | --- |
| `min_area_px` | `min_area_px` | integer 2..64 |
| `morph_open` | alias of `open_radius_px` | exactly 0 or 1 |
| `gmm2.match_sigmas` | alias of `ive_approx.match_sigmas` | 2.5..4.0 |
| `gmm2.var_min` | alias of `ive_approx.var_min` | 25.0..100.0 |

Native `open_radius_px` and `ive_approx.*` spellings are accepted, but two
aliases for one knob are refused. Everything else is frozen.

## Probe inputs

The standard probe has six movers. The one sparse variant has exactly three.
Both use `skyweave2.edge.benchmark` with a fresh explicit seed. The retained
input is the exact short RAM clip derived by existing budget arithmetic
(currently 36 slots), not a 630-frame file. Truth is stored per clip slot and
the run uses `clip_slot = run_frame_seq % ram_clip_frames`; movers and noise
repeat exactly at wraps.

Loading a manifest re-runs the named benchmark generator and compares session
metadata plus every stored luma byte. Re-hashing a hand-authored or modified
SWIJ clip and relabeling it with generator-derived truth is refused.

Manifest, clip, truth, provision proof, external MAC/image/remote-clip binding,
stats, CCL JSONL, packet log, optional SWFM masks, host artifact, score, and
ledger retain SHA256s. Host and score artifacts snapshot raw inputs beneath
their artifact directories; ledger evidence cannot depend on an absolute,
symlinked, mutable source.

Per-attempt CCL JSONL always retains `frame_seq`, `api_failure`, nullable
`s8_label_status`, `u8_region_num`, `u32_cur_area_thr`,
`nonzero_region_slots`, `region_count_mismatch`, `accepted_components`, and
`overlap_pairs`; successful rows also retain every accepted centroid and bbox.
Each current successful component carries `centroid_u_bits` and
`centroid_v_bits` as exactly 16 lowercase hexadecimal digits. Those lossless
IEEE-754 binary64 fields are authoritative for persistence and packet replay;
the decimal centroids remain readable diagnostics and must be identical or an
immediate one-ULP neighbor. This narrow check accommodates the measured uClibc
formatter defect without relaxing exact wire lineage. The mismatch bit is
false on API/label failures and otherwise must equal the comparison of the two
raw fields. Logs lacking the mismatch field remain invalid CCL artifacts.
Otherwise-valid logs lacking only the exact centroid bits remain replayable
only inside immutable predecessor archives and cannot enter Shift 4.

### Runner and ledger

`campaign_c001 prepare` creates an immutable probe. `host` runs exact-loop host
analysis with optional paired CCL/SWFM evidence. `score` requires stats, CCL,
packets, probe, identity/source binding, and `provision.json`, then snapshots
them. `record` derives result, wall time, recovery use, authorization, and
verdict checks from hashed artifacts. `status` verifies artifacts before
reporting confirmation; `--skip-artifact-check` explicitly omits confirmation.
`stop` writes one immutable typed `STOP.json` for an incident that cannot be a
valid score. `successor` requires the expected STOP SHA256 and an authorization
note, then atomically archives the complete stopped root and opens its
successor. Production CLI commands accept only the canonical
`v2/docs/campaigns/C-001` campaign root; library entry points remain
temporary-directory friendly for isolated tests.

The strict validators define the retained Subject-to and BUG-verification
schemas, but this module does not manufacture those attestations. Before real
Phase 1, a reviewed producer must freeze the source-tree/build-input manifest,
run the exact full-suite and fenced-status commands, execute the identity-bound
hashed daemon self-test on board, run E2/E5, and package their raw stdout/stderr
attachments. Hand-authored true booleans cannot replace that evidence.

The supported board path is `skyweave2.edge.campaign_c001_run`: it reserves a
durable physical attempt before transport, performs identity-bound SSH through
an explicit ProxyJump, uses a unique run-id remote directory, verifies the
deployed binary and RAM source, requires natural exit zero before the deadline,
and collects fresh raw outputs. Its attempt and recovery ledgers are locked,
append-only hash chains under the canonical campaign directory; every SCORE
retains their current snapshots, including an empty/zero-cycle recovery chain.
Later scores must strictly extend the attempt chain and prefix-extend the
recovery chain, so switching ledger roots cannot reset a budget.

PoE remains an injected adapter: no switch credentials, `poe.cgi` endpoint, or
network secret is stored here. Identity is re-read after recovery. The command
is executable over the supported SSH transport, but access credentials and a
real switch adapter remain operator-supplied.

Each shift starts without `C-001/ledger.jsonl`; the file appears only when real
evidence is recorded. Rows are sequential, hash-chained, and installed with
one locked `O_APPEND` write only after artifact/input digests pass. Preparation,
tests, rollover, and inherited attempt/recovery history do not create a score
row.

### Successor shifts

A terminal `STOP.json` never gets removed or bypassed. With explicit operator
authorization, run:

```text
python -m skyweave2.edge.campaign_c001 successor \
  --campaign-dir v2/docs/campaigns/C-001 \
  --expected-stop-sha256 <sha256> \
  --note <authorization record>
```

The command takes an exclusive lock shared with evidence and board runners,
renames the entire stopped root to the deterministic sibling
`C-001-shifts/shift-NNNN-<stop-prefix>`, and appends one hash-chained
`C-001-shifts/lineage.jsonl` row. The fresh canonical root initially contains
only `SUCCESSOR.json` plus independent, byte-identical copies of the cumulative
attempt and recovery ledgers. It does not inherit the old score ledger, STOP,
source snapshot, gate proof, BUG proof, or run artifacts.

The lineage append is the commit point. Failures before it restore the stopped
root to the canonical path; failures after it can only roll forward from the
durable journal. Repeating the identical command is idempotent. Every current
shift validation replays all archived tree hashes, ledgers, STOPs, successor
pointers, and cumulative budget prefixes. A successor to
`contract_change_required` must bind a different source-tree SHA256 in its
first Phase 1.1 row. Its experiment ledger restarts at n=1, while the next
physical reservation continues the inherited attempt number.

## Budget

- maximum 40 experiments per shift. Ordered non-hardware Phase-1 measurements
  plus every reserved physical attempt count; timeout, validation failure,
  interrupt, and abandoned reservations are not free;
- maximum 20 measured wall-minutes per experiment, derived from
  `provision.json` for scored runs;
- maximum 6 PoE cycles per shift, counted from the latest durable recovery
  chain even when failed attempts never produce SCORE artifacts;
- no more than two recovery cycles for one unreachable board.

## Stop rules

- stop after 8 objectively derived consecutive regressions against the best
  retained scalar;
- stop when one experiment wedges a board twice;
- stop when a board remains unreachable after two recovery cycles;
- stop on clean host, Subject-to violation, an `other` failure, or any required
  contract change;
- stop after fresh-seed and distinct-MAC confirmation, pending planning-session
  ratification.

Detected knob/Subject-to, wedge, unreachable-board, and contract incidents use
an append-only typed `STOP.json`; terminal identity and two-cycle recovery
stops also bind immutable source evidence plus the failed physical-attempt
hash chain (and the recovery chain where applicable). Later run/record attempts
refuse the marker. The external PoE state machine requires MAC plus
image-marker revalidation and excludes a mismatch instead of silently
substituting another board.

## Win condition

`detector_fail_rate <= 0.02` (Provisional; the 288x162 baseline measured
0.004), subject to two exact recall checks:

1. **Raw detector recall:** every visible post-warm-up truth mover is matched
   one-to-one to a distinct accepted CCL component bbox.
2. **Eligible emitted recall:** every *persistence-eligible* truth mover is
   matched one-to-one to a distinct bbox decoded from actual `packets.hex`.

Eligibility is derived before the run: the same mover must be visible for the
frozen two-frame chain, every consecutive truth displacement must be <=12.0
processing pixels, and all chain frames must be post-warm-up because warm-up
does not seed persistence. This explicitly excludes frame 30, a reappearance
after clipping, and a RAM-wrap jump over the gate. The score also publishes
unfiltered emitted recall and every structural exclusion. Missing only an
ineligible frame may pass; missing one eligible observation fails.

Matching is maximum one-to-one per frame. A bbox and truth point are each used
once. The only predicate is half-open bbox containment in the named coordinate
space; no centroid radius exists. Persistence/cap suppression therefore cannot
be hidden by raw components.

Both current benchmark and sparse probes contain at least one visible truth
mover on every post-warm-up frame. A CCL label failure returns no accepted
component on that frame, so exact raw detector recall makes any such failure
non-winning. The nominal scalar threshold remains `<= 0.02`, but the effective
candidate threshold for these predeclared probes is therefore **zero label
failures**. This is intentional strictness, not a post-hoc threshold change.

A passing run is Provisional. The same knobs must pass a fresh seed and a
second identity-verified physical MAC; relabeling one MAC cannot count. Only
confirmation-phase rows count. Even then status is
`confirmed_pending_planning_session_ratification`; the runner never edits or
ratifies D0.

Deliverables are the ledger plus `C-001/SHIFT.md`, a Provisional winner with
objective and raw/eligible recall, fresh-seed and distinct-MAC evidence, and
BUG A/B hardware proof. Findings should be retained in a tracked campaign
findings file and, where the ignored legacy path is still required, appended to
`D8_1_C1_FINDINGS.md`. Only planning may close the D0 governance gap and open
C-002.
