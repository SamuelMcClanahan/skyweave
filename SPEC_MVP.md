# Skyweave Software Specification — MVP and V1 Architecture

- **Status**: Draft v0.3 — scope and architecture pass
- **MVP target hardware**: single host, 3× OV9281 USB/UVC cameras, optional laptop camera
- **V1 target hardware**: RV1106/Luckfox-class SC3336 edge nodes, central compute node, turret camera
- **Core thesis**: Skyweave uses the Rayweave engine to turn calibrated motion pixels into rays, rays into a sparse 4D Weavefield, and Weavefield peaks into tracks.

---

## 0. Product scope and phase split

Skyweave is a multi-camera flying-object tracking system. The novel piece is not
only detecting motion in camera images; it is converting camera evidence into a
3D, time-varying evidence field that can be inspected, scored, and tracked.

Naming in this document:
- **Skyweave** is the product/system name.
- **Rayweave** is the core math engine that projects calibrated 2D motion
  evidence into 3D voxel evidence.
- **Weavefield** is the sparse 4D evidence map produced by Rayweave over time.

This spec separates two deliverables:

| Phase | Purpose | Hardware | Primary proof |
|---|---|---|---|
| MVP | Demonstrate pixel-to-voxel tracking in a controlled local volume | Single host + USB cameras | Browser shows a sparse 4D Weavefield, peak estimates, and filtered trajectory |
| V1 | Move toward outdoor distributed tracking | SC3336/RV1106 edge nodes + central node + turret | Edge nodes send motion evidence packets; central node Rayweave-scores, tracks, and uses turret observations |

The MVP is deliberately smaller than the final system, but it must validate the
same reusable spine:

```text
calibration -> pixel/mask to ray/frustum -> Rayweave scoring -> Weavefield -> measurement -> track -> viz/replay
```

Triangulation remains in the system as a baseline and refinement method, but the
headline MVP demonstration is Rayweave evidence accumulation and Weavefield
visualization.

---

## 1. Architecture principles

### 1.1 Reusable core

The MVP runs on one host, but the code is shaped so the same math moves into V1:

- `CameraSource` produces local frames. Remote V1 nodes publish motion packets
  and may separately publish compressed debug video.
- `MotionExtractor` converts frames into masks, blobs, centroids, and optional
  sparse changed-pixel patches.
- `MotionPacket` / `DetectionPacket` is the stable boundary between camera-side
  processing and central fusion.
- `RayweaveScorer` consumes calibrated camera evidence and produces sparse 3D
  Weavefield volumes plus peak measurements.
- `TrackManager` consumes voxel-derived measurements and optional triangulated
  measurements.
- `VizServer` publishes tracks, camera frustums, and Weavefield history to
  the browser.

In MVP, all of these modules run in one process or one host. In V1, only the
camera-side source/extractor moves to RV1106 edge nodes; central Rayweave scoring,
tracking, recording, and visualization stay on the central node.

### 1.2 4D spatiotemporal evidence

Rayweave builds a sparse 4D evidence map, called the Weavefield:

```text
W(x, y, z, t) = evidence that moving image content came from voxel (x, y, z) at time t
```

Implementation must not allocate a dense full `x*y*z*t` tensor. Use a sparse,
bounded representation:

- per-timestep voxel chunks around the calibrated working volume or active track;
- a ring buffer of the last `N` evidence volumes;
- decay weights for older evidence;
- top-K voxel peaks and/or thresholded sparse point clouds for visualization;
- optional local dense chunks for scoring, never a global sky-sized dense grid.

The three.js UI should show the Weavefield as a time-decayed evidence cloud or
heatmap, with a time slider/history control later if useful. This is feasible
in the MVP because the browser receives sparse voxel points, not a massive dense
grid.

### 1.3 Transport posture

Raw SC3336 frames cannot be the V1 realtime path over 100M Ethernet. Compressed
H.264/H.265/MJPEG video is useful for monitoring and debugging, but compression
can destroy or smear tiny aircraft evidence and can add buffering latency.

Therefore:

- MVP may ingest compressed USB/UVC frames centrally because that is the
  available demo hardware.
- V1 realtime path should send edge-produced motion evidence packets:
  centroids, bboxes, scores, small binary mask patches, or sparse changed pixels.
- V1 debug path may also send compressed video, but tracking must not depend on
  compressed video quality.

### 1.4 Tracking posture

MVP uses a regular constant-velocity Kalman filter on 3D measurements derived
from voxel peaks and/or triangulation. V1 may add UKF/EKF-style nonlinear
measurement updates for bearing-only cameras, turret pan/tilt observations,
range shells, or rolling-shutter compensation. IMM belongs after there are
multiple motion models worth switching between, such as aircraft, drone, glider,
bird-like, and ballistic/paper-airplane motion.

---

## 2. MVP goal

Throw a paper airplane or similarly small moving object through the FOV overlap
of 3 cameras. Browser viz renders:

- calibrated camera positions and frustums;
- foreground/motion detections per camera;
- sparse 3D Weavefield evidence for recent timesteps;
- current voxel peak / measurement estimate;
- smoothed Kalman track and short prediction after the object leaves view.

Calibration is done once and persists across runs.

**Demo acceptance criteria**:
- Throw a paper airplane across the volume; trail appears in viz at ≥20Hz with <300ms latency.
- The UI visibly shows the Weavefield, not just a triangulated line or point.
- Track persists for ≥2s after object exits all camera FOVs (KF prediction).
- A calibration/evaluation target reports a 3D position stable to ±10cm across re-launches.
- A second throw 30 seconds later shows a separate track, not a continuation of the first.

---

## 3. Tech stack (MVP defaults)

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | One language across MVP; numpy/scipy/opencv are mature; ships fastest |
| Camera I/O | OpenCV (`cv2.VideoCapture`) over V4L2/UVC | Standard path for OV9281 USB/MJPEG demo cameras |
| CV ops | OpenCV 4.x | Background subtraction, morphology, components |
| Numerical | NumPy + SciPy | Matrix ops, optimization |
| Rayweave scoring | NumPy first, optional Numba/C++ later | Keep math inspectable first; optimize after profiling |
| Kalman filter | `filterpy.KalmanFilter` for MVP; UKF/EKF candidates for V1 | Linear KF is correct for 3D point measurements; nonlinear filters wait for nonlinear measurements |
| Calibration | OpenCV (`aruco`, `solvePnP`) + scipy.optimize for bundle adjust | Standard pipeline |
| AprilTag detection | `pupil-apriltags` (Python wrapper around the C library) | Faster than pure-Python detectors, mature |
| Async runtime | `asyncio` (stdlib) | Native, no extra dep |
| HTTP/WebSocket server | `aiohttp` | One asyncio-native server for static files and WebSocket frames |
| Config | YAML + `pydantic` for schema validation | Type-checked configs |
| Logging | `structlog` + stdlib `logging` | Structured JSON to file, pretty console |
| Metrics | Periodic structured log lines (no Prometheus yet) | Keep MVP simple |
| Serialization | `msgpack` for camera/fusion packets; JSON for browser viz | Efficient machine boundary, easy browser boundary |
| Viz frontend | three.js (ES modules from CDN) | Specified by user; no build step needed |
| Testing | `pytest`, `pytest-asyncio` | Standard |
| Packaging | `pyproject.toml` + `uv` or `pip` | Modern Python project layout |

Languages used: Python for MVP backend/math, JavaScript for `viz_web/`. C++,
Numba, or Rust are allowed later only where profiling shows the Rayweave scorer or
edge motion extractor needs it.

---

## 4. Directory layout

