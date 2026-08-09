# SkyWeave v2 detection contracts (D0)

**Revision:** 2026-08-04
**Status:** Provisional until every test in §8 passes. Nothing here is Measured.
**Scope:** Guide Step 1 / EXP-001 implementation-order step 1. Frozen semantics for
frames, pixels, time, observations, localization results, and tracks.
**Serialization policy (Chosen):** this spec plus pydantic models is the contract
through D6. The reviewed `.proto` lands in D7 and must map 1:1 onto the field
numbers reserved here. Wire format before D7 stays msgpack.

Where this document and v1 code disagree, this document wins for v2; v1 stays
untouched as the golden generator.

---

## 1. Coordinate frames and transforms (Chosen)

- World: local ENU Cartesian, meters. x east, y north, z up. Origin: declared
  array reference point in the scene/deployment manifest.
- Camera: OpenCV convention. +X right, +Y down, +Z along the optical axis.
- Transform notation: `T_A_B` maps points from frame B into frame A:
  `p_world = T_world_cam @ p_cam`. 4x4 row-major homogeneous.
- Rotations are 3x3 matrices inside the estimator. Quaternions (xyzw) exist only
  at the viz boundary and are derived, never authoritative.
- v1 `look_at_pose` / `T_world_cam` must be audited against this definition by
  test T1 before any v2 code consumes it.

## 2. Pixel conventions (Chosen)

- Continuous pixel coordinates. (0.0, 0.0) is the **center** of the top-left
  pixel. u right, v down. An image of width W spans u in [-0.5, W-0.5].
- Distortion: Brown-Conrady (k1, k2, p1, p2, k3). The estimator's calibration is
  a separately serialized object from any renderer truth (never shared by
  reference).
- Resolution scaling law: centroids are always **reported** in full-resolution
  calibrated pixel coordinates, and every observation **declares** the
  processing resolution it was measured at. Mapping between grids of width
  `W_proc` and `W_full` with scale `s = W_full / W_proc`:

  ```text
  u_full = (u_proc + 0.5) * s - 0.5
  ```

  Same form for v. Test T2 pins the half-pixel behavior.
- Crops/masks carry their offset (x0, y0) in full-resolution coordinates.
- v1's `cx = x0 + (size-1)/2` is consistent with this convention and is kept.

## 3. Time (Chosen)

Four clock domains, never conflated, never overwritten:

| Domain | Meaning |
| --- | --- |
| `SYNTHETIC` | truth/scene time from a dataset manifest |
| `NODE_MONO` | edge node monotonic clock |
| `NODE_PTP` | edge node PTP/chrony-disciplined wall clock |
| `JETSON_RX` | Jetson receive/dequeue time |

- `capture_ts_ns` names the **exposure midpoint** of the frame (Chosen).
  Rationale: a motion-blurred blob's centroid physically corresponds to the
  target near mid-exposure, so measurement and timestamp name the same event.
  The D8 coded-LED bench test later measures the offset between real board PTS
  and this declared event; until then every real-PTS mapping is Provisional.
- Rolling shutter: frame metadata carries `exposure_us` and `line_readout_us`
  regardless of whether any correction is applied. Exposure start/end are
  derived, not stored twice. Truth sidecars additionally carry per-row/exposure
  window truth (EXP-001 contract).
- Every observation carries its capture timestamp plus clock domain plus an
  honest `time_sync_error_ms`. The Jetson stores receive time **beside**
  capture time. Clock mapping (offset, drift, uncertainty) is estimated
  centrally; remaining time uncertainty flows into measurement quality.
- Session identity: every node boot generates a `session_uuid`. `frame_seq` is
  monotonically increasing within one session only. A reboot is a new session,
  detected by uuid change, never by heuristics on sequence numbers (test T4).

## 4. FrameEnvelope and Observation2D

`FrameEnvelope` is the identity of one capture event. `Observation2D` is one
measurement extracted from it. Losing optional evidence (mask/crop) must never
lose the measurement (test T7).

Reserved field numbers (for the D7 `.proto`; pydantic uses the same names):

**FrameEnvelope**

