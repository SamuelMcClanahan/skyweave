# Skyweave detection architecture: working plan

**Status:** canonical summary for the fixed-camera detection rewrite

**Primary scope:** detect a moving flying object in multiple fixed cameras,
localize it in 3D, and maintain a truthful track with uncertainty.

**Human-note sources:** the original ray/voxel note, the longer flying-object
detection note, and the preservation-first reformatted notes. The deeper
research/framework documents are supporting evidence and implementation detail.

## 1. What this document covers

This is the initial plan for revising only the Skyweave detection pipeline. It
combines Samuel's original ray/voxel and flying-object detection notes with the
decisions made during the architecture review.

It covers:

- fixed RV1106 camera nodes;
- background modeling and motion proposals;
- compact observation packets;
- multi-camera timing and correspondence;
- voxel-assisted candidate generation;
- continuous multi-ray localization;
- uncertainty, outlier rejection, and EKF tracking;
- deterministic Blender data and physical-node replay; and
- later adaptation to real cameras and field noise.

It does **not** cover the turret, drone, onboard perception, guidance, physical
interception, classification beyond optional false-positive rejection, or
high-altitude performance claims.

## 2. The system in one page

Each fixed camera learns what its normal view looks like. When something moves,
the edge node extracts one or more small 2D observations instead of sending the
whole frame continuously. Each observation becomes a calibrated ray or narrow
cone in world coordinates.

The Jetson groups observations that may describe the same object and uses them
in two related ways:

1. a sparse or bounded voxel search asks where several camera cones agree and
   proposes possible 3D regions;
2. a continuous multi-ray solver starts from those candidates, or directly
   from a simple deterministic initializer, and finds the 3D point that best
   explains the camera evidence.

The continuous result, residuals, and covariance form one localization
measurement. Robust gates reject inconsistent cameras. An EKF maintains the
object's position and velocity over time.

```text
camera exposure
  -> luma/background model
  -> foreground mask
  -> components and 2D observations
  -> calibrated observation packet
  -> time alignment and correspondence
  -> direct and/or voxel candidate hypotheses
  -> continuous multi-ray refinement
  -> residual, covariance, and systematic bounds
  -> EKF track lifecycle
  -> recorder and visualizer
```

## 3. Ownership boundaries

### Edge camera node

The RV1106 node owns work that is local to one camera:

- capture the image and preserve the best available sensor timestamp;
- obtain or produce the luma image used by detection;
- run the IVE GMM2/background model where it proves useful;
- clean the foreground mask with small, measured filters;
- extract connected components or contours;
- maintain only a short-lived local tracklet when needed to stabilize a blob;
- attach the immutable camera/calibration revision; and
- send bounded observation packets plus health/resource metrics.

It does not decide the global object identity or final 3D position.

### Jetson central node

The Jetson owns decisions requiring multiple cameras:

- validate packets and source sessions;
- map timestamps into a common event-time model;
- buffer, reorder, expire, and align observations;
- associate observations across cameras;
- construct rays/cones from the camera model;
- generate direct and voxel-assisted hypotheses;
- refine the best hypothesis continuously;
- reject outliers and compute conditional covariance;
- maintain track birth, confirmation, prediction, update, and deletion;
- record all inputs, decisions, residuals, and timing; and
- publish a sparse state for the visualizer.

## 4. Camera model and observation contract

Before detection code is treated as real geometry, freeze:

- world, camera, and image coordinate conventions;
- pixel-center convention and image origin;
- units, quaternion convention, and transform direction;
- distortion model and whether pixels are raw or undistorted;
- timestamp event, clock domain, and timestamp quality;
- camera intrinsics, pose, and immutable calibration revision; and
- the target reference represented by a centroid.

A minimum 2D observation contains:

```text
camera ID and source session
frame sequence and capture timestamp
timestamp source/domain/uncertainty
calibration revision
bbox, centroid, contour or compact mask evidence
pixel covariance or detector-quality fields
foreground area and local tracklet ID if present
processing resolution and transform back to sensor pixels
detector configuration revision
```

The first wire format is protobuf, but the semantic contract comes first.
Fresh measurement packets use non-retransmitted UDP datagrams, provisionally
kept below about 1200 bytes until the real network is measured. Calibration,
configuration, health, and requested debug evidence use a reliable control
plane. Full frames are not the normal real-time path. Cropped/debug frames or
masks may travel on a separate bounded, independently droppable channel, and
all source frames remain recordable for replay.