```
skyweave/
├── pyproject.toml
├── SPEC_MVP.md                       # this document
├── README.md
├── skyweave/                         # main Python package
│   ├── __init__.py
│   ├── messages.py                   # all message schemas (pydantic + msgpack)
│   ├── config.py                     # pydantic config models, YAML loading
│   ├── log.py                        # structlog setup
│   ├── timestamps.py                 # ns timestamp utilities
│   ├── camera/                       # frame source abstraction
│   │   ├── __init__.py
│   │   ├── base.py                   # CameraSource abstract
│   │   ├── v4l2.py                   # USB / V4L2 implementation
│   │   ├── replay.py                 # source from recorded sessions
│   │   └── network.py                # V1 network receiver placeholder
│   ├── detection/                    # detection pipeline (per camera)
│   │   ├── __init__.py
│   │   ├── pipeline.py               # composes the stages
│   │   ├── knn_bg.py                 # KNN background subtractor
│   │   ├── morphology.py             # erode + dilate
│   │   ├── blob.py                   # connected components + filtering
│   │   ├── centroid.py               # sub-pixel center-of-mass
│   │   ├── motion_patch.py           # cropped/RLE foreground patch helpers
│   │   └── coherence.py              # temporal coherence filter
│   ├── rayweave/                     # pixel/ray evidence scoring engine
│   │   ├── __init__.py
│   │   ├── grid.py                   # bounded voxel grids/chunks
│   │   ├── dda.py                    # ray-AABB + voxel traversal
│   │   ├── scorer.py                 # Rayweave mask/ray/frustum scoring
│   │   ├── peaks.py                  # peak extraction + measurement covariance
│   │   └── history.py                # Weavefield ring buffer / decay
│   ├── fusion/                       # central-side fusion
│   │   ├── __init__.py
│   │   ├── aligner.py                # time alignment of multi-camera motion evidence
│   │   ├── associator.py             # cross-camera evidence/measurement association
│   │   ├── geom.py                   # SE(3), projection, ray math
│   │   ├── measurements.py           # voxel/triangulation measurement types
│   │   ├── triangulator.py           # DLT + L-M refinement + covariance
│   │   ├── kalman.py                 # filterpy-based per-track filter
│   │   └── tracks.py                 # track manager (create/update/kill)
│   ├── edge/                         # V1 edge-node packet contracts/stubs
│   │   ├── __init__.py
│   │   ├── packets.py                # MotionPacket, DebugFramePacket
│   │   └── rv1106.py                 # V1 implementation placeholder
│   ├── turret/                       # V1 turret contracts/stubs
│   │   ├── __init__.py
│   │   ├── packets.py                # turret pose/observation schemas
│   │   └── model.py                  # pan/tilt camera geometry
│   ├── calib/                        # calibration tools
│   │   ├── __init__.py
│   │   ├── intrinsic.py              # ChAruco-based per-camera intrinsics
│   │   ├── extrinsic.py              # AprilTag-based multi-camera extrinsics
│   │   ├── bundle.py                 # bundle adjustment
│   │   └── store.py                  # load/save calibration files
│   ├── viz/                          # viz backend
│   │   ├── __init__.py
│   │   ├── server.py                 # aiohttp static files + WebSocket endpoint
│   │   └── frames.py                 # builds VizFrame from track state
│   ├── recording/                    # flight record/replay
│   │   ├── __init__.py
│   │   ├── recorder.py               # writes packets, voxels, tracks, optional media
│   │   └── replayer.py               # replays recorded sessions
│   ├── transport/                    # message transport
│   │   ├── __init__.py
│   │   ├── bus.py                    # in-process asyncio pub/sub
│   │   └── pack.py                   # MsgPack wrappers
│   └── app/
│       ├── __init__.py
│       └── mvp.py                    # MVP entry point: composes all above
├── viz_web/                          # three.js frontend
│   ├── index.html
│   ├── src/
│   │   ├── main.js
│   │   ├── scene.js
│   │   ├── tracks.js
│   │   ├── cameras.js
│   │   ├── stats.js
│   │   └── wsclient.js
│   └── README.md
├── tools/                            # one-off scripts
│   ├── calib_intrinsic.py            # CLI wrapper
│   ├── calib_extrinsic.py            # CLI wrapper
│   ├── record.py                     # record a session
│   ├── replay.py                     # replay a session through fusion
│   ├── horizon_mask_gen.py           # interactive tool to define sky mask
│   └── inspect_packets.py            # decode + pretty-print MsgPack
├── tests/
│   ├── test_geom.py
│   ├── test_voxel_dda.py
│   ├── test_rayweave_scorer.py
│   ├── test_triangulator.py
│   ├── test_kalman.py
│   ├── test_associator.py
│   ├── test_blob.py
│   ├── test_centroid.py
│   ├── test_aligner.py
│   └── data/                         # tiny test fixtures
├── configs/
│   ├── mvp.yaml                      # MVP config (3 cams, Rubik Pi 3)
│   └── livingroom.yaml               # paper-airplane-in-living-room variant
└── data/                             # gitignored, local datasets
    └── .gitkeep
```

---

## 5. Message schemas

All schemas are defined in `skyweave/messages.py` as pydantic models. Machine
boundaries use MsgPack. Browser visualization uses JSON derived from these same
models.

Timestamp rule:
- `capture_ts_ns` is the best available frame-capture timestamp in the declared
  `clock_domain`.
- `publish_ts_ns` is when the packet left the producing module, in that same
  domain unless otherwise documented.
- MVP uses one central-host monotonic clock domain: `mvp_host_monotonic`.
- Wall-clock / Unix epoch timestamps are for logs, manifests, and human
  correlation. They are not the realtime ordering source.
- Raw timestamps from different clock domains must not be directly compared
  until they are mapped into a shared central timeline.
- V1 packets will keep the same field shape and add time-sync diagnostics so
  central fusion can quantify skew. V1 sync policy is intentionally left for
  the later V1 spec.

### 5.1 Common packet header

```python
class PacketHeader(BaseModel):
    v: int = 1
    source_id: str                # "cam0", "edge3", "turret0"
    source_type: Literal["camera", "edge", "turret", "replay", "synthetic"]
    frame_seq: int                # monotonic per source
    capture_ts_ns: int
    publish_ts_ns: int
    clock_domain: str = "mvp_host_monotonic" # e.g. "mvp_host_monotonic", "edge3_chrony", "ptp"
    time_sync_error_ms: float | None = None
```

### 5.2 `MotionPacket`

Primary camera-side evidence packet. In MVP it is produced from USB/UVC frames
on the central host. In V1 it is the realtime packet emitted by RV1106/SC3336
edge nodes.

```python
class MotionBlob(BaseModel):
    blob_id: int
    cx: float
    cy: float
    bbox_x: int
    bbox_y: int
    bbox_w: int
    bbox_h: int
    area_px: int
    mean_diff: float              # mean absolute foreground/diff value
    max_diff: float
    confidence: float             # 0..1 from coherence/filtering
    local_track_id: int | None = None

class MotionPatch(BaseModel):
    bbox_x: int
    bbox_y: int
    bbox_w: int
    bbox_h: int
    encoding: Literal["rle_u8", "png_gray", "sparse_xy"]
    payload: bytes                # binary mask/diff patch in the encoding above
    value_scale: float = 1.0      # converts encoded values to scorer weights

class MotionPacket(BaseModel):
    header: PacketHeader
    camera_id: int
    image_width: int
    image_height: int
    blobs: list[MotionBlob]
    motion_patches: list[MotionPatch] = []
    detector: str                 # "frame_diff", "knn", "temporal_highpass"
    exposure_us: float | None = None
    gain_db: float | None = None
```

For MVP, `motion_patches` may be omitted at first and the Rayweave scorer can cast
from blob centers or bbox samples. The preferred MVP proof includes at least one
patch-based mode so voxel evidence is visibly richer than centroid
triangulation.

### 5.3 `DetectionPacket`

Compact derived detections. This is useful for baseline triangulation,
association debugging, and future classifiers. It is not the only input to
Rayweave scoring.

```python
class Detection(BaseModel):
    cx: float
    cy: float
    bbox_x: int
    bbox_y: int
    bbox_w: int
    bbox_h: int
    area_px: int
    confidence: float
    local_track_id: int | None = None

class DetectionPacket(BaseModel):
    header: PacketHeader
    camera_id: int
    detections: list[Detection]
```

### 5.4 `WeavefieldVolume`

Sparse 3D evidence for one aligned timestep. This is the core artifact for the
MVP demo and for replay/debugging the Rayweave math. A sequence of these volumes
forms the 4D Weavefield.

```python
class VoxelGridSpec(BaseModel):
    frame_id: str                  # e.g. "world"
    origin: tuple[float, float, float]
    voxel_size_m: float
    dims: tuple[int, int, int]     # dense chunk dimensions if materialized

class SparseVoxel(BaseModel):
    ix: int
    iy: int
    iz: int
    score: float

class VoxelPeak(BaseModel):
    position: tuple[float, float, float]
    score: float
    covariance: list[list[float]]  # 3x3 estimate from peak shape / geometry
    supporting_camera_ids: list[int]
    n_voxels: int                  # voxels contributing to this peak region

class WeavefieldVolume(BaseModel):
    ts_ns: int
    grid: VoxelGridSpec
    voxels: list[SparseVoxel]      # top-K or thresholded sparse evidence
    peaks: list[VoxelPeak]
    decay_s: float
    source_packet_ids: list[str]
```

### 5.5 `Measurement3D`

Internal fusion measurement passed into the tracker.

```python
class Measurement3D(BaseModel):
    ts_ns: int
    source: Literal["voxel_peak", "triangulation", "turret", "synthetic"]
    position: tuple[float, float, float]
    covariance: list[list[float]]  # 3x3
    score: float
    supporting_camera_ids: list[int] = []
```

### 5.6 `Track`

```python
class Track(BaseModel):
    id: int
    state: list[float]             # 6D MVP: [px, py, pz, vx, vy, vz]
    covariance: list[list[float]]  # 6x6
    status: Literal["candidate", "active", "coasting"]
    classification: str | None = None
    classification_confidence: float = 0.0
    created_ts_ns: int
    last_update_ts_ns: int
    update_count: int
    miss_count: int
    trail: list[tuple[float, float, float, int]]
```

### 5.7 `VizFrame` (server -> browser, JSON)

`VizFrame` is intentionally downsampled for browser performance. It should carry
enough sparse Weavefield history to make the 4D map visible without shipping
full dense grids.

```python
class VizCamera(BaseModel):
    id: int
    position: list[float]
    rotation_quat: list[float]     # [x, y, z, w]
    fov_h_deg: float
    fov_v_deg: float
    fps: float
    online: bool

class VizFrame(BaseModel):
    ts_ns: int
    tracks: list[Track]
    cameras: list[VizCamera]
    measurements: list[Measurement3D]
    weavefield_history: list[WeavefieldVolume] # last N sparse volumes, downsampled
    stats: dict[str, float]        # fps, latency, n_tracks, n_voxels, etc.
```

