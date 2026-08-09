# Skyweave v2 implementation framework

**Date:** 2026-07-15<br>
**Purpose:** Get a truthful vertical slice working quickly while Samuel retains
architectural control and reviews the project in small engineering segments.

This framework implements the conclusions in
[`RESEARCH_REPORT.md`](./RESEARCH_REPORT.md). It is intentionally not a request
to scaffold the whole future system at once.

The [2026-07-17 follow-up](./FOLLOWUP_DECISIONS_2026-07-17.md) records the
current execution choices, and
[EXP-001](./experiments/EXP-001_800FT_SYNTHETIC_FULL_STACK.md) is the first
synthetic full-stack milestone.

### Chosen execution order as of 2026-07-20

The first system milestone is now synthetic-first but physically wired. Fake
camera data from Blender is replayed by real nodes, crosses the real
Ethernet/protobuf/UDP path, and is fused by the real Jetson. Real CSI capture,
PTS characterization, and outdoor noise are the next adaptation phase.

The tracks below remain the ownership map. The current dependency order is:

1. freeze frame, pixel, time, observation, covariance, and systematic-bound
   semantics;
2. prove projection, direct geometry, refinement, and uncertainty analytically;
3. use Monte Carlo to freeze one canonical 800 ft EXP-001 scene and its
   statistical gates;
4. generate deterministic per-camera Blender frames and truth sidecars;
5. build the host luma foreground detector and calibrated `Observation2D`;
6. build direct/voxel hypotheses, robust refinement, outlier gates, and the
   EKF-capable track lifecycle;
7. inject image, geometry, detection, time, and packet faults one at a time;
8. implement the common recorded-Y, protobuf, UDP, and replay paths;
9. replay the same Blender dataset through physical nodes and the real switch;
10. complete the wired synthetic acceptance report;
11. replace recorded-Y input with real CSI/ISP input and characterize
    PTS/GMM2/CCL on the exact board;
12. measure real sky noise, calibration, pose stability, and controlled field
    accuracy before adding the turret, classifier, CUDA backend, or drone.

Board build/toolchain bring-up and recorded-Y injection may run in parallel as
enabling work. They do not move real-camera behavior into the first milestone.

## 1. Working agreement

### Human authority

Samuel owns:

- the target operating envelope and success criteria;
- architectural decisions and accepted tradeoffs;
- coordinate, timing, packet, calibration, and estimator semantics;
- acceptance of each implementation chunk; and
- any change to scope.

The two notes attached on 2026-07-15 are the current design intent. Existing
`v1` code and old specifications are characterization evidence only.

### Appropriate AI use

AI can:

- research a bounded engineering question with primary sources;
- propose one ADR or interface for review;
- write focused tests for an agreed behavior;
- implement one accepted seam;
- diagnose measured failures;
- port mechanical serialization/build code after the contract is accepted;
- compare a v2 result with an existing v1 fixture; and
- build the disposable JavaScript visualization against a read-only schema.

AI should not:

- silently choose units, time semantics, coordinate frames, or covariance
  meaning;
- scaffold speculative modules merely because they appear in the roadmap;
- port the v1 runtime wholesale;
- tune an estimator and report accuracy on the same generated cases;
- turn a hardware or performance hypothesis into a claim;
- replace a reviewed interface while “cleaning up”; or
- put tracking/fusion logic in the browser.

### Review unit

Every nontrivial change is delivered as one review unit:

1. **Question:** the one engineering question being answered.
2. **Decision:** a short ADR or already accepted contract.
3. **Tests first:** exact behaviors and failure cases.
4. **Implementation:** the smallest code that satisfies those tests.
5. **Evidence:** test output, benchmark, replay diff, or hardware measurement.
6. **Review map:** files and symbols Samuel should inspect.
7. **Deferred list:** deliberately unimplemented ideas.

Avoid mixed “geometry + networking + tracker + UI” diffs. A review unit should
normally change one boundary and its tests.

## 2. Two definitions of working

### Offline development gate

The offline development gate is satisfied when it:

1. reads a deterministic recording of calibrated, timestamped pixel
   observations from at least three cameras;
2. converts them to distortion-aware world bearings;
3. creates/associates a single-target hypothesis;
4. emits a continuous 3D measurement with anisotropic covariance and quality
   status;
