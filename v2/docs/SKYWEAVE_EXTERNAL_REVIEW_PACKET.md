# Skyweave external architecture review packet

**Date:** 2026-07-19<br>
**Review target:** proposed Skyweave v2 architecture and implementation plan<br>
**Implementation status:** pre-implementation; this is a design and experiment review<br>
**Review status:** historical review input; reviewed on 2026-07-19

> The current post-review execution order is synthetic-first over real wiring.
> Read [SKYWEAVE_PROJECT_GUIDE.md](./SKYWEAVE_PROJECT_GUIDE.md) and
> [IMPLEMENTATION_FRAMEWORK.md](./IMPLEMENTATION_FRAMEWORK.md) for the active
> plan. This packet remains unchanged in substance so the external response can
> be understood against the material it reviewed.

## Instructions to the reviewer

Act as an independent technical reviewer. Do not merely summarize this packet
or agree with its recommendations. Look for incorrect math, hidden assumptions,
missing interfaces, weak experiments, unnecessary complexity, and risks that
could invalidate the project.

Treat this as a read-only review unless the user separately asks for changes.
Lead with concrete findings, ordered by severity. Distinguish among:

- a fatal physical or mathematical limitation;
- an architecture problem that should be fixed before implementation;
- a reasonable provisional choice that needs measurement;
- an optimization or feature that can wait; and
- a matter of preference rather than correctness.

When challenging a factual claim, show the calculation or cite a primary or
official source. When recommending a change, explain which measured failure it
prevents. Do not replace the design with a generic computer-vision stack
without comparing it against the actual cost, hardware, timing, and accuracy
constraints described here.

## 1. What Skyweave is

Skyweave is a low-cost, distributed flying-object detection and tracking
system. Several fixed camera nodes watch overlapping parts of the sky. Each
node performs inexpensive motion detection and sends compact, timestamped
image observations to a central Jetson. The Jetson combines those observations
to estimate:

- a target's 3D position;
- its velocity and track history;
- the cameras and observations supporting the estimate; and
- the uncertainty and quality of the result.

The human-authored idea began as:

```text
pixel -> ray -> rays are scored -> combined into voxels -> measured -> tracked
```

The purpose is to explore how much useful tracking can be obtained from cheap
camera nodes and central compute. It is not currently presented as a radar
replacement, an all-weather system, or a proven interception system.

## 2. Design authority and current status

The user's original notes are the design intent. The current research can
correct a physical misconception, but it should not silently replace the
user's goals or make scope decisions for them.

The existing v1 repository is characterization evidence. It contains a
working MVP shape, tests, calibration helpers, a voxel scorer, fusion code,
simulation code, recording/replay code, and visualization code. It is not the
architectural authority for v2.

The v2 workspace currently contains documentation and two empty code
placeholders. No new v2 detector, geometry system, packet protocol, voxel
backend, or tracker has been implemented. This review is therefore about the
proposed architecture, contracts, feasibility, experiments, and execution
order rather than code quality.

The v1 suite was previously reported as 82 passing tests on 2026-07-15. It was
not rerun while preparing this packet because the current default Python does
not have pytest installed. That older result is characterization evidence, not
verification of v2.

The intended engineering process is deliberately segmented: one bounded
question, reviewed semantics, tests, the smallest implementation, measured
evidence, and a human review before the next substantial chunk.

## 3. Operating regimes

The notes combine two different physical problems. They may share software and
hardware contracts, but they must not share unexamined accuracy assumptions.

### First controlled regime

- Target range: 800 ft / 243.84 m.
- Target: one controlled moving object.
- Cameras: at least three synthetic cameras initially.
- Baseline sweep: approximately 5, 10, 25, and 50 m.
- Illustrative sensor width and FOV: 2312 pixels and 60 degrees horizontally.
- Provisional clean-case goal: 3D error at or below 5 m for a named geometry.
- Purpose: prove the full software path and characterize sensitivity.

