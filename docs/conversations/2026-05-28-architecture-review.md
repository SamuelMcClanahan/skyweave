# Architecture Review Conversation - 2026-05-28

This note captures the working discussion around AeroVis MVP direction, with
emphasis on pixel-to-voxel projection, far-aircraft tracking, close-drone
reconstruction, calibration, and time synchronization.

## User Goal

Track barely visible aircraft around 25,000 ft altitude using distributed camera
arrays pointed at the sky, while also supporting more precise reconstruction and
motion profiling of closer drones.

The current demo hardware is OV9281-based. The intended v1 direction is many
cheap camera nodes, likely Luckfox Pico Ultra-class boards with SC3336 color
MIPI-CSI cameras, reporting to a central Jetson over Ethernet. Later sensors may
include IMX415-class or better cameras. Later compute may scale from Rubik Pi or
MacBook Pro M4 to Jetson Orin Nano Super, AGX, or Thor-class hardware.

## Main Architectural Clarification

Voxel projection and triangulation are not competing geometry models. Both are
ways to evaluate where calibrated camera rays agree in 3D.

- Triangulation solves for a continuous 3D point or track state.
- Voxel scoring discretizes the search into candidate cells and assigns each
  cell a likelihood/evidence score.

For far aircraft, the object is effectively point-like. A voxel grid can help
initialize or visualize uncertainty, but the final estimator should probably be
a ray/bearing track filter with altitude, velocity, and motion priors.

For close drones, the object spans more pixels and bounded voxel reconstruction
or visual-hull scoring becomes more useful.

## Current Recommended Split

Core shared layer:

```text
calibration -> pixel to ray -> ray likelihood -> track estimator
```

Mode A, far aircraft:

```text
tiny blob detection -> bearing packets -> ray association -> MAP/Kalman/particle track
```

Mode B, close drones:

```text
foreground masks -> local sparse voxel gather -> 3D centroid/shape estimate -> track
```

## Voxel Memory And Speed Strategy

Avoid a full uniform sky-volume grid. Use one or more of:

- object-centered local voxel volumes around an existing track estimate;
- frustum-intersection regions from camera detections;
- coarse-to-fine scoring;
- sparse `32x32x32` or similar active chunks;
- quantized `uint8` or `uint16` evidence buffers;
- range shells or inverse-depth bins for far sky;
- time-decayed evidence volumes.

The major risk is search failure: if the broad 2D detector misses the object,
the local voxel chunk may never get allocated. A cheap all-sky / full-frame
motion search still needs to run ahead of any local voxel refinement.

## Frustum, Ray, Ring, And Shell Concepts

A single 2D detection does not identify a 3D point. It identifies a cone or
thin pyramid in 3D:

```text
camera center -> pixel bbox corners -> 3D detection frustum
```

For a tiny point-like target, this can be treated as a central ray plus angular
uncertainty. For a blob, the 2D bbox/mask back-projects to a cone/frustum.

Range shells are candidate depth bands along that cone:

```text
ray direction + range bin [r0, r1]
```

For far aircraft, range shells can encode priors like plausible altitude or
distance. Instead of scoring a giant XYZ cube, score candidate ranges along rays
and only refine where multiple cameras agree.

## Hungarian Assignment And Mahalanobis Distance

The Hungarian algorithm is useful for assignment, not for the geometry itself.

Good places to use it:

- assigning multiple 2D detections across cameras;
- assigning multiple 3D measurements to existing Kalman tracks;
- resolving multiple candidate voxel peaks against active tracks.

Mahalanobis distance is useful when each candidate has covariance:

```text
d2 = (z - Hx)^T S^-1 (z - Hx)
```

Use Hungarian to minimize total assignment cost when there are multiple
detections/tracks. Use Mahalanobis gates to reject physically unlikely matches.

## Voxel Refinement Around Intersections

Yes, once approximate ray agreement is known, run optimization only in the local
region. A practical pipeline is:

1. detect moving pixels or blobs in each camera;
2. convert detections to rays/cones;
3. form candidate intersections or coarse voxel peaks;
4. allocate a small local grid around the candidate;
5. score the local grid by projecting voxels back into all camera masks/images;
6. refine the peak with continuous reprojection-error minimization;
7. feed the resulting measurement and covariance into the tracker.

This is more efficient than ray-casting through a full world grid.

## Clouds, Birds, And Motion Detection

KNN background subtraction can help but is not enough by itself.

Clouds are difficult because they move, deform, and change brightness. Birds are
harder because they are real moving targets. Useful filters beyond KNN:

- fixed exposure/gain where possible;
- temporal high-pass / frame differencing for tiny point targets;
- slow sky background modeling;
- star/sun/cloud masks when relevant;
- multi-camera ray consistency;
- track continuity gates;
- angular speed and acceleration limits;
- apparent size and flicker features;
- altitude and range priors;
- optional classifier once enough data exists.

For SC3336 color cameras, converting to grayscale for motion detection is a
reasonable first step, but keep access to color channels for later filters. A
color sensor has a Bayer/color-filter penalty versus mono for faint point
targets, but scale and cost may outweigh that for v1.

## Time Sync

Time sync is a first-class architecture problem.

Approximate pixel error from timestamp skew:

```text
pixel_error ~= focal_length_px * angular_rate_rad_s * time_error_s
```

Close drones are more sensitive than far aircraft because angular rates are
higher. A 30 m/s drone at 100 m is about 0.3 rad/s. With a 1000 px focal length,
10 ms skew is about 3 px.

Recommended staged approach:

1. MVP: software timestamps on receive/read, with measured skew logs.
2. V1 early: chrony or NTP across Ethernet, plus timestamp-at-frame-capture as
   close to the camera driver as possible.
3. V1 better: PTP/linuxptp if the NIC/switch/driver path supports it.
4. V1.5: GNSS PPS at each node or a central hardware trigger.
5. Best: hardware-triggered exposure plus frame-start timestamps tied to a
   synchronized hardware clock.

GNSS modules help only if the camera timestamp is tied to PPS or hardware frame
events. GNSS-synchronized system clocks alone still leave camera pipeline jitter
unless measured.

## Calibration Options Discussed

Calibration must estimate both intrinsics and extrinsics.

Near-field / practical:

- ChArUco or AprilTag board for intrinsics and near extrinsics;
- a large elevated AprilTag can help if every camera sees it clearly, but at
  long distances the tag must be very large and sharply imaged;
- laser rangefinder plus measured angles can provide rough extrinsics, but
  angular measurement error will dominate unless the procedure is careful.

Sun-based:

- useful as an orientation reference because the sun direction is known from
  time/location;
- risky for full extrinsic calibration because it provides direction, not
  position/baseline;
- requires safe optics/exposure handling.

ADS-B averaging:

- potentially useful for long-run sanity checks or coarse bearing calibration;
- not reliable as the primary calibration source because ADS-B timestamps,
  aircraft position uncertainty, barometric altitude, and unknown exact aircraft
  shape/visual centroid introduce bias.

GNSS on camera nodes:

- useful for coarse camera positions;
- ordinary GNSS is often meter-level, which is not enough by itself for long
  baseline precision;
- averaging helps static position but not orientation.

RTK drone calibration:

- plausible and valuable for v1.5;
- requires knowing the lever arm from RTK antenna to visible target/LED/marker;
- requires synchronized camera frames and RTK logs;
- best done with a bright active marker or LED array on the drone, not just the
  drone body;
- use many 3D observations across the volume in a bundle adjustment.

## Synthetic Data

Isaac Sim-style synthetic data is useful, especially before field deployment.

Use it for:

- camera layout sensitivity studies;
- pixel noise and timestamp skew sweeps;
- Kalman/process-noise tuning;
- false positive and bird/cloud stress tests;
- voxel scoring parameter sweeps;
- regression tests for geometry.

Do not overtrust photorealism. The highest-value synthetic tests are geometric:
known camera poses, known target trajectory, known pixel noise, and measured
reconstruction/track error.

## Open Technical Decisions

- Exact coordinate convention: `T_world_cam`, OpenCV camera frame, and ray
  direction signs must be locked first.
- Far-aircraft estimator: EKF/UKF/particle filter/MAP smoother.
- Voxel scoring function: additive evidence, log odds, visual hull, or
  reprojection likelihood.
- Chunk allocation policy: from 2D detections, predicted tracks, or both.
- Time sync target for MVP and v1.
- Calibration target procedure for v1 field deployments.
- Whether SC3336 rolling-shutter effects need explicit correction.

## Source Notes

- Luckfox Pico Ultra public docs list RV1106, MIPI CSI 2-lane, 10/100M
  Ethernet, 256MB DDR3L, ISP max 5MP at 30 fps, and 1 TOPS-class NPU for G3.
- Luckfox SC3336 camera docs identify the module as a 3MP camera for the Pico
  ecosystem.
- NVIDIA lists Jetson Orin Nano Super as 67 INT8 TOPS with 8GB LPDDR5 and
  102GB/s memory bandwidth.
- Hardware PTP accuracy depends heavily on NIC, switch, driver, and hardware
  timestamp support.

## Technical Points, Condensed

With 15-50 m baselines and 6-8 cameras, triangulation becomes much more viable,
especially for association and false-positive rejection. More cameras reduce
uncertainty, but they do not erase the basic range geometry issue: far range
error still depends heavily on baseline, focal length, pixel error, and camera
pose accuracy.

Use Hungarian assignment for matching detections/candidate measurements to
tracks. Use Mahalanobis distance for gating and assignment cost. Do not use
Hungarian to make voxels align; use voxel likelihood scoring plus continuous
reprojection optimization for that.

Frustum/range shell idea: a 2D detection box projects into a 3D cone/pyramid
from the camera. A range shell is just a depth band along that cone. Intersect
or score those shells across cameras instead of filling a massive world grid.

The rough-intersection-then-local-optimization intuition is right. Do coarse
ray/voxel candidate generation, allocate a local chunk, score it, then refine
continuously using reprojection error.

KNN helps, but clouds/birds need more: fixed exposure, temporal high-pass,
multi-camera ray consistency, track continuity, angular velocity/acceleration
limits, apparent size/flicker, altitude/range priors, and eventually classifiers
from field data.

For Luckfox Pico Ultra-class nodes, public docs list 10/100M Ethernet, not GbE.
A GbE switch is still fine, but each node may be 100M. That matters for
streaming raw frames; edge-side detection packets are much more realistic.

V1 time sync path: start with chrony/NTP and measured skew, then test linuxptp
only if the MAC/driver exposes useful timestamping, then move to GNSS PPS or
hardware trigger. GNSS helps most if frame-start timestamps are tied to PPS or a
synchronized hardware clock.

Calibration: AprilTag/ChArUco for near-field, sun for orientation sanity checks,
ADS-B only as weak long-run validation, GNSS for rough node positions, RTK drone
with a known lever arm plus visible LED/marker for real bundle adjustment. Laser
rangefinder/trig is usable for rough extrinsics, but angular measurement error
will dominate fast.