## 6. Detection pipeline

The detection pipeline converts local camera frames into compact 2D motion
evidence. It does not perform 3D Rayweave scoring. In MVP, this runs on the central
host after reading USB/UVC frames. In V1, the cheap first-pass version runs on
RV1106/SC3336 edge nodes so the network carries motion evidence rather than raw
frames.

### 6.1 Stage diagram

```text
camera frame
  -> grayscale/luma extraction
  -> optional horizon/ROI mask
  -> motion image: frame diff, temporal high-pass, or KNN foreground
  -> threshold + morphology
  -> connected components
  -> blob filtering and bbox inflation
  -> MotionPatch extraction
  -> per-camera temporal consistency
  -> MotionPacket
  -> optional DetectionPacket for triangulation/debug
```

The important output is `MotionPacket`. `DetectionPacket` is a compact derived
view of the same evidence and exists for baseline triangulation, debugging, and
future classifiers.

### 6.2 Frame input and luma conversion

MVP reads frames with OpenCV from USB/UVC cameras. The frame may arrive as MJPEG
or another compressed UVC format, depending on camera settings. The detector
works on a grayscale/luma image:

```python
gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
```

For monochrome OV9281 paths, this may already be a single-channel frame. For
SC3336 V1 edge nodes, use the ISP/luma output if available and avoid expensive
color processing in the realtime path.

Exposure and gain should be fixed where possible. Auto-exposure can turn global
brightness changes into false motion.

### 6.3 ROI/horizon mask

A static binary mask can exclude non-sky regions, moving trees, walls, or
irrelevant indoor clutter. MVP living-room mode may use an all-ones mask.
Outdoor mode should use a horizon/sky mask.

Tool to generate: `tools/horizon_mask_gen.py` — interactive polygon mask,
exported as PNG.

### 6.4 Motion image

The detector should support at least two interchangeable motion modes:

1. **Frame differencing / temporal high-pass**

   ```python
   diff = cv2.absdiff(gray_t, gray_t_minus_1)
   motion = diff
   ```

   This is closest to the reference voxel prototype and is simple enough for
   RV1106-class edge nodes.

2. **KNN background subtraction**

   ```python
   bg = cv2.createBackgroundSubtractorKNN(
       history=300,
       dist2Threshold=400.0,
       detectShadows=False,
   )
   motion = bg.apply(gray)
   ```

Frame differencing should be the V1 edge default unless field tests show KNN is
worth the extra state and compute. KNN remains useful for central MVP tests and
slower-changing backgrounds.

### 6.5 Threshold and morphology

Threshold the motion image into a binary foreground mask, then clean it:

```python
_, fg = cv2.threshold(motion, threshold, 255, cv2.THRESH_BINARY)
fg = cv2.bitwise_and(fg, roi_mask)
fg = cv2.erode(fg, kernel_erode, iterations=1)
fg = cv2.dilate(fg, kernel_dilate, iterations=1)
```

The threshold may be static for MVP. V1 should allow an adaptive threshold based
on image noise, exposure, and recent background statistics.

### 6.6 Connected components and blob filtering

Use `cv2.connectedComponentsWithStats(mask, connectivity=8)`. A component
becomes a `MotionBlob` candidate if:

- area is within `[min_area_px, max_area_px]`;
- bbox does not touch the image boundary unless edge detections are explicitly
  enabled;
- aspect ratio is plausible for the target class;
- mean/max diff are above threshold;
- optional ROI/horizon mask overlap is high enough.

For tiny aircraft and paper airplanes, defaults should allow very small blobs.
The filter should be conservative about deleting evidence; later Rayweave scoring
and multi-camera consistency can reject false positives.

### 6.7 MotionPatch extraction

For each accepted component, inflate its bbox by `patch_margin_px` and crop the
motion evidence inside that bbox.

`MotionPatch` is a small bounded representation of changed pixels, not a full
video frame. It can be encoded as:

- `rle_u8`: run-length encoded binary/diff mask;
- `png_gray`: small grayscale patch for debugging or richer evidence;
- `sparse_xy`: list of changed pixel coordinates and optional weights.

The central Rayweave scorer uses each patch by projecting the changed pixels,
sampled pixels, or bbox/frustum into the 3D voxel volume. This is the bridge
from 2D motion isolation to 3D voxel projection.

### 6.8 Per-camera temporal consistency

A motion patch should become more trusted if it persists and moves coherently
across frames. Track blob candidates locally for the last `N` frames using
nearest-neighbor matching in image coordinates.

Candidate consistency features:

- minimum age, e.g. observed in at least `3` of the last `5` frames;
- centroid displacement within plausible pixel/frame limits;
- low directional variance;
- area and bbox size do not jump wildly;
- mean/max diff do not collapse to noise;
- optional apparent velocity is plausible for the current mode.

For MVP, temporal consistency can run centrally after USB frame capture. For V1,
the RV1106 edge node should run a cheap version to reduce network spam, while
central fusion still performs the stronger multi-camera consistency check.

### 6.9 Pipeline interface

```python
class MotionExtractor:
    def __init__(self, config: DetectionConfig): ...

    def process(
        self,
        frame: np.ndarray,
        capture_ts_ns: int,
        frame_seq: int,
    ) -> tuple[MotionPacket, DetectionPacket | None]: ...
```

Stateful components include the previous frame, optional KNN background model,
local blob tracks, and threshold/noise statistics.

---

## 7. Fusion and Rayweave pipeline

The central pipeline is where the system becomes 3D. It consumes aligned
`MotionPacket`s from multiple calibrated cameras, projects their 2D motion
evidence into a bounded 3D voxel volume, extracts voxel peaks as 3D
measurements, and tracks those measurements over time.

### 7.1 Stage diagram

```text
MotionPackets / optional DetectionPackets
  -> TimeAligner
  -> VoxelGridAllocator
  -> RayweaveScorer: MotionPatch pixels/bboxes -> calibrated rays/frustums -> 3D evidence
  -> WeavefieldVolume + sparse 4D history
  -> PeakExtractor: voxel evidence peaks -> Measurement3D
  -> optional Triangulator baseline/refinement
  -> TrackManager + KalmanFilter
  -> VizFrameBuilder
```

The core MVP proof is the `WeavefieldVolume`: a sparse representation of the 3D
voxel projection at one timestep. A time history of these volumes is the
Weavefield. Tracking uses peaks from this evidence, while triangulation remains
available as a baseline and sanity check.

### 7.2 Time alignment

Class `skyweave.fusion.aligner.TimeAligner`. Maintains per-source deques of recent
`MotionPacket`s and optional `DetectionPacket`s. It emits an `AlignedEvidence`
object when at least `min_cameras` have packets within `window_ns`.

MVP defaults:
- `min_cameras = 2`;
- `window_ns = 33_000_000` for 30 fps cameras;
- `wait_ns = 50_000_000` before closing a window;
- late packets are dropped and logged.

V1 alignment must track `clock_domain` and `time_sync_error_ms`. Packets with
unknown or excessive skew can still be visualized but should receive lower
fusion confidence.

### 7.3 Voxel grid allocation

MVP uses a bounded local working volume that covers the calibrated camera
overlap region. Example: a living-room or yard-sized box around the throw
volume.

```python
class VoxelGridAllocator:
    def allocate(self, aligned: AlignedEvidence, tracks: list[Track]) -> VoxelGridSpec: ...
```

Allocation policy:
- MVP starts with one fixed dense chunk in world coordinates.
- The scorer emits sparse high-score voxels from that chunk.
- Later V1 modes may allocate chunks around predicted tracks, ray/frustum
  intersections, or range shells.

Do not use a full uniform sky grid for V1. The architecture should allow:
- fixed local MVP volume;
- object-centered chunks;
- frustum-intersection chunks;
- range-shell / inverse-depth bins for far aircraft;
- sparse active chunks with time decay.

### 7.4 Pixel/ray/frustum projection

For each `MotionPatch`, map changed image pixels into calibrated rays. For each
camera with intrinsic matrix `K`, distortion `D`, and pose `T_world_cam`:

```text
pixel (u, v)
  -> undistort through K, D
  -> ray in camera frame
  -> ray in world frame using T_world_cam
```

If the patch is small, the scorer can cast rays for changed pixels or sampled
changed pixels. If only a bbox/centroid is available, the scorer can project a
central ray or a bbox frustum with lower confidence.

This is the cleaned-up version of the reference prototype:

```text
reference: changed pixel -> FOV/yaw/pitch ray -> hardcoded grid
spec: changed pixel/patch -> calibrated OpenCV ray/frustum -> bounded grid/chunk
```

### 7.5 Rayweave scoring

`skyweave.rayweave.scorer.RayweaveScorer` accumulates evidence into the allocated 3D
volume.

Basic MVP scoring:

```text
score(voxel) += patch_weight * pixel_weight * camera_weight
```