5. updates a baseline Cartesian constant-velocity track;
6. records all inputs/outputs with configuration and calibration revisions;
7. replays deterministically; and
8. emits a sparse, read-only visualization frame.

No live UDP, RV1106 firmware, CUDA, UKF, classifier, turret, or dense 4D grid is
required for this milestone.

### Wired synthetic vertical slice

This is the current first system milestone. It is working when:

1. Blender produces one deterministic Y/luma recording and truth stream per
   virtual camera;
2. physical nodes replay those camera-specific recordings through the reviewed
   edge observation boundary;
3. real protobuf/UDP packets cross the intended switch to the Jetson;
4. the same central fusion engine used by local replay produces candidates,
   refined measurements, and an EKF track;
5. deterministic and wall-clock replay modes distinguish synthetic capture
   time from node scheduling and packet arrival;
6. image, calibration, time, detection, packet, and node faults are injectable
   and recorded;
7. held-out acceptance scenes report range/cross-range error, velocity error,
   false tracks, acquisition age, outlier behavior, and resource use;
8. conditional covariance coverage and systematic-error sensitivity are
   reported separately; and
9. every output can be traced to its exact observations, seed, configuration,
   and truth manifest.

This milestone proves architecture and robustness to modeled faults. It does
not prove that the simulated noise matches the SC3336, RV1106 camera path, or
the outdoor environment.

### First field MVP

The field MVP is working when:

1. two or three real fixed cameras emit bounded observations over the selected
   transport;
2. each observation has characterized capture-time semantics and uncertainty;
3. calibration is versioned and evaluated on held-out control points;
4. a controlled dynamic target produces repeatable 3D tracks;
5. predicted covariance has reasonable empirical coverage;
6. packet loss/reorder and node restarts do not corrupt identity or time;
7. raw evidence can be replayed; and
8. a measurement report states both accuracy and the conditions under which it
   was achieved.

High-altitude-aircraft performance is a later operating-envelope experiment,
not the definition of the first field MVP.

## 3. Non-negotiable invariants

1. **Capture time describes a defined exposure event.** It never means “socket
   received,” “frame dequeued,” or “packet published.”
2. **Every value has a frame and unit.** A bare `position`, `rotation`,
   `timestamp`, or `confidence` is invalid at an external boundary.
3. **Pixel convention is fixed.** Define origin, axis directions, pixel-centre
   convention, image size, crop, and scale.
4. **Observations are immutable evidence.** Estimation may reject one, but
   cannot rewrite what the edge reported.
5. **Calibration is versioned.** Every observation identifies the exact
   intrinsics, distortion, pose, line timing, and mapping used.
6. **Detector confidence is not covariance.** Pixel uncertainty, geometric
   quality, learned class probability, and track existence are separate.
7. **Conditional covariance and systematic error are separate.** The runtime
   filter covariance conditions on the current calibration/clock model;
   calibration, mount, and other shared bias bounds remain separately visible.
8. **Local IDs are scoped.** Use
   `(node_id, camera_id, boot_id, local_track_id)`.
9. **The center owns global identity.** Edge nodes do not permanently suppress
   targets or assign global object IDs.
10. **Voxels are a computation/view, not automatically the state.**
11. **Live, replay, and simulation use one fusion engine.**
12. **The visualizer is read-only.** It can disappear without changing an
    estimate.
13. **v1 is not a runtime dependency.** v2 may consume copied/adapted fixtures,
    but production modules do not import v1 internals.
14. **Optimization follows a profile.** Python orchestration remains until a
    measured hot path proves otherwise.
15. **A rejected or late observation remains diagnosable.**

## 4. Proposed repository boundaries

Do not create every directory immediately. This is the intended ownership map
as modules become necessary.

```text
v2/
  pyproject.toml
  src/skyweave/
    contracts/          # canonical in-process types and generated wire adapters
    geometry/           # frames, camera model, SE(3), row time, Jacobians
    localization/       # hypotheses, triangulation, optional sparse volumes
    tracking/           # track bank, assignment, filters, lifecycle
    ingest/             # datagrams, source sessions, loss/reorder metrics
    alignment/          # clock mapping, watermarks, event-time batches
    fusion_engine/      # the one orchestration path used by live/replay/sim
    recording/          # immutable evidence/output logs and manifests
    calibration/        # datasets, solvers, immutable calibration store
    edge_reference/     # host luma detector and packet reference
    simulation/         # independent truth generation and fault injection
    ops/                # health, metrics, service/WebSocket adapters
  firmware/
    rv1106/             # vendor-toolchain C/C++; no central estimator code
  web/                  # disposable three.js consumer
  schemas/              # reviewed wire schema and compatibility fixtures
  tests/
    unit/
    property/
    golden/
    replay/
    fault/
    field/
  recordings/           # manifests/pointers; large assets may live externally
  docs/
    adr/
    experiments/
    reports/
```

