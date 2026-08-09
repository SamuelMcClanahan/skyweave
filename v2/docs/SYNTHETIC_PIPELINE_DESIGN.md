# Synthetic frame pipeline design

**Date:** 2026-07-26
**Status:** Proposed design, pre-implementation
**Scope:** Blender scene → sensor model → RV1106 detector → Observation2D
**Relationship to EXP-001:** this document specifies *how* Tier 1–3 frames are
produced and injected. EXP-001 remains the experiment; this is its plumbing.

---

## 1. What this pipeline is for

One sentence: **produce frames with known 3D truth that are wrong in
realistic ways, so the detector and geometry stack can be scored and
stress-swept before real hardware exists.**

It answers:

- Does the detector find the target, and how accurately is the centroid placed?
- What false proposals does a realistic sky produce, *with labels*?
- How does 3D error and covariance degrade as noise, pose, and timing error grow?
- Does GMM2 on the real board behave like the host reference on identical input?

It does **not** answer: SC3336 image quality, RV1106 PTS semantics, real field
pose accuracy, weather performance, or true end-to-end latency. Those are
measured on hardware, not simulated. See §8 for the split.

---

## 2. Design principles

These are the decisions that constrain everything below.

1. **Randomize, don't replicate.** The goal is a detector that survives a
   *swept range* of sensor error, not one tuned to match one measured sensor
   at one temperature. Fidelity to a point is worth less than robustness
   across a bracket.
2. **One measurement anchors the sweep.** Conversion gain (e⁻/DN) is the only
   parameter that must be measured, because without it noise magnitudes have
   no scale. Everything else is swept within plausible bounds.
3. **The renderer emits radiance, never images.** Blender's job ends at linear
   scene radiance. All sensor and lens behavior lives in a separate,
   unit-testable module.
4. **Labeled negatives are a first-class output.** Knowing which blobs are
   *not* the target is the thing real sky footage can never give us. Confuser
   objects carry truth labels just like the target does.
5. **Every threshold is config, not a constant.** Sim validates structure and
   robustness range; real data sets the numbers. Any baked constant is a
   sim2real bug waiting to happen.
6. **Truth and estimator models are separate serialized objects.** Never
   shared by reference. (Carried over from EXP-001.)

---

## 3. Architecture

```text
scene manifest (YAML)
  │
  ├─► [A] Blender generator (bpy, headless)
  │        exact-resolution linear EXR per camera per frame
  │        + truth sidecars (camera poses, trajectory, exposure windows)
  │
  ├─► [B] Sensor model (numpy, host)
  │        lens → sampling → radiometry → noise → mosaic → ISP → U8 Y
  │        + per-camera independent noise seeds
  │
  ├─► [C] Injection
  │        C0: host reference detector (no board)
  │        C1: app-layer Y into RV1106 IVE GMM2/CCL   ← first build
  │        C2: RAW into ISP readback                   ← parked
  │        C3: CSI-2 emulation                         ← parked
  │
  ├─► [D] Detector scorecard  (§7 — the GMM2 validator)
  │
  └─► [E] Observation2D → existing central fusion engine
```

Stages A, B, and D are independently runnable and independently testable.
That matters: stage B can be validated against measured sensor data with no
renderer, and stage D can score real-sky footage with no truth.

---

## 4. Stage A — Blender scene generation

### 4.1 Approach

Render the **full scene from N camera POVs**. No tiling, no compositing
tricks in the first build. A Nishita sky plus a small object is close to the
cheapest scene Cycles can render, and full-scene rendering is what makes the
confuser objects in §4.4 possible.

All N cameras render the **same scene frame time** — switch `scene.camera`,
do not advance the frame. Truth trajectory is evaluated once per frame time
and shared by all cameras.

### 4.2 Render settings — the ones that matter

These are not preferences. Getting any of them wrong silently invalidates the
radiometry:

| Setting | Value | Why |
|---|---|---|
| View transform | **Standard** (not AgX/Filmic) | AgX tone-maps and destroys linear radiance. Single most common failure. |
| Output | OpenEXR half-float, linear | 18.1 MB/frame at 2312×1304 RGB; f32 RGBA would be 48.2 MB |
| Denoising | **Off** | Denoisers destroy noise statistics and erase 2-px targets |
| Sampling | Native resolution, high sample count | Cycles already jitters sub-pixel; separate supersampling is only needed for PSF convolution (§5.2), and 2× suffices |
| Motion blur | **On**, shutter matched to exposure time | At 800 ft a 250 m/s target smears 2.3 px per ms of exposure |
| Rolling shutter | Cycles top-bottom, duration = measured line time × height | Optional in first pass; the truth sidecar must carry row times regardless |
| Seed | Explicit per frame, animated off | Determinism |
| Blender version | **Pinned** | Version changes alter output; the dataset ID must include it |

### 4.3 Cameras

Intrinsics are set from the manifest via `sensor_width`, `lens` (mm),
`sensor_fit`, and `shift_x`/`shift_y` for principal point offset. Render
**pinhole with FOV margin** — geometric distortion is applied as a
post-process warp in stage B, not in Blender.

Two reasons: the distortion model is then exactly the math the estimator
inverts (and can be perturbed independently), and Cycles has no native
Brown-Conrady support anyway. The margin prevents the warp from sampling
outside the rendered frame.

### 4.4 Scene contents

Tiered so the first dataset stays clean:

**Tier 1 (clean):** Nishita sky (sun elevation/azimuth from lat/lon/date/time),
one target, nothing else.

**Tier 1.5 (confusers)** — each carries a truth label:

| Confuser | Placement | Why it matters |
|---|---|---|
| Insects | 1–5 m, fast, heavily defocused | Worst real-world false-proposal source; trivially cheap |
| Birds | 50–300 m, animated | The realistic ambiguous case for association |
| Cloud edges | Rotating CC0 HDRI dome | What actually breaks background models; far cheaper than volumetrics |
| Sun glare | Nishita sun sweep | Forward scatter and AE transients |
| Treeline | Frame bottom, wind-animated | Only for low-elevation cameras |

**Asset fidelity note:** at 2–9 px, a textured plane is indistinguishable from
a detailed model. Only the 5 m "resolved" target case needs real geometry.
Poly Haven (CC0) and BlenderKit's free tier cover the HDRIs and what geometry
is needed.

### 4.5 Cost and storage

Do not estimate — **render one frame, time it, multiply by 900**
(10 s × 30 fps × 3 cameras). That single benchmark decides whether cloud GPU
is needed better than any prediction.

Storage for a 10 s 3-camera clip:

| Format | Per frame | 900 frames |
|---|---|---|
| EXR f16 RGB (radiance) | 18.1 MB | 16.3 GB |
| EXR f16 mono | 6.0 MB | 5.4 GB |
| U8 Y (post sensor model) | 3.0 MB | 2.7 GB |
| EXR f16 RGB, 2× oversampled | 72.4 MB | 65.1 GB |

Use compressed half-float EXR and short clips. Radiance EXRs are
intermediates — once a dataset's U8 Y output is frozen and hashed, the
radiance layer can be deleted and regenerated from the manifest if needed.

---

## 5. Stage B — Sensor model

A pure host-side module. Input: linear radiance + camera pose + exposure
window. Output: U8 Y (and optionally packed RAW10). Renderer-agnostic, so
Isaac Sim can feed the same chain later.

### 5.1 Chain

```text
linear radiance (EXR)
 → relative illumination / vignetting
 → PSF convolution                    (at 2× if enabled, else skip)
 → geometric distortion warp          (Brown-Conrady)
 → decimate to sensor grid            (box filter = pixel fill factor)
 → radiance → electrons               (aperture, QE, exposure, pixel area)
 → shot noise                         (Poisson)
 → dark current + DSNU
 → PRNU
 → full-well clip
 → analog gain                        (AE controller output, §5.3)
 → read noise                         (Gaussian, gain-dependent)
 → black level offset + ADC quantize  (10-bit)
 → Bayer mosaic                       (BGGR — verify against driver)
 → [RAW10 pack]  or  → demosaic → CCM → gamma → RGB→Y → U8 Y
```

