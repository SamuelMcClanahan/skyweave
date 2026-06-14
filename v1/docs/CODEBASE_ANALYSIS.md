# Skyweave — Codebase Analysis, Assumption Audit, and Scaling Study

**Scope:** the non-three.js core (Python: camera → motion → Rayweave scoring → fusion → tracking → operator runtime), measured against `SPEC_MVP.md`, the high-level system spec (`docs/specs/system-spec-high-level.md`), and the 2026-05-28 architecture-review note.
**Goal:** explain *what* the system does, *how* it does it — both the math and the way the code itself is written — *why* each design choice is reasonable or risky, and *where* it should go as it scales to the final architecture: **many deployed RV1106 nodes with MIPI-CSI cameras sending motion patches over Ethernet through a PoE switch to a central Jetson, with CUDA-accelerated scoring.**

Every assertion below is explained from first principles and, where useful, compared to how production tracking systems and the research literature handle the same problem. File references use `path:line` so they are clickable. Every code claim was verified against the source at time of writing.

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [What the system is and how it runs](#2-what-the-system-is-and-how-it-runs)
3. [Stage-by-stage walkthrough](#3-stage-by-stage-walkthrough)
4. [Spec-vs-reality scorecard](#4-spec-vs-reality-scorecard)
5. [The voxel-scoring assumption audit](#5-the-voxel-scoring-assumption-audit)
6. [How the code is written — implementation quality audit](#6-how-the-code-is-written--implementation-quality-audit)
7. [Scaling to the final architecture: RV1106 patch streams → Jetson + CUDA](#7-scaling-to-the-final-architecture-rv1106-patch-streams--jetson--cuda)
8. [Prioritized recommendations](#8-prioritized-recommendations)

---

## 1. Executive summary

Skyweave converts motion pixels from several calibrated cameras into rays, accumulates those rays into a 3D voxel "evidence field" (the *Weavefield*), extracts the peak of that field as a 3D position measurement, and smooths it with a Kalman filter. The novel pitch — versus plain triangulation — is that the evidence field is *inspectable*: you can see where in space the cameras agree, not just a single fused point.

The honest assessment, in four sentences:

- **The geometric core is real and tested.** Ray construction, DDA voxel traversal, multi-camera score combination, soft-argmax peak extraction, and a constant-velocity Kalman filter all exist, pass synthetic RMSE gates, and have a Numba-accelerated fast path the spec only aspired to.
- **The systems scaffolding for distribution is mostly absent.** The async message bus, the stateful time-aligner, the multi-target track manager, the temporal (4D) Weavefield, the async flight recorder, and the edge/turret packet contracts are stubs or missing — exactly the pieces a multi-node field deployment needs.
- **The voxel scorer is the right *demo* but the wrong *final estimator*** for faint far targets; for point-like objects a bearing/triangulation filter is more accurate, and your own architecture-review note says so. The code has not yet made that split.
- **The code is locally clean but structurally duplicated.** The domain-math modules (`geom`, `rayweave`, `fusion`) are small, typed, and testable; the application layer has three copy-pasted pipeline drivers, five copies of `_percentile`, sixteen-plus cross-module imports of underscore-private helpers, and pydantic object churn in every hot loop. None of this is fatal today; all of it compounds badly when the edge/center split arrives (§6, §7).

The rest of this document substantiates each of those claims.

---

## 2. What the system is and how it runs

### 2.1 The intended pipeline (from the spec)

`SPEC_MVP.md` defines a single reusable "spine":

```
calibration → pixel/mask → ray/frustum → Rayweave scoring → Weavefield → measurement → track → viz/replay
```

The load-bearing idea: synthetic data, recorded data, and live cameras all feed the *same* message boundary (`MotionPacket`), so the fusion math never knows where packets came from. This is the same "sensor abstraction" discipline used in robotics middleware (ROS topics) and AV perception stacks. The codebase honors it: the synthetic generator, the rendered-frame generator, and the live camera path all emit `MotionPacket`s ([messages.py:48](src/skyweave/messages.py)) into the same aligner→scorer→peaks→Kalman chain.

The spec is equally explicit about the V1 endgame (§1.3, §9.2): the realtime network path is **edge-produced motion evidence packets — centroids, bboxes, scores, small RLE/sparse mask patches — over UDP**, never raw or compressed video; video is a lagging debug stream only. That sentence is the design contract §7 of this document architects around.

### 2.2 What actually runs

There is **no single always-on async pipeline** matching the spec's bus diagram. Instead there are two concrete drivers that call the same core objects directly:

1. **`sim/check.py`** ([src/skyweave/sim/check.py](src/skyweave/sim/check.py)) — the offline harness behind `skyweave-sim-check`. It builds a synthetic scene, generates packets, runs aligner → scorer → peaks → triangulation → Kalman, and reports RMSE against ground truth with explicit pass/fail thresholds ([check.py:317](src/skyweave/sim/check.py)). **This is the workhorse**: it is what the test suite and the `configs/*.yaml` profiles exercise, and its existence is why this document can make quantitative claims at all.

2. **`operator/runtime.py`** ([src/skyweave/operator/runtime.py](src/skyweave/operator/runtime.py)) — an 858-line threaded loop driving the web operator UI, with three modes: `real` (live UVC + ChArUco extrinsics), `stress` (pre-generated synthetic packets), and `rendered` (synthetic *frames* pushed through the real frame-diff extractor, [runtime.py:329](src/skyweave/operator/runtime.py) — a genuinely good idea, because it tests the actual pixel→packet path).

A third driver, **`recording/replayer.py`**, re-runs recorded sessions through a third copy of the same orchestration. The triplication is a §6 topic.

There are 15 CLI entry points ([pyproject.toml:33](pyproject.toml)) covering sim-check, autotune, benchmark, replay, viz export, camera tools, and a full ChArUco calibration workflow.

**Why the missing bus matters:** the spec's `transport/bus.py` asyncio pub/sub (MVP-16) does not exist; the runtime is a plain synchronous `while` loop. That is *fine* — arguably better — for a single-host MVP. But it means the "distributed-style boundaries from day one" goal was not met: the network seam V1 depends on exists only as a schema, never as a boundary code actually crosses. §7.1 and §7.6 return to this.

### 2.3 The data flow in one diagram

```
                       (per camera, per frame)
  frame_t, frame_{t-1} ──► FrameDiffMotionPacketBuilder        camera/motion.py
                              │  |diff| ≥ threshold → mask
                              │  connected components → blobs
                              │  RLE-encoded mask patches (≤225 px each)
                              ▼
                          MotionPacket ───────────────┐
                                                      ▼
                       TimeAligner.align_frame()   fusion/aligner.py
                          (stateless window check)
                                                      ▼
                       RayweaveScorer.score()      rayweave/scorer.py
                          per-camera ray DDA → per-camera grids
                          support-count gate → combined grid → top-K sparse
                                                      ▼
                       PeakExtractor.extract()     rayweave/peaks.py
                          percentile threshold → components → soft-argmax
                                                      ▼
                       Measurement3D ──► TrackManager (1× CV Kalman)  fusion/kalman.py
                                                      ▼
                       VizFrame JSON ──► operator UI / three.js
```

---

## 3. Stage-by-stage walkthrough

For each stage: what the code does, why it is reasonable, and where it diverges from the spec or from good practice.

### 3.1 Geometry and ray construction — `fusion/geom.py`

**What it does.** `ray_from_pixel` ([geom.py:73](src/skyweave/fusion/geom.py)) builds a world-space ray from a pixel: back-project through the intrinsics to a camera-frame direction `normalize([(u−cx)/fx, (v−cy)/fy, 1])`, rotate into world with the rotation block of `T_world_cam`, anchor at the camera center. Textbook OpenCV pinhole model, matching the spec's Appendix C exactly. The Numba scorer re-implements identical math in scalar form ([numba_scorer.py:40](src/skyweave/rayweave/numba_scorer.py)).

**The flaw to flag — lens distortion is silently dropped.** `CameraCalib` stores distortion coefficients `D` ([geom.py:13](src/skyweave/fusion/geom.py)), the calibration tools *solve* for `D` and use it inside `solvePnP` ([extrinsics.py:221](src/skyweave/calibration/extrinsics.py)), but `ray_from_pixel` and the Numba scorer **never undistort the pixel**.

- *Why this is invisible in testing:* the synthetic scene sets `D = zeros` ([scene.py:44](src/skyweave/sim/scene.py)). A pinhole camera with zero distortion is exactly what the runtime assumes, so **every passing sim test is structurally blind to this gap** — the classic "the simulator validates the simulator" trap.
- *Why it bites on real hardware:* the spec (§8.1) explicitly says "the pixel is first undistorted." Real OV9281/SC3336 lenses have several pixels of radial displacement at the corners. Skipping undistortion introduces a *systematic, position-dependent* bearing error that eats the ±10 cm acceptance target and masquerades as a calibration error, sending you debugging the wrong subsystem.
- *Fix:* `cv2.undistortPoints(pixels, K, D)` per packet — or, for the hot path, a per-camera static undistortion lookup table computed once (standard in real-time systems; undistortion is a fixed map for a fixed lens).

### 3.2 Motion extraction — `camera/motion.py`

**What it does.** Frame differencing: `|gray_t − gray_{t−1}| ≥ threshold` → binary mask → optional morphological merge → connected components → up to 8 blobs, each with centroid, bbox, area, mean/max diff, confidence, and an RLE-encoded mask patch. Three interchangeable backends (pure-Python BFS, OpenCV `connectedComponentsWithStats`, OpenCV contours) with auto-selection ([motion.py:302](src/skyweave/camera/motion.py)).

**Why frame differencing is the right default.** Cheapest possible temporal detector, matches the reference prototype, runs on RV1106-class CPUs — and for sub-resolution targets it is *more sensitive* than adaptive background models, which can absorb a small fast target into the background before it registers (a standard finding in infrared small-target "detect-before-track" literature).

**Three details that shape the evidence the scorer sees:**

1. **Evidence is decimated to ≤225 pixels per blob** (`max_motion_pixels`, [motion.py:33](src/skyweave/camera/motion.py)) via a deterministic `linspace` stride. Sane bandwidth/CPU cap that *previews the V1 network budget* (§7.3 shows it is almost exactly MTU-shaped) — but the patch becomes a row-major-biased *sample* of the mask, not the mask.
2. **Confidence is `area / max_motion_pixels` capped at 1** ([motion.py:127](src/skyweave/camera/motion.py)) — confidence ≡ size. A big blob of rustling leaves scores 1.0; a genuine 4-pixel aircraft scores 0.02. Backwards for the sky use case, and it silently feeds scoring weights and triangulation candidate selection.
3. **`merge_radius_px` / `fill_fragments`** implement morphological closing so a fragmenting target becomes one blob — pragmatic, clearly learned from real camera tests.

**Missing relative to spec:** KNN background subtraction (MVP-10), the temporal coherence filter (MVP-14), ROI/horizon masks (MVP-11). Defensible indoors; *not optional* for sky, where your architecture note lists exactly these as the cloud/bird rejection toolkit. Deferred necessities, not skipped polish — and the coherence filter is also the single best network-spam reducer for the edge nodes (§7.2).

### 3.3 The voxel scorer — `rayweave/scorer.py`, `rayweave/dda.py`, `rayweave/numba_scorer.py`

The heart of the system; full audit in §5. Mechanics:

1. For each camera, for each evidence pixel (patch pixels, or blob centroids in `centroids` mode), cast a ray and **add weight to every voxel the ray traverses**, via 3D-DDA ([dda.py:35](src/skyweave/rayweave/dda.py), [numba_scorer.py:106](src/skyweave/rayweave/numba_scorer.py)). DDA (Amanatides & Woo 1987) steps cell-to-cell in exact boundary-crossing order — the standard grid-marching algorithm.
2. Each camera's pixel weights are normalized to sum to 1 ([scorer.py:71](src/skyweave/rayweave/scorer.py)).
3. **Separate score grid per camera**; combined score = cross-camera sum, kept only where ≥ `min_supporting_cameras` cameras put *any* score ([scorer.py:84](src/skyweave/rayweave/scorer.py)).
4. Top-K voxels become the sparse `WeavefieldVolume`.

**Why per-camera-then-intersect is the right skeleton.** Tracking *which* cameras hit a voxel is what turns rays into a localized 3D point, and it is exactly the structure a probabilistic upgrade (log-odds, §5-A2) needs — good scaffolding even though the combination rule is crude.

**The Numba backend's key optimizations:** a **touched-voxel list with per-frame stamps** ([numba_scorer.py:108](src/skyweave/rayweave/numba_scorer.py)) so combine/sparsify only visit voxels rays actually hit — the correct *work ∝ evidence* pattern and the seam to exploit on GPU (§7.5) — plus stamp-based clearing instead of re-zeroing.

One inefficiency: the Numba path still allocates a fresh dense `(n_cameras, *dims)` float32 tensor every frame ([scorer.py:139](src/skyweave/rayweave/scorer.py)) — ~7 MB/frame at room resolution, ~700 MB/s of churn at 100 Hz. Harmless on a Mac, measurable on a Jetson. Preallocate and stamp-clear, as the touched list already does.

### 3.4 Peak extraction — `rayweave/peaks.py`

**What it does** ([peaks.py:18](src/skyweave/rayweave/peaks.py)): percentile-threshold the sparse voxels, group into 6-connected components, compute a **soft-argmax** (softmax-weighted centroid over a dense neighborhood of the best voxel) plus covariance from weight spread with a `voxel²/12` quantization floor.

**Why this beats the spec's plain centroid.** Hard argmax snaps between voxels frame-to-frame, feeding quantization noise into the Kalman filter; soft-argmax gives a smooth sub-voxel estimate — the discrete analog of sub-pixel centroiding in star trackers and PIV. The `voxel²/12` term is the correct uncertainty floor for a quantized grid.

**Two subtleties:**

- **The percentile is computed over the already-truncated top-K sparse set** ([peaks.py:22](src/skyweave/rayweave/peaks.py)), not the full distribution. `threshold_percentile=99.5` means "top 0.5% *of the top 5000*" — the effective threshold silently couples to `top_k_voxels` and to ray-touch counts. Two knobs secretly move each other.
- **The covariance describes blob shape, not geometric precision** — §5-A6, the more consequential issue.

### 3.5 Triangulation — `fusion/triangulator.py`

**What it does** ([triangulator.py:9](src/skyweave/fusion/triangulator.py)): linear least-squares "closest point to a set of rays" (`P = I − ddᵀ` per ray, solve `(ΣP)x = ΣP·origin`). Covariance: isotropic `max(mean_residual, 3cm)²`.

**Why it is correct but incomplete.** The midpoint solve is legitimate, but the spec (§7.8) asked for **DLT + Levenberg–Marquardt minimizing reprojection error** — the statistically correct objective (noise lives in pixels, not 3D), used by every serious multi-view pipeline (COLMAP, OpenMVG). And the **isotropic covariance is the bigger problem**: real triangulation uncertainty is strongly anisotropic — tight across bearings, elongated along depth as baselines narrow. Collapsing it discards exactly what the Kalman filter needs. Consequence: the triangulation path cannot do its spec-assigned jobs (refinement; calibration diagnostic via voxel-vs-triangulation disagreement). Today it is a sanity marker.

### 3.6 Tracking — `fusion/kalman.py`

**What it does** ([kalman.py:16](src/skyweave/fusion/kalman.py)): a 6-state constant-velocity KF via `filterpy`, variable dt, white-acceleration process noise (standard Bar-Shalom CV form), Mahalanobis gating with nearest-gated selection. In-place matrix updates avoid per-frame allocation — a good habit.

**Why a linear CV KF is correct here.** The measurement is a 3D point → linear measurement model → the KF is optimal. EKF/UKF wait for nonlinear measurements (bearings-only, turret pan/tilt). Spec and notes agree; the code agrees.

**Three observations:**

1. **Structurally single-target.** One filter, one id; coast-expiry retires and re-seeds ([kalman.py:39](src/skyweave/fusion/kalman.py)). No associator, no Hungarian assignment. Any phantom that survives the gate competes with the real target for the one track and causes thrash. Every real MOT system (SORT/ByteTrack; JPDA/MHT in radar) separates *association* from *filtering*; here they are fused into one greedy step.
2. **Tuned constants drifted to mask the covariance problem.** Spec gate χ²(3, 99%) = 11.345; defaults are 35.0 plus `measurement_var_scale = 3.0` ([config.py:70](src/skyweave/config.py)). A 3× loose gate plus a 3× covariance fudge is the signature of a filter whose `R` has the wrong shape (§5-A6). Fix `R`, re-tighten the gate.
3. **Coast-expiry re-init can chain two different objects** into consecutive ids — the "second throw is a separate track" demo criterion passes only by timing accident.

### 3.7 Time alignment — `fusion/aligner.py`

**What it does** ([aligner.py:22](src/skyweave/fusion/aligner.py)): a 43-line *stateless* check — given packets handed in together, return a bundle if ≥ `min_cameras` fall within `window_ns`; bundle timestamp = mean of capture timestamps.

**Why this works today and breaks tomorrow.** In sim and the synchronous loop, all packets for a frame are produced in the same iteration — alignment is trivially satisfied. The spec (§7.2) specified a *stateful* aligner: per-source deques, a `wait_ns` close timer, late-drop logging. The current design assumes packets arrive synchronously and together — the exact assumption that evaporates with UDP from independent edge nodes. **This is the single biggest "works in sim, won't survive V1" gap**, and it must be rewritten *before* network code exists, or the network will be debugged against a broken aligner.

Also: `clock_domain` and `time_sync_error_ms` are on every `PacketHeader` ([messages.py:19](src/skyweave/messages.py)) and **nothing reads them**. The forward-compatible plumbing exists; the logic does not.

### 3.8 Recording, replay, and transport

- **The recorder is synchronous, not the spec's async flight recorder.** `Recorder._write_one` packs and **flushes to disk on every model write** ([recorder.py:81](src/skyweave/recording/recorder.py)), inline in the scoring loop; `JsonlLogger.event` likewise flushes per line ([log.py:28](src/skyweave/log.py)). The spec (§11.5) demanded a bounded queue + writer task so disk stalls never become loop jitter. Invisible on a Mac SSD; on Jetson/eMMC under load, a 50–200 ms write stall lands directly in the <300 ms p95 budget. Small fix, spec already wrote the backpressure policy.
- **Replay exists** ([recording/replayer.py](src/skyweave/recording/replayer.py)) and round-trips sessions through the pipeline — the right substrate for testing loss/jitter before writing network code (spec §9.4 says exactly this).
- **Transport is vestigial.** `transport/pack.py` is 19 lines of msgpack helpers; no bus, no socket, no source/sink interface. The packet contract is exercised as a *file format*, never as a *boundary*. The spec's "serialize even in-process so contracts are exercised from day one" is the standard defense against schema drift; skipping it means the first serialization stress test coincides with the first network debugging session.

### 3.9 The synthetic stack — `sim/`

The project's actual quality system: parametric camera rings + seven scripted trajectories (`scene.py`); idealized square patches with configurable noise/dropout/jitter/false-positives (`generator.py`); a rasterized-disk mode pushed through the **real** extractor (`rendered.py`); RMSE pass gates (`check.py`); a parameter autotuner; perturbation configs. This is "trust geometric synthetic tests, not photorealism" done properly. Its blind spots are the assumptions *shared* between simulator and system — zero distortion, identical timestamps, one object, and (critically) the *same* `CameraCalib` objects generating and scoring packets, so calibration error is structurally unrepresentable. Catalogued in §5-A12.

### 3.10 Operator runtime — `operator/`

A threaded loop that owns cameras, **rebuilds the whole pipeline when UI settings change** (immutable pipeline + revision bump, [runtime.py:224](src/skyweave/operator/runtime.py) — a clean pattern), runs motion → fusion per frame, JPEG-encodes previews at ~8 Hz, and publishes JSON `VizFrame`s into shared state for the aiohttp server.

Two scaling notes: every frame does full pydantic `model_dump(mode="json")` of tracks/measurements/volumes plus `model_copy` header rewrites in stress mode — pydantic in the hot loop is a known CPU tax (§6.5). And preview (resize + ChArUco detect + JPEG encode) shares the loop thread with fusion, so a slow preview frame delays tracking — against the spec's own "debug video must never block tracking" posture.

---

## 4. Spec-vs-reality scorecard

| Spec area (MVP ID) | State | Explanation |
|---|---|---|
| Message schemas (MVP-02) | ✅ solid | pydantic models + msgpack round-trip, tested |
| Config (MVP-03) | ⚠️ partial | Flat sim-centric config vs. the spec's nested `mvp.yaml`; no per-camera source schema |
| Geometry (MVP-06) | ⚠️ | Correct pinhole; **distortion never applied** at runtime (§3.1) |
| Frame diff (MVP-09) | ✅ | Three backends + merge/fill options |
| KNN / coherence / ROI mask (MVP-10/11/14) | ❌ | Not implemented — the sky noise-rejection toolkit (§3.2) |
| Motion patch encoding (MVP-13) | ⚠️ | `rle_u8` only; `png_gray`/`sparse_xy` absent; 225-px decimation undocumented in spec |
| In-process bus (MVP-16) | ❌ | Replaced by direct calls (§3.8) |
| Time aligner (MVP-17) | ⚠️ | Stateless; no async/late handling; ignores clock-domain fields (§3.7) |
| DDA + scorer (MVP-19/20) | ✅ | Plus a Numba fast path beyond spec |
| Peak extraction (MVP-21) | ✅⚠️ | Soft-argmax beats spec; percentile-over-top-K coupling (§3.4); covariance shape wrong (§5-A6) |
| Weavefield history/decay (MVP-22) | ❌ | No ring buffer; one volume per frame; `decay_s` hardcoded 1.0 |
| Triangulator (MVP-23) | ⚠️ | Midpoint not DLT+L-M; isotropic covariance (§3.5) |
| Kalman filter (MVP-24) | ✅⚠️ | Clean and correct; gate/scale constants drifted to mask covariance issues (§3.6) |
| Track manager (MVP-25) | ❌ | **Single-target**, no association (§3.6) |
| Calibration (MVP-26/27/28) | ✅ | ChArUco intrinsics + extrinsics + live capture — beyond MVP scope |
| Recorder (MVP-33) | ⚠️ | Present, but synchronous flush-per-write, not the async flight recorder (§3.8) |
| Replay (MVP-34) | ✅ | Present and used |
| Viz server (MVP-30) | ✅ | aiohttp + operator UI |
| V1 edge / turret contracts (MVP-36/37) | ❌ | No `edge/` or `turret/` packages exist |

**Reading the scorecard:** the *geometric core* is real, tested, and partly optimized — effort went *deeper* than required on calibration tooling and synthetic validation. The *distribution scaffolding* was skipped. Rational sequencing for a demo, but the remaining work is concentrated in exactly the systems layer the Jetson/edge deployment needs.

---

## 5. The voxel-scoring assumption audit

Every assumption baked into the voxel-scoring setup: why it holds in the demo, why it may fail at the real goal, how research/industry handles the same problem.

Framing note: the deepest question is whether a **metric voxel grid is the right primitive at all**. A grid is intuitive and superb for visualization, but it commits you to fixed metric resolution over a fixed volume. The alternatives that far-field systems actually use — rays + range shells, inverse-depth bins, transient local grids — abandon that commitment. Keep this tension in mind throughout.

---

### A1 — A dense world grid is the right representation

**Assumed.** A fixed-origin, fixed-dimension dense grid ([grid.py](src/skyweave/rayweave/grid.py)); room preset 96×96×64 ≈ 590k voxels; dense `(n_cameras, *dims)` tensors per frame ([scorer.py:139](src/skyweave/rayweave/scorer.py)).

**Why it works now.** Room scale, 5 cm voxels, few cameras → a few MB and milliseconds; the touched-voxel list keeps *work* sparse even though *memory* is dense.

**Why it fails at the goal.** A 2×2×1 km airspace at even 5 m voxels is 16M voxels; at resolution matched to a 1-pixel bearing it explodes into billions. Memory is dense even when work is sparse. The spec (§1.2, §7.3: "do not use a full uniform sky grid") and your architecture note both forbid this. The `pixel_plane_crossing` preset (42×24×14 m at 0.5 m, [runtime.py:763](src/skyweave/operator/runtime.py)) is the current ceiling — still backyard geometry.

**Real-world comparison.** The canonical scaling wall of **occupancy grids** (Elfes/Moravec) and **space carving / visual hulls** (Kutulakos & Seitz 2000). Everyone who scaled them moved to **sparse voxel hashing/octrees** (KinectFusion → Nießner's voxel hashing, OctoMap), **local grids around hypotheses**, or **no grid** (continuous estimators over rays). Radar — the field whose geometry most resembles yours — never grids the sky.

**Recommendation.** Treat the dense grid as a *room-scale demo primitive*. For V1: detect → rays → cheap candidate intersection (pairwise closest-points) → *small transient grid around each candidate* → refine → discard. Grid as scratchpad, never world model.

---

### A2 — Additive evidence along the entire ray is a good scoring function

**Assumed.** Each ray adds weight to *every* traversed voxel ([numba_scorer.py:107](src/skyweave/rayweave/numba_scorer.py)); consensus enforced only afterward by the support gate.

**Why it works now.** With well-separated cameras and one clean target, only voxels near the true intersection survive the gate; the intersection lights up.

**Why it is fragile.** A ray paints its entire line of sight, so the post-gate survivor is not a point — it is the **intersection volume of the view cones**, an elongated lozenge along depth when baselines are narrow. The peak inside is set by voxel-count geometry, not true convergence: with 2 near-parallel cameras the "peak" can sit tens of cm off in depth *with zero pixel noise*. And addition is not consensus — one camera's mass can dominate.

**The two research-grade alternatives:**

| Alternative | Mechanism | Pros | Cons |
|---|---|---|---|
| **Log-odds / multiplicative fusion** | Per-camera evidence combined as sum of log-likelihoods | Genuine consensus; the Bayesian-correct occupancy update used since Elfes; "camera saw nothing" becomes usable negative evidence | Must model visibility/occlusion or one blind camera vetoes everything |
| **Back-projection (space-carving direction)** | For each *voxel*, project its center into each camera and test the motion mask | No ray smear; "seen by K of N" falls out naturally; **embarrassingly parallel on GPU** — one thread per voxel, zero atomics | Cost ∝ voxel count → demands A1's local-grid discipline; masks must be resident per camera |

**Recommendation.** Flip the formulation for the GPU path: **back-projection over a local grid, fused with log-odds** — simultaneously the sounder model and the GPU-natural one (§7.5). Keep the forward-DDA additive scorer as the CPU reference oracle; the dual-backend pattern already supports this.

---

### A3 — Per-camera pixel-count normalization is the right weighting

**Assumed.** Each camera's weights normalized to sum to 1 ([scorer.py:71](src/skyweave/rayweave/scorer.py)).

**Why half-right.** Prevents "the loudest camera dominates" — good — but *erases absolute confidence*: a crisp 200-pixel silhouette and 3 marginal pixels contribute identical mass. Detection quality is created in the extractor and discarded at the scorer boundary.

**The phantom knob.** Spec/old configs mention `normalize_by_ray_length`; **no ray-length normalization exists**. A ray crossing the grid's long diagonal deposits ~3× the mass of one clipping a corner — total evidence depends on where the grid box sits, an uncontrolled geometric bias.

**Recommendation.** Explicit layered weighting: `pixel_weight × blob_quality × camera_trust` (quality from contrast/coherence, not raw area — §3.2; trust from calibration residual + sync error). Mandatory once a precise turret camera fuses with coarse wide-field nodes.

---

### A4 — Binary support counting is a sufficient consensus test

**Assumed.** A voxel survives if ≥ `min_supporting_cameras` cameras have *any* positive score there ([scorer.py:84](src/skyweave/rayweave/scorer.py)). One grazing decimated pixel counts like a dead-center silhouette.

**Why brittle.** Rays are *lines*, and unrelated lines pass near each other constantly in 3D. With N cameras, spurious 2-camera near-intersections grow ~O(N²): rare at 3 cameras, every-frame at 10+. A binary gate cannot distinguish "two cameras glanced here" from "two cameras strongly agree."

**Recommendation.** Soft support: K cameras *each above a per-camera evidence floor*, or threshold on consensus mass (e.g. second-smallest per-camera score). Falls out automatically under log-odds (A2) — another argument for it.

---

### A5 — The evidence-blob centroid is the object's location

**Assumed.** Measurement = soft-argmax centroid of the evidence component ([peaks.py:45](src/skyweave/rayweave/peaks.py)).

**Why biased for point targets.** Per A2 the blob is the elongated cone-intersection; its centroid is the middle of the lozenge, not the ray-convergence point unless geometry is symmetric. A *bias*, not noise — the Kalman filter cannot average it away.

**The irony worth internalizing.** For a *point-like* target, **plain triangulation beats the voxel peak** — it solves directly for convergence rather than taking the centroid of a discretized smear. Your architecture note concludes this. For an *extended* target (close drone spanning many pixels) the voxel/hull picture wins, because the object genuinely occupies a region.

**Recommendation.** The Mode A / Mode B split, made executable: angular-size-aware estimator selection. Point-like → bearings/triangulation (with proper covariance); extended → voxel/hull centroid. The Weavefield remains the always-on *evidence, association, and initialization* layer in both modes — an honorable role, and arguably the project's real product.

---

### A6 — Covariance from blob spread is valid measurement uncertainty

**Assumed.** Covariance = weighted blob spread + `voxel²/12` ([peaks.py:47](src/skyweave/rayweave/peaks.py)).

**Why wrong.** Blob spread measures *evidence fuzziness*; measurement uncertainty should measure *how well the camera geometry constrains the position*. Two cameras 10° apart have a true ellipsoid hugely elongated along depth; blob spread under-reports that anisotropy and tells the filter depth ≈ bearing. The filter over-trusts depth, mis-gates, and operators compensate by inflating `measurement_var_scale` and loosening the gate — which §3.6 shows is exactly what happened.

**Real-world comparison.** Multi-view systems propagate pixel noise through the measurement Jacobian: `R = σ_px² (JᵀJ)⁻¹` — the spec's own Appendix C formula; GDOP from GPS applied to cameras. Vicon/OptiTrack and photogrammetry all do this.

**Recommendation.** Geometry-derived `R` for voxel peaks *and* triangulation; then restore χ² = 11.345 and delete `measurement_var_scale`. Highest-leverage correctness fix after distortion — every gating/fusion decision flows through `R`.

---

### A7 — Per-frame scoring with no temporal accumulation

**Assumed.** Each frame scored from scratch; `decay_s` hardcoded 1.0; no ring buffer; `max_peaks` defaults 1.

**Why this undercuts the project's own thesis.** The pitch is a *4D* Weavefield; the temporal dimension does not exist in code. Beyond branding, this forfeits the strongest sky-goal tool: **faint-target detection lives in temporal integration.** A 1–2-pixel marginal-SNR target is invisible per-frame and obvious accumulated along consistent space-time paths over 10–30 frames — this is **track-before-detect**, standard in radar/IR point-target tracking, same principle as long-exposure astronomy. A decayed Weavefield (`W_t = λ·W_{t−1} + E_t`) is the cheapest TBD implementation: one fused multiply-add over the touched set.

**Recommendation.** Implement the ring buffer + exponential decay (MVP-22). Cheap, the actual novelty, and the feature distinguishing "voxel triangulation with extra steps" from "an evidence field worth the name."

---

### A8 — Uniform metric voxels match the sensor

**Assumed.** One `voxel_size_m` everywhere.

**Why mismatched.** A pixel subtends constant *angle*, not constant *distance*. A 5 cm voxel at 2 m is resolved by many pixels (wastefully fine); at 500 m it is a fraction of a pixel (meaninglessly fine while absurdly expensive). Uniform metric voxels are efficient only when volume depth ≪ distance — true indoors, false for sky.

**Real-world comparison.** Why monocular SLAM adopted **inverse-depth parameterization** (Civera et al. 2008) and why your note proposes **range shells**: bins uniform in angle and inverse depth match a bearing sensor's information geometry — uncertainty is roughly isotropic *in parameter space* even when wildly anisotropic in XYZ.

**Recommendation.** Beyond room scale: score in `(az, el, 1/depth)`, or keep rays first-class and voxelize only locally around candidates (A1). The single biggest representational change between demo and sky-capable V1 — schedule it as such.

---

### A9 — All packets in a frame are simultaneous (alignment is free)

**Assumed.** The aligner takes the *mean* timestamp and scores all packets as simultaneous ([aligner.py:38](src/skyweave/fusion/aligner.py)).

**Why it matters.** Rays from non-simultaneous observations of a mover intersect *behind* the truth. Quantified (`Δpx ≈ f·ω·Δt`): a 10 m/s indoor throw at 2 m is ω≈5 rad/s; 15 ms intra-window skew ≈ 75 px of inconsistency at f=1000 px — catastrophic, and invisible because sim timestamps are identical by construction. Correct treatments: **ray retiming** (predict each ray to a common epoch using track velocity — what Hawk-Eye-class systems do) or **asynchronous filter updates** (each measurement at its own timestamp — standard multi-sensor fusion). Mean-timestamp simultaneity is the worst option and survives only because sim never exercises it.

---

### A10 — One object, and evidence segmentation is trivial

**Assumed.** `max_peaks=1`; single TrackManager; connected components assumed to separate objects.

**Why it breaks.** Two objects with overlapping view-cones merge into one component (the gate keeps the union); one object fragmented by decimation splits into two. No peak-to-track association layer absorbs either error. Multi-target isn't an MVP requirement — but the *schemas* support it while the *logic* doesn't, the kind of latent mismatch that costs a rewrite. The fix is well-trodden: K peaks → gating matrix → Hungarian → per-track KF with birth/death (SORT's architecture with your existing filter).

---

### A11 — Evidence decimation (225 px) is harmless

**Assumed.** Patches capped at 225 sampled pixels regardless of true blob size ([motion.py:33](src/skyweave/camera/motion.py)).

**Why to keep but refine.** The cap is the right *kind* of constraint — §7.3 shows it is almost exactly the MTU budget. But uniform-stride sampling is row-major-biased and treats all pixels equally. Better: sample highest-diff pixels or the silhouette *boundary* (for hull-style scoring the boundary carries all the information), and carry true area separately so confidence doesn't conflate "sampled size" with "real size."

---

### A12 — The synthetic gates validate the system

**Assumed.** Passing `sim-check` RMSE (0.20 m) means the pipeline works.

**Why half-true.** The synthetic discipline is genuinely good — but the simulator shares the system's assumptions: zero distortion, identical timestamps, one object, uniform clean patches, and the *same* `CameraCalib` objects generating and scoring (calibration error is structurally unrepresentable). The autotuner then optimizes *inside* this agreeable world. **Add adversarial axes:** perturb the scorer's extrinsics vs the generator's, desynchronize per-camera timestamps, inject a second object, apply synthetic distortion. Each is a few lines in `generator.py` and converts the sim from confirmation tool to falsification tool.

---

### Voxel-scoring audit — bottom line

The current engine is a **correct room-scale, close-range, synchronized, single-object, additive estimator** — every qualifier an assumption above. The defensible target architecture is the one your own note wrote down:

> broad 2D detection → bearing rays → coarse candidates (ray intersection / range shells) → **transient local grid** per candidate → log-odds/back-projection scoring with temporal decay → geometry-derived covariance → multi-target tracker,

with the Weavefield repositioned from "the estimator" to "the evidence, association, initialization, and visualization layer" — a role in which its genuine strengths survive and none of its biases (A2, A5, A6) leak into the metric output.

---

## 6. How the code is written — implementation quality audit

Separate from the math: an audit of the *code itself* — structure, duplication, library use, hot-loop discipline — against how optimized real-world pipelines are written, and against what the final edge/center architecture will demand.

### 6.1 What is already genuinely good

Credit first, because these are habits to protect during any refactor:

- **The domain core is small, pure, and typed.** `geom.py` (83 lines), `dda.py` (80), `grid.py` (58), `aligner.py` (43), `triangulator.py` (53) are side-effect-free functions over numpy arrays with full type hints and frozen dataclasses. This is exactly the shape numerical code should have: trivially testable, trivially portable to Numba/CUDA.
- **The reference/optimized dual-backend pattern** (`ScorerBackend` Protocol, [scorer.py:29](src/skyweave/rayweave/scorer.py)) is how serious numerical libraries are built — OpenCV's universal-intrinsics dispatch, FFTW's planner, BLAS reference vs tuned. The python↔numba parity tests make the optimized path falsifiable. This is the single most valuable structural asset in the repo.
- **Good hot-loop instincts exist where it counts:** in-place F/Q matrix updates in the Kalman filter ([kalman.py:209](src/skyweave/fusion/kalman.py)), stamp-clearing instead of re-zeroing in the Numba scorer, the grab-all-then-retrieve-all camera read pattern ([check_common.py:132](src/skyweave/camera/check_common.py)) that minimizes inter-camera capture skew — a subtle and correct multi-camera trick most first implementations miss.
- **Stage timing instrumentation** is threaded through both drivers (`stage_ms` dicts, `PipelineStatus` fields) — the profiling substrate already exists.
- **The settings-revision/pipeline-rebuild pattern** in the operator ([runtime.py:110](src/skyweave/operator/runtime.py)): treat the pipeline as immutable, rebuild on config change rather than mutating live objects. Clean, and it eliminates a whole class of "half-applied settings" bugs.

### 6.2 Duplication: three pipelines, five percentiles

The codebase has reached the size where copy-paste has begun compounding:

- **The fusion pipeline is assembled and driven in three places** — [check.py:50](src/skyweave/sim/check.py), [replayer.py:36](src/skyweave/recording/replayer.py), [runtime.py:284](src/skyweave/operator/runtime.py) — each constructing `TimeAligner` + `RayweaveScorer` + `PeakExtractor` + `TrackManager` and hand-rolling the same per-frame orchestration (align → score → peaks → kalman → record/log). They have **already drifted**: the replayer feeds only `measurements[0]` to the tracker while check/runtime feed the full list; the replayer's pass criterion requires `len(peak_errors) == frames` while check has a `required_peak_frames` parameter; the replayer computes triangulation and discards the result ([replayer.py:63](src/skyweave/recording/replayer.py)).
- **Utility helpers are copy-pasted across modules:** `_percentile` ×5 (check.py, replayer.py, check_common.py, charuco_check.py, exporter.py), `_rmse` ×3, `_summary` ×3, `_elapsed_ms` ×3, `_fps_from_times` ×2, plus `effective_fps`/`record` duplicated between the two stats dataclasses inside check_common.py itself, and `_top_k_sparse` vs `_top_k_sparse_from_flat_indices` in the scorer.

**Why this matters more than style.** Duplication is how silent behavioral drift happens — the replayer/check divergence above means "replay reproduces the run" (spec acceptance #10) is *already* not strictly true. And `_percentile` shouldn't exist even once: `np.percentile` is correct, tested, and faster.

**The fix is one class.** A `FusionPipeline` owning the four stage objects with `process_frame(packets, ts_ns) -> FrameResult` (volume, peaks, measurements, track, stage timings) collapses three drivers into one engine plus three thin shells (sim feeder, replay feeder, live feeder). This is not cosmetic: that class **is** the unit that will run on the Jetson, and the seam where the transport layer (§7) plugs in. Real-world analogue: this is precisely ROS's node-vs-driver split, or the "library-first, binary-second" rule used in production perception stacks — the pipeline is a library; processes are thin compositions of it.

### 6.3 Module boundaries and the private-import tangle

The dependency *direction* in the application layer is inverted in places, and the convention for privacy has broken down:

- **16+ cross-module imports of underscore-private names**: `operator/runtime.py` imports `_resize_gray`, `_sharpness_score` from `calibration.charuco_live_capture` and `_read_live_frames` from `camera.check_common`; `operator/server.py` imports `_display_host` from `calibration.charuco_live_server`; CLI modules import `_capture_loop`, `_html_page`, `_run_live`, etc. In Python, the underscore is the *only* API boundary you have; once modules reach into each other's privates, every internal rename is a cross-package breaking change and the "what can I safely refactor?" answer becomes "nothing."
- **Layering inversions:** the operator (an application) depends on `camera.live_benchmark` (a diagnostic tool) for its default config path, and on the *ChArUco live-preview tool* for generic image helpers like resize/sharpness. Helpers born inside one tool became load-bearing for another, and the dependency graph now encodes development history rather than design.
- **God modules in the application layer:** `operator/runtime.py` (858 lines) mixes camera lifecycle, synthetic playback, preview rendering, ChArUco detection, fusion orchestration, and telemetry; `operator/state.py` (773) mixes settings schemas, validation, thread synchronization, recording-session management, and JSON shaping; `operator/server.py` (778) similarly. Contrast with the domain core's 50–80-line modules — the discipline exists in the repo, it just stopped at the application boundary.

**The fix pattern (standard, cheap):** promote shared helpers into honest homes — `skyweave/util/stats.py` (percentile/rmse/fps), `skyweave/camera/preview.py` (resize/sharpness/jpeg), `skyweave/camera/reader.py` (the grab/retrieve multi-camera read) — with public names, and let `import linting` (ruff's `flake8-tidy-imports` banned-api, or `import-linter` contracts: *operator may not import calibration internals*) enforce the layering mechanically. Production codebases do not maintain layering by vigilance; they maintain it with a linter rule that fails CI.

### 6.4 Hand-rolled code vs. the libraries that already won

The repo hand-implements several things that mature, SIMD-optimized libraries already do. Each was presumably written to keep the no-OpenCV install path alive — a defensible goal for tests, but the pure-Python versions are 100–1000× slower and now constitute a *third* implementation to maintain alongside the OpenCV ones:

| Hand-rolled | Where | Library replacement | Notes |
|---|---|---|---|
| BFS connected components in Python | [motion.py:357](src/skyweave/camera/motion.py) | `cv2.connectedComponentsWithStats` (already a backend!) or `scipy.ndimage.label` | The OpenCV path exists; the Python path should be test-only, never `auto`-selected for production |
| Binary dilate/erode via disk-offset loops | [motion.py:468](src/skyweave/camera/motion.py) | `cv2.morphologyEx` (already used in the cv2 path) | Same triple-implementation pattern |
| Per-pixel Python RLE codec | [patches.py:8](src/skyweave/rayweave/patches.py) | Vectorized numpy RLE (`np.diff`/`flatnonzero` — ~10 lines), or `cv2.imencode('.png')` (the spec's `png_gray` mode), or `np.packbits` + zlib | `encode_rle_u8` loops per pixel *in Python* and is called per patch per frame on the hot path; decode builds a Python list. This is the patch wire codec — it must be fast on both edge and center |
| Sparse-dict BFS connected components in voxel space | [peaks.py:75](src/skyweave/rayweave/peaks.py) | `scipy.ndimage.label` over a small dense crop of the touched bounding box | Also removes the Python-loop `_local_peak_scores` triple loop (numpy slicing) |
| Per-pixel patch iteration with tuple lists | [scorer.py:242](src/skyweave/rayweave/scorer.py) | `np.nonzero` → stacked arrays end-to-end | Currently decodes to numpy, then degrades to a Python list of tuples, then re-converts to arrays for Numba |
| `_percentile` ×5, `_rmse` ×3 | various | `np.percentile`, `np.sqrt(np.mean(np.square(x)))` | — |
| Hand PGM writer, hand BGR→gray luma math | [check_common.py:210](src/skyweave/camera/check_common.py), [source.py:199](src/skyweave/camera/source.py) | `cv2.imwrite`, `cv2.cvtColor` | Fallbacks fine, but the float32 per-channel luma fallback allocates 3 full-frame float arrays per frame |

Two library-strategy calls worth making explicitly:

- **Decide where OpenCV is mandatory.** On every *deployment* target (Jetson, dev machines, even Rubik Pi) OpenCV is available and is the optimized implementation; on the *final RV1106 edge* none of this Python runs at all (§7.2). So the pure-Python fallbacks serve only `pip install skyweave` without extras — i.e., CI convenience. Recommendation: keep at most one fallback per operation, clearly marked test-only, and make `auto` refuse to silently select it in live modes (today `resolve_motion_backend` will happily run the BFS components on a live camera if cv2 is missing — a 10× frame-time regression that manifests as "the tracker is bad" instead of an error).
- **`filterpy` is effectively unmaintained** (last release 2018). The code already builds every matrix itself and uses filterpy only for the ~15-line predict/update algebra. Inline those two methods (or use the Joseph-form update for numerical robustness) and drop the dependency — fewer wheels to build for aarch64/Jetson, one less bitrot risk.

**The general principle** (how optimized real-world code does it): *libraries for the inner loops, your code for the orchestration.* OpenCV/NumPy/SciPy inner loops are SIMD-vectorized C that a Python loop will never approach; the value Skyweave adds is the pipeline, the geometry conventions, and the fusion logic — never the morphology kernel.

### 6.5 Hot-loop discipline: objects where arrays should be

The single biggest *systemic* performance pattern to fix: **pydantic models and Python object graphs flow through the per-frame, per-pixel, per-voxel paths.**

Evidence:

- The scorer consumes `MotionPacket` objects and iterates `packet.blobs` / patch pixels via Python attribute access per element; `_packet_evidence_pixels` returns a `list[tuple[float, float, float]]` that the Numba backend converts back into arrays through Python lists every frame ([scorer.py:190](src/skyweave/rayweave/scorer.py)).
- `_top_k_sparse` constructs up to **5000 pydantic `SparseVoxel` objects per frame** ([scorer.py:255](src/skyweave/rayweave/scorer.py)); the peak extractor then immediately re-converts them to numpy ([peaks.py:22](src/skyweave/rayweave/peaks.py)). At 30–100 Hz this is hundreds of thousands of validated-object constructions per second whose only purpose is to be turned back into arrays.
- The operator hot loop does `model_copy(update=...)` per packet per frame (stress mode) and `model_dump(mode="json")` of tracks/measurements/full volumes per frame ([runtime.py:684](src/skyweave/operator/runtime.py)).
- The recorder and logger flush per write inside the loop (§3.8).

**Why this is the pattern to care about.** Pydantic v2 validation is fast *for a validation library*, but it is still ~µs per object versus ~ns for array element access; allocation churn also defeats CPU caches. Production Python perception/data pipelines (and pydantic's own docs) converge on the same rule: **validate at the boundary, then use plain arrays inside.** This is "parse, don't validate" applied to realtime: a packet is parsed *once* on ingest into an efficient internal form, and pydantic never appears again until serialization.

**The concrete internal form (and it already half-exists):** the Numba backend's `_rays_from_packets` builds exactly the right thing — flat parallel arrays `(camera_slot[], u[], v[], weight[])`, structure-of-arrays (SoA). That should be the *canonical post-ingest representation* of an aligned frame, produced once by the aligner:

```python
@dataclass(frozen=True)
class EvidenceBatch:          # one aligned timestep, SoA
    ts_ns: int
    camera_slot: np.ndarray   # int16  (n_pixels,)
    u: np.ndarray             # float32 (n_pixels,)
    v: np.ndarray             # float32 (n_pixels,)
    weight: np.ndarray        # float32 (n_pixels,)
    per_camera_meta: ...      # counts, quality, capture_ts per slot
```

Every consumer — numpy scorer, Numba scorer, future CuPy/CUDA scorer, triangulator — takes `EvidenceBatch`. This is data-oriented design (the game-engine/HFT discipline) translated to Python: *long-lived preallocated arrays, mutated in place, objects only at the edges.* It is also, not coincidentally, **exactly the memory layout a GPU wants** (coalesced loads over parallel arrays) and exactly what the Jetson ingest path should decode network packets into directly (§7.4) — so adopting it now means the GPU port and the network port are layout-compatible for free.

Similarly on the output side: a `WeavefieldVolume` should internally be `(flat_indices: int32[], scores: float32[])` + grid spec, converted to pydantic/JSON only at the recorder/viz boundary, and only for the downsampled subset that actually ships.

### 6.6 Concurrency model

Current: one OS thread for the operator loop (camera + motion + fusion + preview), aiohttp serving from the asyncio thread, shared state behind a `threading.Lock`/`Condition` ([state.py:277](src/skyweave/operator/state.py)). For one host and three cameras this is defensible and the lock discipline looks correct (snapshots under lock, work outside).

What it won't survive: preview encoding and ChArUco detection share the fusion thread (a slow JPEG delays tracking — §3.10); camera `read()` is serial across cameras within the loop; and the GIL caps any CPU-bound Python at ~1 core. The standard shape for this class of system — and the recommendation for the Jetson (§7.6) — is **process-per-concern with explicit queues**: capture/ingest, fusion, ops (record/serve/preview), connected by the same packet contract. Threads-with-locks is the hardest concurrency model to keep correct as a codebase grows; message-passing processes are both faster (no GIL) and easier to reason about, which is why ROS 2, GStreamer pipelines, and every production vision stack are structured that way.

---

## 7. Scaling to the final architecture: RV1106 patch streams → Jetson + CUDA

The committed topology (spec §1.3/§9.2, high-level spec, and your direction): **many deployed RV1106 nodes, each with an SC3336 MIPI-CSI camera, doing on-node motion extraction and sending `MotionPacket`s — blobs + small mask patches — over Ethernet through a PoE switch to one Jetson** that aligns, scores (CUDA), tracks, records, and serves the UI. A 100 fps global-shutter turret camera attaches directly to the Jetson. This section architects code around that, stage by stage.

### 7.1 The compute split, settled by arithmetic

SC3336 raw video: 2304×1296×8 bit×30 fps ≈ **716 Mbit/s — 7× a 100M link** before overhead, so raw frames are physically impossible; H.26x compression would fit but smears the exact tiny targets you care about and adds latency (spec §1.3 forbids depending on it). A `MotionPacket` with 8 blobs and 225-px RLE patches is **~2–6 KB ≈ 0.5–1.5 Mbit/s per node at 30 Hz** — 50+ nodes share the uplink with headroom. Meanwhile 256 MB of edge RAM cannot hold a voxel grid. The thin-edge/fat-center split (spec MVP-D13) is therefore not a preference but a consequence.

**Real-world validation:** this "features-to-center" topology is how deployed multi-sensor localization systems work — Hawk-Eye centralizes per-camera ball observations, not video; ShotSpotter ships acoustic detections with timestamps; multi-radar fusion exchanges *plots*, not returns (the radar literature's "plot-level fusion"); Vicon/OptiTrack cameras compute 2D centroids *on the camera* and centralize only points — almost exactly the Skyweave plan.

One refinement: **cheap per-camera temporal coherence belongs on the edge** even in the thin-edge model (spec §6.8 says the same) — a blob that appears in 1 of 5 frames should never cost network or central compute. Thin edge ≠ zero intelligence; it means no 3D and no state the center depends on.

### 7.2 The edge node: what the RV1106 code should be

**The Python stack does not go to the edge.** An RV1106 (single Cortex-A7 @ ~1.2 GHz, 256 MB, no Python ecosystem worth shipping) running pydantic+numpy is a non-starter, and OpenCV is heavier than the job needs. The edge program is a small C/C++ daemon — and this is normal: in production camera systems the edge firmware is always a distinct, minimal implementation of a shared wire contract. The architecture that makes this safe:

1. **The wire contract is the product of the Python repo.** `MotionPacket`/`MotionPatch` schemas (plus a frozen byte-level wire spec, §7.3) are the deliverable; the Python motion extractor becomes the **reference implementation**; the C edge daemon is validated against it with **golden fixtures** — recorded raw frame sequences → expected packet bytes — exactly the oracle pattern the scorer backends already use. This is how embedded teams keep firmware and server honest (conformance suites, not hope).
2. **Capture path: use the SoC's hardware, skip the abstractions.** SC3336 → Rockchip ISP (rkisp/RKMPI or plain V4L2) delivering **NV12 into DMA buffers**. Two free wins the current OpenCV path doesn't have: the **Y plane of NV12 *is* the grayscale image** — zero conversion cost (versus `cvtColor` per frame today), and the ISP can emit a **downscaled stream simultaneously** — run detection at e.g. 1152×648 (4× fewer pixels) and crop patches from the full-res Y plane only where blobs are found. Detect-coarse, sample-fine is the standard ISP-era trick.
3. **Frame diff in NEON.** `|a−b|≥T` over 0.75 MP (downscaled) at 30 fps is ~22 M byte-ops/s/stage; NEON processes 16 pixels/instruction (`vabdq_u8` + `vcgeq_u8`), so the whole motion stage is a few percent of the A7. No OpenCV needed — ~200 lines of C, or `libyuv` if you want it for free. Connected components on the *sparse thresholded points* (blobs are tiny by construction) rather than full-image labeling.
4. **Timestamping at the kernel.** Use the V4L2 buffer timestamp (`VIDIOC_DQBUF`), not userspace read time, and run PTP/chrony to discipline the clock (§7.7 of the previous revision, now folded into §7.8). Fill `clock_domain` and `time_sync_error_ms` honestly — the center will finally read them.
5. **Output: one UDP datagram per camera frame** (§7.3), plus a 1 Hz health packet (temps, fps, drop counts, sync error). The daemon is single-threaded with a capture→process→send loop and a watchdog; total complexity on the order of 1–2 kLOC of C. Future option: the RV1106's 0.5-TOPS NPU can run a tiny patch classifier (bird/plane/noise) later without changing the architecture — it just adds a field to the packet.

### 7.3 The wire format: design the datagram around the MTU

The current msgpack-of-pydantic encoding is fine for recording, but the network hot path deserves a deliberate layout:

- **One frame = one datagram, ≤ ~1400 bytes.** Staying under Ethernet MTU avoids IP fragmentation, which on lossy links turns one lost fragment into a lost packet. Do the math: header (~32 B fixed: magic/version/source-id/frame-seq/capture-ts/sync-err/flags) + 8 blobs × ~24 B + patches. The existing 225-pixel patch cap RLE-encodes to ≤ 675 B worst case — **the current evidence budget already fits the MTU almost exactly.** Make that alignment explicit and enforced rather than accidental: the patch budget *is* the MTU budget.
- **Fixed little-endian binary layout** (a hand-packed struct or FlatBuffers if you want schema evolution tooling) rather than msgpack for the hot path: the Jetson ingest can then decode straight into the SoA `EvidenceBatch` arrays (§6.5) without per-field dict allocation — at 50 nodes × 30 Hz = 1500 packets/s, parse cost matters in Python and vanishes in a struct view. Keep msgpack/pydantic for the control plane, config, health, and recordings, where flexibility beats nanoseconds.
- **Sequence numbers per source** (already in `PacketHeader`) drive loss accounting; **no retransmission** — stale evidence is worthless, which is exactly the case UDP is for. Reliability lives in the *fusion logic* (tolerate missing cameras, inflate uncertainty), not the transport. This is the same reasoning that makes RTP/UDP the universal realtime media transport.
- **Debug video (when wanted) is a separate, lower-priority stream** — MJPEG/H.264 over a different port, allowed to lag and drop, never consumed by fusion (spec §9.3).

### 7.4 The Jetson ingest: from datagrams to `EvidenceBatch`

The center-side shape that makes everything downstream simple and GPU-ready:

```
UDP socket(s) ──► ingest thread/process
                    decode struct → validate → per-source ring buffer (preallocated)
                          ▼
                  stateful TimeAligner (watermark close on wait_ns)
                          ▼
                  EvidenceBatch (SoA arrays, §6.5)  ──► pinned staging buffer
                          ▼                                   ▼
                  CPU fusion path (oracle)            CUDA scorer (stream-overlapped)
```

- **Per-source ring buffers, preallocated** — the aligner pops from them with a watermark rule ("close window W when every live source has delivered past W, or `wait_ns` expires"). This is the streaming-systems watermark pattern (Flink/Beam use the identical concept for out-of-order event time) and is the correct generalization of the spec's §7.2 aligner.
- **Decode directly into SoA.** Because the wire layout (§7.3) and the internal layout (§6.5) are co-designed, ingest is a `np.frombuffer` view + a few column copies — no object graph, no pydantic until the recorder.
- **Batch per aligned window, not per packet.** The scorer (CPU or GPU) consumes one `EvidenceBatch` per timestep; per-camera metadata (capture_ts, quality, sync error) rides along for weighting (§5-A3) and retiming (§5-A9).

### 7.5 CUDA on the Jetson: formulation first, kernel second

- **Forward ray-DDA is GPU-hostile** (divergent ray lengths → warp divergence; concurrent voxel writes → atomic contention). **Back-projection is GPU-native**: one thread per candidate voxel projects into ≤N cameras, samples each camera's motion mask from a texture, combines (log-odds), writes once — no atomics, coalesced, uniform work. This scatter→gather flip is the canonical GPU transformation (it is why GPU volume renderers march per-pixel instead of splatting per-voxel), and it is the same formulation §5-A2 prefers *statistically*. One change fixes both.
- **Pipeline the frame:** upload masks/`EvidenceBatch` on one CUDA stream while scoring the previous window on another; on Jetson, exploit **unified memory** (CPU/GPU share DRAM — use pinned/managed buffers and skip x86-style explicit copies). Use **NVJPEG/NVDEC** for the turret camera's 100 fps MJPEG (CPU-decoding that costs an entire core) and VPI/NPP for any image preprocessing.
- **Implementation ladder:** CuPy back-projection behind the existing `ScorerBackend` Protocol first (fastest port, vectorized gather; `RawKernel` escape hatch), Numba-CUDA or C++/pybind11 only if Orin profiling demands it; PyTorch only if you later want *learned* scoring weights (autodiff through `grid_sample`), which the high-level spec muses about.
- **Sober reality check:** the Orin Nano Super is memory-bandwidth-bound (~102 GB/s shared) for grid work, and the end-to-end <300 ms p95 budget will be dominated by capture, decode, network, and Python serialization — not the scorer. The Numba scorer is already ms-scale at room resolution. **Extend the existing stage-timing instrumentation across the live path and profile on the Orin before writing any kernel** — consistent with the README's own "benchmark target hardware before optimizing."

### 7.6 Process architecture on the Jetson

One Python process doing ingest + fusion + GPU + preview + recording + serving will fight the GIL and itself. The production shape (mirrors ROS 2 / GStreamer / every deployed perception stack):

- **ingest** process: sockets → validation → shared-memory rings (or ZeroMQ IPC) of `EvidenceBatch`es. Realtime priority.
- **fusion** process: aligner → scorer (owns the GPU) → peaks → tracker. Realtime priority, zero disk I/O.
- **ops** process: recorder (bounded queue, drop-policy per spec §11.5), health aggregation, viz/HTTP server, debug-video handling. Best-effort priority; allowed to lag; killing it must not affect tracking.

Connect all three with the same Source/Sink packet interface (§6.2's `FusionPipeline` is the core of the fusion process). The discipline this buys: the realtime path has *no* shared fate with preview encoding, disk flushes, or a browser reconnect storm — the exact failure couplings the current single-loop operator has (§3.10, §6.6).

### 7.7 Code-architecture moves, in dependency order

1. **`FusionPipeline` class** unifying the three drivers (§6.2) — the unit of deployment and testing. ~1–2 days, removes drift, creates the seam.
2. **`MotionPacketSource`/`Sink` transport interface** — in-process queue impl (MVP), UDP impl (V1), replay impl (exists in spirit). Route even the synthetic generator through it with serialization on, so the contract is exercised continuously.
3. **Stateful, watermark-based `TimeAligner`** (§7.4), unit-tested against replayed sessions with injected reorder/loss/jitter — *before* any socket code exists.
4. **`EvidenceBatch` SoA internal form** (§6.5) — pydantic at boundaries only. This simultaneously fixes the hot-loop churn, prepares the CUDA layout, and defines what ingest decodes into.
5. **Freeze the wire spec** (§7.3) + golden conformance fixtures for the future C edge daemon.
6. **Helper/module hygiene** (§6.3): promote shared utilities, eliminate private cross-imports, add an import-linter contract to CI.
7. **Multi-peak → Hungarian → multi-track** (§5-A10); **health/heartbeat + per-source loss/latency accounting** (spec §9.4).

### 7.8 Time synchronization — the make-or-break

Bearing-fusion error from clock skew is `Δpx ≈ f·ω·Δt`: a 30 m/s drone at 100 m (ω≈0.3 rad/s) at f≈1000 px turns 10 ms of skew into ~3 px of disagreement — comparable to every other error source combined. Staged plan, cheapest first:

1. **Now (software, ~free):** chrony on the LAN (~1 ms on a quiet switch); **consume `time_sync_error_ms` — inflate measurement covariance with the projected `(f·ω̂·Δt)²`**. The field exists on every packet and nothing reads it; this one change makes the system degrade gracefully instead of mysteriously. An afternoon of work.
2. **V1: PTP (IEEE 1588)** over the managed PoE switch — software PTP ~10–100 µs, hardware-stamped ~1 µs; either is far below frame interval. Check the RV1106 MAC's timestamp support.
3. **V1: kernel capture timestamps** (V4L2 `DQBUF`) on both edge and Jetson — removes the largest jitter term (userspace read delay) for free.
4. **V1.5: hardware trigger / PPS.** No clock protocol fixes *asynchronous exposure* — uncoordinated cameras are up to half a frame apart forever. A shared trigger or GNSS-PPS-disciplined trigger (the STM32 idea) collapses exposure skew to µs; this is what mocap rigs and Hawk-Eye do, and the only path to close-fast-drone precision.
5. **Rolling shutter** (SC3336): each row exposes at a different time (up to ~10–20 ms across the frame). Once timestamps are honest, the fix is cheap: record the blob's row, correct capture time by `row × line_time`, feed the corrected per-observation time to the (asynchronous, §5-A9) filter. Punt for MVP; mandatory for fast close targets.

---

## 8. Prioritized recommendations

Ordered by leverage (correctness and V1-readiness per unit of effort):

| # | Change | Why it's high-leverage | Effort |
|---|---|---|---|
| 1 | Undistort pixels before ray construction (§3.1) | Removes a systematic bias invisible to all current tests, corrupting every real-camera measurement | Low |
| 2 | Geometry-derived (Jacobian/GDOP) covariance for peaks + triangulation; restore χ² gate, drop `measurement_var_scale` (§5-A6, §3.6) | The KF is only as good as `R`; current `R` has the wrong shape and the gates were detuned to hide it | Medium |
| 3 | Consume `time_sync_error_ms` → covariance inflation; V4L2 buffer timestamps; chrony (§7.8) | Cheapest defense against the #1 multi-camera failure mode; pure software | Low |
| 4 | `FusionPipeline` class unifying check/replay/operator drivers (§6.2) | Stops behavioral drift (already happening); creates the deployable unit and the transport seam | Low-Medium |
| 5 | Stateful watermark `TimeAligner` + Source/Sink transport interface, tested via replay with injected loss/jitter (§3.7, §7.4, §7.7) | The hard prerequisite for any distributed deployment; biggest sim-only gap | Medium |
| 6 | `EvidenceBatch` SoA internal form; pydantic at boundaries only; kill per-frame `SparseVoxel` object churn (§6.5) | Fixes the systemic hot-loop pattern, and pre-aligns memory layout with both the wire format and CUDA | Medium |
| 7 | Async flight recorder + non-flushing logger (§3.8) | Removes disk I/O from the latency budget before embedded storage makes it bite | Low |
| 8 | Temporal Weavefield ring buffer with decay (§5-A7) | Delivers the project's actual novelty and track-before-detect faint-target capability | Medium |
| 9 | Library hygiene: vectorized/PNG patch codec, scipy/cv2 components, drop filterpy, dedupe `_percentile` et al., test-only fallbacks never auto-selected (§6.4) | Deletes the slowest hand-rolled loops on the hot path and a third maintenance burden | Low-Medium |
| 10 | Module hygiene: shared util/preview/reader homes, no private cross-imports, import-linter in CI (§6.3) | Makes every later refactor safe; layering by linter, not vigilance | Low |
| 11 | Multi-peak → Hungarian → multi-track manager (§5-A10, §7.7) | Stops phantoms corrupting the real track; assembly of existing parts | Medium |
| 12 | Adversarial sim axes: calibration error, per-camera desync, second object, synthetic distortion (§5-A12) | Converts validation from confirmation to falsification before parameters are trusted | Low-Medium |
| 13 | Freeze MTU-shaped wire spec + golden fixtures for the C edge daemon (§7.2, §7.3) | Lets edge firmware development start independently and stay provably compatible | Medium |
| 14 | CuPy back-projection + log-odds behind `ScorerBackend`; profile end-to-end on Orin first (§5-A2, §7.5) | The GPU-natural formulation that also fixes the ray-smear bias | Medium-High |
| 15 | Transient local grids + inverse-depth scoring for far targets (§5-A1, A8) | The core representational change between room demo and sky-capable V1 | High |

**The throughline:** the geometric core is sound and unusually well-tested for a project at this stage, and the code is clean *in the small*. The work ahead is (a) closing correctness gaps that synthetic-only validation structurally cannot see, (b) consolidating the application layer before duplication drift compounds, and (c) building the distribution, data-layout, and far-field scaffolding the demo never needed but the RV1106→switch→Jetson system requires. Almost all of it was anticipated in `SPEC_MVP.md` and the architecture-review note — the value now is sequencing, and the recognition that the wire format, the SoA internal layout, and the CUDA memory layout are *one design decision*, best made once, now.
