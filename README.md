# Skyweave

Experimental multi-camera aircraft and drone tracking system.

The current repo contains:

- `SPEC_MVP.md` - draft Skyweave MVP software specification.
- `src/skyweave/` - headless synthetic MVP core implementation.
- `configs/sim*.yaml` - synthetic packet validation configs.
- `tests/` - unit and synthetic pipeline tests.
- `docs/conversations/` - design discussion notes and decision logs.
- `docs/specs/` - older/high-level spec notes.
- `reference/pixel-to-voxel-projector/` - preserved reference projector code.

- **Rayweave**: calibrated 2D motion evidence projected into 3D voxel evidence;
- **Weavefield**: sparse 4D evidence history for visualization, replay, and
  tracking;
- **Tracking**: voxel peaks and triangulation baselines filtered into stable
  object tracks.

## Headless Synthetic Check

Create a local virtual environment and install the package:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run the 10 cm baseline synthetic packet validator:

```bash
.venv/bin/skyweave-sim-check --config configs/sim.yaml
```

Record a replayable packet session:

```bash
.venv/bin/skyweave-sim-check --config configs/sim.yaml --record
```

Replay the recorded session:

```bash
.venv/bin/skyweave-replay --session data/recordings/<session-id>
```

Benchmark stage latency without per-frame logging:

```bash
.venv/bin/skyweave-benchmark --config configs/sim.yaml --frames 300 --warmup 30
```

Run the optimized Numba scorer profile:

```bash
.venv/bin/python -m pip install -e '.[numba]'
.venv/bin/skyweave-benchmark --config configs/sim_numba.yaml --frames 300 --warmup 30
.venv/bin/skyweave-benchmark --config configs/sim_05_numba.yaml --frames 300 --warmup 30
.venv/bin/skyweave-benchmark --config configs/sim_mvp_ov9281_100hz_numba.yaml --frames 300 --warmup 90
```

Resolution profiles are available at `configs/sim_075.yaml` and
`configs/sim_05.yaml`. `configs/sim_numba.yaml` keeps the 10 cm baseline grid
and switches only the scorer backend. `configs/sim_05_numba.yaml` runs the
optimized scorer at 5 cm. `configs/sim_mvp_ov9281_100hz_numba.yaml` keeps the
5 cm grid, uses 100 Hz synthetic timestamps, and uses OV9281-like 1280x800
image geometry. Perturbation profiles are available at
`configs/sim_perturb_pixel_noise.yaml`, `configs/sim_perturb_jitter.yaml`,
`configs/sim_perturb_false_positive.yaml`, and
`configs/sim_perturb_dropout.yaml`.

Export a deterministic visualization bundle:

```bash
.venv/bin/python -m pip install -e '.[dev,numba]'
.venv/bin/skyweave-viz-export --config configs/sim_mvp_ov9281_100hz_numba.yaml --output data/viz/mvp-100hz --frames 120
```

The bundle contract is documented in `docs/viz-data-contract.md`.

Run the live Three.js/Cesium visualizer demo:

```bash
.venv/bin/python -m pip install -e '.[dev,viz]'
.venv/bin/skyweave-viz-demo
```

Visualizer notes are in `docs/visualizer.md`.

Run the headless camera packet-generation smoke check:

```bash
.venv/bin/skyweave-camera-check --frames 120 --width 1280 --height 800 --fps 100 --square-size 18 --console-every 20
```

This validates frame differencing, blob extraction, bounded motion patches, and
packet latency without requiring real cameras or a browser.

Run the same packet-generation check against live UVC cameras:

```bash
.venv/bin/python -m pip install -e '.[dev,camera]'
.venv/bin/skyweave-camera-check --device /dev/video0 --frames 120 --width 1280 --height 800 --fps 100 --fourcc MJPG --console-every 20
.venv/bin/skyweave-camera-check --devices /dev/video0,/dev/video2,/dev/video4 --frames 300 --width 1280 --height 800 --fps 100 --fourcc MJPG --warmup-frames 10 --jsonl data/logs/live_camera_check.jsonl --snapshot-dir data/camera_snapshots
```