| # | Field | Type | Notes |
| --- | --- | --- | --- |
| 1 | `camera_id` | uint32 | |
| 2 | `session_uuid` | string | per node boot |
| 3 | `frame_seq` | uint64 | per session |
| 4 | `capture_ts_ns` | int64 | exposure midpoint |
| 5 | `clock_domain` | enum §3 | |
| 6 | `time_sync_error_ms` | float | honest, may be large |
| 7 | `exposure_us` | float | |
| 8 | `gain_db` | float | |
| 9 | `full_width` / 10 `full_height` | uint32 | sensor/calibrated grid |
| 11 | `proc_width` / 12 `proc_height` | uint32 | detector grid (F2) |
| 13 | `calibration_rev` | string | hash of estimator calibration |
| 14 | `detector_rev` | string | detector build/config hash |
| 15 | `line_readout_us` | float | rolling shutter row time |

**Observation2D**

| # | Field | Type | Notes |
| --- | --- | --- | --- |
| 1 | `envelope` | FrameEnvelope | or reference to one |
| 2 | `obs_id` | uint32 | unique within frame |
| 3 | `u` / 4 `v` | double | full-res calibrated px, §2 |
| 5 | `cov_uu` / 6 `cov_uv` / 7 `cov_vv` | double | 2x2 centroid covariance, full-res px². Floor set by repeatability study (D4), never zero |
| 8 | `bbox` | x0,y0,w,h | full-res px |
| 9 | `area_px` | uint32 | at proc resolution (declared in envelope) |
| 10 | `persistence_count` | uint32 | consecutive frames seen |
| 11 | `confidence` | float 0..1 | detector-internal, not a probability |
| 12 | `local_blob_id` | uint32 | optional edge-local association |
| 13 | `evidence_ref` | string | optional, droppable mask/crop id |

v1 gap recorded: `MotionBlob`/`Detection` carry no uncertainty and no
calibration revision; both are superseded at the fusion boundary. Motion
patches remain optional evidence for the voxel path, not measurements.

## 5. LocalizationResult and covariance semantics

Two separate error channels (Chosen):

- **Conditional covariance:** random, frame-to-frame error under the current
  calibration (pixel noise, timing jitter). Full 3x3, anisotropic, derived from
  the inverse of the accumulated normal-equations matrix scaled by residual
  (per the §14 ledger: v1's isotropic form is rejected for v2; the math lands
  in D1, the schema lands here). This is the only channel the track filter
  consumes.
- **Systematic bound:** per-axis bound with source labels for biases that do
  not average away: `calibration`, `clock`, `mount`, `target_reference`.
  Reported alongside every result, used by gates and reports, never fed to the
  filter as noise.

**Truth reference (Chosen):** truth error is measured against the projected
**geometric center** of the target volume from the manifest. The detector
reports the foreground centroid; the stable centroid-vs-center mismatch is the
`target_reference` systematic term. Truth is therefore independent of render
materials and lighting.

**LocalizationResult**

| # | Field | Notes |
| --- | --- | --- |
| 1 | `ts_ns` + 2 `clock_domain` | capture-event time of the fused set |
| 3 | `position` (3) | meters, world ENU |
| 4 | `covariance` (9) | conditional 3x3, row-major |
| 5 | `systematic_bound` (3) | per-axis meters |
| 6 | `systematic_sources` | repeated enum |
| 7 | `residual_px_rms` | reprojection residual |
| 8 | `supporting_camera_ids` | repeated |
| 9 | `triangulation_angle_deg` | max pairwise ray angle |
| 10 | `condition` | normal-equations conditioning |
| 11 | `status` | `tentative, confirmed, weak_geometry, late, rejected` |
| 12 | `obs_ids` | exact observations consumed (traceability) |

Multiple LocalizationResults per capture event are legal. That is what makes
data association representable; the v1 assumption of one measurement per frame
is rejected for v2.

## 6. Track contract

- States: `tentative, confirmed, coast, reacquired, deleted` (replaces v1
  `candidate/active/coasting`).
- `track_id` uint64, unique within one Jetson `session_uuid`, never reused.
- State: `[x, y, z, vx, vy, vz]`, 6x6 covariance. EKF-capable interface;
  refined-XYZ update is the first contract (working doc; a UKF swap would be a
  §14 ledger change, not a silent one).
- Every track update logs the `obs_ids` and LocalizationResult it consumed.
  This is the field that makes the D9 gate "every track update traceable to
  its exact observations and configuration" checkable.

## 7. v1 characterization findings (pin with goldens, do not fix in v1)

