# EXP-001: 800 ft synthetic full-stack localization

**Status:** Current first system experiment; synthetic data, real wiring<br>
**Scene generator:** Blender first; Isaac Sim later if it adds value<br>
**Primary target range:** 800 ft / 243.84 m

## Question

Can the planned fixed-camera stack accept recorded, sensor-like frames; produce
foreground evidence; packetize and align observations; generate 3D voxel/ray
hypotheses; refine them continuously; and maintain an EKF track within a
declared error budget?

This experiment proves the architecture, real packet/wiring path, and
estimator behavior against controlled truth and injected faults. It does not
prove SC3336 image quality, RV1106 exposure timing, field calibration, weather
performance, or high-altitude aircraft accuracy.

## Success statement

The experiment succeeds when one command/reproducible manifest can:

1. generate or replay exact-resolution frames from three or more cameras;
2. preserve independent truth and estimator camera models;
3. run a reference foreground detector;
4. serialize the same protobuf observation contract planned for real nodes;
5. inject transport timing/loss/reorder;
6. recover a 3D candidate by voxel voting and/or direct pair hypotheses;
7. refine that candidate and emit covariance/quality diagnostics;
8. update a six-state EKF;
9. reproduce the result from the saved manifest and seed;
10. replay the same per-camera Y sequences through physical nodes, the real
    switch, and the Jetson; and
11. report range, cross-range, velocity, false-track, outlier, timing, and
    resource metrics.

## Coordinate and camera baseline

Use metres internally. Put the array in a local ENU-like Cartesian frame.

Initial nominal layout:

~~~yaml
world:
  units: m
  target_nominal_range: 243.84

cameras:
  count: 3
  horizontal_fov_deg: 60.0
  image_width: 2312
  image_height: 1304
  baseline_sweep_m: [5.0, 10.0, 25.0, 50.0]
  common_overlap_required: true
~~~

Do not begin with a 137 m / 450 ft baseline merely because it is the eventual
maximum. At 800 ft that is a very large convergence angle and may reduce common
FOV. Sweep baseline and camera placement deliberately.

The actual deployed lens/FOV replaces the illustrative 60 degree value once
known.

## Canonical scene decision gate

The sweeps above explore sensitivity. They do not define the acceptance scene.
Before Blender work begins, freeze one exact manifest containing:

- camera count, positions, orientations, and useful projected baseline;
- render resolution and the separate detector processing resolution;
- FOV/lens model and distortion;
- target dimensions, material, declared tracking reference point, trajectory,
  and speed;
- frame rate, duration, background warm-up, target entry time, and shared-FOV
  interval;
- detector centroid-precision budget at the processing resolution;
- deterministic seed and renderer/environment revisions; and
- exact clean-case acceptance statistics.

Use Tier-0 Monte Carlo to decide whether the named gate uses a 10 m or larger
baseline. Keep 5 m as a deliberate weak-geometry case unless the measured
centroid budget supports it. Do not let full-resolution pixel calculations
stand in for a downscaled detector's measured centroid precision.

## Target matrix

Use three appearance scales rather than one object:

| Target | Approximate width | Purpose at 800 ft / 60 degree FOV |
|---|---:|---|
| Tiny | 0.25 m | roughly 2 pixels; tests weak bearing evidence |
| Small | 1.0 m | roughly 9 pixels; tests blob/centroid path |
| Resolved | 5.0 m | roughly 45 pixels; tests mask/contour path |

Motion sweep:

~~~yaml
target:
  speed_mps: [0.0, 10.0, 30.0, 100.0, 250.0]
  trajectories:
    - lateral_constant_velocity
    - diagonal_constant_velocity
    - constant_acceleration
    - shallow_turn
  occlusion_cases:
    - none
    - one_camera_dropout
    - partial_mask
~~~

The 250 m/s case is a timestamp stress test, not a realistic local flight.

## Four experiment tiers

### Tier 0: exact geometry

Input:

- exact calibrated pixel centroids generated analytically;
- exact capture time;
- no distortion or packet faults;
- one known target.

Purpose:

- prove coordinate conventions;
- test deterministic initializer;
- test range/cross-range equations;
- compare direct and voxel-seeded refinement.

Expected result:

- numerical error near the solver tolerance;
- explicit weak-geometry status for degenerate layouts.

### Tier 1: deterministic image evidence

Input:

- exact-resolution Blender frames;
- fixed exposure and deterministic seed;
- ideal Y conversion;
- no clock or camera-model mismatch.

Purpose:

- test frame-to-GMM/reference detector;
- compare centroid, contour, and downsampled-mask observations;
- establish nominal voxel and continuous error.