The live mode stops at `MotionPacket` stats. Calibration and Rayweave fusion are
the next gate after the camera-side packet stream looks sane. `--snapshot-dir`
writes one grayscale PGM frame per live camera so capture can be verified
without a browser or GUI.

Current Rubik Pi 3 lab result with three Arducam OV9281 UVC cameras:

```bash
.venv/bin/skyweave-camera-check --devices /dev/video0,/dev/video2,/dev/video4 --frames 120 --width 640 --height 480 --fps 100 --fourcc MJPG --warmup-frames 10 --jsonl data/logs/rubik_3cam_640x480_check.jsonl --snapshot-dir data/camera_snapshots_640
```

Three 640x480 MJPG streams work at the 100 FPS request. Three 1280x800 MJPG
streams need each camera on a separate USB2 root path. With two OV9281 cameras
sharing the Renesas blue USB-A controller path, the second shared-path camera
fails at stream start (`VIDIOC_STREAMON: No space left on device`). Moving one
camera to the USB-C path allowed three full-resolution streams at the V4L2
level.

The optimized full-resolution packet smoke path uses OpenCV-backed motion
extraction and one worker per camera. `opencv_contours` is the sparse-motion
performance backend; `opencv` keeps the exact connected-components path.

```bash
.venv/bin/skyweave-camera-check --devices /dev/video0,/dev/video2,/dev/video4 --frames 1000 --width 1280 --height 800 --fps 100 --fourcc MJPG --warmup-frames 30 --profile-stages --motion-backend opencv_contours --parallel-cameras
```

Verified Rubik Pi 3 result with the USB-C topology: 1000 frames per camera,
zero read failures, about 100 FPS effective per camera, and p95 packet latency
around 9-10 ms per camera.

The combined live-camera plus voxel benchmark keeps real camera capture,
MJPEG decode, grayscale conversion, frame differencing, and packet extraction in
the loop, then replaces the uncalibrated live evidence with calibration-consistent
synthetic patches so Rayweave reconstruction is measurable before live
calibration exists:

```bash
.venv/bin/skyweave-live-benchmark --config configs/sim_mvp_ov9281_100hz_numba.yaml --devices /dev/video0,/dev/video2,/dev/video4 --frames 6000 --warmup-frames 100 --width 1280 --height 800 --fps 100 --fourcc MJPG --motion-backend opencv_contours --rayweave-input stress-patches --console-every 1500
```

With sparse touched-voxel combination, stamp-based dedupe, and the contour packet
backend, the 5 cm profile processed 6000 measured frames with zero read failures,
`aligned=6000`, `measurements=6000`, 99.6 FPS p50, 12.95 ms p95 total latency,
and 15.86 ms p99 total latency. That keeps p99 below the 16.67 ms frame interval
for 60 FPS in this controlled stress-patch benchmark. A camera-packet-only
6000-frame run held about 101 FPS effective per camera with zero read failures.

OpenCV thread count is left at OpenCV's platform default unless
`SKYWEAVE_OPENCV_THREADS` is set. On the Rubik, forcing one thread improved
median timing but worsened p95 camera wait in longer runs.

To rerun the camera-only and voxel benchmarks as one pass/fail sweep on the
Rubik:

```bash
.venv/bin/python scripts/rubik_perf_sweep.py --devices /dev/video0,/dev/video2,/dev/video4 --frames 6000 --warmup-frames 100 --width 1280 --height 800 --fps 100 --fourcc MJPG --motion-backend opencv_contours --output data/logs/rubik_perf_sweep.json
```

The sweep fails nonzero if any camera reports read failures, any camera falls
below 90 FPS effective throughput, voxel alignment/measurements do not cover all
measured frames, or voxel `total` p99 exceeds 16.67 ms.

To compare whole-frame JPEG transport against motion packets:

```bash
.venv/bin/skyweave-camera-check --devices /dev/video0,/dev/video2,/dev/video4 --frames 150 --width 1280 --height 800 --fps 100 --fourcc MJPG --warmup-frames 20 --profile-stages --live-pipeline jpeg-frame --jpeg-quality 80
```

Prepare the ChArUco calibration target before printing:

```bash
.venv/bin/python scripts/generate_charuco_board.py --squares-x 10 --squares-y 7 --square-mm 40 --marker-mm 30 --dictionary DICT_5X5_1000
```

This writes a printable PNG and YAML metadata under `data/calibration_targets/`.
Print at 100 percent scale, mount it flat, then measure the printed square size
with a ruler or calipers. Use the measured square size for calibration, not just
the requested file size. A monitor or tablet can smoke-test ChArUco detection,
but it is not a substitute for camera calibration because screen flatness,
scaling, glare, and pixel geometry make the physical dimensions unreliable.

For the measured MacBook-screen smoke target currently used in the lab:

```bash
.venv/bin/skyweave-charuco-check --devices /dev/video0,/dev/video2,/dev/video4 --frames 120 --warmup-frames 10 --width 1280 --height 800 --fps 30 --fourcc MJPG --squares-x 10 --squares-y 7 --square-mm 24 --marker-mm 18 --dictionary DICT_4X4 --snapshot-dir data/charuco_snapshots --jsonl data/logs/charuco_laptop_check.jsonl
```

The command expands `DICT_4X4` across the OpenCV 4x4 dictionary families and
uses the best marker/corner count. This is only a detection smoke and rough
calibration trial; printed rigid-board calibration should replace it.

Map current `/dev/videoN` nodes to stable USB paths and physical labels:

```bash
.venv/bin/skyweave-camera-inventory --labels cam1,cam2,cam3 --output configs/cameras.local.yaml
```

Use `id_path` from that file as the stable identity; `/dev/videoN` may change
after unplugging cameras. The `/dev/video0,/dev/video2,/dev/video4` commands
below are current-node examples; refresh them from `configs/cameras.local.yaml`
after replugging.

Capture structured ChArUco observations once the board is visible:

```bash
.venv/bin/skyweave-charuco-capture --camera-config configs/cameras.local.yaml --frames 180 --sample-every 5 --width 1280 --height 800 --fps 15 --fourcc MJPG --squares-x 10 --squares-y 7 --square-mm 24 --marker-mm 18 --dictionary DICT_4X4_50 --min-corners 24 --save-images
```

This writes `manifest.yaml`, `observations.jsonl`, and optional frame snapshots
under `data/calibration/`. Laptop-screen captures are useful for smoke testing;
the printed rigid board should be used for final calibration.

For easier aiming, run the lightweight live web viewer on the Rubik:

```bash
.venv/bin/python -m skyweave.cli.charuco_live --devices /dev/video0,/dev/video2,/dev/video4 --width 1280 --height 800 --fps 15 --fourcc MJPG --squares-x 10 --squares-y 7 --square-mm 24 --marker-mm 18 --dictionary DICT_4X4 --display-scale 0.5 --detect-every 2 --host 0.0.0.0 --port 8090
```

Then open `http://10.42.0.111:8090/` from the laptop. It streams an annotated
JPEG view with marker/corner counts, best dictionary, detection rate, FPS, and
latency. Camera buttons at the top switch the active device; failed/disconnected
cameras are marked in red without stopping the viewer.

For the first Rubik Pi 3 target sweep, use
`docs/rubik-pi-setup.md`.

The command prints per-frame truth, voxel peak, triangulation, track estimate,
errors, score, and latency, then writes JSONL logs under `data/logs/`. Final
summaries separate packet drops from `not_visible` camera/object pairs where the
synthetic target projected outside a camera image. Recorded sessions are written
under `data/recordings/`.

## Layout Notes

The active Python package is under `src/skyweave/`. Historical prototype code is
kept under `reference/` so it is available for comparison without looking like
part of the MVP runtime.
