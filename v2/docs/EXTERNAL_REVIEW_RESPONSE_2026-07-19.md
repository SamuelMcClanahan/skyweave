# External review response: Skyweave v2 architecture

**Date:** 2026-07-19
**Reviewed:** SKYWEAVE_EXTERNAL_REVIEW_PACKET.md, SKYWEAVE_PROJECT_GUIDE.md,
EXP-001, IMPLEMENTATION_FRAMEWORK.md, RESEARCH_REPORT.md (checkout state:
pre-implementation, docs only)
**All §14 quantitative anchors were independently recomputed. All check out.**

---

## 1. Executive verdict

The core project and the first milestone are technically coherent. The
architecture's central disciplines are correct and unusually well stated:
edge proposes / center decides, one fusion engine for sim/replay/live,
voxels as search rather than state, one refined measurement per pixel set,
truth separated from estimator calibration. Nothing in the packet is fatally
wrong for the 800 ft milestone.

Two things must change before implementation. First, the 5 m acceptance gate
is internally inconsistent with parts of the design space it is supposed to
gate (finding F2): it fails by the packet's own arithmetic at the 5 m
baseline, and it silently assumes sub-pixel centroid precision if detection
runs at any resolution the RV1106 can actually afford. Second, the covariance
semantics decision (F3) cannot stay open, because the Tier 2 acceptance
criteria ("covariance coverage is reported") are unevaluable until it is made.

The high-altitude regime should be reclassified now from "no accuracy claim
accepted" to "range estimation physically excluded at useful precision"
(F1). This costs nothing — the regime was already bearing-dominant in spirit —
and prevents the 5 m number from ever migrating upward.

---

## 2. Findings, ordered by severity

### F1 — Physical limit: high-altitude range estimation is excluded, not merely unproven

Severity: fatal physical limitation *for that regime's range output only*.

The packet's own numbers: 5 m range σ at 25,000/35,000 ft with a 137 m
baseline requires 2.4/1.2 arcsec (11.8/6.0 µrad) total relative bearing
error. Verified. Compare against the environment:

- Daytime atmospheric turbulence over a slant path produces image motion
  (tilt jitter) of tens of µrad; astronomical sites at night achieve
  1–3 arcsec *seeing* under far better conditions than a field deployment.
- Differential refraction across two ground stations, mount thermal drift,
  and calibration residuals each plausibly contribute ≥ 0.1 mrad
  (≈ 20 arcsec) of slowly varying bias.

With a realistic 0.1 mrad relative error (itself demanding), σ_range is
42–83 m — and bias, not noise, so it will not average down quickly. A
plausible field bias of 0.5 mrad gives 200–400 m range error.

**Resolution:** move "high-altitude output is bearing-plus-coarse-range
(hundreds of metres at best)" from §3's "may need to remain" language into
§17 *Settled*. Design the track output schema so range validity/precision is
a per-axis field, since cross-range at 0.1 mrad is only ~0.8–1.1 m and
remains genuinely useful.

### F2 — Gate/design contradiction: the 5 m gate vs. baseline and processing resolution

Severity: architecture problem; fix before EXP-001 is frozen.

Two independent collisions:

1. **Baseline.** At B = 5 m, one full-resolution pixel of bearing error gives
   5.39 m depth σ (verified). The 5 m gate cannot pass at the 5 m baseline
   even in the clean case unless centroids are substantially sub-pixel. The
   sweep is fine; the *named gate geometry* must be B ≥ 10 m, or the gate
   must be defined per-baseline.

2. **Processing resolution.** The GMM2 memory numbers (§7, verified: 36 MB
   at full res for state alone, before CMA, ISP buffers, CCL, Linux) mean the
   board will realistically detect at 1280×720 or 640×360. At 640 wide, one
   *processed* pixel is 1.64 mrad = 0.40 m cross-range, and one
   processed-pixel centroid error at B = 10 m is **9.7 m depth** — double the
   gate. The gate therefore silently assumes one of: (a) detection at ≥
   1280 wide, (b) sub-pixel weighted centroids that recover most of the
   downscale loss, or (c) full-res crop re-centroiding around each detection.
   None of these is currently written down, and (b)'s achievable precision
   for a 2-px target is exactly open question §19.9.

