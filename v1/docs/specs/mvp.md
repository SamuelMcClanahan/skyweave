# Skyweave MVP Delivery Spec

- **Status**: Draft v0.3 — scope and architecture pass
- **MVP target hardware**: single host, 3× OV9281 USB/UVC cameras, optional laptop camera
- **Scope**: This document is the MVP *delivery plan* — goal, tech-stack defaults,
  runtime configuration, feature list, open decisions, and definition of done.
  The architecture, message schemas, detection/fusion/Rayweave pipeline,
  calibration, transport, visualization, and V1 forward plan live in the system
  spec: [`SPEC_MVP.md`](../../SPEC_MVP.md). Section references below of the form
  "§6.8", "§7.5", etc. point into that system spec.

---

## 1. MVP goal

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

## 2. Tech stack (MVP defaults)

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

## 3. Configuration

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
  source: "packet_generator"       # packet_generator first; rendered_frames later
  default_scene: "paper_airplane_arc"
  timestep_hz: 30
  patch_encoding: "rle_u8"
  patch_size_px: 8
  pixel_noise_std_px: 0.5
  dropout_probability: 0.0
  timestamp_jitter_ms: 0.0
  emit_ground_truth: true
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

## 4. MVP feature list

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
| MVP-08 | Replay and synthetic packet sources | `skyweave/camera/replay.py`, `skyweave/sim/`, test fixtures | Emit recorded sessions or virtual-camera `MotionPacket`s with known ground truth |
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
| MVP-40 | Synthetic integration test | `tests/test_e2e_synthetic.py` | Synthetic 3-camera `MotionPacket` scene with known trajectory; assert Weavefield peak and track match within tolerance |
| MVP-41 | Performance/latency profiling | `tools/profile_mvp.py`, logs | Capture p50/p95 stage latency and recorder queue health on target host |
| MVP-42 | Demo recording and docs | `data/recordings/livingroom_demo_v1/`, `README.md` | Capture paper-airplane run, replayable session manifest, quickstart, calibration notes |

---

## 5. Open MVP decisions

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

## 6. Definition of done (MVP)

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
