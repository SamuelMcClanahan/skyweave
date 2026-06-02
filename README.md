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

Resolution profiles are available at `configs/sim_075.yaml` and
`configs/sim_05.yaml`. Perturbation profiles are available at
`configs/sim_perturb_pixel_noise.yaml`, `configs/sim_perturb_jitter.yaml`,
`configs/sim_perturb_false_positive.yaml`, and
`configs/sim_perturb_dropout.yaml`.

The command prints per-frame truth, voxel peak, triangulation, track estimate,
errors, score, and latency, then writes JSONL logs under `data/logs/`. Final
summaries separate packet drops from `not_visible` camera/object pairs where the
synthetic target projected outside a camera image. Recorded sessions are written
under `data/recordings/`.

## Layout Notes

The active Python package is under `src/skyweave/`. Historical prototype code is
kept under `reference/` so it is available for comparison without looking like
part of the MVP runtime.