**Resolution:** add a centroid-precision line item to the error budget
(e.g. "≤ 0.5 full-res-pixel-equivalent, 1σ, at the named target size and
SNR") and make EXP-001 Tier 1 measure centroid repeatability *at the
detection resolution actually planned for the board*, not only at 2312 wide.
State the gate as range/cross-range separately (the packet already does this
for high altitude; apply it at 800 ft).

### F3 — Covariance semantics must be decided now, not listed as tension

Severity: architecture problem; blocks Tier 2 acceptance criteria.

"Covariance coverage is reported" (§15) is unevaluable until §19.5 is
resolved, because coverage against truth depends on what the covariance
claims to cover. Shared camera-pose, clock, and lens errors are *biases*
across all measurements from a camera; stuffing them into a per-measurement
diagonal R will make NEES/coverage tests fail structurally, and inflating R
until they pass destroys the filter's gating usefulness.

**Resolution (smallest defensible):** define the 3×3 covariance as
**conditional on the current calibration and clock mapping** (propagated
pixel noise + timing jitter only), and add a separate reported
systematic-bound field (per-axis, derived from calibration residuals). The
KF consumes the conditional covariance; the systematic bound travels with
the result for consumers and for honest reporting. Marginalizing calibration
uncertainty properly is a later, evidence-justified upgrade (it correlates
successive measurements and effectively requires a smoother or
consider-parameter filter).

### F4 — Non-simultaneous rays: choose the contract now

Severity: architecture problem; the packet correctly flags it (§9, §19.3)
but must not enter implementation unresolved.

**Resolution (smallest defensible):** for the first milestone, refined-XYZ
KF with a **quantified simultaneity gate** at track birth: accept a
multi-ray solve only when v_max · Δt_capture < k · expected depth σ (k ≈
0.5). Concretely: at 100 m/s and 2.7 m depth σ, Δt_capture ≤ ~13 ms — under
half a 30 FPS frame period, so this is feasible *only if* node sync is a few
ms or better, which couples this gate directly to the timing measurements
(F5). For established tracks, motion-compensate each observation to the
filter reference time using the predicted velocity (the innovation then
correctly reflects prediction error). Sequential per-bearing EKF updates
dissolve the problem entirely and are the right *second* contract —
EXP-001's plan to run both modes behind one interface is correct; but declare
mode 1 the acceptance-gated contract and mode 2 the comparison.

### F5 — PTS-to-exposure binding is the single most decision-relevant unknown, and it is cheap to measure

Severity: reasonable provisional choice that needs measurement — but the
measurement is mis-ordered (see §6 below).

The packet is right that no amount of chrony/PTP fixes an unknown
PTS↔exposure offset (§9). What it understates: this is measurable in an
afternoon with no hardware modification. Point the camera at an LED blinked
in a coded pattern by a host-clock-driven GPIO (or a GPS-disciplined
blinker). The frame in which the pattern transitions, plus the row position
of the transition (bonus: this also measures line readout time and validates
the rolling-shutter model), binds PTS to physical exposure time and directly
measures jitter and drift. This experiment should be board-track step 2, not
an implicit part of a later soak.

### F6 — Acceptance statistics are not yet falsifiable

Severity: architecture problem, small but blocking the report generator.

"95 % of eligible frames" needs: eligible = frames after detector warm-up in
which the truth target subtends ≥ N processed pixels and lies in the shared
FOV of ≥ 3 cameras. "Below 5 m" needs: per-axis (range/cross-range) RMSE
and p95 over a named segment of a named trajectory, excluding birth.
Covariance coverage needs a number (e.g. 3D NEES within χ²₃ 5–95 % bounds
for ≥ 90 % of updates, given the F3 conditional definition). Freeze these
alongside the frozen EXP-001 scene (§19.6) — they are one decision, not two.

### F7 — Mount stability is missing from the calibration story

Severity: risk that could invalidate the field (not synthetic) milestone.

§12 calibrates orientation once, then treats extrinsics as immutable
versioned data. But 1 px = 0.45 mrad ≈ 0.026°. Outdoor mounts (printed
enclosure, pole or tripod, sun on one side) drift by more than that
thermally within a day, before wind loading. The calibration architecture
needs a *stability* answer, not just an accuracy answer: either periodic
re-solving against optical control points, or continuous background-feature
/ celestial monitoring. Night star fields are free, effectively-at-infinity
control points and the SC3336 is marketed as starlight-capable; a
star-transit check could both validate initial orientation and measure
drift. Add mount-stability measurement to the field prerequisites and
consider the BNO055's real role to be coarse tamper detection only (it
cannot see sub-milliradian drift).

### F8 — Minor factual and consistency items

