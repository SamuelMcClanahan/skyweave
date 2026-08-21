# Campaign C-001: CCL detector-fail rate at 1152x648

**Status:** open 2026-08-20. Protocol: `../CAMPAIGN_PROTOCOL.md`.
Authority for the underlying finding: D0 log "D8.1 C3 opening"
(F-C1-6 row: remedy deliberately open; warmup=30 mandatory;
anti-tuning unchanged). BUG A and BUG B fixes are ALREADY sanctioned
there and are prerequisite work, not campaign knobs.

## Objective

Minimize `detector_fail_rate` = fraction of post-warmup frames with
`s8LabelStatus != 0`, at 1152x648, warmup=30, on the probe clips
below, measured over >= 600 frames per experiment on a rig board.

## Win condition

fail_rate <= 0.02 (Provisional; the 288x162 baseline measured 0.004),
confirmed with a fresh seed AND reproduced on a second board, with
observations-per-frame on movers not degraded below the probe clip's
truth count (a "win" that goes blind is a loss; blindness is checked
by comparing detected-mover recall on the probe truth, which probe
clips carry precisely so that recall is checkable without gate scenes).

## Phase 1 before any climbing (the discriminator, one shift)

1. Implement BUG A + BUG B per the D0 entry (full-array region
   iteration; mask-moment-in-bbox centroid on BOTH board and host
   oracle; overlapping-bbox counter). Regenerate fixtures if bytes
   move (D0 entry is the recorded reason); E2/E5 re-applied.
2. Host discriminator: same benchmark clip through `ive_approx` at
   1152x648, warmup 30; count components per frame. Symmetric speckle
   justifies parity remedies; a clean host reframes the campaign as a
   board-GMM2 divergence hunt and the knob list below is VOID until
   the planning session re-scopes.
3. Mask diff: ~10 board fg masks on failing frames vs host masks,
   identical frames.
4. Classify all failures threshold-runaway vs sub-cap over one full
   run (instrumentation already logs u8RegionNum + u32CurAreaThr).
Ledger these as experiments n=1..4. They are measurements, not climbs.

## Knob whitelist (valid only if the discriminator shows symmetry)

| Knob | Range | Note |
| --- | --- | --- |
| min_area_px | 2..64, or declared formula scaled by (proc_px / 288x162_px) | applied identically host + board |
| morph_open | off / 1 erode+dilate pass (IVE Erode/Dilate; host mirror) | identical structuring element both sides, cross-checked against the E5 kernel finding |
| gmm2 match_sigmas | 2.5..4.0 | shared config |
| gmm2 var_min | current..4x | shared config |

Frozen explicitly: clip noise (SCENE_NOISE_DN stays the declared
sensor model), region cap (hardware), warmup (30, mandatory),
everything else in DetectorConfig, all contracts, all fenced paths.

## Probe inputs

The existing benchmark clip family regenerated at 1152x648 with fresh
seeds per experiment batch, plus (allowed) one sparser probe variant
whose manifest declares its reduced mover count. All probe manifests
and seeds ledgered. Gate and acceptance scenes are forbidden inputs;
the runner must refuse them.

## Budget and stop rules

Protocol defaults (40 experiments/shift, 20 min/experiment, 6 PoE
cycles). Additional stop rules: any experiment that wedges a board
twice stops the shift (F-C1-5 class regression, report immediately);
any knob outside the whitelist is a subject-to violation; if the
discriminator (phase 1.2) shows a clean host, STOP after phase 1 and
hand the ledger to the planning session for re-scoping.

## Deliverables at campaign end

Ledger + SHIFT.md chain; the winning config as a Provisional entry
awaiting ratification; a clean fail_rate number with confirmation
evidence; the BUG A/B rework verified on hardware; findings appended
to `D8_1_C1_FINDINGS.md`. The planning session then ratifies into the
D0 log and C-002 (clean sweep + soak) opens.