## 5. Edge detection

### 5.1 Background model

The goal of GMM2/MOG2 is to learn the normal sky/background appearance over
time and identify pixels that no longer fit it. This is more appropriate than
plain adjacent-frame differencing because adjacent differencing often produces
only the leading and trailing edges of a moving object.

The initial detector pipeline is:

```text
luma frame
  -> adaptive GMM2 background update
  -> foreground likelihood/mask
  -> threshold
  -> small morphology/noise filters
  -> connected components
  -> component measurements
  -> short temporal stabilization
```

GMM2 does not by itself solve clouds, rain, insects, foliage, exposure changes,
camera motion, or sensor noise. The exact RV1106 IVE implementation must be
benchmarked for supported formats, memory, resolution, frame rate, model count,
latency, and thermal behavior. Hardware acceleration is not proof that the
complete capture-to-component path is fast enough.

### 5.2 Basic filtering

Start with inexpensive, interpretable filters:

- minimum/maximum component area;
- contour shape and fill ratio;
- border-contact rejection where appropriate;
- persistence across a few frames;
- velocity/acceleration plausibility;
- detector confidence and foreground stability; and
- multi-camera geometric support.

Do not add a classifier until recorded false positives show that these gates
are insufficient. Birds may initially become valid tracks; proving consistent
detection and localization matters first.

### 5.3 Detection precision

The detector's useful output is not merely a box. Measure centroid repeatability
and bias at the exact processing resolution, target size, exposure, blur, and
background condition. A 640-pixel processing image does not inherit the angular
precision of the full-resolution sensor.

Keep the mapping from processed pixels to original sensor pixels explicit.
Sub-pixel centroids are allowed only after a repeatability test demonstrates
them; they cannot be assumed to meet a range-error budget.

## 6. Time alignment and correspondence

Observations are grouped in event time, not just in arrival order. Each camera
has a reorder buffer and a lateness/expiry policy. Old observations may be used
for replay or diagnostics but must not silently enter a current geometry solve.

Clock offset, timestamp uncertainty, exposure duration, rolling-shutter row
time, and target motion all contribute to geometric error. Preserve these
fields so the solver can propagate observations to a common reference time or
inflate/reject them honestly.

Correspondence cannot be established by “at least N cameras” alone. Camera
count is one confirmation condition, but an association must also satisfy:

- compatible event time;
- compatible epipolar/ray geometry;
- bounded reprojection or angular residual;
- compatible appearance/size where useful;
- plausible motion relative to an existing track; and
- persistence when birthing a new track.

For the first single-target synthetic scene, correspondence can be deliberately
simple. The interfaces must still allow multiple candidate groups so the
single-target shortcut does not become the architecture.

Two-camera support can create a tentative hypothesis. Initial confirmation
should require either consistent support from three or more cameras, or
repeated geometrically consistent two-camera support across event-time batches.
The exact counts and persistence are configuration backed by Monte-Carlo and
replay evidence, not universal constants.

## 7. How voxels and triangulation work together

### 7.1 What a camera contributes

A foreground pixel, centroid, contour, or uncertainty ellipse back-projects
through the calibrated camera into 3D:

- one centroid produces a bearing ray with angular uncertainty;
- a bbox or contour produces a narrow cone/frustum of possible locations; and
- multiple cameras produce overlapping regions rather than perfect point
  intersections.

The finite object size and noisy calibration mean that rays should not be
expected to meet at exactly one point.

### 7.2 Voxel role

The voxel stage is a candidate generator and ambiguity resolver. It asks:

> Which bounded regions of 3D space receive compatible evidence from enough
> independent cameras at approximately the same event time?

It can use DDA/back-projection to accumulate camera support, retain a camera
bitmask rather than double-counting many pixels from one view, find local
maxima, and output one or more coarse candidate regions.

Voxels are valuable when:

- there are several disconnected 2D blobs per camera;
- correspondence is ambiguous;
- the target evidence is an extended contour rather than one reliable point;
- direct initialization yields several plausible intersections; or
- the visualizer needs an understandable 3D evidence volume.