- **AS5408** (guide §Hardware, packet §5): no such part. Almost certainly
  **AS5048A** (14-bit SPI/PWM magnetic encoder); fix before it reaches a BOM.
- Voxel table mixes decimal GB (§14 figures are decimal-consistent;
  fine, but label GB vs GiB once — 1280³ FP32 is 7.81 GiB).
- The 450 ft copper run (§8) plus PoE at 5 V/3.5 A: note the splitter's
  *input* is 48 V PoE so the run itself is standard 802.3af/at — the >100 m
  channel-length problem is real, the voltage-drop problem is not; the
  packet's proposed mitigations (mid-span switch, fibre) are the right ones.
- v1's "82 passing tests" is stale evidence and correctly labeled as such;
  rerunning it is cheap and should happen before v1 is used as a
  characterization oracle for detector comparisons.
- The pixel-to-voxel reference repo's Apache-2.0-plus-restriction license:
  the reference-only treatment is correct; a clean-room reimplementation of
  DDA traversal is trivial and avoids the question entirely.

---

## 3. Assumption audit

| Assumption | Status |
|---|---|
| Pixel/bearing/baseline/orientation arithmetic (§14) | **Established** (independently recomputed, all correct) |
| Voxel memory and full-frame bandwidth arithmetic | **Established** (recomputed; 724 Mbit/s, table correct in decimal GB) |
| Uncompressed full frames cannot ride a 100 Mb/s node link | **Established** |
| GMM2-class background model is a sound first sky detector | **Plausible** (standard practice; clouds/insects/AE remain measured risks) |
| Clone board runs vendor IVE GMM2/CCL as documented | **Unsupported** until the board spike; the packet says so — concur |
| RV1106 PTS closely tracks exposure time | **Unsupported**; cheap to measure (F5) |
| chrony/NTP on isolated wired LAN reaches ~ms offsets | **Plausible** (typical sub-ms to low-ms; must be measured on the actual switch) |
| F9P RTK gives cm-class static translations | **Plausible to established** under open sky with fixed solutions |
| Optical control points / bundle adjustment reach ≤ ~0.2 mrad orientation | **Unsupported** — and load-bearing for the 5 m field goal (F7) |
| Orientation, once calibrated, stays within budget | **Unsupported and likely false** outdoors without monitoring (F7) |
| 5 m gate achievable at the named geometry | **Contradicted at B = 5 m; conditional at B = 10 m** on sub-pixel centroids (F2) |
| 5 m-class range at 25–35 kft | **Contradicted** by atmospheric and calibration physics (F1) |
| Sub-1200-byte observation datagrams | **Established** (listed fields fit comfortably in 200–400 B of protobuf) |

---

## 4. Geometry and accuracy audit

All §14 anchors reverified: 0.453 mrad/px; 0.110 m cross-range/px; 9/45 px
targets; depth σ per pixel 5.39/2.69/1.08/0.54 m at B = 5/10/25/50 m;
4.26/10.64 m lateral per 1°/2.5°; high-altitude angles 1.03°/0.74°;
42/83 m σ_R at 0.1 mrad; 2.43/1.24 arcsec for 5 m; voxel table correct;
724 Mbit/s. No arithmetic errors found.

Two additions the packet should carry:

1. **Triangulation angles at 800 ft** are 1.2°/2.3°/5.9°/11.7° for the
   sweep. Below ~2° the depth direction is poorly conditioned and the
   refinement's covariance will be extremely anisotropic — the B = 5 m case
   is best treated as a designed weak-geometry test rather than a
   candidate named-gate geometry.
2. **Processed-resolution equivalents** (F2): at 640 wide, 1.64 mrad and
   0.40 m cross-range per processed pixel; depth 9.7 m per processed pixel
   at B = 10 m. Every accuracy statement should name the resolution at which
   the centroid was measured.

---

## 5. Architecture assessment