A voxel receives evidence when:
- a ray from a changed pixel traverses it; or
- the voxel projects back into a changed pixel/mask patch; or
- the voxel lies inside a projected bbox frustum when only bbox data is
  available.

MVP should implement ray traversal with ray-AABB intersection plus DDA-style
voxel stepping, matching the useful part of the forked prototype while replacing
hardcoded camera geometry with calibrated geometry.

Scoring should normalize for:
- number of changed pixels per camera;
- ray length or voxel traversal count;
- camera confidence / local temporal consistency;
- patch encoding quality;
- time-sync uncertainty.

The first MVP can use a simple additive score. Later versions may use log odds,
negative evidence, reprojection likelihood, or visual-hull style occupancy.

### 7.6 Weavefield history

Each aligned timestep produces a `WeavefieldVolume`. The runtime keeps a ring
buffer of recent Weavefield volumes:

```text
weavefield = [W_t-N, ..., W_t-1, W_t]
```

The browser receives only downsampled sparse voxels: top-K voxels, voxels above
a score threshold, or clustered evidence points. Older volumes receive a visual
decay factor so the UI shows a 4D motion trail through voxel space.

This is the MVP's Weavefield. It is not a dense 4D tensor; it is a sequence of
sparse 3D evidence volumes.

### 7.7 Peak extraction and measurement covariance

`skyweave.rayweave.peaks.PeakExtractor` turns voxel evidence into one or more
`Measurement3D` objects.

MVP peak extraction:
- threshold evidence;
- find connected components in voxel index space;
- choose local maxima or weighted centroids;
- convert voxel indices to world coordinates;
- estimate covariance from peak spread, voxel size, and supporting camera
  geometry.

Example:

```text
WeavefieldVolume
  -> VoxelPeak(position=[x,y,z], score=s, covariance=3x3)
  -> Measurement3D(source="voxel_peak", position=[x,y,z], covariance=3x3)
```

Low-confidence peaks can still be visualized but should not initialize tracks
unless they persist or are supported by enough cameras.

### 7.8 Triangulation companion path

Triangulation is retained as a serious companion geometry path. It answers a
different but related question:

```text
given one 2D point/ray per camera, where do those rays best agree in 3D?
```

Rayweave scoring answers:

```text
given many changed pixels/masks/frustums, which 3D regions have the most image evidence?
```

Both are useful. Triangulation should support:

- **centroid baseline**: triangulate blob centroids and compare against voxel
  peaks in the UI and logs;
- **candidate generation**: use pairwise ray closest-points or DLT to propose a
  local voxel chunk instead of searching a larger volume;
- **refinement**: start from a voxel peak and minimize reprojection error
  against supporting centroids, masks, or patch pixels;
- **association**: use ray agreement and Mahalanobis gates to reject impossible
  cross-camera matches;
- **debugging calibration**: large disagreement between triangulated points and
  Rayweave peaks is a signal to inspect intrinsics, extrinsics, timestamps, or
  detection quality.

For MVP, triangulation should be implemented enough to produce a comparable
`Measurement3D(source="triangulation")` from centroid detections. It should not
replace the voxel MVP proof. The UI and replay logs should make it possible to
compare:

```text
voxel_peak_measurement
triangulated_centroid_measurement
reprojection_error_px
ray_agreement_residual_m
```

Later versions can use triangulation more deeply as the continuous optimization
step after coarse Rayweave scoring:

```text
coarse voxel peak -> local reprojection optimization -> refined Measurement3D
```

### 7.9 Kalman tracking

MVP uses a constant-velocity linear Kalman filter over 3D position and velocity:

```text
x = [px, py, pz, vx, vy, vz]^T
z = [px, py, pz]^T
```

Measurements come primarily from `Measurement3D(source="voxel_peak")`, with
optional triangulation measurements available for comparison. Innovation gating
uses Mahalanobis distance. Tracks can coast for at least 2 seconds so the demo
shows prediction after the object leaves the cameras.

V1 may add EKF/UKF updates for nonlinear bearing-only or turret measurements.
IMM is reserved for later multi-model motion classification.

### 7.10 Track lifecycle

Track lifecycle:
- **candidate**: one or more low-confidence measurements not yet confirmed;
- **active**: confirmed by repeated measurements or strong multi-camera voxel
  evidence;
- **coasting**: predicted without recent measurements;
- **dead**: removed after timeout.

MVP defaults:
- promote after `3` consistent measurements or one very strong multi-camera peak;
- coast for `>= 2s`;
- keep trail length long enough for the three.js trajectory view.

---

## 8. Calibration

Calibration is the contract between 2D image evidence and 3D voxel space. The
Rayweave scorer, triangulator, camera frustum visualization, and turret model must
all use the same coordinate conventions and calibration store.

### 8.1 Coordinate conventions

Use one convention everywhere:

- **World frame**: right-handed, `z` up, meters.
- **MVP origin**: camera 0 optical center, unless a measured room/yard frame is
  explicitly configured.
- **Camera frame**: OpenCV convention: `x` right, `y` down, `z` forward.
- **Pose stored**: `T_world_cam`, a 4x4 transform mapping camera-frame points to
  world-frame points:

```text
X_world = T_world_cam @ X_cam
X_cam = inverse(T_world_cam) @ X_world
```

Camera center in world coordinates is:

```text
C_world = T_world_cam[:3, 3]
```

Ray construction starts with a pixel location `(u, v)` in the camera image. The
camera intrinsic matrix `K` describes the pinhole camera model: focal length in
pixels and the principal point. The distortion coefficients `D` describe lens
distortion, usually radial and tangential distortion in the OpenCV model. The
pixel is first undistorted and converted into a normalized camera-frame
direction. That camera-frame direction is then rotated into the world frame
using the rotation part of `T_world_cam`, and the ray starts at the camera
center `C_world`. Rayweave scoring and triangulation both use this same ray.

This convention must be tested in `test_geom.py` and reused by Rayweave scoring,
triangulation, and three.js frustum rendering.

### 8.2 Intrinsic calibration

Purpose: estimate each camera's intrinsic matrix `K`, distortion coefficients
`D`, image size, and reprojection error. `K` tells us how pixels relate to
camera-frame directions. `D` tells us how to undo lens distortion before rays are
projected into 3D.

MVP tool:

```text
tools/calib_intrinsic.py --camera-id <id> --device /dev/video<n>
```

Procedure:
1. Use a ChArUco board or checkerboard on a rigid flat backing.
2. Capture varied angles and distances across the image, including corners.
3. Run OpenCV calibration.
4. Save `configs/intrinsics_cam{id}.yaml`.

Acceptance target:
- RMS reprojection error `< 0.5 px` if practical;
- `< 1.0 px` is acceptable for early MVP if voxel/triangulation tests still pass.

For V1 SC3336 nodes, intrinsics must be generated per lens/camera module. Do not
assume all cheap modules share the same intrinsics.

### 8.3 Extrinsic calibration for MVP

Purpose: estimate `T_world_cam` for all cameras in the shared working volume.

MVP practical path:
1. Fix cameras rigidly.
2. Measure rough camera positions and orientations to initialize the solve.
3. Capture a ChArUco/AprilTag target at many positions throughout the overlap
   volume.
4. Run bundle adjustment over camera poses and target poses.
5. Save `configs/extrinsics.yaml`.

Bundle adjustment residual:

```text
residual_{cam,t,corner} =
  project(inverse(T_world_cam) @ T_world_target_t @ corner_3d, K, D)
  - measured_corner_pixel
```

Fix camera 0 or a measured world frame to remove gauge freedom. If physical
measurements are available, add them as soft priors rather than pretending they
are perfect.

Acceptance target:
- BA RMS `< 1 px`;
- reprojection residuals should not be systematically biased by camera;
- known test target should localize within the MVP tolerance, e.g. `±10 cm`.

### 8.4 Calibration validation

Calibration is not accepted just because the solver returns a YAML file. The MVP
must include validation scenes:

- **static target validation**: place a marker or known point at measured
  positions and compare triangulated/voxel estimates;
- **synthetic geometry validation**: known camera poses, known 3D target, known
  pixel observations, verify ray projection and voxel peak location;
- **reprojection validation**: project known 3D points into each camera and
  inspect pixel residuals;
- **frustum sanity view**: three.js camera frustums should visually overlap in
  the expected working volume.

This catches flipped axes, inverted transforms, wrong distortion handling, and
timestamp mistakes earlier than the live demo.

### 8.5 V1 field calibration

V1 should reuse the same calibration store but allow rougher field procedures at
first:

- measured camera positions and baselines by tape/laser rangefinder;
- IMU pitch/roll as weak orientation priors, not ground truth;
- ChArUco/AprilTag board for near-field setup;
- large elevated marker if visible across nodes;
- sun direction as an orientation sanity check when exposure/optics are safe;
- ADS-B over time as weak self-calibration or validation, not primary truth;
- ordinary GPS/GNSS later for rough node positions if available;
- RTK drone or bright active marker as a later funded upgrade for bundle
  adjustment over a larger outdoor volume.

For the short-baseline V1 setup, carefully measured positions may be good enough
to start, but the system should record calibration uncertainty and expose it in
voxel/track covariance.

### 8.6 Calibration store