### Boundary ownership

| Boundary | Owns | Explicitly does not own |
|---|---|---|
| `contracts` | IDs, versioned semantic types, units, coordinate/time metadata, validity | Network sockets, detector algorithms |
| `geometry` | Camera model, distortion, SE(3), projection/unprojection, rolling-row time, derivatives | Correspondence, track lifecycle |
| `edge_reference` | Recorded-luma foreground/CCL/tracklet reference | RK SDK details |
| `firmware/rv1106` | CSI/ISP luma, raw PTS, RKIVE GMM2/CCL, bounded emission | Global state or permanent rejection |
| `ingest` | Datagram validation, source sessions, duplicate/loss/reorder counters, buffers | Time grouping policy, geometry |
| `alignment` | Node-to-common clock mapping, uncertainty, watermark, lateness | Socket ownership, scoring |
| `localization` | Birth hypotheses, triangulation, conditioning/covariance, optional local volumes | Global identity |
| `tracking` | Per-camera gates, global track bank, assignment, lifecycle, estimator state | Pixel processing |
| `fusion_engine` | `process(evidence) -> FrameResult` orchestration | UI/server state |
| `recording` | Original evidence, manifests, hashes, results, deterministic replay | Estimation policy |
| `calibration` | Dataset capture/solve/validation and immutable revisions | Runtime operator state |
| `simulation` | Independent truth and adversarial inputs | Production runtime dependency |
| `ops` | Async write queues, health, metrics, WebSocket adapter | Estimation decisions |
| `web` | Rendering sparse results and explanations | Fusion/tracking logic |

## 5. Language and dependency policy

### Central system

Start in Python with numeric NumPy arrays at hot internal boundaries. Use
dataclasses or a validation library at file/network boundaries, not for every
inner-loop sample. SciPy/OpenCV are reasonable reference dependencies when
their use is explicit and covered by tests.

Move only a measured numerical kernel to Numba, C++, or CUDA. Preserve a
readable CPU oracle and parity tests.

### Edge firmware

Use C/C++ in the vendor's supported Linux cross-compile environment. The
current macOS `v2` workspace has no Rockchip headers/sysroot, so native macOS
`clang` is not the firmware build environment. Pin:

- vendor SDK/repository commit;
- toolchain/container image;
- board image/kernel/media stack;
- compiler and flags; and
- exact clone-board hardware revision.

Keep a host-side edge reference so algorithm work is not trapped inside the
vendor toolchain.

### Wire schema

Use one reviewed canonical model and generated encoders/decoders. During early
offline work, a canonical JSONL representation is useful for inspection. Do
not let the JSONL and binary schema acquire different semantics.

Freeze the binary choice only after the exact-board spike gives real packet and
toolchain constraints. Protobuf, FlatBuffers, or another mature generated
schema can work; compatibility tests matter more than preference.

### Browser

The JavaScript can be intentionally loose. Its containment boundary cannot be:

- it consumes a versioned `VizFrame`;
- it never writes estimator state;
- it does not reinterpret coordinate units;
- it tolerates missing optional fields;
- a static exported frame is the first fixture; and
- WebSocket/live mode reuses exactly the same payload.

## 6. Core semantic types

These are conceptual contracts, not permission to implement them all at once.

### Camera model and calibration

```text
CameraIntrinsicsRevision
  camera_id
  revision_id
  image_size
  pixel_convention
  projection_model
  K
  distortion_model + coefficients
  valid ROI / crop / scale
  focus, enclosure, capture-mode metadata
  solve dataset + validation report references

CameraExtrinsicsRevision
  camera_id
  revision_id
  parent_frame
  child_frame
  T_parent_child
  pose_covariance_6x6
  valid interval / installation identifier

RollingShutterRevision
  camera_id
  frame_timestamp_reference
  readout_direction
  line_delay
  line_delay_uncertainty
```