### 5.2 Parameters: measured vs. swept

| Parameter | Source | Notes |
|---|---|---|
| Conversion gain (e⁻/DN) | **Measured** | The one anchor. ~20 flat-frame pairs, one afternoon |
| Read noise | Measured once, then swept ±2× | Gain-dependent |
| Full well | From the same PTC | |
| Line readout time | Measured (screen-banding test) | Also feeds rolling-shutter truth |
| Dark current, DSNU, PRNU | **Swept** | 0 → plausible upper bound |
| QE, black level | Datasheet / assumed | Low sensitivity |
| Vignetting | Swept (cos⁴ + extra term) | |
| PSF sigma | Swept 0.4–1.5 px | |
| Distortion k1,k2,p1,p2 | Swept | Estimator gets a *perturbed* copy |
| AE convergence rate | Swept | §5.3 |

### 5.3 Why the mosaic and AE stages are not optional

Two effects that look like implementation detail but are sim2real-relevant:

**Bayer round-trip.** A 2-px target hitting a mosaic and coming back through
a demosaicer gets smeared, and its centroid acquires a bias that depends on
sub-pixel phase relative to the CFA. Skipping mosaic→demosaic makes tiny
targets behave better in sim than they ever will in reality. Keep the stage,
make it toggleable, and *measure* how much it costs — that measurement is
itself a useful result.

**AE.** Because the C1 injection path bypasses RKAIQ entirely, the detector
will never see an exposure transient unless the sensor model produces one.
AE fighting the background model is one of the most likely real-world
failure modes. Model a simple AE controller with configurable convergence
speed and inject step changes (cloud crossing the sun).

### 5.4 Seeds

Independent noise seed per camera, derived deterministically from
`(dataset_seed, camera_id, frame_sequence)`. Correlated noise across cameras
is unphysical and would make triangulation look better than it is. Keep
render sampling noise well below injected sensor noise.

---

## 6. Stage C — Injection

**C0 — host reference (build first).** U8 Y into a host MOG2-like detector.
No board required. Establishes the scorecard and the expected numbers.

**C1 — app-layer Y into the RV1106 (first hardware build).** Push U8 luma
frames into the IVE GMM2/CCL path over Ethernet or from local storage. Gets
real detector behavior, real memory pressure, real thermals, real
detection-stage latency. Skips ISP, AE (modeled instead, §5.3), and PTS
(fabricated by the harness with configurable offset/drift/jitter).

**C2 — RAW into ISP readback.** Parked at user request. Note for later:
readback is a real ISP30 feature, but PTS becomes whatever is stuffed in the
buffer, and streaming frames from DDR competes for bandwidth on a 128 MB
board. It buys RKAIQ realism, not timing realism.

**C3 — CSI-2 emulation.** Parked. The only path to real PTS/VI semantics;
costs an FPGA with D-PHY TX plus an I²C register model faithful enough for
driver probe.

---

## 7. Stage D — The GMM2 validator

**Requirement: simple.** One command, one JSON, six numbers, a pass/fail.

```text
skyweave-score <clip> <detector-config> → scorecard.json
```

### 7.1 The scorecard

**Quality** (requires truth):

| Metric | Definition |
|---|---|
| `recall` | Fraction of eligible frames with ≥1 component matching the truth blob |
| `centroid_err_px` | Mean and p95 distance, matched component → truth centroid |
| `false_per_frame` | Mean count of components not matching any truth object |

**Health** (no truth needed — runs on real footage too):

| Metric | Definition |
|---|---|
| `occupancy_pct` | Mean foreground pixel fraction. The divergence alarm |
| `warmup_frames` | Frames until occupancy is within 20 % of steady state |
| `component_count` | Mean components per frame |

**Performance:** `fps`, `peak_mem_mb`, `latency_ms_p95`, `soc_temp_c`.

Reference pixel rates for sizing: 90.4 Mpx/s at full resolution and 30 fps,
27.6 Mpx/s at 1280×720, 6.9 Mpx/s at 640×360. GMM2 state at 3 modes × 4 B is
36.2 / 11.1 / 2.8 MB respectively — the reason processing resolution is an
accuracy decision, not just a performance one (see review finding F2).