**Keep (these are the design's spine):** edge-proposes/center-decides; one
fusion engine for sim, replay, and live; observations that preserve raw PTS
plus clock domain; voxels as candidate search, never persistent world state;
the single-measurement rule (voxel center and refined point never both enter
the filter); read-only visualizer; immutable versioned calibration;
truth/estimator model separation.

**Change:**

- Define the tracker's measurement interface as polymorphic from day one:
  `(time, value, covariance, measurement-model tag)`. This makes the
  XYZ-vs-bearing choice (F4, §19.2) a configuration, not a rewrite, and it
  is exactly how the later turret bearing enters without special-casing.
- Covariance object per F3: conditional covariance + systematic bound as
  two named fields, never summed.
- Demote the BNO055 from "orientation prior" to "tamper/movement alarm" in
  the contract; its data should never enter the geometry path (F7).

**Remove/defer from the first milestone's critical path:**

- The voxel oracle as an EXP-001 *acceptance* dependency. For one compact
  target seen by three cameras, direct hypotheses fully suffice; the voxel
  path's value question (§19.8, Q9) is a comparison study, not a gate.
  Keep it in EXP-001 as the planned comparison — just do not let the
  milestone's pass/fail depend on it.
- Mode B synthetic Bayer: already deferred; concur.

**Boundaries that are correctly drawn and commonly gotten wrong elsewhere:**
the refusal to let the edge classify or permanently suppress; the refusal to
average voxel peaks; the refusal to treat camera count as confirmation.

---

## 6. Implementation-sequence assessment

The two-track order is sound. Four changes, all risk-reducing:

1. **Board track: run the LED time-binding experiment (F5) immediately after
   first capture works** (new step 3). It is the cheapest measurement with
   the largest downstream effect: its result sets the simultaneity gate
   (F4), the lateness window, and whether PPS/VSYNC hardware is needed at
   all — decisions that otherwise stay open through the whole central track.
2. **Central track: insert a Tier-0 Monte Carlo error-budget study before
   freezing the EXP-001 named geometry** (between current steps 4 and 5).
   Pure math over baseline × pixel-noise × pose-error × clock-error grids;
   hours of work; it selects the named gate geometry with evidence and
   settles F2 quantitatively before any Blender frame is rendered.
3. **Freeze F6 statistics definitions in the same review unit as the frozen
   EXP-001 scene**, before the report generator exists.
4. **Rerun the v1 suite** (install pytest; minutes) before v1 outputs are
   used as characterization baselines in the detector comparison.

Everything else — contracts first, analytic before rendered, rendered before
networked, CPU oracle before CUDA — is in the right order.

---

## 7. Experiment plan (ranked by information per unit cost)

1. **Tier-0 Monte Carlo error budget** (hours, pure math). Decides named
   geometry, tests 5 m gate feasibility vs. centroid precision and pose
   error, produces the sensitivity ranking everything else follows.
2. **Ghost-association Monte Carlo** (hours, pure math). Given k blobs per
   camera and the sweep geometry, how often do ≥ 3 rays intersect within
   gate tolerance by chance? Answers §18.7 and calibrates the confirmation
   gates before any tracker exists.
3. **LED/GPIO PTS-to-exposure binding on the clone board** (a day, bench).
   Answers §18.1 and most of the timing strategy; also measures line
   readout time for the rolling-shutter model.
4. **GMM2 resolution/model-count/memory/FPS sweep with one-hour soak**
   (days). Answers §18.2 and fixes the processing resolution, which F2
   makes accuracy-relevant, not just performance-relevant.
5. **24 h chrony offset/jitter log on the actual PoE switch topology**
   (passive). Sets the simultaneity gate and lateness window numbers.
6. **Real-sky clip study**: run existing outdoor sky footage (any webcam or
   the board itself pointed out a window) through the host reference
   detector. First empirical false-proposal rates for clouds, birds,
   insects, AE transients (§18.13) — long before weather hardening.
7. **Night star-transit orientation check** with one assembled node (one
   clear night). Validates the optical-control-point orientation path and
   measures mount thermal drift (F7) with zero surveying equipment.

---

## 8. Open questions that would materially change the design

1. **What centroid precision is achievable at the board's affordable
   processing resolution for the 2-px and 9-px targets?** (F2 / §19.9.)
   If ≥ ~0.5 full-res px is not achievable, the 5 m gate, the resolution
   choice, or the baseline must move.
2. **What does RV1106 PTS actually timestamp, and with what jitter?** (F5.)
   Determines the entire timing architecture between chrony-only and
   hardware-event territory.
3. **Does calibrated camera orientation stay within ~0.2 mrad over hours to
   days on the intended mounts?** (F7.) If not, calibration becomes a
   continuous estimation problem, which changes the architecture more than
   any other single answer.
4. **Does the exact clone expose IVE GMM2/CCL at all?** If not, the edge
   budget changes (software GMM at reduced resolution) and with it the
   observation rate and resolution decisions.

Questions deliberately *not* listed: voxel backend representation, protobuf
details, tracker upgrades, turret — none of their answers changes the first
milestone's design.