### Eventual high-altitude regime

- Target: large aircraft at roughly 25,000-35,000 ft slant range.
- Maximum originally imagined baseline: around 450 ft / 137 m.
- Output may need to remain bearing-dominant or have weak depth.
- Optics, atmospheric effects, target centroid definition, calibration bias,
  and timing may dominate.
- No high-altitude accuracy claim is currently accepted as proven.

### Later local-drone regime

- Shorter range and larger angular rates.
- Timing and rolling shutter may matter more than weak parallax.
- A high-rate global-shutter turret may add bearing and appearance evidence.
- Autonomous guidance or interception is explicitly outside the first MVP.

## 4. What the system should produce

The first useful system should report more than an XYZ coordinate. A valid
localization result should contain:

```text
reference time
position in a named world frame
full 3x3 position covariance
supporting observation IDs and camera IDs
per-camera residuals
triangulation angle and conditioning
capture-time spread and uncertainty
calibration/configuration revisions
status: tentative, confirmed, weak geometry, late, outlier, or rejected
```

The tracker should retain a six-state position/velocity estimate, covariance,
track lifecycle, observation provenance, and a bounded history. Classification
or motion profiling is supporting evidence, not a replacement for geometric
quality.

## 5. Proposed hardware

### Fixed camera node

- RV1106 clone board with approximately 128 MB DDR3L.
- SC3336 2312 x 1304 rolling-shutter sensor over two-lane MIPI CSI.
- Hardware IVE GMM2 and connected-component operations, if the exact clone,
  SDK, memory layout, and installed image support them as expected.
- BNO055 for rough installation orientation and mount-movement detection.
- 5 V / 3.5 A PoE splitter, outdoor Ethernet, and a sealed printed enclosure.
- TPU gaskets, waterproof coating, and a Lexan optical window.

The BNO055 is not intended as final camera orientation truth.

### Central node

- Jetson Orin Nano Super with 8 GB shared system/GPU memory.
- DC PoE switch with eight PoE ports and two SFP paths/adapters in the current
  concept.
- Storage for immutable recordings and deterministic replay.
- Optional travel router for monitoring.
- Battery and voltage conversion for eventual field tests.

### Later turret

- OV9281 global-shutter camera, targeted near 100 FPS if the complete path can
  actually sustain it.
- NEMA17 drive, ESP32-S3 controller, and AS5408 magnetic encoders.
- Timestamped bearing and appearance observations sent into the same central
  tracker.

The turret does not create instantaneous range unless it provides a meaningful
physical baseline. Its principal roles are higher-rate angular measurement,
reacquisition, and appearance/classification evidence.

### Calibration equipment

- ChArUco board for deployed-mode intrinsics and distortion.
- One ZED-F9P base plus a rover moved sequentially to static camera nodes for
  translation surveying.
- Optical control points, a known moving target, or bundle adjustment for
  camera orientation and antenna-to-camera lever arms.

## 6. Proposed end-to-end pipeline

```text
SC3336 exposure
  -> ISP Y/luma frame and sensor metadata
  -> IVE GMM2 foreground proposal
  -> threshold / morphology / connected components / persistence
  -> compact Observation2D
  -> UDP measurement ingest
  -> clock mapping, reorder handling, and event-time alignment
  -> calibrated pixel-to-world bearings
  -> direct pair/clique or sparse voxel hypotheses
  -> continuous robust multi-ray refinement
  -> covariance and quality gates
  -> Kalman/EKF target-state update
  -> immutable recording, metrics, and read-only visualization
```

The edge reports what it observed. The central node decides which observations
belong together and what they mean in 3D. Global identity and permanent target
suppression remain central.

## 7. Edge detection

The old MVP used adjacent-frame grayscale differences. That tends to detect
the leading and trailing edges of a moving object rather than a stable object
region.

The proposed baseline is GMM2 on Y/luma:

```text
Y frame
  -> adaptive GMM2 background model
  -> threshold or hysteresis
  -> morphology
  -> connected components
  -> camera-specific area/shape gates
  -> short temporal persistence
  -> Observation2D
```

GMM2 is an adaptive per-pixel mixture model, not one fixed averaged reference
frame. It proposes foreground; it does not identify birds, clouds, rain,
insects, shadows, or camera movement.

The initial edge should not permanently reject a bird or another real mover.
Optional cropped luma or masks may be sent separately for central debugging or
classification. A lost crop must not remove the required geometric metadata.

The official API makes a hardware experiment plausible, but not proven on the
clone. An approximate three-model state alone is around 36 MB at 2312 x 1304,
11 MB at 1280 x 720, and 2.8 MB at 640 x 360. These numbers exclude Linux,
camera/ISP buffers, factor images, foreground/background images, CCL buffers,
and contiguous-media-memory constraints.

## 8. Observation and transport contract

The required observation should preserve enough truth to reinterpret it later:

```text
schema version
node, camera, boot/session, frame, and local-track IDs
raw sensor/VI PTS and its clock domain
mapped common capture time and uncertainty
dequeue, publish, and central receive times
centroid in full-sensor pixel coordinates and 2x2 covariance
bbox, area, foreground statistics, and optional mask/crop reference
sensor size plus ROI/downscale mapping
exposure, gain, centroid row, and rolling-row metadata
calibration, configuration, firmware, and detector revisions
separate heuristic, geometric, and learned scores
validity and health flags
```

The current direction uses two communication planes:

- small, fresh UDP measurement datagrams with sequence/session IDs and no
  blocking retransmission of stale evidence; and
- reliable control/debug traffic for configuration, calibration, logs, and
  requested crops or video.

The provisional measurement-datagram target is below roughly 1200 bytes to
avoid common path-MTU problems. Full-resolution SC3336 U8 luma at 30 FPS is
about 724 Mbit/s before overhead, so normal uncompressed full-frame transport
cannot fit over one 100 Mb/s node link.

The proposed 450 ft copper run is also longer than the usual 100 m Ethernet
channel. Long field links require an actual cable/PoE soak, an intermediate
switch, fibre/media conversion, or a proven long-reach system.

## 9. Time and rolling shutter

Every timestamp must name both a physical event and a clock domain. The design
retains:

```text
sensor/VI PTS
frame sequence
node dequeue time
node publish time
central receive time
mapped common capture time
capture-time uncertainty
```

The first fallback is a wired isolated LAN, chrony/NTP, a fitted per-node
offset/rate mapping, and measured residual uncertainty. PTP is useful only if
the hardware exposes a usable clock. PTP or packet timestamps still do not
identify the sensor exposure unless the camera path connects them to an
exposure, frame-valid, VSYNC, or trigger event.

An MCU helps only if it observes or controls such a sensor event and shares a
time reference. Timestamping packet arrival with an MCU does not reconstruct
exposure time.

Packet timing alone may not cleanly separate clock offset from variable network
delay. The design still needs a measured answer for whether chrony/NTP plus the
available PTS is sufficient, or whether a shared optical/electrical event,
PTP/PHC, PPS, VSYNC observation, or triggering is required.

Rolling-shutter row time is provisionally:

```text
t(row) = frame_reference_time
       + row_fraction * line_readout_time
       + exposure_reference_offset
```

The system should preserve the row and timing metadata now. It should add a
row-aware moving-target correction only when measured or injected error shows
that the simple centroid-row time is insufficient.

There is also an unresolved estimator question: rays captured at different
times do not intersect one static moving-target position. Target birth may use
a tight simultaneity gate and inflated uncertainty. An established track could
instead motion-compensate every observation to a reference time or consume
bearings sequentially. The first implementation must choose and test one
meaning rather than silently averaging timestamps.

## 10. Geometry: triangulation and voxels

A calibrated pixel is a bearing, not a 3D point. Camera `i` contributes a ray:

```text
ray_i(lambda) = camera_center_i + lambda * direction_i
```

For compact observations with known correspondence, the baseline continuous
initializer minimizes weighted perpendicular distance to all rays. It must
check positive depth, viewing geometry, conditioning, and outliers, then refine
against angular or image reprojection error using the real distorted camera
model.

Triangulation does not solve detection or correspondence. Wrong blobs can form
convincing ghost locations. Near-parallel rays can constrain cross-range while
leaving depth highly uncertain. Camera count alone is therefore not a
confirmation rule.

Voxel back-projection is retained as an evidence and hypothesis frontend:

- a foreground pixel produces a ray;
- a mask produces a cone or bundle of rays;
- sparse or local voxel scores show where several cameras are compatible;
- separate peaks preserve multiple possible explanations; and
- every accepted peak is continuously refined using its supporting camera
  observations.

The intended tandem is:

```text
clear compact correspondence
  -> direct multi-ray candidate
  -> continuous refinement

ambiguous pixels, blobs, or masks
  -> sparse/coarse-to-fine voxel evidence
  -> top-K candidate regions
  -> supporting-observation selection
  -> the same continuous refinement

refined measurement + covariance + quality
  -> one filter update
```

The voxel center and refined point are derived from the same pixels. They must
not be sent to the filter as independent measurements. Voxels search; the
continuous solver measures; the tracker owns time.

Initial confirmation may require at least three cameras, but must also use
camera diversity, minimum triangulation angle, residual, conditioning,
timestamp compatibility, and persistence. Two-camera births may remain
tentative or become credible through repeated temporal support.

For a resolved object, different camera centroids may refer to different
visible surfaces. A mask intersection is a visual-hull-like compatibility
volume, not automatically the object's physical center.

The first covariance definition is also unfinished. It must state whether it
is conditional on fixed camera calibration and timing, or whether it attempts
to marginalize their uncertainty. Shared pose, clock, lens, and association
biases cannot be made independent merely by adding them to a diagonal noise
matrix.

## 11. Tracking and later turret fusion

The first tracker should support multiple track objects even if the first
experiment contains one target. Birth, confirmation, coast, reacquisition,
and deletion need explicit reasons.

The simplest first estimator is a six-state constant-velocity Cartesian KF
consuming refined XYZ measurements and full 3x3 covariance:

```text
x = [px, py, pz, vx, vy, vz]
```

An EKF or UKF becomes relevant when updating directly from nonlinear pixel or
bearing measurements at their own timestamps. A fixed-lag smoother, IMM,
JPDA, or MHT should be added only after replay evidence identifies the failure
it fixes.

The later turret provides an asynchronous bearing, encoder pose, pose
uncertainty, pixel covariance, and class scores. YOLO confidence is neither
pixel-localization covariance nor geometric truth.

## 12. Calibration

Intrinsics should be calibrated with ChArUco at the deployed resolution, crop,
focus, enclosure window, and processing scale. The protective window is part
of the optical system.

Static camera translation and camera orientation are separate problems. A
ZED-F9P survey can provide strong static translation measurements under good
RTK conditions, but it does not directly determine optical yaw, pitch, roll,
or the antenna-to-camera lever arm.

The intended field procedure is:

1. establish one world/ENU datum;
2. occupy each static node with the rover and record a stable fixed solution;
3. measure the antenna-to-camera lever arm;
4. solve camera orientation against shared optical control points or a known
   moving target;
5. refine the set jointly where appropriate; and
6. validate on held-out points or trajectories.

All intrinsics, extrinsics, rolling-row timing, clock mapping, and ROI mappings
are immutable, versioned runtime data referenced by each observation.

## 13. Simulation and validation

Simulation proves software wiring and controlled estimator behavior. It does
not prove SC3336 image quality, RV1106 PTS semantics, outdoor camera pose,
weather performance, or high-altitude accuracy.

The first experiment is divided into four tiers:

1. **Exact analytic geometry:** exact pixels, exact time, and known cameras.
2. **Deterministic Blender evidence:** exact-resolution frames, declared Y
   conversion, and deterministic truth sidecars.
3. **Sensor/model perturbations:** pixel noise, blur, brightness, distortion,
   pose error, clock offset/drift/jitter, and rolling shutter.
4. **Packet/node faults:** loss, duplication, reorder, lateness, reboot, and
   optional crop loss.

Renderer truth and estimator calibration must be separate objects. Otherwise
the same model validates itself. Entire scenes or trajectories should be held
out from tuning.

The same saved dataset must run through the same `RecordedFrameSource`, packet
contract, alignment, localization, tracker, recorder, and visualizer used by
live input. Blender should not be required during replay.

Synthetic post-ISP Y is sufficient for the first foreground test. Synthetic
Bayer/RAW packing is a later explicit sensor model, not something Blender
automatically provides. RKAIQ is an ISP/runtime stack, not a scene simulator.

## 14. Quantitative anchors

These are design calculations, not achieved results.

### At 800 ft

With a 2312-pixel width and illustrative 60-degree horizontal FOV:

- one pixel is about 0.453 mrad;
- one pixel represents about 0.11 m cross-range;
- a 1 m target is approximately 9 pixels wide;
- a 5 m target is approximately 45 pixels wide;
- with a 10 m useful baseline, one-pixel bearing error gives roughly 2.7 m
  depth uncertainty before calibration and timing bias; and
- with a 5 m useful baseline, the same error gives roughly 5.4 m depth
  uncertainty.

At this range, approximately 1 degree of orientation error creates about 4.3 m
of lateral error, and 2.5 degrees creates about 10.6 m. This is why the BNO055
cannot be final pose truth for a 5 m goal.

### At high altitude

With an optimistic fully projected 137 m baseline:

- at 25,000 ft, the triangulation angle is approximately 1.03 degrees;
- at 35,000 ft, it is approximately 0.74 degrees;
- an illustrative 0.1 mrad relative-bearing error implies approximately 42 m
  and 83 m range uncertainty respectively; and
- reaching 5 m range standard deviation would require roughly 2.4 arcseconds
  at 25,000 ft and 1.2 arcseconds at 35,000 ft before all other biases.

Range and cross-range must be reported separately.

### Dense voxel memory

One score array alone requires approximately:

| Grid | FP32 | FP16 |
|---:|---:|---:|
| `512^3` | 0.54 GB | 0.27 GB |
| `768^3` | 1.81 GB | 0.91 GB |
| `1024^3` | 4.29 GB | 2.15 GB |
| `1280^3` | 8.39 GB | 4.19 GB |

These figures exclude the OS, CUDA context, frame buffers, counts, temporary
arrays, packet buffers, recording, and visualization. The proposed response is
frustum clipping, coarse-to-fine search, sparse bricks, top-K extraction,
temporal recycling, and track-local volumes. CUDA is deferred until a CPU
oracle and profile identify a real hot path.

## 15. Provisional acceptance criteria

For the clean analytic tier:

- exact cases reproduce truth within numerical tolerance;
- near-parallel or otherwise weak geometry is identified; and
- direct and voxel-seeded refinement agree after continuous refinement.

For the clean rendered 800 ft case:

- detection succeeds in at least 95 percent of eligible frames;
- a confirmed track forms from at least three-camera support;
- 3D error is at or below 5 m for the named geometry and target; and
- no more than one duplicate confirmed track appears.

For perturbed and packet-fault cases:

- error and covariance degrade honestly;
- covariance coverage is reported;
- failures become weak/late/outlier results instead of precise bad poses;
- packet faults and restarts are visible in metrics;
- stale evidence does not block fresh evidence; and
- deterministic replay reproduces the delivered-event result.

The 5 m gate is provisional and applies only to a named synthetic geometry. It
must not be reused as a high-altitude field claim.

