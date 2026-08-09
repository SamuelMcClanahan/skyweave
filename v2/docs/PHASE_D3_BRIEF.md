# Phase D3 brief: synthetic frames, sensor model, scorecard

**Status:** finalized 2026-08-05. Work order for phase D3.
**Read first:** `/CLAUDE.md`, `v2/docs/DETECTION_CONTRACTS_D0.md`,
`v2/docs/SYNTHETIC_PIPELINE_DESIGN.md` (the design this phase implements),
`v2/configs/exp001_scene.yaml` (the frozen scene).

## Goal

Produce deterministic synthetic frames with known truth that are wrong in
realistic ways, and the scoring tool that grades any detector on any clip.
Build order is fixed: sensor model, then scorecard + hybrid clips, then the
Blender generator. The conversion-gain bench task runs in parallel on real
hardware and does not block the code.

## Scope

In: sensor model (stage B), scorecard + hybrid clip generator (stage D),
Blender Tier 1 clean-scene generator (stage A), dataset contract, tests.
A trivial fixed-background-mean detector is allowed ONLY as scorecard
plumbing proof.

Out: the real GMM2-like detector (D4), confuser objects (later tier),
board injection (D8), Isaac Sim, RAW10 packing as primary path, anything
under `v1/`, `golden/`, or `contracts/`.

## Package layout

```text
v2/src/skyweave2/sensor_model/
  config.py       # SensorModelSpec: every stage toggleable, every param a field
  optics.py       # vignetting, PSF blur, Brown-Conrady warp, decimate to grid
  radiometry.py   # radiance->electrons, shot noise, dark/DSNU/PRNU, full well
  readout.py      # AE gain controller, read noise, black level, 10-bit quantize
  mosaic.py       # Bayer mosaic (BGGR assumed, Provisional) + demosaic + RGB->Y U8
  pipeline.py     # the chain in SYNTHETIC_PIPELINE_DESIGN §5.1, in that order
v2/src/skyweave2/eval/
  labels.py       # truth label records (target + future confusers)
  scorecard.py    # quality/health/perf metrics; JSON out; thresholds in config
  hybrid.py       # composite synthetic blob into real footage + emit truth labels
v2/blender/
  exp001_generate.py   # bpy script reading exp001_scene.yaml; radiance EXR + sidecars
v2/tests/sensor_model/, v2/tests/eval/
```

## Frozen decisions

| Item | Value |
| --- | --- |
| Stage order | Exactly SYNTHETIC_PIPELINE_DESIGN §5.1; renderer emits radiance only |
| Scene | `exp001_scene.yaml` is authoritative; exposure 3 ms (Provisional amendment) |
| Seeds | Per-camera noise seed derived from (dataset_seed, camera_id, frame_seq); cross-camera noise must be independent |
| Determinism | Same manifest + versions + seed = byte-identical U8 Y output; dataset_id = SHA-256 over (manifest, Blender version, sensor-model version, seed, git rev) |
| Truth separation | Renderer truth and estimator calibration are separate serialized objects, never shared by reference; estimator copy is separately perturbable |
| Measured vs swept | Conversion gain: measured anchor (bench, parallel). Read noise/full well: measured once then swept. Dark, DSNU, PRNU, vignetting, PSF, distortion, AE rate: swept within plausible bounds |
| CFA | BGGR Provisional; verify against the Luckfox driver during the bench session before the mosaic freezes |
| Blender settings | Standard view transform; linear EXR half-float; denoiser OFF; motion blur ON, shutter = 3 ms / 33.33 ms of frame time; pinhole camera with warp margin (config); explicit per-frame seed; pinned Blender version recorded into the manifest at first render |
| Render budget rule | Render ONE frame, time it, multiply by 1350 (450 frames x 3 cameras); that number decides Mac vs cloud GPU. No predictions |
| Storage | Radiance EXRs are intermediates (~24 GB per full clip): deletable once the U8 output is frozen and hashed |

## Scorecard specification

`uv run skyweave2-score <clip> <config> -> scorecard.json`

Quality (needs truth): recall, centroid_err_px mean/p95, false_per_frame.
Health (no truth needed): occupancy_pct, warmup_frames, component_count.
Performance: fps, peak_mem_mb, latency_ms_p95.

Provisional pass values (config, not constants): recall >= 0.95,
occupancy < 0.5 %, false_per_frame < 2. Comparison principle: two detectors
are compared by scorecards, never by identical masks.

Hybrid clips: input = real sky clip + a trajectory spec; output = composited
clip + truth labels. The injected blob uses the sensor-model PSF/noise so it
matches the footage. Truth label centroid must match the injected position
to within 0.1 px by construction (test E3).

## The Bayer round-trip experiment (in scope, stage B alone)

Sweep synthetic blobs of 2 to 9 px through mosaic+demosaic on and off, at
sub-pixel phases. Report centroid bias versus blob size and phase in
`v2/docs/D3_SENSOR_NOTES.md`. This answers SYNTHETIC_PIPELINE_DESIGN open
question 1 and feeds the D4 tripwire interpretation.

## Bench tasks (Samuel, parallel; procedure only, not code)

1. Conversion gain: SC3336 on the Luckfox image, manual exposure/gain,
   defocused flat white target. Capture pairs at 8 to 10 exposure steps.
   Slope of (variance of pair difference / 2) versus mean gives 1/gain.
   Prefer RAW capture if the vendor tools allow; if only post-ISP Y is
   available, record that the anchor is post-ISP and keep the limitation
   visible. Record results as Measured in the D3 notes doc.
2. CFA pattern check against the driver (one register/readback or doc check).
3. Record several minutes of sky out a window for hybrid clips.

## Tests

| ID | Test |
| --- | --- |
| S1 | Each sensor stage: known input, hand-computed expected output |
| S2 | Full-chain determinism: same seed, byte-identical U8 output |
| S3 | Cross-camera noise independence (correlation ~ 0) |
| S4 | Mosaic toggle changes small-blob centroids; experiment runs and writes the notes table |
| S5 | Scorecard on a synthetic fixture with known recall/centroid error/false count reproduces those numbers |
| S6 | Health metrics run on a truth-free clip |
| E3 | Hybrid compositor: label centroid matches injected position < 0.1 px |
| S8 | dataset_id changes when any manifest field changes; stable otherwise |

All D0 and D1 tests still pass; ruff clean. Blender script correctness is
proven by sidecar checks (projected truth of the target center lands within
the rendered blob) once a render exists; do not block the phase on render
time if the benchmark forces cloud GPU, report instead.

## Done when

- Sensor model, scorecard, and hybrid generator pass S1-S8/E3.
- One Blender frame is rendered and timed; the full-clip decision is
  recorded (Mac or cloud) with the measured per-frame time.
- `D3_SENSOR_NOTES.md` exists with the Bayer round-trip table.
- No changes under `v1/`, `golden/`, or `contracts/`.
- Hand-back note lists surprises and the render benchmark number.