`skyweave.calib.store.Calibration` is loaded once at startup and passed into
fusion, Rayweave scoring, triangulation, and visualization.

```python
@dataclass(frozen=True)
class CameraCalib:
    id: int
    K: np.ndarray
    D: np.ndarray
    width: int
    height: int
    T_world_cam: np.ndarray
    position_std_m: float | None = None
    rotation_std_deg: float | None = None

    @property
    def position(self) -> np.ndarray:
        return self.T_world_cam[:3, 3]

@dataclass(frozen=True)
class Calibration:
    world_frame: str
    cameras: dict[int, CameraCalib]
    ba_rms_px: float | None = None
```

---

## 9. Transport layer

Transport has two jobs: keep the MVP modular on one host, and define the V1
network boundary without forcing raw camera frames across 100M Ethernet.

### 9.1 MVP in-process bus

MVP uses an asyncio in-process bus. Producers publish pydantic models; the bus
serializes/deserializes them through MsgPack even in-process so packet contracts
are exercised from day one.

Topics:
- `motion.cam{id}`: `MotionPacket` from each camera's `MotionExtractor`;
- `detections.cam{id}`: optional `DetectionPacket` derived from motion evidence;
- `rayweave.weavefield`: `WeavefieldVolume` after central Rayweave scoring;
- `fusion.measurements`: `Measurement3D` objects from voxel peaks/triangulation;
- `fusion.tracks`: active `Track` list;
- `viz.frames`: downsampled `VizFrame` for browser publishing.

This is intentionally simple. It gives the code distributed-style boundaries
without making the MVP depend on network debugging.

### 9.2 V1 realtime network path

The V1 realtime path is packet-based:

```text
RV1106/SC3336 edge node
  -> MotionPacket over UDP or QUIC-like datagrams
  -> central TimeAligner
  -> RayweaveScorer
```

Edge nodes should send:
- `MotionPacket` with blobs, centroids, bboxes, scores, and optional
  `MotionPatch` data;
- packet sequence numbers for loss detection;
- local capture timestamps and clock-domain diagnostics;
- periodic health/status packets.

Raw SC3336 frames are not a V1 realtime requirement. At SC3336 resolutions, raw
video exceeds 100M Ethernet by a large margin. H.264/H.265 can fit but may
damage tiny point targets and add latency, so compressed video is a debug stream
rather than the measurement stream.

### 9.3 Debug video path

Compressed video is still useful:
- aim/focus/exposure checks;
- human monitoring;
- detector debugging;
- offline dataset review.

Debug video may use RTSP, MJPEG, H.264, or H.265 depending on what the RV1106
image supports. It should be low priority and allowed to lag. The tracker must
continue operating from `MotionPacket`s if debug video is disabled.

### 9.4 Reliability and loss behavior

Motion evidence packets are small enough that dropping a packet should not kill
the system. Central fusion should:
- tolerate missing cameras for short windows;
- lower confidence when only one camera contributes;
- log packet loss and latency by source;
- never block Rayweave scoring on debug video;
- record raw packet streams for replay.

For MVP, packet loss can be simulated in replay tests before any network code is
written.

### 9.5 Serialization

Use MsgPack for machine packets:
- `PacketHeader`;
- `MotionPacket`;
- `DetectionPacket`;
- `WeavefieldVolume`;
- `Measurement3D`;
- `Track`.

Use JSON for browser WebSocket messages because three.js and browser tooling are
simpler with JSON. Browser messages may be downsampled versions of the internal
models.

---

## 10. Visualization

The visualization is part of the MVP proof, not just decoration. It must show
that 2D motion evidence is being projected into 3D voxel space over time. The
MVP should include both live/replay visualization and a visually strong
interactive simulation mode that explains the technology even without cameras
connected.

### 10.1 Backend (`skyweave.viz.server`)

Use one `aiohttp` app as the lightweight Python web backend:
- `GET /`: serve `viz_web/index.html`;
- `GET /static/*`: serve frontend modules;
- `WS /ws`: stream JSON visualization messages.

`aiohttp` is not the visualization engine. It is only the HTTP/WebSocket server
that lets the Python runtime deliver calibration, replay, live packets, and
simulation state to the browser. three.js owns the 3D rendering.

On connect, the server sends:
- camera calibration summary and frustum geometry;
- current config summary;
- latest known system status.

At runtime, the server publishes downsampled `VizFrame` messages at a target of
`30 Hz` or lower if browser/network load requires it. In simulation mode, the
server may publish synthetic camera/object/voxel data or the frontend may run
the simulation fully client-side.

### 10.2 Frontend goals

The three.js UI should make these things visible at a glance:

- where cameras are;
- where their frustums overlap;
- where the tracked object is in `x/y/z`;
- the object's local axes and motion direction;
- camera rays cast toward the object or motion patch;
- which 2D motion patches contributed;
- where the current sparse 3D Weavefield volume is;
- how recent evidence volumes decay over time;
- where voxel peaks are;
- where triangulated centroid estimates are;
- what the Kalman track believes;
- motion profile data: speed, heading, acceleration estimate, update age, and
  confidence;
- whether latency, packet loss, or camera dropout is affecting confidence.

### 10.3 Interactive simulation mode

The MVP should include a visually polished simulation/explainer scene. This is
allowed to be more illustrative than the live telemetry view, but it should use
the same geometry concepts:

- a small flying object that can be moved in 3D with gizmo/drag controls;
- visible `x`, `y`, and `z` axes on or near the object;
- multiple camera nodes around the scene;
- rays drawn from each camera through the object's image location;
- optional frustum cones/pyramids from bboxes or motion patches;
- voxel cells or point-cloud evidence lighting up where rays/frustums agree;
- a labeled object tag showing position, velocity, speed, acceleration estimate,
  heading, confidence, and track ID;
- a toggle that compares voxel peak and triangulated estimate;
- playback controls for a scripted paper-airplane-like path.

The simulation can be built after the core MVP algorithms, but it is part of the
demo goal. It should be impressive enough that someone can understand the
technology from the browser before seeing the real cameras run.

### 10.4 Live/replay scene elements

Core scene:
- world grid and axes, `z` up;
- calibrated camera nodes;
- camera frustums;
- ray lines from cameras to selected voxel peaks or triangulated points;
- optional image-plane billboards or tiny overlays showing recent motion patches;
- sparse voxel evidence points from `WeavefieldVolume`;
- voxel peaks as highlighted markers;
- triangulation measurements as separate markers;
- Kalman track as a sphere plus trail;
- object/track label with motion profile data;
- covariance ellipsoid for track or measurement uncertainty;
- status overlay with FPS, latency, active tracks, packet loss, and per-camera
  online state.

Visual encoding:
- Weavefield evidence color/brightness maps to score;
- Weavefield evidence alpha decays with age;
- current timestep is brighter than history;
- voxel peak and triangulation markers use different shapes/colors;
- coasting tracks should visibly differ from recently updated tracks.

### 10.5 Sparse 4D controls

MVP controls:
- toggle camera frustums;
- toggle camera rays;
- toggle Weavefield evidence;
- toggle triangulation markers;
- toggle covariance ellipsoids;
- switch live / replay / simulation mode;
- clear/recenter view;
- pause/resume stream.

Nice-to-have controls:
- scrub recent Weavefield history;
- adjust voxel score threshold in the browser;
- pin one track and show its measurements;
- inspect per-camera contributions to a selected voxel peak.

The UI should never require shipping dense voxel grids. The server should send
top-K or thresholded sparse voxels, already downsampled for browser rendering.

### 10.6 Frontend file layout

```text
viz_web/
├── index.html
└── src/
    ├── main.js             # bootstrap, animation loop
    ├── scene.js            # renderer, camera, lights, grid
    ├── cameras.js          # camera meshes/frustums
    ├── rays.js             # camera rays/frustum-ray overlays
    ├── sim.js              # interactive simulation/explainer mode
    ├── weavefield.js       # sparse Weavefield evidence/history rendering
    ├── tracks.js           # tracks, trails, covariance
    ├── measurements.js     # voxel peaks + triangulation markers
    ├── stats.js            # overlay
    └── wsclient.js         # websocket state updates
```

### 10.7 Performance constraints

The browser renderer must remain responsive with live Weavefield history enabled.
Therefore:
- cap voxel points per frame;
- reuse three.js buffers where possible;
- decay/remove old evidence on a fixed ring buffer;
- avoid sending unchanged calibration/static geometry every frame;
- log browser-side FPS in the stats overlay.

## 11. Logging, recording, and replay

Logging answers "what happened?" Recording answers "can we replay and improve
it?" Both are required because the MVP will improve through after-the-fact
simulation, replay, and parameter sweeps.

Library: `structlog` configured in `skyweave.log.setup()`.

### 11.1 Outputs

Two sinks, both active simultaneously:
- **Console**: pretty-printed, colored, INFO+ by default (DEBUG with `--verbose`).
- **File**: JSONL to `data/logs/<date>/run-<timestamp>.jsonl`, DEBUG+ always.

### 11.2 Structure

Every log entry is JSON with keys:
```json
{
  "ts": "2026-05-27T18:23:11.123456Z",
  "level": "info",
  "logger": "skyweave.fusion.tracks",
  "event": "track_created",
  "track_id": 7,
  "position": [1.2, 3.4, 5.6],
  "n_obs": 3
}
```

