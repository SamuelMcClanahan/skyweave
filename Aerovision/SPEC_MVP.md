# Aero-Net MVP — Software Specification (Phase 0)

**Status**: Draft v0.2 — MVP only
**Target hardware**: Rubik Pi 3 + 3× OV9281 USB cameras (single-host)
**Scaling principle**: Every module must be reusable as-is when we move to distributed edge nodes in Phase 1. The MVP is the prod architecture running in one process; Phase 1 just splits the producer side onto separate machines.

---

## 0. Scaling principle (read this first)

The MVP runs on one host but is **architected as if it were already distributed**. Concretely:

- Each camera is a `CameraSource` that produces frames into an asyncio queue. In MVP, three `V4L2CameraSource` instances run in the same process. In Phase 1, each `CameraSource` is a `NetworkCameraSource` receiving frames over UDP from a remote node. **Downstream consumers don't care which.**
- The detection pipeline (`bg_subtract → blob → centroid → coherence`) is the same code in both modes. It runs in-process for MVP, on edge nodes in Phase 1.
- The output of detection is always a `DetectionPacket` (MsgPack-serializable schema, defined in §4). In MVP it's passed in-process via an asyncio queue. In Phase 1 it's MsgPack-encoded over UDP. **Same data structure.**
- The fusion pipeline (`align → associate → triangulate → kalman → track`) consumes `DetectionPacket`s. It doesn't know whether they came from in-process queues or UDP sockets.

When we go to Phase 1, only two things change: (a) `CameraSource` and `Detector` move to a separate process on edge nodes, (b) a `UdpTransport` is inserted between detection and fusion. **No fusion, calibration, KF, or viz code changes.**

This is enforced by:
- Strict module boundaries (see §3 directory layout)
- No direct camera/detection access from fusion code
- Single message schema (`aero.messages`) used everywhere

---

## 1. MVP goal

Throw a paper airplane through the FOV of 3 cameras. Browser viz renders a smoothed 3D trail of the trajectory, with Kalman-filter prediction continuing for 1-2 seconds after the object leaves view. Calibration is done once, persists across runs.

**Demo acceptance criteria**:
- Throw a paper airplane across the volume; trail appears in viz at ≥20Hz with <300ms latency.
- Track persists for ≥2s after object exits all camera FOVs (KF prediction).
- A static reference object (small ball on a stand) reports a 3D position stable to ±10cm across re-launches.
- A second throw 30 seconds later shows a separate track, not a continuation of the first.

---

## 2. Tech stack (locked)

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | One language across MVP; numpy/scipy/opencv are mature; ships fastest |
| Camera I/O | OpenCV (`cv2.VideoCapture`) over V4L2 | Standard, well-tested with OV9281 USB |
| CV ops | OpenCV 4.x | Background subtraction, morphology, components |
| Numerical | NumPy + SciPy | Matrix ops, optimization |
| Kalman filter | **filterpy** (`KalmanFilter`, `UnscentedKalmanFilter`) | Specified by user; mature, well-documented |
| Calibration | OpenCV (`aruco`, `solvePnP`) + scipy.optimize for bundle adjust | Standard pipeline |
| AprilTag detection | `pupil-apriltags` (Python wrapper around the C library) | Faster than pure-Python detectors, mature |
| Async runtime | `asyncio` (stdlib) | Native, no extra dep |
| WebSocket server | `websockets` library | Simple, asyncio-native |
| HTTP server (for serving viz HTML) | `aiohttp` | Pairs with websockets |
| Config | YAML + `pydantic` for schema validation | Type-checked configs |
| Logging | `structlog` + stdlib `logging` | Structured JSON to file, pretty console |
| Metrics | Periodic structured log lines (no Prometheus yet) | Keep MVP simple |
| Serialization | `msgpack` (Python lib) for `DetectionPacket` even in-process | Exercises Phase 1 protocol from day 1 |
| Viz frontend | three.js (ES modules from CDN) | Specified by user; no build step needed |
| Testing | `pytest`, `pytest-asyncio` | Standard |
| Packaging | `pyproject.toml` + `uv` or `pip` | Modern Python project layout |

