# Skyweave follow-up decisions

**Date:** 2026-07-17<br>
**Execution update:** 2026-07-20<br>
**Purpose:** Resolve the design questions raised after the first report and
turn them into the next implementation order.

Your attached notes remain the design authority. The linked voxel project and
the existing v1 implementation remain reference evidence, not requirements.

## Current direction

1. Start with a Blender-generated, 800 ft, recorded-frame experiment.
2. Finish the deterministic local pipeline, then replay those same per-camera
   Y sequences through physical nodes, the real switch, and the Jetson.
3. Treat real CSI/PTS/GMM2 behavior and outdoor noise as the next adaptation
   phase, while board build/recorded-Y enablement may run in parallel.
4. Keep two compatible localization frontends:
   - sparse pixel/mask back-projection for finding hypotheses when correspondence
     is unknown;
   - continuous multi-ray estimation for metric refinement and uncertainty.
5. Feed the resulting measurements into an EKF-capable tracker before a turret
   exists; refined XYZ is the first linear update and raw bearings remain a
   comparison/extension.
6. Add the turret later as an asynchronous bearing/appearance sensor.
7. Keep conditional filter covariance separate from calibration/mount/clock
   systematic bounds.
8. Use RTK-B/F9P for the physical pose-calibration boundary. Do not treat
   BNO055 or synthetic pose as field truth.
9. Treat high-altitude output as bearing-dominant; precise high-altitude depth
   is not a first-system requirement.

The central data path is:

~~~text
frame
  -> GMM2 foreground proposal
  -> threshold / morphology / CCL / persistence
  -> Observation2D
  -> event-time alignment
  -> voxel or pair hypotheses
  -> continuous metric refinement
  -> EKF target-state update
  -> track history / motion profile / optional class evidence
~~~

Camera pose calibration is a different problem from target state estimation.
The former is solved offline and versioned; the latter is filtered online.

## 1. Triangulation tradeoffs

Triangulation is not bad. It is the right continuous estimator when
observations are compact and correspond to the same physical feature. Its
limitations are specific.

### 1.1 Correspondence

If camera A's blob is paired with the wrong blob in camera B, the two rays can
still intersect at a plausible location. More cameras can reinforce a wrong
hypothesis if the false pairing is consistent.

This is the main reason to keep a voxel/evidence frontend: it can search for
locations supported by many pixel-level observations before committing to a
particular 2D-to-2D correspondence.

### 1.2 Weak depth geometry

For useful projected baseline *B_perp*, range *Z*, and relative bearing error
*sigma_theta*:

~~~text
cross-range uncertainty ~= Z * sigma_theta
range uncertainty       ~= Z^2 * sigma_theta / B_perp
~~~

Depth error grows quadratically with distance and improves only linearly with
useful baseline. Near-parallel rays have a long uncertainty ellipsoid, not a
spherical error.

### 1.3 Shared errors

Independent pixel noise can average down. A common yaw bias, incorrect focal
length, wrong distortion model, or common timestamp offset is correlated across
measurements. Adding cameras can make the estimate look more confident while
leaving the same bias.

### 1.4 Extended objects

The centroid in one camera may be the nose while the centroid in another is the
center of a silhouette. Those are not the same 3D point. A contour or mask
should produce a volume/cone likelihood, not be forced into one exact point.

### 1.5 Detection versus estimation

Triangulation says where a supplied set of rays is most compatible. It does
not decide whether a pixel change is a bird, cloud edge, camera shake, or
target. Detection, association, geometry, and tracking stay separate.

## 2. Deterministic initializer

Suppose each camera gives a line:

~~~text
line_i(lambda) = camera_center_i + lambda * unit_direction_i
~~~

Noisy lines do not meet at one exact point. For candidate point *x*, define:

~~~text
P_i = I - direction_i * direction_i^T
~~~

This projects the error onto the plane perpendicular to camera *i*'s ray. It
removes the component along the ray and retains the shortest-distance error to
that line.

The initializer solves:

~~~text
minimize_x  sum_i weight_i * || P_i * (x - camera_center_i) ||^2

A = sum_i weight_i * P_i
b = sum_i weight_i * P_i * camera_center_i
A * x0 = b
~~~

*X0* is the point closest to all weighted rays. With two rays it is the
midpoint of the common perpendicular when they are not parallel. It is
deterministic if camera order, floating-point policy, and tie handling are
fixed. It is a seed, not the final answer.