Voxels are not the final accuracy mechanism. A voxel center is quantized by the
cell size, dense global grids scale badly, and silhouette cones describe visual
support rather than depth-sensor free space.

### 7.3 Continuous localization role

For every direct or voxel-seeded candidate, solve for the continuous 3D point
that minimizes robust angular or reprojection error across its supporting
cameras. The solver returns:

```text
position at a declared event time
supporting and rejected camera IDs
per-camera residuals
geometry/conditioning metrics
conditional measurement covariance
systematic sensitivity/bound fields
```

The direct and voxel paths are alternate hypothesis frontends for the same
refinement. Do not send the voxel center and the refined point into the EKF as
two measurements: they came from the same pixels and would double-count the
evidence.

### 7.4 Deterministic initializer

The first direct initializer is a deterministic weighted least-squares
closest-point solve over calibrated bearing lines. For camera center `c_i`,
unit bearing `d_i`, and weight `w_i`:

```text
P_i = I - d_i d_i^T
A   = sum(w_i P_i)
b   = sum(w_i P_i c_i)
A x0 = b
```

In simple terms, `x0` is the point with the smallest weighted total squared
perpendicular distance to the rays. It is fast, repeatable, and gives the
nonlinear robust refinement a stable starting point.

It is not the final estimate because it does not by itself handle outliers,
extended targets, distorted pixels, timing differences, or degenerate camera
geometry. If its conditioning is poor, return weak/invalid depth rather than a
confident point.

## 8. Sparse voxel strategy

Do not allocate a few billion dense floating-point cells. Even a
`500 × 500 × 500` float32 volume is about 500 MB before temporary buffers and
metadata.

Begin with a small CPU oracle for correctness, then use whichever measured
representation the workload justifies:

- restrict the volume by known operating range and overlapping camera FOV;
- use coarse-to-fine cells;
- allocate blocks only around active ray/cone intersections;
- use integer/saturating evidence and a camera-support bitmask where possible;
- clear by generation ID instead of memset over the whole world;
- preserve only peaks and local neighborhoods after each event-time batch;
- seed a local grid from direct pairwise hypotheses or an existing EKF track;
- use an octree or hash grid only after the simple sparse-block version is
  measured; and
- use CUDA only after profiling shows the candidate stage is the bottleneck.

Float16 does not solve the architecture by itself. Evidence counts often need
far less than 16 bits, while position and continuous optimization may still
need float32/float64. Choose data types by field semantics.

One DDA per changed pixel is parallelizable, but it can still waste work and
memory bandwidth. Prefer compact contours, sampled boundary/interior evidence,
or component-level cones when they preserve the needed accuracy. Benchmark
these against the full-pixel oracle.

## 9. Robust localization and uncertainty

Use layered rejection before and during the solve:

1. packet/schema and calibration revision checks;
2. event-time/lateness checks;
3. camera-count and geometry checks;
4. robust loss on angular or reprojection residuals;
5. leave-one-camera-out or consensus validation;
6. conditioning/depth-observability checks; and
7. innovation gating against an existing track.

The filter covariance represents random detector/timing uncertainty conditional
on the supplied calibration. Calibration error, mount drift, clock bias, and
target-reference ambiguity remain separate systematic bounds or sensitivities.
Do not simply add an unknown fixed bias into covariance and call it calibrated.

Report range and cross-range uncertainty separately when useful. Far-range
depth degrades approximately with range squared and inversely with projected
baseline. A system can have useful bearing/cross-range while range is weak or
invalid.

## 10. Tracking

Start with a constant-velocity EKF state:

```text
x = [position_x, position_y, position_z,
     velocity_x, velocity_y, velocity_z]
```

The filter predicts to the measurement event time, updates with the continuous
3D localization and its conditional covariance, and then predicts to publish
time. Keep observation time, filter time, and publish time explicit.

Track birth requires:

- support from the configured minimum number of independent cameras;
- acceptable geometry and residuals;
- persistence over multiple event-time batches; and
- no strong conflict with an existing track.

Use explicit lifecycle states such as tentative, confirmed, coast, reacquired,
and deleted. Track confirmation, coasting, and deletion use named configuration
thresholds. Reject impossible innovations and implausible accelerations before
they pull the state. A commercial aircraft motion profile can inform process
noise and gates, but it must not erase a real maneuver merely because it is
unexpected.

