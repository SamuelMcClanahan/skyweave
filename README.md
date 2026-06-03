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