## 16. Proposed implementation order

Two work tracks can proceed without coupling every change together.

The current documents contain several different-looking linear orders: some
start with semantic ADRs, some with exact analytic geometry, some with Blender,
and some with the RV1106 spike. The two-track order below is a proposed
dependency-based reconciliation, not a decision the reviewer should accept
without checking.

### Central software track

1. Freeze world/camera frames, pixel conventions, ROI mappings, units, and
   timestamp meanings.
2. Implement distortion-aware projection/unprojection with exact tests.
3. Define a minimal observation plus deterministic JSONL recording/replay.
4. Build analytic Tier 0 synthetic geometry.
5. Build deterministic Blender frames and truth sidecars.
6. Implement the weighted continuous ray initializer.
7. Add robust reprojection refinement and covariance validation.
8. Add a small CPU pixel/mask-to-voxel oracle and compare it with direct
   hypotheses.
9. Add the KF/EKF track lifecycle and report generation.
10. Add protobuf/UDP adapters and packet fault injection around the same fusion
    engine.

### Exact-board track

1. Pin the vendor SDK, toolchain, image, sensor mode, and board revision.
2. Capture bounded Y clips plus PTS, sequence, dequeue/publish time, exposure,
   and gain.
3. Port the smallest official GMM2/CCL path.
4. Sweep resolution, model count, learning schedule, and cleanup operations.
5. Measure FPS, total media memory, latency, thermals, dropped frames, and
   false proposals through a one-hour soak.
6. Replay the same clips through the host reference detector and explain
   differences.

The tracks merge at the reviewed observation/frame contract. Multiple real
nodes, clock alignment, F9P/optical calibration, the turret, and drone work
come later.

## 17. Settled, provisional, and deferred choices

### Settled for the first implementation

- Original notes define intent; v1 is characterization evidence.
- Edge nodes emit observations rather than global tracks.
- Capture time, units, frames, and calibration revisions are explicit.
- Continuous robust geometry produces the final localization measurement.
- Voxels are sparse/local candidate evidence, not the persistent world state.
- Voxel and refined outputs are not independent measurements.
- Simulation, replay, and live input use one fusion engine.
- Start with the simplest KF/EKF measurement model that is mathematically
  appropriate.
- The visualizer is read-only.
- The turret and drone are not prerequisites for fixed-camera tracking.

### Provisional and measurement-dependent

- GMM2 processing resolution, model count, and learning policy.
- Exact cleanup and persistence rules.
- Three-camera confirmation thresholds.
- Protobuf implementation details and measurement datagram size.
- Clock synchronization method and lateness window.
- Rolling-shutter correction.
- Voxel representation, score type, block size, and GPU backend.
- Baseline, lens/FOV, and target used for the first field experiment.
- Whether the tracker should consume refined XYZ or raw bearings first.

### Explicitly deferred

- Dense global 4D voxel tensors.
- CUDA before profiling and a CPU oracle.
- UKF, IMM, JPDA, MHT, or fixed-lag smoothing without a measured need.
- Edge-side permanent rejection or hard bird classification.
- Full production weather/night capability.
- High-altitude inch-scale claims.
- Turret-dependent localization.
- Autonomous interception or drone guidance.

## 18. Known unknowns and concerns

1. What physical event does the RV1106 PTS actually represent?
2. Can the exact clone sustain useful GMM2/CCL settings in its memory and
   thermal envelope?
3. Does the selected lens provide enough target pixels and shared FOV at the
   intended baselines?
4. Can camera orientation be calibrated and remain stable well below the
   target error budget?
5. How much rolling-shutter correction is required for fast local targets?
6. How should mask or contour evidence define a stable reference point for an
   extended object?
7. How often do multiple blobs create geometrically convincing ghost tracks?
8. Which per-camera normalization makes voxel scores comparable without
   inventing probability semantics?
9. Does sparse voxel search materially improve candidate recall enough to
   justify its memory and runtime?