Refine *x0* by minimizing robust angular or reprojection error, then compute
anisotropic covariance. A voxel peak can supply *x0*; this is not triangulating
twice. The voxel stage searches for a mode, and the continuous stage measures
that mode accurately.

## 3. Averaging camera errors

Yes, a weighted all-ray solve combines independent information. No, adding
cameras does not guarantee that error disappears.

Use one information-weighted solve, not an unweighted average of pairwise XYZ
answers:

~~~text
information ~= J^T * R^-1 * J
covariance  ~= inverse(information)
~~~

Independent angular noise often improves approximately as *1 / sqrt(N)*.
Camera placement still matters: three cameras with nearly identical viewing
directions add little depth information, while two cameras with a useful
projected baseline can be stronger.

For the first implementation:

~~~text
tentative:
  at least 2 distinct cameras

confirmed:
  support_count >= N_min
  distinct camera/node IDs
  minimum triangulation angle
  condition number below limit
  normalized residual below limit
  persistence over K observations
~~~

For one synthetic target, greedy highest-support selection is fine. Keep
rejected hypotheses in the log. Full MHT/JPDA is not needed to prove the first
vertical slice.

## 4. Evidence should follow apparent image scale

Range affects likely representation, but target size, contrast, blur, and
occlusion matter just as much.

| Image evidence | Payload | First useful estimator |
|---|---|---|
| Less than about 2 pixels | centroid/bearing plus large covariance and intensity change | bearing update or voxel ray evidence |
| Small compact blob | floating centroid, 2x2 pixel covariance, bbox, area | pair hypothesis and continuous refinement |
| Resolved blob | centroid, bbox, contour or downsampled mask | voxel/cone evidence plus refined point/volume |
| Large silhouette | mask/RLE, contour, crop reference | visual-hull-like local evidence |
| Persistent proposal | all above plus image-plane velocity/age | central association and EKF |

Use one Observation2D type with optional fields. Do not make the protocol choose
between raw frame and centroid as mutually exclusive worldviews.

## 5. What a 2D contour becomes in 3D

A foreground pixel does not become a 3D point. It becomes a ray through the
camera center. A filled mask therefore becomes a bundle of rays, or
approximately a viewing cone/frustum.

### Ray accumulation

For each sampled foreground pixel, traverse relevant 3D cells and add a
normalized contribution. This is the direct descendant of the linked
pixel-to-voxel code.

### Voxel projection

For each candidate voxel, project its centre (or footprint) into each camera
and ask whether the projected location falls inside the foreground mask. This
can be cheaper after a coarse candidate volume has limited the search.

The intersection of several silhouettes is a visual-hull-like occupied volume.
It is not a precise surface or unique point. A contour does not need to become
a dense 3D mesh.

Cost is approximately:

~~~text
foreground samples * traversed cells per ray * camera count
~~~

So a 20x20 mask across nine cameras is manageable in a local volume; casting
every pixel of a 2.3 MP frame through a kilometre-scale grid is not. Start
with centroid rays for tiny targets, downsampled masks for larger targets,
cone bounds, a bounded range/frustum ROI, and temporal persistence.

## 6. Where voxels and triangulation fit

They are complementary, not duplicate estimators.

~~~text
Observation2D from each camera
  |
  +--> known/compact correspondence
  |      -> direct weighted ray initializer
  |      -> robust reprojection refinement
  |
  +--> ambiguous pixels/masks
         -> sparse voxel or cone evidence
         -> connected components / NMS / weighted peak
         -> weighted ray initializer
         -> robust reprojection refinement

refined position + covariance + quality
  -> EKF
~~~

The voxel frontend is not overscope if it is scoped as candidate generation. It
becomes overscope when it is simultaneously a dense global world model, a
temporal database, the tracker, the uncertainty model, and the browser format.

## 7. Accuracy audit of the reference code

