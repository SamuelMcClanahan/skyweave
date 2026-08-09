"""D5 tracking report generator + the multi-blob seed-variant dataset.

Two subcommands (both seeded, deterministic):

    uv run python -m skyweave2.fusion.report build-multiblob \
        --seed 7 --out ../output/exp001_multiblob

    uv run python -m skyweave2.fusion.report generate --seed 7 \
        --gate-clips ../output/exp001_clips/gate \
        --gate-render ../output/exp001_renders/gate \
        --multiblob ../output/exp001_multiblob \
        --out docs/D5_TRACKING_REPORT.md \
        [--evidence docs/d5_evidence.json]

The report body is byte-deterministic: wall-clock runtimes are zeroed in
the body (D4 precedent) and written live to the evidence JSON beside it.

The multi-blob dataset stresses correspondence: the true target on the
manifest trajectory plus per-camera decoy blobs drifting INDEPENDENTLY in
image space (any cross-camera 3D crossing is transient by construction),
rendered through the D3 sensor model with fresh per-frame noise — no new
Blender render required.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import yaml

from skyweave2.contracts import CameraModel
from skyweave2.detector.config import Backend, DetectorConfig
from skyweave2.eval.clips import write_clip
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.metrics import (
    SceneEval,
    Truth,
    evaluate_scene,
    load_jsonl_truth,
    load_render_truth,
    observation_stream,
)
from skyweave2.sensor_model import SensorModelSpec, render_frame
from skyweave2.sensor_model.config import TruthCameraOptics

CAL_REV = "d5-fixed-cal-r1"
DET_REV = "d5-mog2-1536"

# Multi-blob scene constants (fixture geometry = exp001 trio).
MB_FRAMES = 150
MB_ENTRY_FRAME = 30
MB_FPS = 30.0
MB_RANGE_M = 243.84
MB_ALT_M = 20.0
MB_BASELINE_M = 20.0
MB_WIDTH, MB_HEIGHT = 2304, 1296
MB_F_PX = (MB_WIDTH / 2.0) / np.tan(np.radians(60.0) / 2.0)
MB_DECOYS_PER_CAMERA = 2


def _camera_pose(camera_id: int, x: float) -> np.ndarray:
    aim = np.array([0.0, MB_RANGE_M, MB_ALT_M])
    pos = np.array([x, 0.0, 1.5])
    z_cam = aim - pos
    z_cam /= np.linalg.norm(z_cam)
    up = np.array([0.0, 0.0, 1.0])
    x_cam = np.cross(z_cam, up)
    x_cam /= np.linalg.norm(x_cam)
    y_cam = np.cross(z_cam, x_cam)
    t = np.eye(4)
    t[:3, 0], t[:3, 1], t[:3, 2], t[:3, 3] = x_cam, y_cam, z_cam, pos
    return t


def multiblob_cameras() -> dict[int, CameraModel]:
    out = {}
    for camera_id, x in enumerate((-MB_BASELINE_M / 2, 0.0, MB_BASELINE_M / 2)):
        t = _camera_pose(camera_id, x)
        out[camera_id] = CameraModel(
            camera_id=camera_id,
            width=MB_WIDTH,
            height=MB_HEIGHT,
            k=((MB_F_PX, 0.0, (MB_WIDTH - 1) / 2),
               (0.0, MB_F_PX, (MB_HEIGHT - 1) / 2),
               (0.0, 0.0, 1.0)),
            t_world_cam=tuple(tuple(float(v) for v in row) for row in t),
            calibration_rev=CAL_REV,
        )
    return out


def gate_cameras(render_dir: str | Path) -> dict[int, CameraModel]:
    """Estimator cameras from the render truth (calibration held fixed per
    the manifest's clean-case acceptance note)."""
    records = json.loads((Path(render_dir) / "truth" / "cameras.json").read_text())
    out = {}
    for record in records:
        out[record["camera_id"]] = CameraModel(
            camera_id=record["camera_id"],
            width=record["nominal_width"],
            height=record["nominal_height"],
            k=((record["fx"], 0.0, record["cx"]),
               (0.0, record["fy"], record["cy"]),
               (0.0, 0.0, 1.0)),
            t_world_cam=tuple(tuple(float(v) for v in row)
                              for row in record["t_world_cam"]),
            calibration_rev=CAL_REV,
        )
    return out


def _mb_truth_position(frame: int) -> np.ndarray | None:
    if frame < MB_ENTRY_FRAME:
        return None
    t = frame / MB_FPS
    mid_t = (MB_ENTRY_FRAME + MB_FRAMES) / 2.0 / MB_FPS
    return np.array([30.0 * (t - mid_t), MB_RANGE_M, MB_ALT_M])


def build_multiblob(seed: int, out_dir: str | Path) -> None:
    """Seeded frame-level dataset through the D3 sensor model."""
    out_dir = Path(out_dir)
    cameras = multiblob_cameras()
    spec = SensorModelSpec(enable_ae=False)
    sigma_blob = 4.0 / 2.354820045
    v_grid, u_grid = np.meshgrid(
        np.arange(MB_HEIGHT, dtype=np.float64),
        np.arange(MB_WIDTH, dtype=np.float64),
        indexing="ij",
    )
    truth_rows = []
    for camera_id, camera in sorted(cameras.items()):
        optics = TruthCameraOptics(
            width=MB_WIDTH, height=MB_HEIGHT, fx=MB_F_PX, fy=MB_F_PX,
            cx=(MB_WIDTH - 1) / 2, cy=(MB_HEIGHT - 1) / 2,
        )
        decoy_rng = np.random.default_rng(
            np.random.SeedSequence((seed, camera_id, 0xDEC0))
        )
        decoys = []
        for _ in range(MB_DECOYS_PER_CAMERA):
            start = decoy_rng.uniform((200, 200), (MB_WIDTH - 200, MB_HEIGHT - 200))
            drift = decoy_rng.uniform(-6.0, 6.0, size=2)
            decoys.append((start, drift))
        frames = []
        ae_state = None
        for frame in range(MB_FRAMES):
            radiance = np.full((MB_HEIGHT, MB_WIDTH), 0.35)
            truth = _mb_truth_position(frame)
            if truth is not None:
                proj = camera.project(truth)
                if proj is not None:
                    blob = 1.2 * np.exp(
                        -0.5 * (((u_grid - proj[0]) / sigma_blob) ** 2
                                + ((v_grid - proj[1]) / sigma_blob) ** 2)
                    )
                    radiance = radiance + blob
            for start, drift in decoys:
                cu, cv = start + drift * frame
                if not (0 <= cu < MB_WIDTH and 0 <= cv < MB_HEIGHT):
                    continue
                radiance = radiance + 1.0 * np.exp(
                    -0.5 * (((u_grid - cu) / sigma_blob) ** 2
                            + ((v_grid - cv) / sigma_blob) ** 2)
                )
            y_u8, ae_state = render_frame(
                radiance, optics, spec, dataset_seed=seed,
                camera_id=camera_id, frame_seq=frame, ae_state=ae_state,
            )
            frames.append(y_u8)
        write_clip(frames, out_dir / f"cam{camera_id}", fps=MB_FPS,
                   source=f"d5-multiblob seed {seed} cam{camera_id}")
        print(f"cam{camera_id}: {len(frames)} frames", flush=True)
    for frame in range(MB_FRAMES):
        truth = _mb_truth_position(frame)
        truth_rows.append({
            "frame_seq": frame,
            "visible": truth is not None,
            "target_world_m": None if truth is None else [float(v) for v in truth],
        })
    with open(out_dir / "truth.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for row in truth_rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    (out_dir / "dataset.json").write_text(json.dumps({
        "schema": "d5-multiblob/1",
        "seed": seed,
        "decoys_per_camera": MB_DECOYS_PER_CAMERA,
        "frames": MB_FRAMES,
        "entry_frame": MB_ENTRY_FRAME,
    }, indent=2, sort_keys=True) + "\n")


