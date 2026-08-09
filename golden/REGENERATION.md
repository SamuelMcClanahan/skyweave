# Golden artifacts: regeneration policy and record

Policy (D0): goldens pin v1 behavior. Regenerating any golden requires a
recorded reason in this file. `MANIFEST.sha256` holds the current hashes.

## Contents

- `peak_baselines.json`: v1 Rayweave peak baselines. Generator:
  `v1/scripts/generate_golden_peaks.py`, backend `python_numpy`.
- `sim_check_summary.json`: deterministic subset of the v1 sim-check
  `RunSummary` on `v1/configs/sim.yaml`, source `packet`. Latency fields are
  excluded because they depend on wall-clock speed.

## Record

### 2026-08-05 (D0, T6)

- Verified `peak_baselines.json`: regenerated from the live v1 tree in a
  clean Python 3.10 environment. Result is identical to the committed file.
  Two consecutive runs are byte-identical.
- Added `sim_check_summary.json` and `MANIFEST.sha256`.
- Finding F-D0-8: with OpenCV absent, `skyweave/camera/motion.py` falls back
  to pure-Python connected components and the two `rendered`-source cases
  change slightly (`room_7cam_rendered` peak RMSE 0.0656 vs 0.0653 m).
  Golden regeneration therefore REQUIRES OpenCV installed
  (`opencv-contrib-python-headless`). The packet-source cases do not depend
  on OpenCV.

Environment for this record: CPython 3.10.18, numpy 2.x, pydantic 2.x,
opencv-contrib-python-headless 5.0.0.93, `python_numpy` scorer backend.