### 7.2 Provisional pass criteria

On the clean golden clip: `recall` ≥ 95 %, `occupancy_pct` < 0.5 %,
`false_per_frame` < 2, no divergence over a one-hour loop, `centroid_err_px`
p95 within the budget set by the Tier-0 error study.

These are config values in the scorer, not constants in the code.

### 7.3 The comparison loop

The same scorer runs on host MOG2 and on real IVE GMM2 with identical input.
**Do not require identical masks** — require comparable scorecards. A
divergence in `centroid_err_px` or `false_per_frame` between the two is a
finding; a divergence in individual mask pixels is not.

### 7.4 Hybrid clips — the cheap sim2real bridge

The highest-value validation available right now, and it needs no renderer:

**Composite a synthetic moving blob into real sky footage captured out a
window.** Real clouds, real AE transients, real sensor noise, real
compression artifacts — with *known* target pixel positions. This is the only
way to get truth on real backgrounds, and it directly attacks the sim2real
gap for the detector specifically.

It runs today on a laptop and a board pointed at the sky. Treat hybrid clips
as a permanent tier of the validation set, not a stopgap.

### 7.5 Golden clip

One short canonical clip, its manifest hash checked into the repo alongside
the existing `golden/peak_baselines.json` convention. Every detector change
is scored against it. Regression detection is then a diff of two JSONs.

---

## 8. What is simulated vs. measured

| Question | Path |
|---|---|
| Noise realism bracket | Measured anchor (§5.2) + sweep |
| Detection + 3D accuracy vs. truth | Full synthetic pipeline |
| False proposals, *labeled* | Synthetic confusers (§4.4) + hybrid clips (§7.4) |
| GMM2 FPS / memory / thermals | Real board, any input — camera out a window |
| False proposals, unlabeled realism | Real sky, camera out a window |
| E2E latency, packets, PTS | Real camera + real network. Not simulated |

The pipeline exists for rows 1–3. Rows 4–6 are cheaper and more truthful
measured directly, and must not be faked.

---

## 9. Determinism contract

Dataset ID = hash of (manifest, Blender version, sensor-model version,
dataset seed, git revision). Same inputs → byte-identical U8 Y output.

Each dataset ships: `dataset.json`, `truth/cameras.json`,
`truth/trajectory.jsonl`, `truth/labels.jsonl` (target *and* confusers),
`sensor-model.json`, and per-frame sidecars carrying exposure start/mid/end,
row times, and the truth target state at row time.

The estimator's calibration is a **separately serialized, separately
perturbed** object. Never the same file the renderer read.

---

## 10. Build order

1. Sensor model (stage B) with swept parameters, unit-tested against
   synthetic inputs. No renderer needed.
2. Scorecard (stage D) + hybrid clip generator. Validate on real sky footage
   out a window with a composited synthetic blob. **Runs today.**
3. Conversion-gain measurement to anchor the sweep.
4. Blender generator (stage A), Tier 1 clean scene only, 3 cameras, one
   trajectory. Benchmark one frame before committing to a full clip.
5. C0 host reference detector scored on the clean synthetic clip.
6. C1 injection into the RV1106; compare scorecards against C0.
7. Tier 1.5 confusers; measure false-proposal rates with labels.
8. Tier 2 perturbation sweeps feeding the central geometry stack.

Steps 1–3 need nothing but the laptop and the board already on hand.

---

## 11. Parked and open

**Parked:** ISP readback (C2), CSI-2 emulation (C3), synthetic RAW as the
primary path, Isaac Sim, volumetric clouds, rain/snow.

**Open:**

1. Does the Bayer round-trip meaningfully bias 2-px centroids? (§5.3 — this
   is measurable in stage B alone, before any board work.)
2. What processing resolution does GMM2 actually sustain, and what does the
   downscale cost in centroid precision? (Review finding F2.)
3. Is the SC3336 CFA pattern BGGR as the Luckfox issue suggests? Verify
   against the driver before the mosaic stage is frozen.
4. How much does render sampling noise need to be suppressed before it stops
   contaminating the injected sensor-noise sweep?