### Observation

```text
BearingObservation
  observation_id
  node_id, camera_id, boot_id, frame_sequence
  raw_sensor_pts + domain
  common_capture_time + domain + sigma
  centroid_uv + covariance_2x2
  bbox / area / optional mask or crop reference
  processing ROI-to-full-sensor transform
  exposure, gain, centroid row
  scoped local tracklet metadata
  calibration/configuration/firmware/model revisions
  separate heuristic, detector, and class scores
```

The name `BearingObservation` does not imply that edge firmware must compute a
world bearing. Transmit the calibrated full-sensor pixel observation; the
central geometry module creates the bearing under a named calibration revision.

### Localization

```text
TriangulationResult
  measurement_id
  reference_time
  position_world
  covariance_world_3x3
  supporting_observation_ids
  standardized_residuals
  minimum_triangulation_angle
  normal_matrix_condition
  status
```

### Tracking

```text
GlobalTrack
  global_track_id
  lifecycle_state
  state_time
  state_vector + covariance
  motion_model_id
  associated_observation_ids
  semantic_evidence
  birth/update/coast/delete reasons
```

### Fusion output

```text
FrameResult
  processing_interval
  accepted/rejected/late observation references
  localization results
  global track snapshots
  health/quality summaries
  VizFrame
```

## 7. Coordinate and time ADRs to settle before geometry code

### ADR-0001: coordinate frames

Proposed starting convention:

- world frame: local East-North-Up (ENU) in metres;
- camera optical frame: OpenCV convention
  `+x right, +y down, +z forward`;
- rigid transform notation:
  `T_A_B` maps coordinates expressed in frame `B` into frame `A`;
- rotations are active only through the transform definition, never inferred
  from a variable name;
- quaternions, if serialized, specify order and handedness;
- geodetic WGS84 input is converted once into the named local ENU origin.

Review questions:

- Is ENU the desired field/operator convention?
- Does `T_A_B` notation match Samuel's preference?
- Where is the ENU origin anchored and versioned?

Acceptance tests:

- exact basis-vector transforms;
- camera at origin looking along world axes;
- projection/unprojection round trip;
- independent hand-calculated two-camera case;
- serialized transform fixture decoded in both languages.

### ADR-0002: pixel convention

Proposed:

- integer pixel index `(u,v)` refers to the centre at those numeric
  coordinates, matching the chosen OpenCV routines;
- `u` increases right and `v` increases down;
- width/height and ROI bounds are explicit;
- every downscale supplies a reviewed affine transform back to full-sensor
  coordinates; and
- no normalization to `[-1,1]` at a contract boundary.

Tests must cover odd/even image sizes, ROI offsets, resize scale, and the four
corners.

### ADR-0003: time

Proposed:

- internal common time is integer nanoseconds in a named monotonic/common clock
  domain, not a float;
- frame timestamp semantics explicitly choose start, midpoint, or another
  calibrated exposure reference;
- each observation retains the raw device PTS and common mapped time;
- uncertainty is a duration, not a quality enum;
- wall-clock/UTC/TAI conversion is isolated from estimator time;
- a node reboot starts a new `boot_id` and clock segment.

Review cannot be completed until the hardware timing experiment establishes
what the device PTS represents. The ADR can define the interface before it
defines the measured mapping.

### ADR-0004: localization representation

Proposed:

- continuous robust triangulation is the point-observation baseline;
- sparse/local voxel scoring is a pluggable candidate generator for ambiguous
  evidence;
- a voxel peak must be refined into continuous geometry before becoming a
  measurement; and
- the persistent state is a trajectory/track, not a dense `W(x,y,z,t)` array.

## 8. Fusion engine shape

One engine prevents sim, replay, and live from drifting:

```text
EvidenceSource (live | replay | simulation)
  -> IngestedEvidence
  -> ClockNormalizer / Aligner
  -> CalibratedObservation
  -> Association + Localization
  -> TrackBank
  -> FrameResult
  -> Recorder + Viz adapter
```

The source owns I/O only. It does not call a separate “simulation tracker” or
“live scorer.”

A useful API direction:

```python
class FusionEngine:
    def process(self, evidence: EvidenceBatch) -> FrameResult:
        ...
```

Later, direct asynchronous bearing updates may make the unit smaller than a
camera batch. The architecture still preserves individual timestamps; the
batch is transport/orchestration, not a claim of simultaneity.