### Tier 2: sensor/model perturbations

Inject independently:

~~~yaml
pixel:
  read_noise_sigma_dn: [0.0, 1.0, 3.0]
  shot_noise: [off, on]
  blur_px: [0.0, 0.5, 1.5]
  exposure_scale: [0.5, 1.0, 2.0]
  global_brightness_step: [0, 5, 20]

camera_model:
  focal_error_pct: [0.0, 0.1, 0.5]
  principal_point_error_px: [0.0, 0.5, 2.0]
  orientation_error_deg: [0.0, 0.05, 0.1, 0.5, 1.0]
  position_error_m: [0.0, 0.01, 0.1, 1.0]
  distortion_mismatch: [off, mild, measured_like]

time:
  node_offset_ms: [0.0, 0.5, 2.0, 10.0]
  drift_ppm: [0.0, 10.0, 100.0]
  jitter_ms_sigma: [0.0, 0.1, 1.0]
  rolling_readout_ms: [0.0, 5.0, 15.0, 30.0]
~~~

The renderer and estimator must not read the same camera-model object by
reference. Serialize truth, then construct a separately perturbed estimator
calibration.

### Tier 3: packet and node faults

Inject:

- random loss;
- burst loss;
- duplication;
- reorder;
- late arrival;
- node restart and sequence reset;
- clock segment change;
- optional crop loss independent of observation metadata.

Purpose:

- prove source/session handling;
- test lateness and stale-observation policy;
- show that optional evidence cannot block localization;
- verify deterministic replay of the delivered event stream.

## Synthetic frame contract

Each frame set includes:

~~~text
dataset.json
truth/cameras.json
truth/trajectory.jsonl
frames/cam-<id>/frame-<sequence>.exr
frames/cam-<id>/frame-<sequence>.png
frames/cam-<id>/frame-<sequence>.json
sensor-model.json
network-model.json
expected/analytic-observations.jsonl
~~~

Frame sidecar fields:

~~~text
dataset_id
camera_id
frame_sequence
truth exposure start/mid/end
truth target state at row/exposure time
truth camera transform and intrinsics revision
render seed and renderer revision
image path, width, height, stride, format
sensor-model revision
~~~

## Sensor presentation modes

### Mode A: post-ISP Y

This is the first mode. Convert Blender linear/RGB output through a declared
luma/noise/exposure model and produce exact-resolution U8 luma.

It tests:

- reference GMM/MOG behavior;
- masks and observations;
- packet/replay;
- central geometry.

It does not test Bayer capture or RKAIQ.

### Mode B: synthetic Bayer/RAW

Add only after Mode A is stable:

1. linear RGB/spectral approximation;
2. declared CFA mosaic;
3. exposure and black level;
4. shot/read noise;
5. gain, clipping, and quantization;
6. RAW8/10/12 packing;
7. explicit ISP/Y conversion.

Call this a synthetic sensor model, not SC3336 raw truth.

### Mode C: actual RV1106 recorded Y

Capture real VI/ISP output, PTS, frame sequence, exposure/gain, and luma. Run
the same downstream frame envelope and compare the reference detector with
IVE GMM2/CCL.

## Edge detector matrix

Run:

1. fixed-background mean as a simple sanity baseline;
2. host OpenCV MOG2-like reference;
3. host approximation of selected IVE GMM2 parameters;
4. actual IVE GMM2 on recorded frames when the board spike is ready.

Do not require bit-identical masks. Compare:

- centroid error;
- pixel recall/precision;
- connected-component count;
- foreground occupancy;
- persistence;
- false proposals per frame;
- processing latency and memory.

## Voxel/continuous comparison

For the same observations, evaluate:

### Direct

- pair hypotheses;
- support from all cameras;
- deterministic all-ray initializer;
- robust reprojection refinement.

### Voxel seeded

- coarse dense or sparse grid;
- per-camera-normalized voting;
- minimum unique-camera support;
- NMS/connected-component modes;
- weighted peak/cluster centroid;
- same continuous refinement as the direct path.

Report:

- candidate recall;
- pre-refinement voxel error;
- post-refinement error;
- runtime and memory;
- camera support;
- residual and condition;
- false candidates.

This determines where voxel back-projection adds value rather than assuming it
always does.

## EKF

Initial state:

~~~text
x = [px, py, pz, vx, vy, vz]
~~~

Run two update modes:

1. refined XYZ measurement plus conditional 3x3 covariance;
2. raw sequential camera bearings/pixels through a nonlinear measurement
   function.

The first can be a standard KF mathematically; the second requires EKF
linearization. Keeping both behind one track interface makes the choice
evidence-driven.