Fixed-lag smoothing, IMM, JPDA/MHT, classifiers, and learned motion models are
deferred until replay evidence shows a specific failure that the EKF and robust
gates cannot handle.

## 11. Output and observability

Every localization/track output should expose enough evidence to audit it:

```text
track ID, lifecycle state, and update count
state timestamp and publish timestamp
position, velocity, and conditional covariance
range/cross-range observability or validity
systematic bounds/sensitivities
supporting/rejected cameras and observation IDs
per-camera angular/reprojection residuals
candidate source: direct, voxel, or both
voxel peak/support summary when used
latency, lateness, drop, and resource metrics
```

The recorder stores the raw observation packets, calibration/config revisions,
clock mapping, association choices, solver diagnostics, filter states, and
evaluator-only synthetic truth. The browser visualizer consumes the recorded
state; it does not define geometry behavior.

## 12. First milestone: real wiring, fake data

The first proof uses deterministic synthetic images but the real system
boundaries:

```text
Blender scene and truth
  -> saved per-camera image/luma sequences
  -> host reference detector and geometry
  -> same saved sequences replayed by real RV1106 nodes
  -> real protobuf/UDP packets
  -> real Ethernet switch
  -> real Jetson fusion/tracking/recording
```

Blender is the first generator. Isaac Sim is added only if it supplies a needed
capability that Blender lacks. Normal Blender output is not SC3336 Bayer RAW or
the RV1106 ISP, so sensor effects are explicit, independently switchable
transforms rather than claims of exact hardware simulation.

The canonical first scene is the existing 800 ft experiment, but range,
baseline, processing resolution, target size, and centroid precision must be
named together. Illustrative calculations are not acceptance results.

### Faults to inject

Add one category at a time before held-out combinations:

- image noise, blur, exposure, clouds, and confusers;
- centroid bias, missed detections, split/merged blobs, and false proposals;
- camera intrinsic/extrinsic and mount perturbations;
- clock offset, jitter, row time, and frame age;
- packet loss, duplication, reorder, corruption, and delay; and
- node restart, session reset, and camera dropout.

### Milestone passes when

- one manifest and seed reproduce the dataset and truth;
- exact geometry and Monte Carlo tests pass their statistical gates;
- clean direct and voxel-seeded paths converge to the same continuous solution
  within declared tolerance;
- outliers and weak geometry produce honest rejection/uncertainty;
- local and physical-node replay use the same central engine;
- packet/session/timing faults are visible and handled deterministically;
- wired replay agrees with the host reference within declared tolerance; and
- the report includes accuracy, coverage, false tracks, acquisition time,
  latency, packet behavior, and resource use.

## 13. Implementation order

| Step | Do | Figure out | Done when |
| --- | --- | --- | --- |
| 1. Freeze semantics | Write ADRs for coordinates, pixels, time, observations, localization, covariance, and systematic bounds | Any ambiguous transform, timestamp, or centroid meaning | Golden serialization/projection tests agree on every convention |
| 2. Analytic geometry | Implement projection, bearing construction, deterministic line initializer, robust continuous refinement, and degeneracy metrics | Baseline/resolution regimes with useful depth | Exact and Monte Carlo tests recover truth and report honest weak depth |
| 3. Freeze EXP-001 | Name cameras, baseline, target, resolution, timing, trajectories, seeds, faults, and statistical gates | Whether the canonical geometry is achievable and informative | One manifest fully reproduces the scene and evaluator truth |
| 4. Build Blender data | Render deterministic per-camera frames plus truth and sensor sidecars | Which effects must be rendered versus postprocessed | Saved sequences replay without Blender running |
| 5. Host detector | Implement luma GMM2/MOG2 reference, morphology, components, observations, and scorecard | Centroid repeatability, bias, false proposals, and resolution tradeoff | Clean and faulted clips produce measured detection metrics |
| 6. Central vertical slice | Implement time grouping, simple single-target association, direct/voxel hypotheses, refinement, robust gates, and EKF lifecycle | Thresholds justified by the synthetic budget | One target is born, tracked, coasted, and deleted with auditable evidence |
| 7. Fault campaign | Inject detector, geometry, timing, packet, and node faults one at a time | Which failures are architectural versus tunable | Every failure appears in metrics and produces bounded behavior |
| 8. Wire path | Freeze protobuf, recorded-Y source, UDP transport, session/reorder/expiry behavior, and recorder | Packet size, latency, loss policy, and debug-crop budget | Deterministic and wall-clock local replay pass |
| 9. Physical replay | Run the saved sequences through the RV1106 nodes, real switch, and Jetson | Board throughput, memory, latency, and parity | Wired acceptance report passes or names measured blockers |
| 10. Real camera adaptation | Replace recorded-Y with CSI/ISP, characterize PTS and IVE GMM2/CCL | Actual timestamp event, sustainable resolution/FPS, and real sky noise | Same observation contract works with measured field error budgets |
| 11. Controlled field validation | Calibrate intrinsics/poses, run two/three-camera known targets, and compare against bounded reference truth | Pose stability, weather, baseline, and accuracy envelope | Field report states where range is valid, weak, or unavailable |
| 12. Add sophistication only as earned | Consider sparse CUDA voxels, classifier, fixed-lag smoother, IMM, or richer association | Which recorded failure each addition fixes | New component improves a named metric without breaking resource/latency gates |