The vendored source is at
[v1/reference/pixel-to-voxel-projector](../../v1/reference/pixel-to-voxel-projector).
Its audited upstream commit is
[011722ac4e7403de4dbf764b6877a6561a0cf45c](https://github.com/ConsistentlyInconsistentYT/Pixeltovoxelprojector/tree/011722ac4e7403de4dbf764b6877a6561a0cf45c).

It proves the software shape:

~~~text
changed pixel -> pinhole ray -> world rotation -> ray-box traversal -> vote
~~~

It does not provide an accuracy bound:

| Reference choice | Accuracy consequence |
|---|---|
| N=500, voxel_size=6 | Cell-centre output has a nominal 6 m quantization scale; worst cell-centre distance is about 5.2 m before other errors. |
| Horizontal FOV camera model | No calibrated focal asymmetry, principal-point offset, distortion, or enclosure-window effect. |
| Hand-entered Euler pose | No pose covariance or surveyed world frame. |
| Adjacent-frame absolute difference | Leading/trailing edges; no adaptive background model. |
| One accumulated grid over all frames/cameras | Temporal smearing and no exposure-time alignment. |
| attenuation computed but not applied | Long rays can receive more total vote because they traverse more cells. |
| Random noise from random_device | Non-reproducible runs and no controlled error study. |
| Brightest-percentile display | Visualization threshold, not confidence or error. |

The nominal 6 m cell size cannot substantiate a 5 m target claim. A weighted
cluster centroid or continuous refinement could improve it, but the reference
does not measure either.

The correct accuracy check is a deterministic harness with independent truth,
perturbed calibration/timing/pixels/masks, voxel candidate generation,
continuous refinement, and position/covariance/residual/support metrics.

## 8. Dense-grid optimization

The Orin Nano Super shares an 8 GB unified memory pool between CPU, GPU,
runtime, frame buffers, and applications. Do not treat all 8 GB as available
to the accumulator.

| Cubic grid | FP32 accumulator | FP16 accumulator |
|---:|---:|---:|
| 512^3 | 0.54 GB | 0.27 GB |
| 768^3 | 1.81 GB | 0.91 GB |
| 1024^3 | 4.29 GB | 2.15 GB |
| 1280^3 | 8.39 GB | 4.19 GB |
| 1536^3 | 14.50 GB | 7.25 GB |

These are decimal GB for one array. They exclude counts, temporary buffers,
CUDA context, decoded frames, OS memory, protobuf buffers, and visualization.

Use this hierarchy:

1. frustum clip;
2. coarse global or angular/range pass;
3. top-K/NMS;
4. fine local bricks around modes or EKF predictions;
5. decay/recycle instead of indefinite accumulation.

For an 800 ft first target, a local 400^3 FP16 volume is roughly 128 MB. That
is a reasonable experiment, not a full-sky representation.

Benchmark camera-frustum bricks, hashed 8^3/16^3 blocks, octrees,
direction-plus-inverse-range bins, and track-local Cartesian ROIs. Use FP16
for bounded scores only after quantization/overflow tests; keep counts,
normalization, covariance, and final refinement in FP32.

CUDA one-thread-per-changed-pixel is a valid first kernel, but shared-cell
atomic contention, irregular DDA traversal, grid clearing, and memory bandwidth
may dominate. Build the CPU oracle first, then compare CUDA peak location,
support, and error, not only visual similarity.

## 9. GMM2, noise, and edge output

Your product intuition is right: establish what the scene normally looks like,
then flag current pixels that do not fit. The implementation detail is that
GMM2 is an adaptive mixture of per-pixel Gaussian components, not merely one
saved background frame.

The intended edge path is:

~~~text
Y plane
  -> GMM2
  -> threshold hysteresis
  -> morphology
  -> CCL
  -> camera-specific area/shape filters
  -> 2-of-3 temporal persistence
  -> Observation2D
~~~

Hardware acceleration should make this much cheaper than a single ARM-core
implementation. It does not prove end-to-end throughput: buffer allocation,
CSI/ISP copies, warm-up, CCL, serialization, and thermal behavior still need
measurement.

Start with sky/static ROI masks, per-camera threshold/hysteresis, morphology,
CCL bounds, scene-wide exposure-change veto, and short persistence. Do not
delete a few-pixel target with a generic minimum-area threshold. Freeze or
slow background learning around active tracks and keep masks/luma clips for
replay.

## 10. Timing and the real-time data path

### What to send

Normal production traffic should be:

1. required measurement protobuf: observations, timestamps, covariance,
   calibration revision, sequence, and health flags;
2. optional evidence packet: cropped packed-Y/gray patch, RLE mask, or
   short-ring reference for classifier/debug;
3. low-rate health packet: temperature, FPS, drops, GMM state, memory,
   firmware/config hashes.

Do not send full frames on the measurement path. Full SC3336 luma at 30 fps is
about 724 Mb/s before overhead, so it cannot fit over 100 MbE. Crops are fine
when independently droppable. RTSP can remain a debug stream; custom UDP is
appropriate for fresh low-latency measurement data.

Each measurement datagram needs sequence/session IDs, capture time and clock
domain, calibration/config revision, a size below path MTU, loss/reorder
counters, a stale-observation TTL, and drop-old behavior under congestion.

For small metadata messages, protobuf encode/decode should be negligible next
to frame processing and DDA. Confirm that with a microbenchmark, reuse buffers,
avoid per-frame heap churn on the edge, and use a bounded C implementation such
as nanopb only if it fits the vendor environment. Do not put a large raw mask
or crop into the latency-critical message; reference a separate optional
packet instead.

### Timestamp layers

Keep every layer:

~~~text
sensor/VI PTS
frame sequence
node monotonic dequeue time
node publish time
central receive time
mapped common capture time
capture-time uncertainty
~~~

First establish what the RV1106 PTS means on the deployed driver. If exposure
hardware timestamping is unavailable:

1. use the best V4L2/VI timestamp and frame sequence;
2. measure fixed sensor/ISP/dequeue latency;
3. synchronize clocks with chrony/NTP initially;
4. estimate per-node offset and drift centrally;
5. store fit residual as `sigma_t`;
6. reject/downweight observations outside a lateness window.

PTP is worth testing if the Ethernet path exposes a usable PHC. It synchronizes
clocks, not sensor exposure, unless connected to a trigger/frame-valid event.
An MCU only helps if it sees VSYNC/frame-valid or controls a shared trigger.

Use a simple fitted mapping:

~~~text
t_common = offset_node + rate_node * t_local
~~~

For an observation with time uncertainty `sigma_t`, add approximately
`||velocity|| * sigma_t` to positional uncertainty, or
`focal_length_px * angular_rate * sigma_t` to pixel uncertainty. Reject or
downweight when it exceeds the target-profile budget.

No optical LED is required as a deployment feature. A shared GPIO/PPS or
photodiode/strobe event is an optional lab calibration method only.

## 11. Filtering and the later turret

Filtering starts before the turret:

~~~text
edge cleanup
  -> time alignment
  -> cross-camera candidate association
  -> triangulated or raw-bearing measurement
  -> EKF prediction/update
  -> track history and motion profile
~~~

Use an EKF now if it consumes raw pixel/bearing measurements:

~~~text
state x = [px, py, pz, vx, vy, vz]
f(x, dt) = constant-velocity propagation
h_i(x) = camera_i bearing or pixel projection
~~~

If the input is a refined XYZ point from voxel/triangulation, the measurement
model is approximately linear and a standard KF is simpler. Starting with an
EKF is reasonable; forcing one where h is linear is unnecessary.

When the turret arrives, feed its timestamped bearing asynchronously into the
same EKF and use the predicted state to point it. A co-located rotating turret
improves angular sampling and appearance evidence but does not create
instantaneous range. It improves depth when physically displaced or paired with
another useful baseline, or through a well-calibrated temporal-motion model.

## 12. BNO055 and RTK-B

At 800 ft (243.84 m), orientation error produces approximately:

~~~text
0.1 degree  -> 0.43 m lateral error
1.0 degree  -> 4.26 m lateral error
2.5 degrees -> 10.6 m lateral error
~~~

BNO055 is useful for an orientation prior, mount movement detection, and
sanity-checking a solved pose. It is not final optical-axis calibration for a
5 m goal.

The ZED-F9P RTK receiver specification is adequate for static node translation
at the 800 ft/5 m scale under good correction, antenna, sky, and multipath
conditions. It does not directly provide camera yaw/pitch/roll or the
antenna-to-optical-centre lever arm.

One fixed base at the Jetson plus one rover occupied sequentially at each
static node is a valid survey procedure:

1. establish and record the base datum/correction source;
2. occupy each node for a stable fixed solution;
3. express all positions in one ENU/world frame;
4. measure the antenna-to-camera lever arm;
5. solve orientation with ChArUco/control points or a known moving target;
6. refine poses and timing jointly with bundle adjustment.

Sequential rover positions are enough for static nodes. A dynamic ground-truth
flight needs a rover on the target and separate calibration/evaluation paths.

## 13. Blender, Isaac Sim, RKAIQ, and synthetic RAW

RKAIQ should be treated as a Rockchip ISP tuning/runtime stack, not a scene
renderer or proof that a Blender/Isaac frame equals SC3336 Bayer data.

Use a project-side sensor emulator:

~~~text
Blender/Isaac scene + truth
  -> calibrated camera render
  -> target motion / exposure / rolling-row model
  -> optional Bayer + noise + quantization model
  -> optional ISP/Y conversion model
  -> exact-resolution recorded frame envelope
  -> reference GMM2 or real RV1106 source
  -> protobuf/UDP
  -> central alignment/localization/EKF
  -> metrics/replay/visualization
~~~

Blender RGB/EXR/PNG is sufficient for the first Y/GMM2/reference pipeline if
resampled to deployed resolution and passed through a declared Y/noise model.
For later raw tests, render linear intensity, apply CFA mosaic, exposure,
black level, shot/read noise, gain, clipping, RAW8/10/12 packing, and declared
distortion/ISP stages. Label this `synthetic_sensor_model`, not SC3336 raw.

The important full-stack proof is that a RecordedFrameSource emits the same
frame envelope as live input, then uses the same protobuf, packet loss,
alignment, voxel/triangulation, EKF, recorder, and visualizer paths.

## 14. 4D evidence versus a finite-lag trajectory

Your intended data item is straightforward:

~~~text
Evidence4D = (x, y, z, t, score, supporting observations)
~~~

Store those points and EKF states in a bounded ring buffer. This makes the
track visible over time and supports motion profiling.

A finite-lag smoother is a different, optional algorithm. It keeps the last
fraction of a second or few seconds of states and re-optimizes them when a late
measurement arrives. It then discards/marginalizes older states. Start with
the EKF plus ring buffer; add a finite-lag smoother only if measured
out-of-order data makes it worthwhile.

## 15. 800 ft first milestone

See [EXP-001](experiments/EXP-001_800FT_SYNTHETIC_FULL_STACK.md).

At 800 ft with illustrative 60 degree horizontal FOV and SC3336 width:

- one pixel is about 0.11 m cross-range;
- a 1 m target is about 9 pixels wide;
- a 5 m target is about 45 pixels wide;
- with 10 m useful baseline, one-pixel bearing error gives roughly 2.7-3.0 m
  depth uncertainty before calibration/timing bias.

Use 800 ft as a controlled geometry target, not a proxy for 25,000 ft
airliner performance. Report range and cross-range separately. Treat 5 m as a
provisional clean-case goal, then sweep harder perturbations.

For the eventual high-altitude case, the output is bearing-dominant. A precise
5 m depth target is not a requirement. With a fully projected 137.16 m / 450
ft baseline, the approximate relative-bearing error that 5 m range standard
deviation would require is:

| Slant range | Required relative-bearing error |
|---:|---:|
| 25,000 ft | about 2.4 arcseconds |
| 35,000 ft | about 1.2 arcseconds |

That is far below one pixel for the illustrative lenses and must include
centroid bias, optical calibration, pose, atmosphere, timing, and association.
Report cross-range and depth separately, and mark depth weak or invalid when
the measured geometry does not support a useful range estimate.

## 16. Current implementation order

1. Freeze frame/pixel/time, observation, conditional-covariance, and
   systematic-bound semantics.
2. Prove analytic geometry and run the Tier-0 Monte Carlo error budget.
3. Freeze one exact EXP-001 manifest and its range/cross-range, velocity,
   acquisition, false-track, and coverage gates.
4. Generate deterministic per-camera Blender frames and truth sidecars.
5. Build the host foreground detector and calibrated `Observation2D`.
6. Build direct and voxel hypotheses, continuous refinement, robust/outlier
   gates, and the EKF-capable track lifecycle.
7. Inject image, calibration, detection, rolling-shutter, time, packet, and
   node faults one at a time, then in held-out combinations.
8. Implement `RecordedYFrameSource`, protobuf, UDP, and deterministic replay.
9. Replay the same saved data through physical nodes, the real switch, and the
   Jetson in deterministic and wall-clock modes.
10. Complete the wired synthetic acceptance report.
11. Replace recorded-Y with real CSI/ISP capture; measure PTS, line time,
    GMM2/CCL configuration, resources, and real masks.
12. Characterize real sky noise, F9P/optical calibration, and PETG-CF mount
    stability in the intended low-wind environment.
13. Add the turret and classifier only after fixed-camera tracking has
    repeatable real evidence.
14. Consider CUDA only after profiling, and drone/autonomy only after the
    detection/tracking field milestone.