Log:

- prediction/update time;
- innovation and covariance;
- NIS;
- accepted/rejected measurements;
- support-camera count;
- state/covariance after each update;
- truth error.

The first acceptance path uses the refined-XYZ update through an EKF-capable
track interface. Calibration, clock-model, mount, and target-reference
systematic bounds are reported separately from the conditional covariance used
by the filter. The raw-bearing update remains a comparison until evidence
shows it should replace the first contract.

Outlier rejection is layered:

1. foreground cleanup and persistence;
2. camera support, reprojection residual, angle, and conditioning gates;
3. robust refinement such as Huber/RANSAC where appropriate;
4. EKF innovation/NIS gating; and
5. track confirmation, coast, reacquisition, and deletion rules, with
   target-profile process noise and soft acceleration/turn-rate gates.

Record false accepts and false rejects at every layer. Smoothing must not turn
a rejected ghost into a plausible track. A fixed-lag or offline smoother is a
later comparison for delayed data or reconstructed trajectories.

## Provisional acceptance gates

Clean Tier 0:

- exact cases reproduce truth within numerical tolerance;
- weak geometry is detected;
- direct and voxel-seeded refinement agree within tolerance.

Clean Tier 1:

- target detected in at least 95 percent of eligible frames, where eligibility
  is defined by warm-up completion, target pixel support, and shared-camera
  visibility in the frozen manifest;
- confirmed track forms from at least three-camera support;
- range and cross-range RMSE/p95 meet the frozen limits, with a provisional
  5 m bound only for the named baseline/FOV/resolution/target case;
- velocity error and acquisition time meet the frozen limits;
- no more than one duplicate confirmed track.

Perturbed Tier 2:

- error and conditional covariance degrade consistently with injected random
  noise where expected;
- conditional covariance coverage is evaluated with calibration held fixed;
- calibration, clock-model, mount, and target-reference biases are swept and
  reported separately as systematic sensitivity/bounds;
- no crash or NaN under a tested perturbation;
- failure is labeled weak/late/outlier instead of emitting a precise bad pose.

Packet Tier 3:

- loss/reorder/reboot are visible in metrics;
- stale observations do not block fresh data;
- optional crop loss does not remove required geometry;
- replay reproduces the same delivered-event result.

Wired synthetic pass:

- each physical node replays its assigned camera's saved Y sequence;
- real protobuf/UDP packets cross the intended switch to the Jetson;
- deterministic mode reproduces local replay within declared tolerances;
- wall-clock mode reports actual packet age, node clock mapping, loss, reorder,
  and resource use; and
- truth remains evaluator-only and is never consumed by the runtime estimator.

The 5 m gate is provisional and applies only to a named synthetic geometry.
Report range and cross-range separately. Do not reuse it as a claim for
25,000-35,000 ft aircraft.

## Required report

The generated report must contain:

- manifest and git revisions;
- camera/target diagrams and parameter table;
- target pixel size;
- baseline and triangulation-angle sweep;
- detector recall/false-proposal plots;
- direct versus voxel pre/post-refinement error;
- memory/runtime by representation;
- EKF position/velocity error and NIS;
- timing fault sensitivity;
- conditional covariance coverage and separate systematic sensitivity;
- per-layer outlier false-accept and false-reject rates;
- local-versus-wired replay parity and physical-node resource use;
- every failed acceptance gate.

## Implementation order

1. Freeze frame/pixel/time, covariance, and systematic-bound semantics.
2. Implement analytic Tier 0 and its Monte Carlo error budget.
3. Freeze the canonical scene manifest and statistical acceptance gates.
4. Generate deterministic Blender frames and truth sidecars.
5. Build the host foreground detector and calibrated `Observation2D`.
6. Implement direct hypotheses, continuous refinement, robust gates, and the
   EKF-capable track lifecycle.
7. Add the small CPU voxel oracle as an ambiguity/comparison frontend.
8. Add one-at-a-time image, geometry, detection, time, and packet faults, then
   held-out combined scenes.
9. Implement `RecordedYFrameSource`, protobuf, UDP, and deterministic replay.
10. Replay the same saved camera sequences through physical nodes, the real
    switch, and the Jetson in deterministic and wall-clock modes.
11. Generate the complete wired synthetic acceptance report.
12. After EXP-001 passes, replace recorded-Y input with real CSI/ISP capture
    and begin PTS/GMM2/CCL and real-noise characterization.

The experiment is complete only when all paths use the same central fusion
engine, the same saved dataset can be replayed without Blender running, and
physical-node replay agrees with the local reference within declared
tolerances.
