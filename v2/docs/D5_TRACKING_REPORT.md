# D5 tracking report

**Labels.** Gate-clip lines are Measured on the L4 golden clips (the
noise inside them remains Modeled mid-bracket until the conversion-gain
bench lands). Multi-blob lines are Modeled (seeded sensor-model
fixture). Runtimes are zeroed in this deterministic body and written
live to the evidence JSON (D4 precedent).

Seed 7; detector d5-mog2-1536 (mog2 @1536x864); fusion config frozen
defaults; voxel default evaluated below. Anti-tuning: thresholds were
tuned on seed-variant fixtures only; this is the gate clip's single
scored run for this frozen config.

## Gate-clip acceptance table (manifest `exp001_scene.yaml`)

| Line | Gate | Value | Verdict |
| --- | --- | --- | --- |
| Detection recall (carried from D4, mog2 @1536x864) | >= 0.95 | 0.996 (Measured, D4 report) | PASS |
| Range p95 (m) | <= 5.0 | 0.396 | PASS |
| Cross-range p95, random channel (m) | <= 0.5 | 0.127 (total 0.156) | PASS |
| Target-reference bias, estimated (m) | <= 0.5 | 0.092 (max 0.278) | PASS |
| Velocity RMSE, random channel, settled (after 15 batches) (m/s) | <= 2.0 | 0.593 (transient 8.574, whole-run 2.063, total 2.193) | PASS |
| Velocity settling time from confirmation (s) | <= 1.0 | 0.200 | PASS |
| Acquisition (s from shared-FOV entry) | <= 1.5 | 0.033 | PASS |
| Duplicate confirmed tracks | <= 1 | 0 | PASS |

- Published samples: 281 track states near truth
  over 283 batches.

### Two-channel scoring (D5 amendment, per D0's two-error-channel rule)

- Estimator: the SYSTEMATIC channel is a per-axis centered windowed
  mean of the published-track position error over ±15
  event-time batch indices (~1 s at 30 fps) — slow enough to track the
  shading-driven target-reference drift across the crossing, wide
  enough to average the frame-to-frame noise. The RANDOM channel is
  the residual about that estimate; the same split applies to velocity.
- Estimated bias: mean magnitude 0.092 m, max 0.278 m; run-mean per axis (x, y, z): (-0.018, 0.023, -0.007) m.
- Nothing is hidden: total-error values sit beside every random-channel
  value in the table above.

### Velocity settled-window scoring (D5.2 amendment)

- Velocity is physically unobservable at single-batch birth (route A
  confirms on one 3-camera batch), so the manifest declares a
  15-batch convergence window
  (~0.50 s) excluded from the
  gated RMSE; a separate gate bounds how long settling may take.
- Transient RMSE (first 15 batches): 8.574 m/s — reported, never hidden.
- Settled RMSE over 266 samples: 0.593 m/s.
- Settling time = batches from confirmation until the velocity error
  stays under the gate for the REST of the run (a momentary dip does
  not count): 0.200 s.

## Track-filter covariance coverage

- Gate clip (voxel off): 0.70 / 0.90 / 0.98 at 1/2/3 sigma.
- Caveat (D1 finding): with 3 cameras the measurement covariance scale
  is estimated from 3 degrees of freedom, so the statistic is
  F-distributed, not chi-square — nominal masses are ~0.55/0.78/0.89,
  and filter smoothing shifts them further. Report, don't re-tune.

## Voxel on/off comparison (F10)

| Scene | Voxel | Candidate recall | False cand./batch | Voxel groups | Runtime (s) |
| --- | --- | --- | --- | --- | --- |
| gate | off | 0.989 | 0.000 | 0 | 0.000 |
| gate | on | 0.989 | 0.000 | 0 | 0.000 |
| multiblob | off | 1.000 | 0.034 | 0 | 0.000 |
| multiblob | on | 1.000 | 0.034 | 3 | 0.000 |

### Recommendation (rule: enable only if multi-blob candidate recall
gains > 0.02 without > 0.5 extra false candidates/batch)

- Multi-blob recall off/on: 1.000 / 1.000; false cand./batch off/on: 0.034 / 0.034.
- **Recommended default: voxel OFF.** To be
  recorded in D0 §10 with these numbers.

## Lifecycle and rejection statistics

### gate (voxel off)

- Final track statuses: {"coast": 1}
- Engine rejections: {}; NIS-gated updates: 17; aligner exclusions: {}
- Audit samples (obs_id traceability):
  - `track 0 accepted=True nis=None obs=['0:6a803032-afc5-572e-98e0-fa178c8a67a1:91:0', '1:a9a02030-66b6-5458-ba87-fae68a231ee8:91:0', '2:9ce00a50-b119-5e34-bb5a-dc4879e1aa5b:91:0']`
  - `track 0 accepted=True nis=1.84 obs=['0:6a803032-afc5-572e-98e0-fa178c8a67a1:92:0', '1:a9a02030-66b6-5458-ba87-fae68a231ee8:92:0', '2:9ce00a50-b119-5e34-bb5a-dc4879e1aa5b:92:0']`
  - `track 0 accepted=False nis=16.72 obs=['0:6a803032-afc5-572e-98e0-fa178c8a67a1:100:0', '1:a9a02030-66b6-5458-ba87-fae68a231ee8:100:0', '2:9ce00a50-b119-5e34-bb5a-dc4879e1aa5b:100:0']`

### multiblob (voxel off)

- Final track statuses: {"confirmed": 1, "deleted": 1}
- Engine rejections: {"residual": 7}; NIS-gated updates: 4; aligner exclusions: {}
- Audit samples (obs_id traceability):
  - `track 0 accepted=True nis=None obs=['0:72fdb8fd-5db1-5291-be88-c4e2a51671e7:91:1', '1:7e14d7ce-682b-54c0-80b1-65f3bc25f9f9:91:2', '2:c0587a1b-cbd0-5dfb-90ca-041acd5bcec8:91:1']`
  - `track 0 accepted=True nis=2.56 obs=['0:72fdb8fd-5db1-5291-be88-c4e2a51671e7:92:1', '1:7e14d7ce-682b-54c0-80b1-65f3bc25f9f9:92:2', '2:c0587a1b-cbd0-5dfb-90ca-041acd5bcec8:92:1']`
  - `track 0 accepted=False nis=15.25 obs=['0:72fdb8fd-5db1-5291-be88-c4e2a51671e7:93:1', '1:7e14d7ce-682b-54c0-80b1-65f3bc25f9f9:93:2', '2:c0587a1b-cbd0-5dfb-90ca-041acd5bcec8:93:1']`