## 14. What is settled, provisional, and deferred

### Settled architecture

- thin edge nodes propose calibrated 2D observations;
- the Jetson owns global association, geometry, tracking, and recording;
- voxels propose candidates; continuous refinement produces the filter input;
- uncertainty and systematic bounds are separate;
- EKF and robust gates come before advanced tracking;
- the first milestone is deterministic synthetic data through real wiring; and
- the exact same contracts support host replay, physical-node replay, and later
  real cameras.

### Provisional until measured

- processing resolution and frame rate on RV1106;
- IVE GMM2 model/memory configuration;
- morphology and component thresholds;
- packet/crop sizes and transport rate;
- camera count needed for confirmation;
- voxel cell size and sparse representation;
- centroid covariance model;
- timing/lateness windows;
- EKF process noise and lifecycle thresholds; and
- the baseline/range envelope that meets a given accuracy target.

### Deferred

- CUDA voxel projection;
- dense global evidence grids;
- onboard or turret perception;
- object classification beyond measured false-positive needs;
- fixed-lag smoothing and multi-model tracking;
- complex multi-target association;
- Isaac Sim as a required dependency; and
- high-altitude or 5 m range-accuracy claims without a complete measured error
  budget.

## 15. Conflicts resolved from the original notes

- The original notes describe a dense time-varying voxel field. The revised
  design keeps immutable timestamped observations, sparse/local transient voxel
  evidence, and a bounded track history instead of a persistent dense world.
- The original notes consider full-frame transport. The normal path now sends
  compact observations; frames/crops/masks are bounded debug evidence.
- The original notes lean toward a UKF. The baseline is the simpler six-state
  KF/EKF-capable interface updated by refined XYZ; a UKF is added only for a
  measured nonlinear failure.
- Requiring `N` cameras alone does not solve correspondence. Event time,
  epipolar/ray geometry, residuals, conditioning, motion, and persistence are
  also required.
- More cameras reduce independent random error but do not average away shared
  pose, focal, clock, mount, or target-reference bias.
- Any 5 m accuracy statement applies only to a frozen geometry with named
  baseline, processing resolution, centroid precision, timing, and calibration
  budgets. Report depth and cross-range separately.
- A later synthetic-pipeline refinement suggests building the sensor model,
  scorecard, and hybrid clips before Blender. The initial reviewed dependency
  order remains contracts and analytic geometry first, followed by the frozen
  canonical Blender scene; hybrid clips may be enabling work in parallel.

## 16. Immediate first review units

To keep the rewrite human-directed, implement and review these separately:

1. **Semantic contracts:** coordinate, pixel, time, observation, covariance,
   and systematic-bound ADRs plus golden tests.
2. **Geometry oracle:** projection, deterministic initializer, robust
   refinement, degeneracy, covariance, and Tier-0 Monte-Carlo analysis.
3. **Canonical experiment:** the exact EXP-001 manifest, dataset contract, and
   statistical acceptance gates.

Only after those three are reviewed should the larger detector, packet, voxel,
and EKF implementation be built around them.