Mandatory fields: `ts`, `level`, `logger`, `event`. Free-form structured fields per event.

### 11.3 Standard events (canonical event names)

| Event | When | Fields |
|---|---|---|
| `app_start` | Process boot | `config_path`, `version` |
| `camera_opened` | V4L2 device opened | `camera_id`, `device`, `width`, `height`, `fps` |
| `camera_disconnected` | Frame read fail | `camera_id`, `error` |
| `calibration_loaded` | At startup | `path`, `n_cameras`, `ba_rms_px` |
| `motion_packet_published` | Per frame/source | `camera_id`, `frame_seq`, `n_blobs`, `n_patches`, `latency_ms` |
| `weavefield_volume_scored` | Per aligned timestep | `ts_ns`, `n_sources`, `n_voxels`, `n_peaks`, `score_max` |
| `measurement_created` | Voxel/triangulation measurement | `source`, `position`, `score`, `covariance_diag` |
| `track_created` | New track | `track_id`, `position`, `n_obs` |
| `track_updated` | Per measurement, DEBUG | `track_id`, `measurement`, `mahalanobis` |
| `track_killed` | Track aged out | `track_id`, `lifetime_s`, `update_count` |
| `outlier_rejected` | Mahalanobis exceeded | `track_id`, `mahalanobis`, `gate` |
| `packet_dropped` | Late/lost packet | `source_id`, `frame_seq`, `lateness_ms` |
| `viz_frame_published` | Browser frame emitted | `n_voxels`, `n_tracks`, `publish_latency_ms` |
| `stats_periodic` | Every 5s | `fps_per_cam`, `latency_p50_ms`, `latency_p95_ms`, `n_active_tracks`, `mem_mb`, `packet_loss` |

### 11.4 Latency tracking

Every packet carries a `PacketHeader` with capture and publish timestamps. The
fusion code timestamps alignment, Rayweave scoring, peak extraction, tracking, and
WebSocket emission. Log latency per stage:

```text
capture -> motion extraction -> alignment -> Rayweave scoring -> tracking -> viz publish
```

For V1, latency logs must include `clock_domain` and `time_sync_error_ms` so
timestamp quality is visible.

### 11.5 Async flight recorder

`skyweave.recording.recorder.Recorder` should be a flight recorder: it captures
enough structured data to reconstruct what the system believed, replay the run,
and build simulation assets later. It must not make the realtime tracking loop
wait on disk I/O during normal operation.

Architecture:

```text
runtime packet/event -> bounded recorder queue -> writer task -> session files
```

Logging and recording are related but not identical:
- JSONL logs are for human-readable events, errors, and summaries.
- Recording streams are for high-volume replay data: packets, measurements,
  Weavefield volumes, tracks, and optional media artifacts.

Record by default:
- session manifest with config, git commit, calibration snapshot, and hardware
  summary;
- all `MotionPacket`s;
- all `DetectionPacket`s if enabled;
- all `WeavefieldVolume`s, possibly downsampled/top-K;
- all `Measurement3D`s;
- all `Track` states;
- structured logs and packet indexes.

Optional recording:
- raw or compressed camera frames for MVP debugging;
- small debug crops corresponding to `MotionPatch` regions;
- browser-ready `VizFrame`s for exact UI replay;
- full dense voxel chunks or per-ray contribution maps during short debug
  bursts only.

Backpressure policy:
- recorder queues are bounded;
- recorder health is logged periodically: queue depth, write rate, dropped
  optional artifacts, disk free space;
- optional heavy artifacts drop first;
- debug crops and browser frames drop next;
- packet streams and track/measurement streams are preserved as long as
  possible;
- realtime tracking is never blocked unless an explicit `record_everything`
  debug mode is enabled.

This lets a Rubik Pi 3-class device record aggressively without turning disk I/O
into loop jitter. A larger Jetson can raise recording budgets later.

### 11.6 Replay and simulation export

`tools/replay.py --session data/recordings/<id>` should replay packet streams
through the fusion/Rayweave/tracking stack at realtime or accelerated speed.

Replay use cases:
- compare Rayweave scoring parameters;
- compare triangulation and voxel peak estimates;
- tune Kalman process noise and gates;
- debug calibration errors;
- regenerate three.js demo/simulation scenes after the fact.

Simulation export should eventually turn a real or synthetic run into a compact
JSON asset for the interactive three.js explainer:

```text
recorded run -> cleaned trajectory + camera poses + sampled rays + Weavefield history -> sim asset
```

---

## 12. Configuration

All runtime config is YAML loaded into pydantic models from `skyweave.config`.
Configuration should be explicit about units and mode. `configs/mvp.yaml` is the
default live-camera entry point; replay and simulation may use separate config
files that inherit the same schema.

```yaml
# configs/mvp.yaml
app:
  name: "mvp"
  mode: "live"                    # live | replay | simulation
  log_level: "INFO"
  log_dir: "data/logs"

cameras:
  - id: 0
    source: "uvc"                  # uvc | replay | edge_packet
    device: "/dev/video0"
    width: 1280
    height: 720
    fps: 30
    pixel_format: "MJPG"
    intrinsics_file: "configs/intrinsics_cam0.yaml"
  - id: 1
    source: "uvc"
    device: "/dev/video2"
    width: 1280
    height: 720
    fps: 30
    pixel_format: "MJPG"
    intrinsics_file: "configs/intrinsics_cam1.yaml"
  - id: 2
    source: "uvc"
    device: "/dev/video4"
    width: 1280
    height: 720
    fps: 30
    pixel_format: "MJPG"
    intrinsics_file: "configs/intrinsics_cam2.yaml"

extrinsics_file: "configs/extrinsics.yaml"

motion:
  mode: "frame_diff"               # frame_diff | temporal_highpass | knn
  horizon_mask_file: null            # null = no mask (living room)
  grayscale: true
  threshold:
    value: 18
    adaptive: false
  bg_subtractor:
    history: 300
    dist2_threshold: 400.0
  morphology:
    erode_kernel: 3
    dilate_kernel: 5
  blob_filter:
    min_area_px: 3
    max_area_px: 500
    max_aspect_ratio: 5.0
    reject_edge_touching: true
  motion_patch:
    enable: true
    patch_margin_px: 8
    encoding: "rle_u8"             # rle_u8 | png_gray | sparse_xy
    max_patches_per_frame: 8
    max_patch_pixels: 4096
  temporal_consistency:
    enable: true
    track_frames: 5
    match_dist_px: 50.0
    min_observations: 3
    max_direction_variance: 1.57

rayweave:
  grid:
    mode: "fixed"                  # fixed | track_local | frustum_intersection
    frame_id: "world"
    origin_m: [-2.0, -2.0, 0.0]
    dims: [96, 96, 64]
    voxel_size_m: 0.05
  scorer:
    method: "ray_dda"              # ray_dda first; reprojection_likelihood later
    min_supporting_cameras: 2
    normalize_by_ray_length: true
    normalize_by_camera_pixels: true
    top_k_voxels: 5000
  peaks:
    threshold_abs: 0.0
    threshold_percentile: 99.5
    max_peaks: 4

fusion:
  align_window_ns: 33_000_000
  align_wait_ns: 50_000_000
  min_cameras_per_frame: 2
  triangulation:
    enable: true
    pixel_noise_px: 1.0
    compare_with_voxel_peak: true
  kalman:
    sigma_accel_mps2: 8.0
    initial_position_var: 10.0
    initial_velocity_var: 100.0
    gate_mahalanobis_squared: 11.345
  tracks:
    init_consecutive: 3
    coast_seconds: 2.0
    trail_length: 200

viz:
  host: "0.0.0.0"
  http_port: 8080
  ws_path: "/ws"
  publish_rate_hz: 30
  mode_default: "simulation"       # simulation | live | replay
  voxel_points_max: 5000
  weavefield_history_frames: 90
  show_rays: true
  show_triangulation: true
  show_motion_profile: true

recording:
  enable: true
  output_dir: "data/recordings"
  queue_max_packets: 10000
  record_everything: false
  streams:
    motion_packets: true
    detections: true
    weavefield_volumes: true
    measurements: true
    tracks: true
    viz_frames: false
    raw_frames: false
    debug_crops: true
  budgets:
    max_disk_mb_per_min: 500
    min_free_disk_gb: 10

simulation:
  enable: true
  default_scene: "paper_airplane_arc"
  export_dir: "data/sim_exports"

v1_edge:
  enable: false
  packet_bind_port: 5055
  debug_video_enable: false
  debug_video_url_template: "rtsp://edge-{id}/live"
```

Important defaults:
- MVP starts in `simulation` UI mode but can switch to live cameras.
- `motion.mode = frame_diff` keeps the first implementation close to the voxel
  prototype and cheap enough for V1 edge nodes.
- `recording.enable = true` records packet-level data by default, while raw
  frames stay off unless explicitly requested.
- Rayweave grid dimensions above are placeholders; they must be tuned to the actual
  camera layout and working volume.

---

## 13. MVP feature list

The deliverable. Each row is a concrete unit of work; reference IDs should be
used in commits and issues. The order follows the dependency spine:

```text
messages/config -> camera/motion -> Rayweave evidence -> measurements/tracks -> viz/replay
```

| ID | Feature | Where | How |
|---|---|---|---|
| MVP-01 | Project scaffold | repo root | `pyproject.toml`, `pytest` config, pre-commit, basic CI |
| MVP-02 | Message schemas | `skyweave/messages.py` | pydantic models for `PacketHeader`, `MotionPacket`, `MotionPatch`, `DetectionPacket`, `WeavefieldVolume`, `Measurement3D`, `Track`, `VizFrame`; MsgPack/JSON round-trip tested |
| MVP-03 | Config system | `skyweave/config.py`, `configs/mvp.yaml` | pydantic-based YAML loader with schema validation |
| MVP-04 | Logging and runtime stats | `skyweave/log.py` | `structlog` + `logging` handlers, JSONL file sink, console sink, periodic stats events |
| MVP-05 | Timestamp utilities | `skyweave/timestamps.py` | `monotonic_ns()`, `wall_ns()`, conversion helpers |
| MVP-06 | Geometry primitives | `skyweave/fusion/geom.py` | SE(3) ops, projection, undistortion, ray construction, camera frustums, CPA; full unit tests |
| MVP-07 | `CameraSource` abstract + V4L2 impl | `skyweave/camera/base.py`, `skyweave/camera/v4l2.py` | asyncio task wraps `cv2.VideoCapture`; emits `(frame, capture_ts_ns)` tuples |
| MVP-08 | Replay and synthetic camera sources | `skyweave/camera/replay.py`, test fixtures | Emit frames or packet streams from recorded/synthetic sessions |
| MVP-09 | Frame differencing / temporal high-pass | `skyweave/detection/pipeline.py` | Cheap motion image path, close to V1 edge-node default |
| MVP-10 | KNN background subtractor | `skyweave/detection/knn_bg.py` | Thin wrapper around `cv2.createBackgroundSubtractorKNN` with config |
| MVP-11 | ROI mask and morphology | `skyweave/detection/morphology.py`, `tools/horizon_mask_gen.py` | Static masks, thresholding, erode/dilate cleanup |
| MVP-12 | Blob extraction + centroid | `skyweave/detection/blob.py`, `skyweave/detection/centroid.py` | `connectedComponentsWithStats`, bbox filters, intensity-weighted centroid |
| MVP-13 | Motion patch encoding | `skyweave/detection/motion_patch.py` | Crop/inflate bbox and encode changed pixels as `rle_u8`, `png_gray`, or `sparse_xy` |
| MVP-14 | Temporal coherence filter | `skyweave/detection/coherence.py` | Per-camera blob tracker, age/displacement/directional-variance gates |
| MVP-15 | Motion pipeline composition | `skyweave/detection/pipeline.py` | `MotionExtractor.process(frame, ts)` -> `MotionPacket` plus optional `DetectionPacket` |
| MVP-16 | In-process bus | `skyweave/transport/bus.py` | asyncio pub/sub over `motion.*`, `rayweave.weavefield`, `fusion.measurements`, `fusion.tracks`, `viz.frames`; MsgPack ser/de |
| MVP-17 | Time aligner | `skyweave/fusion/aligner.py` | Sliding-window grouping of multi-camera `MotionPacket`s by timestamp |
| MVP-18 | Rayweave grid allocator | `skyweave/rayweave/grid.py` | Fixed MVP world chunk with room/yard bounds and voxel-size config |
| MVP-19 | Ray/voxel traversal | `skyweave/rayweave/dda.py` | Ray-AABB intersection and DDA stepping through grid cells |
| MVP-20 | Rayweave scorer | `skyweave/rayweave/scorer.py` | Project `MotionPatch` pixels/bboxes/frustums into 3D evidence; emit sparse scores |
| MVP-21 | Peak extraction + covariance | `skyweave/rayweave/peaks.py` | Connected components/local maxima in voxel space -> `VoxelPeak` / `Measurement3D` |
| MVP-22 | Weavefield history | `skyweave/rayweave/history.py` | Ring buffer of recent `WeavefieldVolume`s with decay and top-K/downsampling |
| MVP-23 | Triangulator baseline/refinement | `skyweave/fusion/triangulator.py` | DLT + L-M + covariance; compare centroid triangulation to voxel peaks |
| MVP-24 | Per-track Kalman filter | `skyweave/fusion/kalman.py` | filterpy `KalmanFilter` factory with 6D CV state, F/H/Q/R builders |
| MVP-25 | Track manager | `skyweave/fusion/tracks.py` | Init / update / coast / kill; trail buffers; Mahalanobis gating |
| MVP-26 | Calibration loader | `skyweave/calib/store.py` | Immutable calibration objects from YAML, shared by Rayweave, triangulation, and viz |
| MVP-27 | Intrinsic calibration tool | `tools/calib_intrinsic.py`, `skyweave/calib/intrinsic.py` | Interactive ChAruco/checkerboard capture + OpenCV solver |
| MVP-28 | Extrinsic calibration tool | `tools/calib_extrinsic.py`, `skyweave/calib/extrinsic.py`, `skyweave/calib/bundle.py` | AprilTag/ChAruco observations + bundle adjustment |
| MVP-29 | Calibration validation tools | `tools/validate_calib.py`, tests | Static target check, reprojection residual report, synthetic projection sanity tests |
| MVP-30 | Viz server | `skyweave/viz/server.py` | `aiohttp` static file serving + WebSocket endpoint; 30Hz downsampled `VizFrame` push |
| MVP-31 | three.js live/replay frontend | `viz_web/` | Cameras, frustums, rays, sparse voxels, peaks, triangulation markers, tracks, labels, stats, OrbitControls |
| MVP-32 | Interactive simulation/explainer | `viz_web/src/sim.js` | Movable object with x/y/z axes, camera rays, voxel evidence, motion-profile tag, scripted paper-airplane path |
| MVP-33 | Async flight recorder | `skyweave/recording/recorder.py` | Bounded writer queue; records packets, Weavefield volumes, measurements, tracks, logs, optional crops/frames |
| MVP-34 | Replay runner | `skyweave/recording/replayer.py`, `tools/replay.py` | Replay a session through Rayweave/fusion/tracking at realtime or accelerated speed |
| MVP-35 | Simulation export | `tools/export_sim.py` | Turn recorded/synthetic runs into compact browser explainer assets |
| MVP-36 | V1 edge packet contracts | `skyweave/edge/packets.py`, `skyweave/edge/rv1106.py` | `MotionPacket` network stubs, debug-frame packet schema, loss/latency diagnostics |
| MVP-37 | V1 turret contracts | `skyweave/turret/` | Turret pose/observation schemas and camera-geometry placeholder |
| MVP-38 | MVP app entrypoint | `skyweave/app/mvp.py` | Composes config, cameras/replay/sim, motion, Rayweave, tracking, recording, viz; CLI `skyweave-mvp --config configs/mvp.yaml` |
| MVP-39 | Unit tests | `tests/` | Focused tests for geometry, DDA, scorer, peaks, triangulator, Kalman, blob/coherence, aligner, recording |
| MVP-40 | Synthetic integration test | `tests/test_e2e_synthetic.py` | Synthetic 3-camera scene with known trajectory; assert voxel peak and track match within tolerance |
| MVP-41 | Performance/latency profiling | `tools/profile_mvp.py`, logs | Capture p50/p95 stage latency and recorder queue health on target host |
| MVP-42 | Demo recording and docs | `data/recordings/livingroom_demo_v1/`, `README.md` | Capture paper-airplane run, replayable session manifest, quickstart, calibration notes |

---

## 14. Open MVP decisions

| ID | Item | Default | Revisit when |
|---|---|---|---|
| MVP-D1 | USB camera time-sync strategy | Software timestamp at `read()` return | If observed jitter > 30ms |
| MVP-D2 | Frame resolution / pixel format | 1280x720, 30fps, MJPEG or mono UVC depending on camera support | If detection accuracy, USB bandwidth, or loop latency is poor |
| MVP-D3 | Motion extractor default | Frame differencing / temporal high-pass | If KNN is clearly more stable without unacceptable latency |
| MVP-D4 | Temporal coherence enabled by default | Yes | If living-room tests miss real throws because the consistency gate is too strict |
| MVP-D5 | Motion patch encoding | `rle_u8` by default, `png_gray` for debug crops | If Rayweave scoring needs grayscale weights or encoding cost is too high |
| MVP-D6 | MVP voxel grid resolution | Fixed bounded grid, initial target around 5cm voxels | If the demo is too slow or voxel peaks are too coarse |
| MVP-D7 | Rayweave scoring model | Additive ray/DDA evidence with simple normalization | If false positives require negative evidence, log odds, or reprojection likelihood |
| MVP-D8 | Peak extraction threshold | Percentile/top-K plus connected components | If evidence is noisy, fragmented, or merges multiple objects |
| MVP-D9 | Recorder default payload | Packet streams, Weavefield volumes, measurements, tracks, logs; raw frames off | When tuning detection algorithms requires raw frame A/B replay |
| MVP-D10 | Bundle adjustment Jacobian | Finite difference first; analytic only if needed | If extrinsic calibration is too slow or unstable |
| MVP-D11 | Multi-target handling in MVP | Single real target; schemas and UI tolerate multiple tracks | If the demo environment produces persistent spurious tracks |
| MVP-D12 | Tracking filter | Linear constant-velocity KF | If turret/bearing-only/range-shell measurements become part of the active MVP |
| MVP-D13 | V1 edge compute split | Edge does cheap motion extraction; central node does Rayweave scoring/tracking | After RV1106 profiling with SC3336 input and network tests |
| MVP-D14 | Debug video role | Optional monitoring stream only, not measurement input | If motion packets alone cannot explain or tune failures |
| MVP-D15 | Simulation/explainer scope | Included in MVP as a polished three.js mode | If live algorithm work blocks, keep sim data synthetic but preserve the same geometry vocabulary |