def _detector_config() -> DetectorConfig:
    return DetectorConfig(
        backend=Backend.MOG2,
        proc_width=1536,
        proc_height=864,
        warmup_frames=90,
        persistence_frames=2,
        calibration_rev=CAL_REV,
        detector_rev=DET_REV,
    )


def _fmt(x, digits: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        if not np.isfinite(x):
            return "—"
        return f"{x:.{digits}f}"
    return str(x)


def _acceptance_rows(gate: SceneEval, acceptance: dict) -> list[tuple[str, str, str, bool]]:
    rows = []
    rows.append((
        "Detection recall (carried from D4, mog2 @1536x864)",
        f">= {acceptance['detection_recall_min']}",
        "0.996 (Measured, D4 report)",
        0.996 >= float(acceptance["detection_recall_min"]),
    ))
    rows.append((
        "Range p95 (m)", f"<= {acceptance['range_p95_max_m']}",
        _fmt(gate.range_p95_m),
        bool(gate.range_p95_m <= float(acceptance["range_p95_max_m"])),
    ))
    rows.append((
        "Cross-range p95, random channel (m)",
        f"<= {acceptance['cross_range_p95_max_m']}",
        f"{_fmt(gate.cross_p95_random_m)} (total {_fmt(gate.cross_p95_m)})",
        bool(gate.cross_p95_random_m <= float(acceptance["cross_range_p95_max_m"])),
    ))
    rows.append((
        "Target-reference bias, estimated (m)",
        f"<= {acceptance['systematic_bound_target_reference_max_m']}",
        f"{_fmt(gate.bias_mean_m)} (max {_fmt(gate.bias_max_m)})",
        bool(
            gate.bias_mean_m
            <= float(acceptance["systematic_bound_target_reference_max_m"])
        ),
    ))
    rows.append((
        f"Velocity RMSE, random channel, settled "
        f"(after {gate.velocity_convergence_window_batches} batches) (m/s)",
        f"<= {acceptance['velocity_rmse_max_mps']}",
        f"{_fmt(gate.velocity_rmse_settled_mps)} "
        f"(transient {_fmt(gate.velocity_rmse_transient_mps)}, "
        f"whole-run {_fmt(gate.velocity_rmse_random_mps)}, "
        f"total {_fmt(gate.velocity_rmse_mps)})",
        bool(
            gate.velocity_rmse_settled_mps
            <= float(acceptance["velocity_rmse_max_mps"])
        ),
    ))
    settling = gate.velocity_settling_s
    rows.append((
        "Velocity settling time from confirmation (s)",
        f"<= {acceptance['velocity_settling_max_s']}",
        _fmt(settling) if np.isfinite(settling) else "never settled",
        bool(np.isfinite(settling)
             and settling <= float(acceptance["velocity_settling_max_s"])),
    ))
    rows.append((
        "Acquisition (s from shared-FOV entry)", f"<= {acceptance['acquisition_max_s']}",
        _fmt(gate.acquisition_s),
        bool(gate.acquisition_s <= float(acceptance["acquisition_max_s"])),
    ))
    rows.append((
        "Duplicate confirmed tracks", f"<= {acceptance['duplicate_confirmed_tracks_max']}",
        str(gate.duplicate_confirmed),
        gate.duplicate_confirmed <= int(acceptance["duplicate_confirmed_tracks_max"]),
    ))
    return rows


def generate(
    seed: int,
    gate_clips: str,
    gate_render: str,
    multiblob: str,
    out_path: str,
    evidence_path: str | None,
    manifest_path: str = "configs/exp001_scene.yaml",
) -> None:
    manifest = yaml.safe_load(open(manifest_path, encoding="utf-8"))
    acceptance = manifest["acceptance"]
    detector_config = _detector_config()

    gate_cams = gate_cameras(gate_render)
    gate_truth = load_render_truth(gate_render)
    gate_obs = observation_stream(gate_clips, detector_config)

    mb_cams = multiblob_cameras()
    mb_truth: Truth = load_jsonl_truth(Path(multiblob) / "truth.jsonl")
    mb_obs = observation_stream(multiblob, detector_config)

    # Gate parameters come from the MANIFEST, never from code defaults.
    convergence_window = int(acceptance["velocity_convergence_window_batches"])
    velocity_gate = float(acceptance["velocity_rmse_max_mps"])

    evals: dict[tuple[str, bool], SceneEval] = {}
    for scene, obs, cams, truth in (
        ("gate", gate_obs, gate_cams, gate_truth),
        ("multiblob", mb_obs, mb_cams, mb_truth),
    ):
        for voxel_on in (False, True):
            config = FusionConfig()
            config.voxel.enabled = voxel_on
            evals[(scene, voxel_on)] = evaluate_scene(
                scene, obs, cams, truth, config,
                velocity_convergence_window_batches=convergence_window,
                velocity_gate_mps=velocity_gate,
            )

    gate_eval = evals[("gate", False)]  # acceptance scored on the OFF path
    rows = _acceptance_rows(gate_eval, acceptance)

    lines: list[str] = []
    a = lines.append
    a("# D5 tracking report")
    a("")
    a("**Labels.** Gate-clip lines are Measured on the L4 golden clips (the")
    a("noise inside them remains Modeled mid-bracket until the conversion-gain")
    a("bench lands). Multi-blob lines are Modeled (seeded sensor-model")
    a("fixture). Runtimes are zeroed in this deterministic body and written")
    a("live to the evidence JSON (D4 precedent).")
    a("")
    a(f"Seed {seed}; detector {DET_REV} (mog2 @1536x864); fusion config frozen")
    a("defaults; voxel default evaluated below. Anti-tuning: thresholds were")
    a("tuned on seed-variant fixtures only; this is the gate clip's single")
    a("scored run for this frozen config.")
    a("")
    a("## Gate-clip acceptance table (manifest `exp001_scene.yaml`)")
    a("")
    a("| Line | Gate | Value | Verdict |")
    a("| --- | --- | --- | --- |")
    for name, gate, value, ok in rows:
        a(f"| {name} | {gate} | {value} | {'PASS' if ok else 'FAIL'} |")
    a("")
    a(f"- Published samples: {gate_eval.published_samples} track states near truth")
    a(f"  over {gate_eval.batches} batches.")
    a("")
    a("### Two-channel scoring (D5 amendment, per D0's two-error-channel rule)")
    a("")
    a("- Estimator: the SYSTEMATIC channel is a per-axis centered windowed")
    a(f"  mean of the published-track position error over ±{gate_eval.bias_window_batches // 2}")
    a("  event-time batch indices (~1 s at 30 fps) — slow enough to track the")
    a("  shading-driven target-reference drift across the crossing, wide")
    a("  enough to average the frame-to-frame noise. The RANDOM channel is")
    a("  the residual about that estimate; the same split applies to velocity.")
    a(f"- Estimated bias: mean magnitude {_fmt(gate_eval.bias_mean_m)} m, max "
      f"{_fmt(gate_eval.bias_max_m)} m; run-mean per axis (x, y, z): "
      f"({_fmt(gate_eval.bias_axes_m[0])}, {_fmt(gate_eval.bias_axes_m[1])}, "
      f"{_fmt(gate_eval.bias_axes_m[2])}) m.")
    a("- Nothing is hidden: total-error values sit beside every random-channel")
    a("  value in the table above.")
    a("")
    a("### Velocity settled-window scoring (D5.2 amendment)")
    a("")
    a("- Velocity is physically unobservable at single-batch birth (route A")
    a("  confirms on one 3-camera batch), so the manifest declares a")
    a(f"  {gate_eval.velocity_convergence_window_batches}-batch convergence window")
    a(f"  (~{gate_eval.velocity_convergence_window_batches / 30.0:.2f} s) excluded from the")
    a("  gated RMSE; a separate gate bounds how long settling may take.")
    a(f"- Transient RMSE (first {gate_eval.velocity_convergence_window_batches} batches): "
      f"{_fmt(gate_eval.velocity_rmse_transient_mps)} m/s — reported, never hidden.")
    a(f"- Settled RMSE over {gate_eval.velocity_settled_samples} samples: "
      f"{_fmt(gate_eval.velocity_rmse_settled_mps)} m/s.")
    a("- Settling time = batches from confirmation until the velocity error")
    a("  stays under the gate for the REST of the run (a momentary dip does")
    a(f"  not count): {_fmt(gate_eval.velocity_settling_s)} s.")
    a("")
    a("## Track-filter covariance coverage")
    a("")
    cov = gate_eval.coverage
    a(f"- Gate clip (voxel off): {_fmt(cov[0], 2)} / {_fmt(cov[1], 2)} / "
      f"{_fmt(cov[2], 2)} at 1/2/3 sigma.")
    a("- Caveat (D1 finding): with 3 cameras the measurement covariance scale")
    a("  is estimated from 3 degrees of freedom, so the statistic is")
    a("  F-distributed, not chi-square — nominal masses are ~0.55/0.78/0.89,")
    a("  and filter smoothing shifts them further. Report, don't re-tune.")
    a("")
    a("## Voxel on/off comparison (F10)")
    a("")
    a("| Scene | Voxel | Candidate recall | False cand./batch | Voxel groups | "
      "Runtime (s) |")
    a("| --- | --- | --- | --- | --- | --- |")
    for scene in ("gate", "multiblob"):
        for voxel_on in (False, True):
            ev = evals[(scene, voxel_on)]
            a(
                f"| {scene} | {'on' if voxel_on else 'off'} | "
                f"{_fmt(ev.candidate_recall)} | {_fmt(ev.false_candidates_per_batch)} | "
                f"{ev.voxel_groups_total} | 0.000 |"
            )
    a("")
    off_recall = evals[("multiblob", False)].candidate_recall
    on_recall = evals[("multiblob", True)].candidate_recall
    gain = (on_recall - off_recall) if np.isfinite(on_recall) else 0.0
    off_false = evals[("multiblob", False)].false_candidates_per_batch
    on_false = evals[("multiblob", True)].false_candidates_per_batch
    recommend_on = bool(gain > 0.02 and (on_false - off_false) < 0.5)
    a("### Recommendation (rule: enable only if multi-blob candidate recall")
    a("gains > 0.02 without > 0.5 extra false candidates/batch)")
    a("")
    a(
        f"- Multi-blob recall off/on: {_fmt(off_recall)} / {_fmt(on_recall)}; "
        f"false cand./batch off/on: {_fmt(off_false)} / {_fmt(on_false)}."
    )
    a(f"- **Recommended default: voxel {'ON' if recommend_on else 'OFF'}.** To be")
    a("  recorded in D0 §10 with these numbers.")
    a("")
    a("## Lifecycle and rejection statistics")
    a("")
    for scene in ("gate", "multiblob"):
        ev = evals[(scene, False)]
        a(f"### {scene} (voxel off)")
        a("")
        a(f"- Final track statuses: {json.dumps(ev.final_statuses, sort_keys=True)}")
        a(f"- Engine rejections: {json.dumps(ev.rejections, sort_keys=True)}; "
          f"NIS-gated updates: {ev.nis_rejects}; aligner exclusions: "
          f"{json.dumps(ev.excluded, sort_keys=True)}")
        a("- Audit samples (obs_id traceability):")
        for sample in ev.audit_samples[:3]:
            a(f"  - `{sample}`")
        a("")

    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {out_path}")

    if evidence_path:
        evidence = {
            f"{scene}/{'on' if voxel_on else 'off'}": {
                "fusion_runtime_s": ev.fusion_runtime_s,
                "candidate_recall": ev.candidate_recall,
                "false_candidates_per_batch": ev.false_candidates_per_batch,
                "voxel_groups_total": ev.voxel_groups_total,
                "batches": ev.batches,
            }
            for (scene, voxel_on), ev in sorted(evals.items())
        }
        with open(evidence_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(evidence, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"wrote {evidence_path} (live runtimes)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="D5 tracking report tooling")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build-multiblob")
    build.add_argument("--seed", type=int, required=True)
    build.add_argument("--out", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--seed", type=int, required=True)
    gen.add_argument("--gate-clips", required=True)
    gen.add_argument("--gate-render", required=True)
    gen.add_argument("--multiblob", required=True)
    gen.add_argument("--out", required=True)
    gen.add_argument("--evidence", default=None)
    gen.add_argument("--manifest", default="configs/exp001_scene.yaml")
    args = parser.parse_args(argv)
    if args.cmd == "build-multiblob":
        build_multiblob(args.seed, args.out)
    else:
        generate(args.seed, args.gate_clips, args.gate_render, args.multiblob,
                 args.out, args.evidence, args.manifest)


if __name__ == "__main__":
    main()