## 9. Segmented implementation sequence

### Track A: truthful offline spine

#### A0 — Freeze characterization

**Question:** What existing behavior and evidence must remain reproducible?

Work:

- leave v1 unchanged;
- record current git revision/status in a characterization manifest;
- document the correct v1 test invocation;
- inventory golden fixtures and the live recording;
- write a test that v2 production modules do not import v1; and
- distinguish copied v2 fixtures from runtime dependencies.

Gate:

- v1's current 82 tests pass;
- golden files are checksummed;
- live recording has a manifest/provenance note;
- no user dirty-worktree changes are modified.

#### A1 — Frames, pixels, and time ADRs

**Question:** Can every value be interpreted without guessing?

Work:

- review ADR-0001 through ADR-0003;
- implement only small value types/helpers needed by their tests;
- create hand-calculated cross-language fixtures.

Gate:

- transform, pixel, ROI, timestamp/domain, and reboot/session tests pass;
- no geometry solver exists yet.

#### A2 — Camera model

**Question:** Can a deployed pixel be projected/unprojected correctly?

Work:

- calibrated intrinsics type;
- distortion-aware project/unproject;
- camera/world transform;
- numerical and analytic round-trip tests;
- finite checks and explicit invalid status.

Gate:

- sub-tolerance round trips across the full image;
- comparison against an independent OpenCV reference;
- distortion cannot be silently omitted;
- protective-window/deployed-mode metadata is part of calibration identity.

#### A3 — Observation and deterministic replay

**Question:** Can one observation retain all truth needed by later estimators?

Work:

- minimal canonical `BearingObservation`;
- canonical JSONL encoder/decoder;
- recording manifest with calibration/config/code hashes;
- replay iterator;
- malformed/unknown-version tests.

Gate:

- encode/decode/encode is stable;
- replay retains individual timestamps and covariance;
- an observation remains interpretable without access to live node state.

#### A4 — Continuous triangulation initializer

**Question:** Can exact and noisy rays produce a diagnosable continuous point?

Work:

- weighted closest-point initializer;
- cheirality;
- triangulation angle and condition reporting;
- explicit weak-geometry status;
- exact and near-parallel cases.

Gate:

- hand-calculated fixtures pass;
- permutation of camera order does not change the answer;
- weak geometry is never labeled precise;
- v1 analytic fixtures can be adapted for comparison without importing v1.

#### A5 — Robust refinement and covariance

**Question:** Does the result reflect pixel and geometry uncertainty?

Work:

- robust weighted angular or reprojection refinement;
- full conditional 3x3 covariance from a documented linearization;
- separate systematic sensitivity/bounds for calibration, clock-model, mount,
  and target-reference bias;
- outlier/support reporting;
- Monte Carlo harness with an independently perturbed camera model.

Gate:

- deliberately bad camera observations are rejected/downweighted;
- conditional covariance coverage is reported with calibration held fixed;
- systematic sweeps are reported separately rather than added as independent
  per-frame noise;
- no scalar residual is relabeled as isotropic physical covariance.

#### A6 — Single-target fusion vertical slice

**Question:** Can recordings produce a reproducible tracked result?

Work:

- simple time/epipolar grouping;
- EKF-capable six-state constant-velocity track interface;
- refined XYZ as the first, linear measurement update;
- innovation/NIS gating plus geometric/robust gates before the update;
- explicit track birth/active/coast/delete states;
- one `FusionEngine` used by synthetic and replay sources;
- `FrameResult` and sparse `VizFrame`.

Gate:

- deterministic replay;
- input-order and dropout tests;
- innovation/NIS logs;
- visualization can be disabled without changing estimates;
- offline vertical-slice definition is satisfied.

### Track B: exact edge hardware evidence

The reproducible toolchain and recorded-Y injection seam may start after A1/A3
to enable the wired synthetic milestone. Real CSI/PTS characterization and
outdoor clips are the next adaptation phase and do not block the local
synthetic pipeline.

#### B0 — Reproducible vendor environment

**Question:** Can the exact clone be built and reproduced?

Work:

- pin vendor SDK/commit, toolchain, image, and board revision;
- compile and run a vendor camera sample;
- capture exact sensor mode and `u64PTS/u32TimeRef` metadata;
- create a board bring-up report.

Gate:

- another clean environment can build the same binary;
- no native-macOS header fiction;
- boot/image/tool hashes are recorded.

#### B1 — PTS and luma capture

**Question:** What does the node actually expose?

Work:

- acquire Y at selected sensor modes;
- log sequence, raw PTS, dequeue/publish times, exposure/gain;
- record bounded raw luma clips;
- measure frame gaps, latency distributions, memory, and thermals.

Gate:

- PTS monotonicity/reset behavior is characterized;
- sensor FPS and dropped-frame behavior are measured;
- recordings can drive `edge_reference`.

#### B2 — IVE GMM2/CCL benchmark

**Question:** Which detector configuration fits and survives?

Work:

- port the official GMM2/CCL samples with minimal changes;
- sweep processing resolution, model count, learning schedule, and morphology;
- measure total media memory/contiguous allocation, not only process RSS;
- collect clear-sky and adverse-background clips.

Gate:

- selected configuration survives at least a one-hour soak;
- achieved FPS, memory, thermals, false alarms, and failures are reported;
- full-sensor coordinate mapping is verified;
- fallback configuration is documented.

#### B3 — Edge reference parity

**Question:** Can firmware behavior be reasoned about off-device?

Work:

- host detector consumes the same luma clips;
- compare masks, components, centroids, and coordinate transforms;
- define tolerances rather than requiring accidental bit identity.

Gate:

- mismatches are explained;
- golden clips cover target, cloud, shake, illumination, and empty sky.

#### B4 — Ephemeral tracklet and bounded packet

**Question:** Can the edge emit stable, loss-tolerant evidence?

Work:

- simple image-plane tracklet;
- generated binary schema after review;
- bounded measurement datagram;
- independent optional crop/RLE datagrams;
- low-rate health packet;
- reboot/sequence/version tests.

Gate:

- measurement stays below chosen MTU;
- crop loss cannot erase metadata;
- node restart cannot reuse a global identity;
- long-run packet rate and bandwidth are measured.

### Track C: time and network truth

#### C0 — Offline fault injection

Before opening a socket, replay with:

- packet loss, duplication, and reorder;
- node clock offset/drift/jitter;
- late observations;
- node reboot and sequence reset;
- camera dropout; and
- corrupted/unknown schema.

Gate:

- deterministic policies and metrics exist for every case.

#### C1 — Two-plane transport

Implement:

- fresh bounded UDP measurement path;
- reliable control/calibration/debug path;
- per-source reorder buffers;
- source/session registry and health;
- backpressure that drops optional crops before observations.

Gate:

- injected network faults match offline behavior;
- no stale retransmission blocks fresh measurements;
- packet and control schemas are versioned.

#### C2 — Timing bench

Experiments:

1. common flashing/coded LED;
2. known periodic moving target;
3. repeated boot and temperature soak;
4. row-position sweep for rolling shutter.

Estimate:

- fixed offset;
- drift;
- frame/row jitter;
- PTS-to-exposure relationship;
- capture-to-publish age;
- line delay/readout direction; and
- remaining uncertainty.

Gate:

- the timing model and uncertainty are placed in the observation;
- the target-profile geometry budget determines whether NTP/chrony is enough;
- PTP, PPS, MCU, or trigger hardware is added only if evidence requires it.

### Track D: controlled multi-camera field system

#### D0 — Calibration capture and immutable revisions

Work:

- deployed-mode intrinsics with enclosure cover;
- shared control points or dynamic known target;
- joint bundle adjustment;
- held-out validation points;
- pose covariance and residual report.

Gate:

- held-out error and residual-by-camera plots;
- calibration cannot be changed without a new revision;
- replay can select original versus named recalibration.

#### D1 — Two/three-camera controlled target

Work:

- streaming ingest and individual capture times;
- pair hypotheses and robust refinement;
- full 3D covariance;
- CV track;
- ground-truth proxy.

Gate:

- repeatable position/velocity error;
- covariance coverage and NIS;
- error versus triangulation angle;
- capture-to-valid-track age;
- field-MVP definition is satisfied.

#### D2 — Multi-target association

Work:

- per-camera predicted image-space gates;
- pair/clique births;
- per-camera assignment;
- global track bank and lifecycle;
- multiple-hypothesis retention only where needed.

Gate:

- crossing, partial visibility, dropout, and reacquisition suite;
- ID switches/fragmentation reported;
- gated baseline measured before JPDA/MHT.

### Track E: optional representations and sensors

The track letters group ownership; they are not a mandate to run strictly in
alphabetical order. In particular, E0 can begin immediately after A5/A6 and
should be the first representation comparison before field D1 if voxel
back-projection is retained as a core Skyweave mode.

#### E0 — Sparse voxel candidate generator

Implement only after continuous geometry is established. Compare:

- pair/clique geometric births;
- sparse coarse-to-fine cells;
- track-local cell allocation;
- inverse-range versus Cartesian refinement; and
- mask/cone evidence.

Gate:

- the voxel path wins a documented ambiguity, robustness, or runtime tradeoff;
- every peak is continuously refined;
- it does not become a dense persistent world tensor by accident.

The external
[`Pixeltovoxelprojector`](https://github.com/ConsistentlyInconsistentYT/Pixeltovoxelprojector)
project should be retained as design provenance and a characterization
reference. Its exact implementation is audited separately in the research
report; inspiration from its visual result does not require copying its fixed
grid or calibration assumptions.

#### E1 — Turret

Work:

- camera-to-axis and encoder calibration;
- timestamped encoder/camera bearing packet;
- measured pan/tilt backlash and latency;
- exact Jetson/TensorRT benchmark;
- soft semantic accumulation.

Gate:

- turret bearing reduces angular covariance or classification uncertainty on
  held-out data;
- no range is invented from one bearing;
- detector confidence is calibrated separately from pixel covariance.

#### E2 — High-altitude operating envelope

Work:

- select optics from real apparent-size recordings;
- gather ADS-B with NACp/NIC as a bounded comparison source;
- report detection and localization versus range/elevation/geometry/weather;
- retain bearing-only/weak-depth state when parallax is insufficient.

Gate:

- claims are empirical ranges/percentiles and conditions, not a universal
  “voxel resolution.”

#### E3 — Sophistication only after measured failures

Candidates:

- direct-bearing EKF/UKF or fixed-lag nonlinear smoother;
- rolling-shutter target-motion correction;
- PTP/PPS/external trigger;
- edge NPU classifier;
- IMM;
- JPDA/MHT;
- CUDA localization;
- larger-scale sparse voxel backend.

Each requires a before/after replay report showing which failure it fixes.

## 10. The first review chunks

These are the recommended immediate coding sequence after Samuel reviews this
framework:

| Chunk | Deliverable | What Samuel reviews |
|---|---|---|
| 1 | Frames/pixels/time plus covariance/systematic-bound ADRs and exact tests | Meanings that every later result depends on |
| 2 | Distortion-aware camera model, direct initializer, and Tier-0 Monte Carlo | Geometry, weak-depth behavior, and dominant error terms |
| 3 | Frozen canonical EXP-001 manifest and statistical gates | Exact baseline, resolutions, target reference, and definition of pass/fail |
| 4 | Blender per-camera recordings plus independent truth sidecars | Reproducibility and truth/estimator separation |
| 5 | Host luma detector and calibrated `Observation2D` | Warm-up, centroid repeatability, covariance floor, and negative scenes |
| 6 | Direct hypotheses, CPU voxel oracle, continuous refinement, robust gates, and EKF track | Candidate semantics, no double counting, outlier behavior, and lifecycle |
| 7 | Layered noise/fault suite with held-out scenes | Which errors reject, bias, or degrade uncertainty |
| 8 | Recorded-Y/protobuf/UDP/replay path | Packet bounds, timestamp mapping, optional evidence, and deterministic faults |
| 9 | Physical-node Blender replay through the real switch and Jetson | Local-versus-wired parity, clock/packet behavior, and resource evidence |

The immediate next three review units are chunks 1-3. Board toolchain and
recorded-Y injection work may proceed in parallel after chunk 1, but should not
force real-camera assumptions into the synthetic contracts.

The first end-to-end code remains a small offline geometry/replay system, then
a deterministic Blender pipeline, then the same dataset over real wiring. The
voxel frontend is an early comparison and ambiguity tool; the wired milestone
does not depend on it outperforming direct geometry in the clean one-target
case.

## 11. Test strategy

### Unit tests

- frame transforms, pixel/ROI mapping, projection/unprojection;
- timestamp domains and node reboot;
- line-time evaluation;
- ray/AABB and triangulation exact cases;
- cheirality and near-parallel status;
- track lifecycle.

### Property and metamorphic tests

- camera-order permutation invariance;
- rigidly transform the entire scene and transform the answer identically;
- scale all metric positions and covariance consistently;
- encode/decode stability;
- adding a consistent camera cannot worsen the optimum residual unexpectedly;
- dropping a camera updates support and uncertainty without ID corruption.

### Golden characterization

- retain current v1 golden peak results as comparisons;
- create new v2 golden observations/results for the new semantics;
- never overwrite old expected data without a reviewed reason;
- check fixture hashes.

### Monte Carlo

Independently vary:

- pixel bias/noise and target extent;
- focal/distortion error;
- camera position/orientation error;
- time offset/drift/jitter;
- rolling line delay;
- range, elevation, projected baseline;
- camera count/placement/dropout;
- false blobs and wrong associations.

Report bias, RMSE, covariance coverage, residuals, and condition—not only mean
Euclidean error.

### Replay and fault tests

- identical input gives equivalent output;
- shuffled delivery preserves event-time behavior;
- bounded loss/reorder/late policy;
- old and unknown schema;
- configuration/calibration revision change;
- reboot/session reset;
- recorder backpressure.

### Hardware tests

- one-hour edge soak minimum before field reliance;
- media memory and contiguous allocation;
- thermal/frequency throttling;
- dropped frames and PTS gaps;
- link/PoE/cable soak;
- crash/restart recovery;
- raw clip reproducibility.

### Field tests

- calibration and evaluation targets are disjoint;
- static held-out points first;
- known dynamic target next;
- multiple targets/crossings later;
- operational negative data deliberately included.

## 12. Observability contract

Start with structured JSONL and deterministic report generation. Add Prometheus
only when a persistent service needs it.

Every result should make these questions answerable:

- Which exact observations produced this track update?
- What times and clock uncertainties did they have?
- Which calibration/config/model revisions were used?
- Why was an observation late, rejected, or unassigned?
- What geometry supported the result?
- What are the covariance eigenvalues and condition?
- What caused track birth, coast, reacquisition, or deletion?
- How old was the target information when the valid track was emitted?

Required report families:

- `edge_benchmark`;
- `timing_characterization`;
- `calibration_validation`;
- `triangulation_monte_carlo`;
- `controlled_field_track`;
- `network_fault_replay`; and
- `operating_envelope`.

Reports include the command, git revision, dirty status, environment, input
manifest, configuration, calibration, and model hashes.

## 13. Performance policy

The old implementation measured voxel scoring as the dominant hot path. The
new architecture changes the algorithm, so that result does not prove the new
hot path.

For each optimization:

1. capture a representative replay;
2. profile end to end;
3. name the measured bottleneck and desired improvement;
4. preserve the CPU/reference implementation;
5. add parity/tolerance tests;
6. benchmark latency distribution, throughput, memory, and age of information;
7. accept the optimization only if the whole-system result improves.

Do not write CUDA merely because the Jetson has CUDA. Direct geometric
hypotheses may reduce the central workload enough that detector, decoding, or
recording dominates instead.

## 14. Scope boundaries

In scope for the first field MVP:

- fixed-camera edge proposals;
- explicit time/calibration truth;
- robust two/three-camera continuous localization;
- covariance and quality gates;
- one fusion/replay engine;
- baseline multi-track-capable ownership;
- deterministic recording and sparse visualization.

Deferred:

- autonomous interception or guidance;
- mmWave radar;
- FLIR array;
- permanent edge suppression;
- learned end-to-end fusion/RL;
- IMM/JPDA/MHT without a measured need;
- dense global 4D evidence;
- production cloud/night/rain operation;
- CUDA before profiling;
- claims of inch-scale high-altitude accuracy.

## 15. Definition of done for any chunk

A chunk is done only when:

- its question and accepted decision are written;
- public semantics have units, frames, time domains, and versions;
- expected and failure-path tests pass;
- replay/hardware evidence is attached where relevant;
- no unrelated user changes were overwritten;
- documentation describes what remains unknown;
- the change is small enough for Samuel to explain back;
- no deferred feature leaked into the implementation; and
- the next chunk does not require guessing what this one meant.

That is the mechanism for making v2 elegant without turning “architecture” into
months of scaffolding before the first truthful result.