Languages used: **Python only** for MVP. JavaScript appears only in `viz_web/`. No C++ in MVP. (C++ is reserved for Phase 1 edge nodes if/when profiling shows Python can't hold 30fps per camera on RV1106.)

---

## 3. Directory layout

```
aero-net/
├── pyproject.toml
├── SPEC_MVP.md                       # this document
├── README.md
├── aero/                             # main Python package
│   ├── __init__.py
│   ├── messages.py                   # all message schemas (pydantic + msgpack)
│   ├── config.py                     # pydantic config models, YAML loading
│   ├── log.py                        # structlog setup
│   ├── timestamps.py                 # ns timestamp utilities
│   ├── camera/                       # frame source abstraction
│   │   ├── __init__.py
│   │   ├── base.py                   # CameraSource abstract
│   │   ├── v4l2.py                   # USB / V4L2 implementation
│   │   ├── replay.py                 # source from recorded dataset
│   │   └── network.py                # Phase 1 stub (raises NotImplementedError for now)
│   ├── detection/                    # detection pipeline (per camera)
│   │   ├── __init__.py
│   │   ├── pipeline.py               # composes the stages
│   │   ├── knn_bg.py                 # KNN background subtractor
│   │   ├── morphology.py             # erode + dilate
│   │   ├── blob.py                   # connected components + filtering
│   │   ├── centroid.py               # sub-pixel center-of-mass
│   │   └── coherence.py              # temporal coherence filter
│   ├── fusion/                       # central-side fusion
│   │   ├── __init__.py
│   │   ├── aligner.py                # time alignment of multi-camera detections
│   │   ├── associator.py             # cross-camera detection association
│   │   ├── geom.py                   # SE(3), projection, ray math
│   │   ├── triangulator.py           # DLT + L-M refinement + covariance
│   │   ├── kalman.py                 # filterpy-based per-track filter
│   │   └── tracks.py                 # track manager (create/update/kill)
│   ├── calib/                        # calibration tools
│   │   ├── __init__.py
│   │   ├── intrinsic.py              # ChAruco-based per-camera intrinsics
│   │   ├── extrinsic.py              # AprilTag-based multi-camera extrinsics
│   │   ├── bundle.py                 # bundle adjustment
│   │   └── store.py                  # load/save calibration files
│   ├── viz/                          # viz backend
│   │   ├── __init__.py
│   │   ├── server.py                 # aiohttp + websockets
│   │   └── frames.py                 # builds VizFrame from track state
│   ├── recording/                    # dataset record/replay
│   │   ├── __init__.py
│   │   ├── recorder.py               # writes raw frames + detections
│   │   └── replayer.py               # reads them back
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

## 4. Message schemas

All schemas defined in `aero/messages.py` as pydantic models. Each has `.to_msgpack()` and `.from_msgpack()` round-trip methods. **All timestamps are uint64 nanoseconds since UNIX epoch.**

### 4.1 `DetectionPacket`

Emitted by the detection pipeline (per camera, per frame). In MVP these flow through an in-process bus; in Phase 1 they're UDP payloads.

```python
class Detection(BaseModel):
    cx: float                # sub-pixel centroid x
    cy: float                # sub-pixel centroid y
    bbox_x: int
    bbox_y: int
    bbox_w: int
    bbox_h: int
    area_px: int
    confidence: float        # 0..1, from coherence filter
    local_track_id: int      # per-camera short-term tracker ID, 0 = none

class DetectionPacket(BaseModel):
    v: int = 1
    camera_id: int
    frame_seq: int                # monotonic per camera
    capture_ts_ns: int            # camera-capture timestamp
    process_ts_ns: int            # when detection finished (for latency diag)
    detections: list[Detection]
```

### 4.2 `Track`

Internal representation in the fusion layer.

```python
class Track(BaseModel):
    id: int
    state: list[float]            # 6D: [px, py, pz, vx, vy, vz]
    covariance: list[list[float]] # 6x6
    classification: str | None = None
    classification_confidence: float = 0.0
    created_ts_ns: int
    last_update_ts_ns: int
    update_count: int
    trail: list[tuple[float, float, float, int]]  # (x, y, z, ts_ns), last N=200 points
```

### 4.3 `VizFrame` (server → browser, JSON)

```python
class VizCamera(BaseModel):
    id: int
    position: list[float]         # [x, y, z]
    rotation_quat: list[float]    # [x, y, z, w]
    fov_h_deg: float
    fov_v_deg: float
    fps: float
    online: bool

class VizFrame(BaseModel):
    ts_ns: int
    tracks: list[Track]
    cameras: list[VizCamera]
    stats: dict[str, float]       # fps_overall, latency_ms_p50, latency_ms_p95, n_tracks
```

---

## 5. Detection pipeline (per camera)

### 5.1 Stage diagram

```
V4L2 frame → (optional horizon mask) → KNN bg subtract → morphology
→ connected components → blob filter → sub-pixel centroid
→ temporal coherence filter → DetectionPacket
```

Each stage is a function or small class. The pipeline is composed in `aero/detection/pipeline.py`.

### 5.2 Horizon mask (optional, off by default in living room)

Static binary mask loaded from PNG at startup. Foreground detection is only run where `mask(x, y) == 255`. For the living-room paper-airplane test, mask = all ones (whole frame). For outdoor sky tests, mask defines "above horizon" region.

Tool to generate: `tools/horizon_mask_gen.py` — interactive, click points to define a polygon, exports PNG.

### 5.3 KNN background subtraction

Library: `cv2.createBackgroundSubtractorKNN`.

```python
bg = cv2.createBackgroundSubtractorKNN(
    history=300,           # frames retained (~10s at 30fps)
    dist2Threshold=400.0,  # squared-distance threshold for foreground
    detectShadows=False
)

# Per frame:
fg_mask = bg.apply(frame)
```

**Math**: For each pixel `(x, y)`, the model maintains a rolling sample of the most recent `history` values. A new pixel value `v` is classified as foreground if fewer than `kNNSamples` (default 2) of the historical samples are within squared L2 distance `dist2Threshold` of `v`:

```
fg(x, y) = |{ s ∈ H_{x,y} : ||v - s||² < dist2Threshold }| < kNNSamples
```

For grayscale this reduces to a scalar squared difference.

### 5.4 Morphology

```python
kernel_erode  = np.ones((3, 3), np.uint8)
kernel_dilate = np.ones((5, 5), np.uint8)
mask = cv2.erode(fg_mask, kernel_erode, iterations=1)
mask = cv2.dilate(mask, kernel_dilate, iterations=1)
```

Erode kills isolated noise pixels; dilate re-connects close foreground pixels into single blobs.

### 5.5 Connected components

```python
n_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(
    mask, connectivity=8
)
```

`stats[i] = (x, y, w, h, area)` for each component. Discard component 0 (background).

### 5.6 Blob filter

A blob is kept if:
- `area ∈ [min_area, max_area]` (default `[5, 500]` px — tune per scene)
- `aspect_ratio = max(w, h) / max(min(w, h), 1) < max_aspect` (default 5.0; rejects long streaks)
- `bbox` does not touch frame boundary (objects on edges have unreliable centroids)

### 5.7 Sub-pixel centroid

Within the blob's bbox, compute the intensity-weighted center of mass on the **foreground mask** (binary 0/255):

```
cx = Σ_{(i,j) ∈ bbox} j · M(i,j) / Σ M(i,j)
cy = Σ_{(i,j) ∈ bbox} i · M(i,j) / Σ M(i,j)
```

Vectorized in NumPy:
```python
ys, xs = np.where(mask[y:y+h, x:x+w] > 0)
if len(xs) == 0:
    cx, cy = x + w/2, y + h/2  # fallback
else:
    cx = x + xs.mean()
    cy = y + ys.mean()
```

This is the standard center-of-mass for a binary image.

### 5.8 Temporal coherence filter (configurable, on by default)

A real flying object moves coherently; rustling/random motion does not. Each blob is tracked across the most recent `N=5` frames using nearest-neighbor matching (Euclidean centroid distance < `tau_track_px = 50`). For each tracked blob, compute the directional variance of velocity vectors:

```
v_k = c_k - c_{k-1}                  # frame-to-frame displacement
θ_k = atan2(v_k.y, v_k.x)
σ²_θ = circular_variance(θ_2, ..., θ_N)
```

Accept the blob (emit a `Detection`) only if `σ²_θ < max_direction_variance` (default `π/2 ≈ 1.57`) **and** the blob has been tracked for ≥3 frames.

Confidence score: `confidence = 1.0 - σ²_θ / π` clamped to `[0, 1]`.

### 5.9 Pipeline interface

```python
class DetectionPipeline:
    def __init__(self, config: DetectionConfig): ...
    def process(self, frame: np.ndarray, capture_ts_ns: int) -> DetectionPacket: ...
```

Stateful (the KNN model and coherence tracker carry state across frames).

---

## 6. Fusion pipeline (central)

Consumes `DetectionPacket`s, produces `Track`s and `VizFrame`s.

### 6.1 Stage diagram

```
DetectionPackets (one per camera per frame)
  → TimeAligner (groups by capture_ts_ns within tolerance)
  → Associator (matches detections across cameras)
  → Triangulator (DLT + L-M + covariance)
  → TrackManager + per-track KalmanFilter
  → VizFrameBuilder
```

### 6.2 Time alignment

Class `aero.fusion.aligner.TimeAligner`. Maintains a per-camera deque of recent `DetectionPacket`s. Emits an `AlignedFrame` (dict of `{camera_id: DetectionPacket}`) when:

- At least `min_cameras` (default 2) have produced packets within a `window_ns` (default `33_000_000` = 33ms).
- `wait_ns` (default `50_000_000` = 50ms) have elapsed since the window's earliest timestamp.

Packets arriving after their window closes are dropped and logged at WARN level.

Pseudocode:
```python
def maybe_emit(self) -> AlignedFrame | None:
    now = monotonic_ns()
    # Find groups by clustering timestamps within window_ns
    groups = cluster_by_proximity(all_buffered_packets, window_ns)
    for g in groups:
        if len(g) >= min_cameras and (now - g.median_ts) > wait_ns:
            self.discard(g)
            return AlignedFrame(packets=g, ts_ns=g.median_ts)
    return None
```

### 6.3 Cross-camera association

Given an `AlignedFrame` with up to one detection per camera per object (could be multiple objects), match detections across cameras that correspond to the same physical 3D point.

**For MVP** (≤3 cameras, typically 1 object in flight):

1. For each detection in each camera, construct the 3D ray from camera center through the pixel.
2. For each pair of detections from different cameras, compute the closest point of approach (CPA) between rays.
3. CPA residual = perpendicular distance between rays at closest approach.
4. Pairs with residual < `tau_assoc_m` (default 0.5m) are association candidates.
5. For 3 cameras: prefer triples where all three pairs have low residuals.
6. Use scipy's `linear_sum_assignment` for the assignment problem when multiple detections per camera exist (rare in MVP).

**Ray construction**. Given pixel `(u, v)` and camera with intrinsic matrix `K` (3×3) and pose `(R, t)` in world frame:

```
# Undistort pixel into normalized image coordinates:
x_n = undistort([u, v], K, D)         # 2-vector

# Ray in camera frame:
d_cam = [x_n[0], x_n[1], 1]
d_cam /= ||d_cam||

# Ray in world frame:
d_world = R · d_cam   (R is camera-to-world rotation)
origin_world = -R · t_in_world         # camera position in world
```

(Convention: we store `R, t` such that a world point `X_w` maps to camera frame as `X_c = R_cw · X_w + t_cw`. Then camera center in world is `c = -R_cw^T · t_cw` and ray direction in world is `R_cw^T · d_cam`.)

**CPA math**. Given rays `r_1(s) = c_1 + s·d_1` and `r_2(t) = c_2 + t·d_2`:

```
w = c_1 - c_2
a = d_1 · d_1 = 1   (unit vector)
b = d_1 · d_2
c = d_2 · d_2 = 1
d = d_1 · w
e = d_2 · w
denom = a*c - b² = 1 - b²

s* = (b*e - c*d) / denom
t* = (a*e - b*d) / denom

closest_pt_1 = c_1 + s* · d_1
closest_pt_2 = c_2 + t* · d_2
residual = ||closest_pt_1 - closest_pt_2||
midpoint = 0.5 * (closest_pt_1 + closest_pt_2)
```

If `denom < 1e-9` (rays parallel), skip the pair.

### 6.4 Triangulation

`aero.fusion.triangulator.Triangulator`. Two-stage: linear DLT initialization, then nonlinear L-M refinement, with covariance.

**Stage 1 — DLT (linear)**.

For each of N cameras with projection matrix `P_i = K_i [R_i | t_i]` (3×4) and observed undistorted pixel `(u_i, v_i)`, contribute two rows to constraint matrix `A`:

```
A_{2i}   = u_i * P_i[2, :] - P_i[0, :]
A_{2i+1} = v_i * P_i[2, :] - P_i[1, :]
```

Stack into `A` of size `(2N, 4)`. Solve `A X̃ = 0` via SVD; `X̃` is the right-singular-vector with smallest singular value. Dehomogenize: `X = X̃[:3] / X̃[3]`.

**Stage 2 — L-M refinement**.

Minimize the sum of squared reprojection errors:

```
f(X) = Σ_i || π_i(X) - (u_i, v_i) ||²
```

Where `π_i(X)` projects 3D point `X` through camera `i` including distortion. Use `scipy.optimize.least_squares(residuals, X_dlt, method='lm', jac=jacobian)`.

`residuals(X)` returns the 2N-vector of pixel errors. `jacobian(X)` returns the 2N×3 Jacobian (computed analytically for speed; falls back to finite differences if analytic is broken).

**Stage 3 — Covariance**.

Assume isotropic pixel noise `σ_px = 1.0 px` per camera (configurable). Measurement covariance `Σ_z = σ_px² · I_{2N}`.

Position covariance:
```
J = ∂π / ∂X | X=X*       (evaluated at refined estimate, 2N × 3)
Σ_X = (J^T · Σ_z^{-1} · J)^{-1}
    = σ_px² · (J^T · J)^{-1}
```

Returns `(X, Σ_X)` from `triangulate(detections, calib)`.

### 6.5 Kalman filter (per track)

Library: `filterpy.kalman.KalmanFilter`. Constant-velocity 6-state model.

**State**: `x = [px, py, pz, vx, vy, vz]^T`

**Transition** (with timestep `dt`):
```
F = [[1, 0, 0, dt,  0,  0],
     [0, 1, 0,  0, dt,  0],
     [0, 0, 1,  0,  0, dt],
     [0, 0, 0,  1,  0,  0],
     [0, 0, 0,  0,  1,  0],
     [0, 0, 0,  0,  0,  1]]
```

**Process noise** (white-acceleration model, `σ_a` configurable, default 8 m/s²):

```
G = [[dt²/2,     0,     0],
     [    0, dt²/2,     0],
     [    0,     0, dt²/2],
     [   dt,     0,     0],
     [    0,    dt,     0],
     [    0,     0,    dt]]

Q = σ_a² · G · G^T
```

In closed form (for documentation):
```
Q = σ_a² · [[(dt⁴/4)·I_3 ,  (dt³/2)·I_3],
            [(dt³/2)·I_3 ,    (dt²)·I_3 ]]
```

**Measurement**: `z = [px, py, pz]^T` from triangulator.
```
H = [[1, 0, 0, 0, 0, 0],
     [0, 1, 0, 0, 0, 0],
     [0, 0, 1, 0, 0, 0]]

R = Σ_X (from triangulator)
```

**filterpy usage**:
```python
from filterpy.kalman import KalmanFilter

def make_kf(dt: float, sigma_a: float) -> KalmanFilter:
    kf = KalmanFilter(dim_x=6, dim_z=3)
    kf.F = build_F(dt)
    kf.H = build_H()
    kf.Q = build_Q(dt, sigma_a)
    kf.R = np.eye(3)          # overwritten per update with triangulator covariance
    kf.P = np.eye(6) * 10.0   # initial state covariance
    return kf

# Each step:
kf.F = build_F(dt_actual)
kf.Q = build_Q(dt_actual, sigma_a)
kf.predict()

kf.R = measurement_covariance
kf.update(measurement)
```

**Innovation gating**. Before calling `update()`, compute Mahalanobis distance:

```
y = z - H · x_pred                 # innovation
S = H · P_pred · H^T + R           # innovation covariance
d² = y^T · S^{-1} · y
```

Accept measurement if `d² < gate_chi2_3dof_99` ≈ `11.345` (3 DOF, 99% confidence). Otherwise reject as outlier.

### 6.6 Track lifecycle

Class `aero.fusion.tracks.TrackManager`. Per-track state:

- `id` — monotonic
- `kf` — `filterpy.KalmanFilter` instance
- `last_update_ts_ns`
- `update_count`
- `miss_count` — consecutive frames with no associated measurement
- `trail` — deque of recent (x, y, z, ts) tuples for viz

**Initialization**: A new track is created from a triangulated measurement when no existing track accepts it (Mahalanobis gate fails for all). The candidate must be confirmed: 3 consecutive triangulations within mutual gate before promoting to a real track.

**Update**: For each triangulated measurement in a frame:
1. Predict all existing tracks to current timestamp.
2. Compute Mahalanobis distance from measurement to each predicted track.
3. Assign measurement to the track with smallest distance, if < gate.
4. Update that track. Reset its `miss_count`.
5. Unassigned measurements become candidates (see init above).

**Coast**: Tracks without measurements predict-only. `miss_count` increments each frame.

**Death**: Track is destroyed when `miss_count > death_frames` (default 30, i.e. 1 second at 30fps).

---

## 7. Calibration

Calibration is **not part of the runtime pipeline** — it's offline tooling that produces a YAML file consumed at startup.

### 7.1 Intrinsic calibration (per camera, once)

Tool: `tools/calib_intrinsic.py --camera-id <id> --device /dev/video<n>`.

Procedure:
1. Print ChAruco board: 7×5 squares, 40mm each, ArUco dict `DICT_5X5_1000`. Mount on rigid flat surface.
2. Run the tool. It pops a preview window. Hold the board at varied angles/distances; press `c` to capture (collect ~40 captures).
3. Press `q` to finish. Tool runs `cv2.aruco.calibrateCameraCharuco`.
4. Output: `configs/intrinsics_cam{id}.yaml` containing `K` (3×3), `D` (5,), `image_size`, RMS reprojection error.
5. Acceptance: RMS < 0.5 px.

### 7.2 Extrinsic calibration (multi-camera, once per deployment)

Tool: `tools/calib_extrinsic.py --config configs/mvp.yaml`.

Procedure:
1. Print one AprilTag, family `tag36h11`, ID 0, physical size measured with calipers (record to ±0.1mm).
2. Mount tag on rigid backing (foam board).
3. Run the tool. It opens preview from all cameras. Hold the tag at ~50 positions distributed through the working volume; press `c` to capture (all cameras snap simultaneously via in-process trigger).
4. Press `q` to finish.

The tool then runs bundle adjustment:

**Unknowns**:
- Camera poses `T_world_cam_i` for `i = 1..N-1` (camera 0 is world frame, fixed).
- Tag pose `T_world_tag_t` for each captured position `t = 1..M`.

**Observations**: For each (camera `i`, capture `t`) where tag was detected, the detected tag pose `T_cam_i^tag_t` and the 4 tag corner pixels.

**Residual** (per camera × capture × corner = 4 residuals × 2D):
```
residual_{i,t,k} = π_i(T_world_cam_i^{-1} · T_world_tag_t · corner_k) - measured_pixel_{i,t,k}
```

Optimize via `scipy.optimize.least_squares` with sparse Jacobian. Initial guess: cam 0 at origin, other cams from per-tag relative pose averaged across captures.

Output: `configs/extrinsics.yaml`:

```yaml
version: 1
world_frame: "camera_0"
cameras:
  - id: 0
    T_world_cam: [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    stderr_translation_m: 0.0
    stderr_rotation_deg: 0.0
  - id: 1
    T_world_cam: [[...]]
    stderr_translation_m: 0.008
    stderr_rotation_deg: 0.05
  - id: 2
    T_world_cam: [[...]]
    ...
ba_total_rms_px: 0.62
n_observations: 4287
```

Acceptance: BA total RMS < 1 px.

### 7.3 Calibration store

`aero.calib.store.Calibration` is a frozen dataclass loaded from YAML. The fusion pipeline takes a `Calibration` at construction; it never re-reads disk.

```python
@dataclass(frozen=True)
class CameraCalib:
    id: int
    K: np.ndarray        # 3x3
    D: np.ndarray        # (5,)
    width: int
    height: int
    T_world_cam: np.ndarray  # 4x4 SE(3)

    @property
    def P(self) -> np.ndarray:
        """Projection matrix K [R|t] in world frame."""
        # (extract R, t from T_world_cam^{-1}, build P = K @ [R|t])
        ...

    @property
    def position(self) -> np.ndarray:
        """Camera center in world frame."""
        ...

@dataclass(frozen=True)
class Calibration:
    cameras: dict[int, CameraCalib]
```

---

## 8. Transport layer (the scaling fulcrum)

### 8.1 In-process bus (MVP)

`aero.transport.bus.Bus` is a simple asyncio pub/sub: producers `await bus.publish(topic, msg)`, consumers `async for msg in bus.subscribe(topic)`. Implemented with `asyncio.Queue` per subscriber.

Topics used in MVP:
- `detections.cam{id}` — `DetectionPacket` published by detector for camera `id`
- `fusion.aligned` — `AlignedFrame` published by aligner
- `fusion.tracks` — `Track` list published by track manager (30Hz)

Messages serialize through MsgPack (`aero.transport.pack`) **even in-process**. This is deliberately wasteful in MVP but ensures Phase 1 swap-in is identity.

### 8.2 Phase 1 network transport (stubbed)

`aero.transport.udp.UdpTransport` is stubbed in MVP. The interface is defined so we know what to implement:
- `UdpPublisher(host, port).publish(msg)` — encodes to MsgPack, sends UDP.
- `UdpSubscriber(bind_iface, port).subscribe()` — async generator yielding decoded messages.

In Phase 1, we register `UdpSubscriber` on the central side under the same topic name, so `aligner` etc. don't change.

---

## 9. Visualization

### 9.1 Backend (`aero.viz.server`)

aiohttp app on port `8080`:
- `GET /` — serves `viz_web/index.html`
- `GET /static/*` — serves `viz_web/src/`
- `WS /ws` — WebSocket connection. On connect, server sends one `CameraConfig` message (camera positions + intrinsics for frustum rendering), then `VizFrame` messages at 30Hz.

WebSocket message format (JSON):
```json
{
  "type": "viz_frame",
  "data": <VizFrame>
}
```

### 9.2 Frontend (`viz_web/`)

ES modules from CDN (no build step):
```html
<script type="importmap">
  { "imports": {
      "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
      "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
    }
  }
</script>
```

Scene elements:
- Ground grid at `z=0`
- Camera node icons at calibrated positions (small camera meshes)
- Camera frustums (semi-transparent, drawn from intrinsics + extrinsics; toggleable)
- Tracked objects as colored spheres (color by track id)
- Track trails as `LineSegments` with fading alpha
- 1σ position covariance ellipsoid (toggleable)
- Track ID label as `CSS2DObject` billboard
- Stats overlay (HTML/CSS): FPS, latency, # active tracks, per-camera online status

Controls: `OrbitControls` for the world-frame camera. Mouse-click a track to pin its info panel.

### 9.3 Frontend file layout

```
viz_web/
├── index.html              # entry, importmap, mount point
└── src/
    ├── main.js             # bootstrap, requestAnimationFrame loop
    ├── scene.js            # three.js scene setup, ground, lights
    ├── cameras.js          # camera node meshes + frustum geometry
    ├── tracks.js           # track spheres, trails, covariance ellipsoids
    ├── stats.js            # HTML overlay update
    └── wsclient.js         # WebSocket → state update
```

---

## 10. Logging

Library: `structlog` configured in `aero.log.setup()`.

### 10.1 Outputs

Two sinks, both active simultaneously:
- **Console**: pretty-printed, colored, INFO+ by default (DEBUG with `--verbose`).
- **File**: JSONL to `data/logs/<date>/run-<timestamp>.jsonl`, DEBUG+ always.

### 10.2 Structure

Every log entry is JSON with keys:
```json
{
  "ts": "2026-05-27T18:23:11.123456Z",
  "level": "info",
  "logger": "aero.fusion.tracks",
  "event": "track_created",
  "track_id": 7,
  "position": [1.2, 3.4, 5.6],
  "n_obs": 3
}
```

Mandatory fields: `ts`, `level`, `logger`, `event`. Free-form structured fields per event.

### 10.3 Standard events (canonical event names)

| Event | When | Fields |
|---|---|---|
| `app_start` | Process boot | `config_path`, `version` |
| `camera_opened` | V4L2 device opened | `camera_id`, `device`, `width`, `height`, `fps` |
| `camera_disconnected` | Frame read fail | `camera_id`, `error` |
| `calibration_loaded` | At startup | `path`, `n_cameras`, `ba_rms_px` |
| `detection_published` | Per-frame, DEBUG level | `camera_id`, `frame_seq`, `n_detections`, `latency_ms` |
| `track_created` | New track | `track_id`, `position`, `n_obs` |
| `track_updated` | Per measurement, DEBUG | `track_id`, `measurement`, `mahalanobis` |
| `track_killed` | Track aged out | `track_id`, `lifetime_s`, `update_count` |
| `outlier_rejected` | Mahalanobis exceeded | `track_id`, `mahalanobis`, `gate` |
| `frame_dropped` | Late packet | `camera_id`, `lateness_ms` |
| `stats_periodic` | Every 5s | `fps_per_cam`, `latency_p50_ms`, `latency_p95_ms`, `n_active_tracks`, `mem_mb` |

### 10.4 Latency tracking

The `DetectionPacket` carries `capture_ts_ns` and `process_ts_ns`. The fusion code timestamps when it consumes a packet. The WebSocket server timestamps emission. Subtract to get per-stage latencies; log histograms in `stats_periodic`.

---

## 11. Configuration

All runtime config in YAML, loaded into pydantic models from `aero.config`. Single file `configs/mvp.yaml` is the entry point.

```yaml
# configs/mvp.yaml
app:
  name: "mvp"
  log_level: "INFO"
  log_dir: "data/logs"

cameras:
  - id: 0
    device: "/dev/video0"
    width: 1280
    height: 720
    fps: 30
    intrinsics_file: "configs/intrinsics_cam0.yaml"
  - id: 1
    device: "/dev/video2"
    width: 1280
    height: 720
    fps: 30
    intrinsics_file: "configs/intrinsics_cam1.yaml"
  - id: 2
    device: "/dev/video4"
    width: 1280
    height: 720
    fps: 30
    intrinsics_file: "configs/intrinsics_cam2.yaml"

extrinsics_file: "configs/extrinsics.yaml"

detection:
  horizon_mask_file: null            # null = no mask (living room)
  bg_subtractor:
    history: 300
    dist2_threshold: 400.0
  morphology:
    erode_kernel: 3
    dilate_kernel: 5
  blob_filter:
    min_area_px: 5
    max_area_px: 500
    max_aspect_ratio: 5.0
  coherence:
    enable: true
    track_frames: 5
    match_dist_px: 50.0
    min_track_length: 3
    max_direction_variance: 1.57

fusion:
  align_window_ns: 33_000_000
  align_wait_ns: 50_000_000
  min_cameras_per_frame: 2
  association:
    tau_assoc_m: 0.5
  triangulation:
    pixel_noise_px: 1.0
  kalman:
    sigma_accel_mps2: 8.0
    initial_position_var: 10.0
    initial_velocity_var: 100.0
    gate_mahalanobis_squared: 11.345
  tracks:
    init_consecutive: 3
    death_frames: 30
    trail_length: 200

viz:
  ws_port: 8080
  http_port: 8080
  publish_rate_hz: 30

recording:
  enable: false
  output_dir: "data/recordings"
```

---

## 12. Dataset recording and replay

### 12.1 Recording

`aero.recording.recorder.Recorder` subscribes to detection topics and (optionally) raw frames. When `recording.enable: true` in config:

- Per camera, raw frames saved as JPEG or AVI to `data/recordings/<session>/cam{id}/`.
- All `DetectionPacket`s saved to `data/recordings/<session>/detections.msgpack-stream`.
- Metadata `data/recordings/<session>/manifest.json`: cameras, calibration snapshot, session start time.

### 12.2 Replay

`tools/replay.py --session data/recordings/<id>` reads the manifest, replays detection packets through the fusion pipeline at real-time or accelerated speed (`--speed 4x`), serves the same WebSocket viz. Lets us iterate on fusion algorithms without recapturing.

Replay-as-source: `aero.camera.replay.ReplayCameraSource` is a `CameraSource` that yields the recorded frames + detections. Means we can A/B detection algorithms against the same input.

---

## 13. MVP feature list

The deliverable. Each row is a concrete unit of work; reference IDs in commits.

| ID | Feature | Where | How |
|---|---|---|---|
| MVP-01 | Project scaffold | repo root | `pyproject.toml`, `pytest` config, pre-commit, basic CI |
| MVP-02 | Message schemas | `aero/messages.py` | pydantic models for `Detection`, `DetectionPacket`, `Track`, `VizCamera`, `VizFrame`; MsgPack round-trip tested |
| MVP-03 | Config system | `aero/config.py`, `configs/mvp.yaml` | pydantic-based YAML loader with schema validation |
| MVP-04 | Logging | `aero/log.py` | `structlog` + `logging` handlers, JSONL file sink + console sink |
| MVP-05 | Timestamp utilities | `aero/timestamps.py` | `monotonic_ns()`, `wall_ns()`, conversion helpers |
| MVP-06 | Geometry primitives | `aero/fusion/geom.py` | SE(3) ops, projection, undistortion, ray construction, CPA; full unit tests |
| MVP-07 | `CameraSource` abstract + V4L2 impl | `aero/camera/base.py`, `aero/camera/v4l2.py` | asyncio task wraps `cv2.VideoCapture`; emits `(frame, capture_ts_ns)` tuples |
| MVP-08 | KNN background subtractor | `aero/detection/knn_bg.py` | Thin wrapper around `cv2.createBackgroundSubtractorKNN` with config |
| MVP-09 | Morphology | `aero/detection/morphology.py` | Erode + dilate with configurable kernels |
| MVP-10 | Blob extraction + filter | `aero/detection/blob.py` | `connectedComponentsWithStats`, filter by area/aspect/edge |
| MVP-11 | Sub-pixel centroid | `aero/detection/centroid.py` | Intensity-weighted center-of-mass within bbox |
| MVP-12 | Temporal coherence filter | `aero/detection/coherence.py` | Per-camera blob tracker, directional variance gate |
| MVP-13 | Detection pipeline composition | `aero/detection/pipeline.py` | `DetectionPipeline.process(frame, ts)` → `DetectionPacket` |
| MVP-14 | In-process bus | `aero/transport/bus.py` | asyncio pub/sub with MsgPack ser/de |
| MVP-15 | Time aligner | `aero/fusion/aligner.py` | Sliding-window grouping by timestamp |
| MVP-16 | Cross-camera associator | `aero/fusion/associator.py` | Pairwise CPA, Hungarian assignment when needed |
| MVP-17 | Triangulator (DLT + L-M + cov) | `aero/fusion/triangulator.py` | NumPy DLT, scipy L-M, covariance via Jacobian |
| MVP-18 | Per-track Kalman filter | `aero/fusion/kalman.py` | filterpy `KalmanFilter` factory with our F/H/Q builders |
| MVP-19 | Track manager | `aero/fusion/tracks.py` | Init / update / coast / kill; trail buffers |
| MVP-20 | Calibration loader | `aero/calib/store.py` | `Calibration` dataclass from YAML, with derived `P` matrices |
| MVP-21 | Intrinsic calibration tool | `tools/calib_intrinsic.py`, `aero/calib/intrinsic.py` | Interactive ChAruco capture + OpenCV solver |
| MVP-22 | Extrinsic calibration tool | `tools/calib_extrinsic.py`, `aero/calib/extrinsic.py`, `aero/calib/bundle.py` | AprilTag detection + bundle adjustment |
| MVP-23 | Viz WebSocket server | `aero/viz/server.py` | aiohttp + websockets, 30Hz `VizFrame` push |
| MVP-24 | three.js frontend | `viz_web/` | Scene, cameras, tracks, trails, stats, OrbitControls |
| MVP-25 | Dataset recorder | `aero/recording/recorder.py` | Save frames + detections to disk |
| MVP-26 | Dataset replayer | `aero/recording/replayer.py`, `tools/replay.py` | Replay detections (or frames) through fusion |
| MVP-27 | MVP app entrypoint | `aero/app/mvp.py` | Composes everything; CLI `aero-mvp --config configs/mvp.yaml` |
| MVP-28 | Unit tests | `tests/` | ≥80% coverage on `fusion/`, `detection/`, `calib/` |
| MVP-29 | Integration test (synthetic) | `tests/test_e2e_synthetic.py` | Synthetic 3-camera scene with known trajectory; assert recovered trajectory matches within tolerance |
| MVP-30 | Living-room demo recording | `data/recordings/livingroom_demo_v1/` | Capture a paper airplane throw; commit to LFS or external storage |

---

## 14. Open MVP decisions

| ID | Item | Default | Revisit when |
|---|---|---|---|
| MVP-D1 | USB camera time-sync strategy | Software timestamp at `read()` return | If observed jitter > 30ms |
| MVP-D2 | Frame resolution | 1280×720 mono | If detection accuracy or compute insufficient |
| MVP-D3 | Coherence filter enabled by default | Yes | If living-room test shows too many missed real detections |
| MVP-D4 | Whether to record raw frames or just detections | Detections only by default | When we want to A/B detection algorithms |
| MVP-D5 | Bundle adjustment Jacobian | Analytic if we can derive cleanly, else finite difference | If extrinsic calibration is too slow (>30s) |
| MVP-D6 | Multi-target handling in MVP | Out of scope; assume single target | If living-room demo has spurious tracks |
| MVP-D7 | Voxel grid as visualization | No (covariance ellipsoid instead) | If covariance ellipsoid doesn't communicate uncertainty well |
| MVP-D8 | Use UKF instead of linear KF | No — linear KF is sufficient for CV model | If we add bearing-only turret measurements (Phase 1+) |

---

## 15. Definition of done (MVP)

The MVP is "done" when **all** of the following are true:

1. `aero-mvp --config configs/mvp.yaml` starts the system; opens 3 USB cameras; logs a clean startup sequence.
2. Viz is reachable at `http://localhost:8080` showing 3 camera positions, ground grid, and stats.
3. A paper airplane thrown through the volume produces a visible 3D trail in the viz within 300ms of motion start.
4. The Kalman filter visibly smooths jitter; you can see it predicting (trail extends) when the object is briefly occluded.
5. A static reference object (a small ball on a stand) reports a 3D position stable to ±10cm across separate runs.
6. `tools/replay.py` can re-play a recorded session and reproduce a visually identical trail.
7. All unit and integration tests pass.
8. End-to-end latency (capture_ts → WebSocket emission) is <300ms p95 in steady state.
9. README has a quickstart that gets a new user from `git clone` to running viz in <30 minutes.
10. Living-room paper-airplane demo recorded and committed (video + dataset).

---

## Appendix A — Coordinate conventions

- **World frame**: right-handed, z-up. Origin = camera 0 optical center.
- **Camera frame**: right-handed, x-right, y-down, z-forward (OpenCV).
- **Camera pose**: `T_world_cam` maps points from camera frame to world frame: `X_w = T_world_cam · X_c`.
- **Projection**: A world point projects to camera `i` via `x_c = T_world_cam_i^{-1} · X_w`, then pinhole + distortion via `K_i, D_i`.
- **Units**: meters, seconds, radians (degrees only in UI).
- **Timestamps**: `int` (Python) representing nanoseconds since UNIX epoch.

## Appendix B — Library version pins (proposed)

```toml
[project]
requires-python = ">=3.11"
dependencies = [
  "numpy>=1.26,<2.2",
  "scipy>=1.13",
  "opencv-python>=4.9",
  "filterpy>=1.4.5",
  "pupil-apriltags>=1.0.4",
  "pydantic>=2.6",
  "pyyaml>=6.0",
  "msgpack>=1.0.7",
  "structlog>=24.1",
  "aiohttp>=3.9",
  "websockets>=12.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "pytest-asyncio>=0.23",
  "pytest-cov>=4.1",
  "ruff>=0.4",
  "mypy>=1.10",
]
```

## Appendix C — Equation summary (one-page reference)

**Background subtraction (KNN)**: pixel is foreground iff
`|{ s ∈ history : (v - s)² < dist2Threshold }| < kNNSamples`

**Sub-pixel centroid** (binary mask M over bbox):
`cx = Σ x·M(x,y) / Σ M(x,y)`, `cy = Σ y·M(x,y) / Σ M(x,y)`

**Ray in world** from pixel `(u,v)` with intrinsics `K, D` and pose `T_world_cam = (R, t)`:
`x_n = undistort([u,v], K, D)`,
`d_cam = [x_n.x, x_n.y, 1] / ||·||`,
`ray_origin = t` (cam center in world),
`ray_dir = R · d_cam`.

**Closest point of approach** between rays `r₁(s) = c₁ + s·d₁` and `r₂(t) = c₂ + t·d₂`:
`s* = (b·e - c·d) / (a·c - b²)`, `t* = (a·e - b·d) / (a·c - b²)`,
with `a = d₁·d₁`, `b = d₁·d₂`, `c = d₂·d₂`, `d = d₁·(c₁-c₂)`, `e = d₂·(c₁-c₂)`.

**DLT triangulation**: stack rows `[u_i p₃ⁱ - p₁ⁱ; v_i p₃ⁱ - p₂ⁱ]` into A, solve `AX = 0` via SVD, dehomogenize.

**L-M refinement**: `argmin_X Σ_i ||π_i(X) - (u_i, v_i)||²` via `scipy.optimize.least_squares`.

**Triangulation covariance**: `Σ_X = σ_px² · (Jᵀ J)⁻¹` where `J = ∂π/∂X` at the refined estimate.

**Kalman filter (CV, dt-variable)**:
`F = [[I₃, dt·I₃], [0, I₃]]`, `H = [I₃ | 0₃]`,
`Q = σ_a² · diag([dt⁴/4 I₃, dt² I₃])` (with cross-terms `dt³/2 I₃` off-diagonal),
`R = Σ_X` from triangulator.

**Innovation gate**: accept iff `yᵀ S⁻¹ y < 11.345` (χ², 3 DOF, 99%).

**Process noise derivation** (white-acceleration model):
`G = [dt²/2·I₃; dt·I₃]`, `Q = σ_a² · G · Gᵀ`.

