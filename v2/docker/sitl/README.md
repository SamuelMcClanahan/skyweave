# Drone SITL tier in Docker (Apple-silicon Mac)

The pinned ArduCopter 4.7.0 SITL (`skyweave2.drone.sitl`) is an x86-64 Linux
binary. This folder runs the SITL tests and the closed-loop harness in a
`linux/amd64` container (`python:3.10-slim` + uv), under Rosetta in colima.
SITL only: nothing here talks to hardware or writes ArduPilot parameters.

## One-time setup

```bash
brew install colima
colima start --vm-type vz --vz-rosetta --cpus 6 --memory 8
docker run --rm --platform linux/amd64 alpine uname -m   # must print x86_64
v2/docker/sitl/run.sh build                              # image skyweave-sitl:py310
```

colima mounts `$HOME` read-write over virtiofs by default, which covers the
repo. Run `colima start` (no flags) after a reboot; it reuses the settings.

**Speed under Rosetta.** At the default `--speedup` 4, Rosetta stalls give
`ATTITUDE` gaps over `attitude_bound_ms` (100 ms; worst seen 800 ms). They
restarted some S1 holds; the 2026-11-20 gate still passed 70/70. Pass
`--speedup 2` to `harness`/`gate` (or `SKYWEAVE_HARNESS_SPEEDUP=2` for
pytest) to remove the gaps at twice the wall time.

## Commands (from the repo root)

```bash
v2/docker/sitl/run.sh fast                                   # drone fast tier
v2/docker/sitl/run.sh pytest tests/drone -m sitl -k "not test_s_gate"   # F series on SITL
v2/docker/sitl/run.sh harness --seed-set probe --probe-count 1 --scenarios S1 S2 \
    --out "$PWD/_local/sim-runs/$(date +%F)/probe"
v2/docker/sitl/run.sh gate "$PWD/_local/sim-runs/$(date +%F)/gate" --jobs 4   # full gate, 70 runs
v2/docker/sitl/run.sh pytest tests/drone -m sitl             # F series + the gate again via pytest
v2/docker/sitl/run.sh shell                                  # bash inside the container
```

`-m slow` and `-m sitl` select the same 9 tests; the six
`test_s_gate_scenario_green_every_repeat[S*]` tests are the whole gate
(`SKYWEAVE_HARNESS_JOBS` and `SKYWEAVE_HARNESS_SPEEDUP` are passed through).

## How it is wired

- The repo is mounted at its own host path, and the container works in
  `<repo>/v2`. Give `--out` an absolute path under `<repo>/_local/` (git-excluded);
  a relative `harness --out` lands inside the tracked `v2/` tree. `gate` turns a
  relative OUT into an absolute one and refuses a path outside the repo.
- The environment is the named volume `skyweave-sitl-venv` at `/opt/venv`
  (`UV_PROJECT_ENVIRONMENT`). The host's arm64 `v2/.venv` is never touched.
- Each command first runs `uv sync --frozen --extra dev --no-install-project`
  (dependencies only, from `uv.lock`) and `ensure_sitl()`. The project is
  imported through `PYTHONPATH=<repo>/v2/src` and never installed, so the tracked
  `v2/src/skyweave2.egg-info` is not rewritten; `UV_NO_SYNC=1` keeps `uv run`
  from installing it.
- The SITL binary and `copter.parm` (SHA-256 pinned) and the uv cache live in the
  named volume `skyweave-sitl-cache` (`SKYWEAVE_SITL_DIR=/cache/sitl-copter-4.7.0`),
  so they download once.
- No bytecode or `.pytest_cache` is written into the tree.
- Scorecards record `versions.git` as `<rev>+dirty` while this folder is
  untracked.

Reset: `docker volume rm skyweave-sitl-venv skyweave-sitl-cache`.
