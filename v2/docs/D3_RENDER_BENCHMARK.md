# D3 render benchmark and full-clip decision

**Measured** (recorded bench result: one timed frame on this machine;
`benchmark.json` archived with the run):

- Machine: Apple M4, 16 GB, Blender 5.2.0 LTS (hash fbe6228777e7), Cycles CPU,
  128 samples, 2400x1392 render (2304x1296 nominal + 48 px warp margin,
  `--margin-px`), motion blur on (shutter 0.09 frame).
- Per-frame render time: **11.6 s** (frame 225, camera 0, target visible;
  an earlier run of the same scene measured 10.0 s — treat ~10-12 s as the
  honest range on this machine).
- Full clip = 450 frames x 3 cameras = 1350 frames -> **4.4 hours** at the
  measured rate (render budget rule: measured x 1350, no predictions).

**Decision: render on the Mac.** ~4.4 h is an overnight run; cloud GPU is not
justified. Revisit only if Tier 1.5 confusers or higher sample counts push the
per-frame time past ~27 s (>10 h clip).

## Scene timing verification

The generator time-shifts the crossing so the first frame with the target
inside every camera's nominal frame lands exactly at the manifest's
`target_entry_s` = 3.0 s (warm-up stays background-only), and asserts the
shared-FOV window against `shared_fov_min_s`: measured window 9.21 s >= 9.0 s.
First visible truth label: frame 90 = 3.0 s exactly.

## Sidecar correctness check (brief requirement)

On the benchmark frame (225, camera 0): projected truth center from
`truth/trajectory.jsonl` = (1173.8, 695.4) on the render grid; rendered target
blob = 30 px silhouette (~96% contrast against sky), centroid (1175.7, 695.5)
— the projected truth lands inside the rendered blob (offset 1.84 px). The
offset along +u is consistent with sun-side shading of the sphere (sun azimuth
140 deg) biasing the thresholded silhouette; truth is the geometric center by
D0 decision, and this shading offset is exactly the kind of `target_reference`
systematic the two-channel rule exists for.

`truth/labels.jsonl` (one row per camera per frame, target + future confusers)
parses with `skyweave2.eval.labels.read_labels`, and the inline `dataset_id`
in `dataset.json` byte-matches the host `skyweave2.dataset.dataset_id` for the
same inputs (git rev and sensor-model version travel to Blender via the
`_host` block that `dataset to-json` embeds).

Blender 5.2 API deltas discovered while bringing the script up (recorded for
regeneration): the Nishita sky enum is gone — `MULTIPLE_SCATTERING` is the
physically-based successor, honored from the manifest pin and recorded in
`dataset.json`; `Action.fcurves` is gone — keyframe interpolation is set via
the new-keyframe preference instead.