| ID | Finding |
| --- | --- |
| F-D0-1 | Sim generator sets `publish_ts_ns = capture_ts_ns`; no transport age exists in v1 data |
| F-D0-2 | `Measurement3D.covariance` is isotropic; over-trusts depth (§14) |
| F-D0-3 | `check.py` passes a list to `tracks.update(...)`, `benchmark.py` passes a single measurement; goldens must pin which behavior is the v1 reference |
| F-D0-4 | No session/boot identity anywhere; a node reboot is invisible |
| F-D0-5 | No processing-resolution field; F2 (GMM2 resolution) is unrepresentable |
| F-D0-6 | `clock_domain` is a free string with a single default |
| F-D0-7 | Highest-confidence-per-camera fusion assumes one physical object (association missing, §14) |

## 8. Done when (all must pass)

| Test | Content |
| --- | --- |
| T1 | Transform audit: hand-derived camera pose and projection case, computed independently of the code, reproduced by `T_world_cam` semantics; camera order invariance |
| T2 | Pixel scaling: half-pixel mapping law round-trips proc↔full grids exactly; crop offset mapping |
| T3 | Distortion project/unproject round-trip within tolerance |
| T4 | Timestamp/session: domain mapping, reboot with `frame_seq` reset and new `session_uuid` does not corrupt alignment; capture time never overwritten by receive time |
| T5 | Serialization: msgpack round-trip byte-stable; unknown-field tolerance; reserved field numbers documented |
| T6 | Goldens: v1 `generate_golden_peaks` output plus pinned-config sim-check `RunSummary` committed under `golden/` with manifest hashes, before any v2 behavior lands |
| T7 | Evidence drop: deleting every optional mask/crop from a recorded set changes no centroid, timestamp, or downstream geometry input |

## 9. Decisions log

| Decision | Label | Settled by |
| --- | --- | --- |
| ENU world / OpenCV camera / `T_A_B` notation | Chosen | T1 |
| Pixel-center convention + scaling law | Chosen | T2 |
| `capture_ts_ns` = exposure midpoint | Chosen | D8 coded-LED PTS characterization measures real offset |
| Truth = object geometric center + `target_reference` systematic term | Chosen | Tier 0/1 reports carry the term |
| Conditional covariance separate from systematic bound | Chosen | D1 math, Tier 2 coverage evaluation |
| Spec + pydantic now, `.proto` at D7 | Chosen | D7 review |
| Anisotropic covariance, association-capable results | Chosen (design), math Deferred to D1 | D1 Monte Carlo |

---

## 10. Decisions and findings since 2026-08-04