10. How should time, pose, and calibration biases enter covariance without
    falsely assuming independence?
11. What packet/lateness policy preserves fresh measurements under loss and
    reorder?
12. Can the intended outdoor Ethernet/PoE topology survive distance, weather,
    power loss, and node restarts?
13. How badly do clouds, glare, insects, foliage, rain, darkness, and camera
    shake affect foreground proposal rates?
14. Which simulation perturbations are realistic enough to expose failures
    without pretending to model the SC3336 exactly?
15. What is the correct first field ground-truth target and measurement method?

## 19. Known internal tensions requiring review

These are already visible gaps. Finding them again is less valuable than
proposing the smallest defensible resolution.

1. **Execution order:** the documents name different first steps. Replace the
   competing lists with dependencies, parallel work, and the next three exact
   review units.
2. **First filter interface:** some sections describe refined XYZ into a
   linear KF; others say to start with an EKF; EXP-001 proposes testing both.
   Select one first contract and explain what evidence would justify the other.
3. **Non-simultaneous geometry:** the static closest-ray initializer does not
   yet define how observations at distinct capture times represent a moving
   target.
4. **Tracked reference point:** the documents recognize that silhouette
   centroids are view-dependent, but the first result is still a point. Either
   restrict the first milestone to a declared point-like reference or define
   an extent observation.
5. **Covariance semantics:** the result promises a full 3x3 covariance without
   yet defining whether calibration, timing, association, and object-extent
   errors are conditional, marginalized, correlated, or represented
   separately.
6. **Canonical EXP-001 scene:** the experiment provides sweeps but has not
   frozen one exact camera layout, target trajectory, frame rate, duration,
   appearance, background, detector warm-up, or selected 5 m baseline.
7. **Acceptance statistics:** "below 5 m" and "eligible frames" need precise
   definitions. Range/cross-range RMSE and p95, velocity error, acquisition
   delay, false tracks per minute, and covariance-coverage targets are not yet
   frozen.
8. **Voxel oracle:** the score equation, scatter/gather convention,
   per-camera normalization, ray-length handling, temporal slice, grid frame,
   and deterministic peak rules remain unspecified.
9. **Pixel covariance:** blob spread, GMM score, and centroid repeatability are
   not equivalent. The method for calibrating `covariance_2x2`, especially for
   two-pixel targets, is open.
10. **Timing observability and latency budget:** clock offset may not be
    identifiable from packet arrivals alone, and no maximum age-of-information
    budget currently drives per-stage latency decisions.
11. **Optics versus detector resolution:** the illustrative 2312-pixel/60-degree
    calculation may not match the lens or downscaled GMM2 mode that fits the
    board.
12. **Wire-schema status:** the latest direction accepts protobuf, while an
    older framework section leaves the binary format open. Treat protobuf as
    the current human choice but review message bounds, optional-evidence
    joins, expiry, and compatibility.

## 20. Questions the external review must answer

### Feasibility and scope

1. Is the 800 ft synthetic milestone scientifically useful and appropriately
   scoped? What would it fail to prove?
2. Is the eventual high-altitude goal physically plausible as coarse 3D
   tracking with the stated sensor and maximum baseline? Correct the numerical
   assumptions where necessary.
3. Are the local-drone and high-altitude regimes sharing too much architecture
   or too little?

### Geometry and uncertainty

4. Is the direct-plus-voxel tandem logically sound, or does it double-count or
   hide correspondence errors anywhere?
5. Is robust continuous reprojection refinement the correct final measurement
   stage for compact targets?
6. What covariance method is appropriate when pixel noise, camera pose,
   timing, and extended-object centroid bias are partly correlated?
7. Are the proposed support, residual, angle, conditioning, and persistence
   gates sufficient for initial confirmation?
8. Should the first tracker consume refined XYZ, individual bearings, or both?

### Detection and edge hardware

9. Is GMM2 plus cleanup a defensible first foreground proposal path for sky
   scenes on this hardware?
