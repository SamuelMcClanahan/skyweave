# Rubik Pi 3 Setup And Benchmark

Use this first target run to choose the starting voxel size from hardware data.
Do not add Numba or C++ until these baseline numbers exist.

## System Packages

On a Debian/Ubuntu-style image:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip build-essential
```

Confirm Python is 3.10 or newer:

```bash
python3 --version
```

## Checkout

Use this path when the Rubik Pi has repo access and you want the board to track
commits directly:

```bash
git clone <repo-url> skyweave
cd skyweave
git pull --ff-only
git rev-parse --short HEAD
```

If the repo is already cloned:

```bash
cd skyweave
git pull --ff-only
git rev-parse --short HEAD
```

## Copy From A Dev Machine

For quick iteration, copying the working tree over is also fine. This is useful
when testing local changes before pushing, and it is closer to the deployment
shape where the SBC receives an application payload rather than acting as the
main development machine.

From the development machine:

```bash
rsync -az --delete \
  --exclude .git \
  --exclude .venv \
  --exclude .uv-cache \
  --exclude .pytest_cache \
  --exclude 'data/' \
  --exclude '__pycache__/' \
  ./ rubikpi:~/skyweave/
```

Use `--delete` only when `~/skyweave/` is dedicated to this mirrored copy. If the
SBC checkout needs to report a commit, record it from the development machine:

```bash
git rev-parse --short HEAD
```

## Python Environment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

For a benchmark-only install, `.[dev]` can be replaced with `.`.

## Smoke Test

```bash
.venv/bin/skyweave-sim-check --config configs/sim.yaml --log-stages
```

This should pass before running longer benchmarks. The `--log-stages` flag writes
per-frame stage timings to JSONL without changing normal log readability by
default.

## Benchmark Sweep

Run each profile with the same frame and warmup counts:

```bash
.venv/bin/skyweave-benchmark --config configs/sim.yaml --frames 300 --warmup 30
.venv/bin/skyweave-benchmark --config configs/sim_075.yaml --frames 300 --warmup 30
.venv/bin/skyweave-benchmark --config configs/sim_05.yaml --frames 300 --warmup 30
```

Record:

- Rubik Pi OS image/version.
- CPU governor, if known.
- `python3 --version`.
- `git rev-parse --short HEAD`.
- Full benchmark output for all three configs.
- Whether the board was thermally stable or throttling.

## Initial Decision Rule

Use `total_p95` and `fps_p50` for the first cut:

- 10 cm is viable if `total_p95` is comfortably below 33 ms.
- 7.5 cm is viable if it remains below 33 ms with room for live camera overhead.
- 5 cm is viable only if it is still below 33 ms after leaving headroom for live
  capture, logging, and future visualization.

If scoring dominates runtime, optimize only the Rayweave scorer backend path.
Start with Python/NumPy cleanup, then try a Numba backend, and only add C++ if the
target data proves it is needed.