| Item | Decision | Label |
| --- | --- | --- |
| Rewrite boundary | v1 is reference/golden generator; core detector fully rewritten (triangulation first, voxel evidence proposes); viz_web carried forward behind an adapter | Chosen |
| Repo mechanics | package `skyweave2`, uv + lockfile, Python 3.10 pinned (Jetson image), ruff only, determinism policy | Chosen |
| D2 partial | gate target 0.75 m; lens 60-70 deg (60.0 Provisional, bench-measure before final freeze); render 2304x1296 (corrects EXP-001's 2312x1304); detection resolution swept {2304x1296, 1536x864, 1152x648}, gate primary 1536x864 | Provisional |
| D2 parked | centroid authority rule, gate speed, exact clip length: finalize after D1 | Open |
| T1 result | v1 `fusion/geom.py` matches the frozen `T_world_cam` and camera-axis convention (200-point audit) | Verified by test |
| F-D0-8 | `motion.py` connected-components output depends on OpenCV presence; golden regeneration requires OpenCV (see `golden/REGENERATION.md`) | Finding |
| Blender version pin (2026-08-07) | First D3 render performed with Blender 5.2.0 LTS (fbe6228777e7); recorded into `configs/exp001_scene.yaml` `determinism.renderer_version` per that file's pre-authorized amendment note | Chosen |
| Sky model (2026-08-07) | Blender 5.x removed the Nishita enum; the physically-based successor `MULTIPLE_SCATTERING` is used and recorded in the manifest and every dataset.json | Chosen |

D0 exit status 2026-08-05: T1-T5, T7 pass (27 tests); T6 goldens verified
identical against the live v1 tree and extended with the sim-check summary.

### D2 closure (2026-08-05)

| Item | Decision | Label |
| --- | --- | --- |
| Named-gate baseline | 20 m (D1 budget verified at 20 m: patch 3.90 m p95 at 1.0 px; edge-only 5.96 m at 1.0 px, 4.39 m at 0.75 px) | Chosen |
| Centroid rule | Edge-only with D4 tripwire at 0.75 px full-res sigma; above it, switch to patch-refined | Chosen, tripwire Provisional |
| Gate speed | 30 m/s lateral constant velocity | Chosen |
| Clip | 15 s at 30 fps (450 frames/camera, 1350 total), 3 s warm-up, entry at 3 s | Chosen |
| Manifest | `v2/configs/exp001_scene.yaml` is the frozen scene | Chosen |
| D1 exit | 46 tests pass, budget byte-deterministic, fenced paths clean; pose-rotation dominance recorded to working-doc question 12 | Verified |

### D3 amendments (2026-08-05)

| Item | Decision | Label |
| --- | --- | --- |
| Scene exposure | `exposure_us: 3000` added to the manifest (~0.75 px smear at gate speed) | Provisional |
| Frame-count correction | D2 closure said 1350 frames/camera; correct value is 450/camera, 1350 total | Correction |
| Hybrid clip source | SC3336 board out a window | Chosen |
| Conversion-gain bench | Hardware on hand, default Luckfox image; runs parallel to D3 code; must land before D6 freezes noise scales | Scheduled |
| CFA pattern | BGGR assumed | Provisional until driver check |

### D4 decisions (2026-08-06)

| Item | Decision | Label |
| --- | --- | --- |
| Host detector baseline | OpenCV MOG2 (pinned, single-threaded) is the D4 primary; `ive_approx` numpy backend mirrors RK IVE GMM2 knobs for D8 prediction; deployment detector remains hardware IVE GMM2 on the RV1106 | Chosen |
| OpenCV dependency | Required for v2 detector path; refusal instead of silent fallback (F-D0-8) | Chosen |
| Negative clip | Target-free render of the gate scene added as a golden input | Chosen |
| Render device | AMENDED same day: golden clips are defined as the GCP L4/OptiX run's artifacts (`v2/cloud/gcp_render/`), hashes pinned in SHA256SUMS; device, driver, and Blender build recorded in each dataset manifest; CPU and other-GPU renders are benchmarks only | Chosen |
| Anti-tuning | Detector parameters tuned only on seed-variant datasets; gate and negative clips scored once per frozen config | Chosen |

### D6.1 closure (2026-08-08)

| Item | Decision | Label |
| --- | --- | --- |
| D6-F1 closed | Systematic channel populated at runtime from DECLARED uncertainty (new `FusionConfig.systematic`: per-camera rotation/position sigma, target width, estimated speed); bounds propagated through the SOLVE geometry, sources labeled, carried to published states beside the frozen Track contract. No contract change | Verified |
| Calibration bound propagation | A declared angular sigma must be pushed through the closest-point solve, not applied per ray: triangulation amplifies it by range/baseline, so the bound scales as range^2/baseline (D1's depth law). The naive per-ray bound was ~10x too small and would have blessed a 32 m error with a 3 m bound | Finding |
| D6-F1 gate closed | Overconfidence = true error vs covariance PLUS declared bound. Honest declaration: 0 events across all Tier III axes (13 of 22 rows non-vacuous; vacuous rows labeled). Dishonest declaration: 224 events, caught as designed | Verified |
| D6-F2 closed | `residual_variance_floor` = 0.066, derived as (0.128 px D4 Measured repeatability / 0.5 px declared centroid sigma)^2. Zero-fault regime: 0 events over 8400 confirmed publications (was 1.3%) | Verified |
| D6-F4 closed | Epipolar prefilter emits per-observation audit records; layer 2 now carries 3600 true / 1958 planted rejections where it previously read 0/0 | Verified |
| Vacuous-pass labeling | Rows where nothing was published (large faults break tracking entirely) are marked VACUOUS in the report rather than counted as clean passes | Chosen |

### D6 closure and D6.1 opening (2026-08-08)

Verification: 212 pass + 2 clip-dependent skips in the cloud workspace
(the one harness failure was a missing package install, not code), ruff
clean, fault manifest byte-unchanged, goldens intact.

| Item | Decision | Label |
| --- | --- | --- |
| D6-F1 | Systematic channel must be populated at runtime: bounds from DECLARED calibration uncertainty (new FusionConfig fields, per-camera rotation/position sigma) propagated through the solve, plus the target_reference bound from the manifest, plus a clock term from declared time_sync_error and estimated speed; sources labeled. No contract change needed | Chosen |
| D6-F1 gate | Overconfidence redefined: true error vs covariance PLUS declared systematic bound. Honest-declaration Tier III runs must show zero events; dishonest-declaration runs are reported as known-lie cases, mirroring the time-honesty design. An UNDECLARED calibration error is by definition detectable only through declared bounds; the system's obligation is honesty about what it was told | Chosen |
| D6-F2 | residual_variance_floor set from D4's Measured repeatability (evidence-derived, not gate-scene tuning); zero-fault regime must then show calibrated coverage | Chosen |
| D6-F4 | Epipolar prefilter gains per-observation audit records; affected campaigns re-run | Chosen |
| D6 process findings | Injector/bookkeeping bugs (clutter arrival order, duplication inversion, restart no-op, lateness labels, hidden far-from-truth) and the hash() salting non-determinism: all fixed with can-fail tests; recorded as the argument for adversarial review and byte-identity gates | Recorded |
| Camera-dropout geometry | Losing the center camera barely degrades covariance; only end cameras carry baseline. Feeds later array-layout choices | Recorded |

### D7 opening (2026-08-08)

| Item | Decision | Label |
| --- | --- | --- |
| Wire format | protobuf over UDP, one capture event per datagram, 1200 B provisional ceiling; field numbers = D0 reservations; nanopb-compatible for the D8 C daemon | Chosen |
| Video | RTSP/VENC remains optional human debug only; no H.264 on the measurement path | Chosen (standing rule) |
| Control plane | TCP, same protobuf, length-prefixed | Provisional |
| Rig | 3-process loopback = D9 topology minus boards; Tier IV faults re-run at socket level | Chosen |

### D7 closure (2026-08-08)

| Item | Decision / record | Label |
| --- | --- | --- |
| Wire result | 325 tests pass; W4 parity exact (file vs socket vs 3-process rig); 0 of 39 Tier IV cells diverge from D6 policies; datagram p95 167 B under the 1200 B ceiling | Recorded |
| Field numbers | ObservationPacket, BoundingBox, HealthPacket, EvidencePacket, ControlMessage numbers and enums as encoded in `v2/proto/skyweave.proto` + golden byte fixtures are hereby the reserved wire numbers (D0 reserved none for these); the .proto is now the authority for wire-level messages | Chosen |
| Capacity limit | 1200 B admits at most 5 observations per capture event; the D4 detector has no per-frame cap, so a cluttered frame is loudly unsendable. Levers: raise ceiling toward MTU, shorten string bounds, allow splitting, or cap detector components. DECISION DEFERRED TO D8 planning (interacts with the edge byte governor) | Closed 2026-08-08, see D8 opening |
| binary32 | Envelope float fields are binary32 on the wire per D0; exact round-trip assertions on float64 values will fail by design; widening would be a D0 change | Recorded |
| Replay pacing | Multi-source replay requires wall-clock pacing with a shared epoch (PTP's job on real boards); accelerated replay bounded by the lateness window, rig pinned to 1x by a derived test | Recorded |
| Acceptance drift note | Gate-clip numbers moved slightly vs D5.3 (e.g. range p95 0.396 -> 0.313) as a consequence of the D6.1 evidence-derived variance floor changing filter weighting; parity within D7 is exact, which is what this phase gates | Recorded |

### D8 opening (2026-08-08)

| Item | Decision | Label |
| --- | --- | --- |
| Datagram ceiling | Raised from 1200 B (Provisional) to 1472 B, the untagged-Ethernet MTU payload (1500 - 20 IP - 8 UDP). The D8/D9 path is a wired switch; MTU 1500 is guaranteed, so no fragmentation. Worst-case capacity by encoding: envelope + header 251 B, 163 B per observation -> 7 observations fit (251 + 7*163 = 1392, headroom 80 B) | Chosen |
| Observation bound | `ObservationPacket.observations` max_count raised 5 -> 7 in `proto/skyweave.options`. Encoding of existing fixtures is unchanged (max_count is an allocation bound, not a wire value) | Chosen |
| Detector per-frame cap | The detector gains a per-frame component cap of 7, keeping the top components by descending confidence; dropped components are counted in stats, never silent. The cap lives in the shared `DetectorConfig` so the host detector remains the oracle for D8 frame->packet fixtures, and it aligns with the edge byte governor (RV1106_EDGE_NODE.md section 8), which requires a fixed per-frame bound. Invariant: wire max_count >= detector cap | Chosen |
| Rejected levers | String-bound shortening (forces golden fixture regeneration for capacity not yet needed) and event splitting across datagrams (adds reassembly and a partial-loss failure mode on lossy UDP, against the loud-failure rule) | Rejected |