10. Which exact board experiments must occur before the observation contract
    or processing resolution is frozen?
11. Is any important information lost by normally sending observations and
    optional crops rather than full frames?

### Time, calibration, and networking

12. Is the fallback timing strategy defensible without hardware exposure
    timestamps? What minimum bench measurement is required?
13. Is sequential F9P surveying plus optical pose refinement a sound low-cost
    calibration plan for static nodes?
14. Which calibration parameters should be solved jointly, and which should
    remain independently measured?
15. Are the proposed UDP/control planes and failure policies appropriate for
    this freshness-sensitive system?

### Simulation and implementation order

16. Does EXP-001 separate truth from estimator assumptions strongly enough?
17. Which synthetic perturbations or held-out tests are missing?
18. Is the implementation sequence likely to produce a truthful vertical
    slice quickly, or are important dependencies in the wrong order?
19. Which proposed modules or contracts are premature?
20. What are the five cheapest decisive experiments that should control the
    next architecture decisions?

## 21. Required review response

Return the review in this order:

1. **Executive verdict:** whether the core project and first milestone are
   technically coherent.
2. **Findings:** bugs, contradictions, physical limitations, and architecture
   risks ordered by severity. Ground each finding in a calculation, source, or
   explicit reasoning chain.
3. **Assumption audit:** label major assumptions established, plausible,
   unsupported, or contradicted.
4. **Geometry and accuracy audit:** independently check the key range,
   baseline, pixel, orientation, timing, and voxel-memory calculations.
5. **Architecture assessment:** identify boundaries to keep, remove, or change.
6. **Implementation-sequence assessment:** propose only changes that reduce
   risk or reach decisive evidence sooner.
7. **Experiment plan:** rank the cheapest experiments that can falsify or
   validate the important assumptions.
8. **Open questions:** list only questions whose answers would materially
   change the design.

Do not hide uncertainty behind a generic confidence score. Do not assume that
more cameras, a finer voxel grid, CUDA, a more complicated Kalman filter, or a
neural classifier automatically solves a measured weakness.

## 22. Repository references

For a reviewer with access to this checkout, read in this order:

1. [`SKYWEAVE_PROJECT_GUIDE.md`](./SKYWEAVE_PROJECT_GUIDE.md) - readable intent
   and current plan.
2. [`FOLLOWUP_DECISIONS_2026-07-17.md`](./FOLLOWUP_DECISIONS_2026-07-17.md) -
   detailed answers to the user's follow-up questions.
3. [`EXP-001_800FT_SYNTHETIC_FULL_STACK.md`](./experiments/EXP-001_800FT_SYNTHETIC_FULL_STACK.md)
   - first synthetic experiment.
4. [`IMPLEMENTATION_FRAMEWORK.md`](./IMPLEMENTATION_FRAMEWORK.md) - contracts,
   ownership boundaries, review chunks, and gates.
5. [`RESEARCH_REPORT.md`](./RESEARCH_REPORT.md) - full truth audit, calculations,
   source ledger, and repository assessment.
6. [`sources/manifest.yaml`](./sources/manifest.yaml) - primary/official source
   catalog and intended use.
7. [`../../v1/reference/pixel-to-voxel-projector`](../../v1/reference/pixel-to-voxel-projector)
   - conceptual reference implementation only.

The two original human-authored note attachments are:

- `/Users/samuelmccanahan/.codex/attachments/5ad1a766-dae6-42fc-9661-aeaad400c755/pasted-text.txt`
- `/Users/samuelmccanahan/.codex/attachments/9e5b26d5-9a0b-4e4c-9857-4008295ad8b3/pasted-text.txt`

The linked pixel-to-voxel repository's license is not plain Apache-2.0. It
contains Apache-2.0 text plus an additional defense-entity restriction. Treat
it as reference-only and independently implement the v2 geometry and DDA
behavior unless legal review says otherwise.