---

## 15. Definition of done (MVP)

The MVP is "done" when **all** of the following are true:

1. `skyweave-mvp --config configs/mvp.yaml` starts cleanly, validates config and calibration, and opens either simulation/replay mode or 3 USB cameras.
2. Viz is reachable at `http://localhost:8080` and shows camera positions, frustums, world grid, stats, and mode controls.
3. Simulation mode shows a movable or scripted object with x/y/z axes, camera rays, sparse Weavefield evidence, voxel peak, triangulation comparison, Kalman track, and motion-profile tag.
4. Live mode produces `MotionPacket`s from each camera, including blobs and at least one working `MotionPatch` encoding mode.
5. The central Rayweave scorer produces `WeavefieldVolume`s from multi-camera motion evidence, and the UI visibly renders sparse 3D Weavefield evidence with recent-history decay.
6. A paper airplane thrown through the calibrated overlap volume produces a visible voxel peak and smoothed 3D track within `<300ms` p95 capture-to-WebSocket latency.
7. The Kalman tracker smooths jitter and coasts for at least `2s` after the object leaves all camera FOVs.
8. Triangulated centroid measurements are available as a baseline, displayed separately from voxel peaks, and logged with reprojection/ray-agreement residuals.
9. Static target validation reports a stable 3D position within `+/-10cm` across repeated placements or re-launches in the MVP volume.
10. `tools/replay.py` can replay a recorded session through motion/Rayweave/fusion/tracking and reproduce the same qualitative Weavefield history and track behavior.
11. The async flight recorder records packet streams, Weavefield volumes, measurements, tracks, logs, and session metadata without blocking the realtime loop under default settings.
12. Unit and synthetic integration tests pass for geometry, voxel DDA/scoring, peak extraction, triangulation, Kalman tracking, detection, alignment, recording, and end-to-end synthetic tracking.
13. README has a quickstart that gets a new user from `git clone` to simulation mode, then to live-camera mode, in under 30 minutes.
14. A living-room or yard paper-airplane demo session is recorded with enough data to replay, inspect Weavefield evidence, and export a three.js simulation asset.
15. V1-facing packet contracts for RV1106 edge nodes and the turret path exist as documented stubs, even if their hardware implementations are not part of MVP completion.

---

## Appendix A — Coordinate and time conventions

- **World frame**: right-handed, z-up. Origin = camera 0 optical center.
- **Camera frame**: right-handed, x-right, y-down, z-forward (OpenCV).
- **Camera pose**: `T_world_cam` maps points from camera frame to world frame: `X_w = T_world_cam · X_c`.
- **Projection**: A world point projects to camera `i` via `x_c = T_world_cam_i^{-1} · X_w`, then pinhole + distortion via `K_i, D_i`.
- **Units**: meters, seconds, radians (degrees only in UI).
- **Realtime timestamps**: `int` nanoseconds in the declared `clock_domain`.
  MVP uses `mvp_host_monotonic`.
- **Wall time**: ISO-8601 / Unix epoch values are used in logs and session
  manifests, not as the primary realtime ordering source.
- **Future V1 note**: distributed clock synchronization policy is deferred to a
  later V1 spec. The MVP keeps `clock_domain` fields now so packet recordings
  remain forward-compatible.

## Appendix B — Library version pins (proposed)

These are proposed minimums for the MVP Python package. Exact versions should
be locked in `uv.lock` or an equivalent lockfile once implementation starts.
Use `opencv-contrib-python`, not plain `opencv-python`, because calibration uses
ArUco/ChArUco modules. On a headless target, substitute
`opencv-contrib-python-headless` for the non-headless package.

```toml
[project]
requires-python = ">=3.11"
dependencies = [
  "numpy>=1.26",
  "scipy>=1.13",
  "opencv-contrib-python>=4.9",
  "filterpy>=1.4.5",
  "pupil-apriltags>=1.0.4",
  "pydantic>=2.6",
  "pyyaml>=6.0",
  "msgpack>=1.0.7",
  "structlog>=24.1",
  "aiohttp>=3.9",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "pytest-asyncio>=0.23",
  "pytest-cov>=4.1",
  "ruff>=0.4",
  "mypy>=1.10",
]

perf = [
  "numba>=0.59",
]
```

## Appendix C — Equation summary (one-page reference)

**Frame differencing / temporal high-pass**:
`motion_t(u,v) = |gray_t(u,v) - gray_{t-1}(u,v)|`.

**Foreground mask**:
`M_t(u,v) = 1` when `motion_t(u,v) >= threshold` and the pixel is inside the
ROI/horizon mask; otherwise `0`.

**KNN background subtraction**: pixel is foreground iff
`|{ s in history : (v - s)^2 < dist2Threshold }| < kNNSamples`.

**MotionPatch**: for a connected component with inflated bbox `B`, store
`P = { (u, v, w) | (u, v) in B, M_t(u,v) = 1 }`, where `w` is a binary or
grayscale motion weight from the encoded patch.

**Sub-pixel centroid** over patch/mask weights:
`cx = sum(u*w) / sum(w)`, `cy = sum(v*w) / sum(w)`.

**Ray in world** from pixel `(u,v)` with intrinsics `K`, distortion `D`, and
pose `T_world_cam = (R, t)`:
`x_n = undistort([u,v], K, D)`,
`d_cam = normalize([x_n.x, x_n.y, 1])`,
`ray_origin = t`,
`ray_dir = R * d_cam`.

**Ray-AABB intersection**: for voxel grid bounds `[b_min, b_max]`, compute the
interval `[t_enter, t_exit]` where `ray_origin + t * ray_dir` lies inside the
grid. If the interval is empty, the ray contributes no voxel evidence.

**DDA voxel traversal**: after ray-AABB entry, step through voxel indices in the
order the ray crosses grid cell boundaries. Each visited voxel receives evidence
from that pixel, optionally normalized by traversal length and per-camera pixel
count.

**Voxel score accumulation**:
`score_t(i,j,k) += patch_weight * pixel_weight * camera_weight * temporal_weight`.
The first MVP uses positive additive evidence. Later versions may add negative
evidence, log odds, or reprojection likelihoods.

**Sparse voxel volume**:
`E_t = { (i, j, k, score) | score_t(i,j,k) >= threshold }`, usually capped by
top-K score for visualization and recording.

**Sparse 4D history**:
`H_t = [E_{t-N+1}, ..., E_t]` with visual/evidence decay
`score_visual(E_{t-a}) = score * exp(-a * dt / tau)`.

**Voxel peak extraction**: threshold `E_t`, find connected components in voxel
index space, and report each component's weighted centroid:
`p_peak = sum(p_voxel * score) / sum(score)`.

**Voxel measurement covariance**: estimate `R_voxel` from voxel size, peak
spread, score concentration, and supporting camera geometry. Broad or
single-camera-supported peaks produce larger covariance.

**Closest point of approach** between rays `r1(s) = c1 + s*d1` and
`r2(t) = c2 + t*d2`:
`s* = (b*e - c*d) / (a*c - b^2)`, `t* = (a*e - b*d) / (a*c - b^2)`, with
`a = d1*d1`, `b = d1*d2`, `c = d2*d2`, `d = d1*(c1-c2)`, `e = d2*(c1-c2)`.

**DLT triangulation**: stack rows `[u_i p3_i - p1_i; v_i p3_i - p2_i]` into
`A`, solve `AX = 0` via SVD, then dehomogenize.

**L-M refinement**:
`argmin_X sum_i ||project_i(X) - (u_i, v_i)||^2` via
`scipy.optimize.least_squares`.

**Triangulation covariance**: `R_tri = sigma_px^2 * inverse(J.T * J)`, where
`J` is the reprojection Jacobian at the refined estimate.

**Kalman filter (constant velocity, variable dt)**:
`x = [px, py, pz, vx, vy, vz]^T`,
`z = [px, py, pz]^T`,
`F = [[I3, dt*I3], [0, I3]]`,
`H = [I3 | 0]`.

**Process noise** (white-acceleration model):
`G = [0.5*dt^2*I3; dt*I3]`,
`Q = sigma_a^2 * G * G.T`.

**Measurement noise**:
`R = R_voxel` for `Measurement3D(source="voxel_peak")`;
`R = R_tri` for `Measurement3D(source="triangulation")`.

**Innovation gate**: accept iff `y.T * inverse(S) * y < 11.345`
(chi-square, 3 DOF, 99%).
