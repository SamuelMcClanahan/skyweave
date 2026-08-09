"""Single-axis and held-out campaign runners.

Order is the brief's: Tier II (detection), Tier IV (time/stream), Tier III
(model), Tier I (image), then the combined hold-out exactly once.

Every magnitude is read from the frozen manifest. Each cell produces:
  - the layer table (FA/FR + terminating layer per injected item)
  - the honesty verdict (NaN, labeled exits, overconfidence events)
  - accuracy against truth (cross-range p95, published count)
  - a decisions fingerprint for the determinism gate

Tier I is handled separately (image.py) because it regenerates faulted U8
clips from the kept radiance rather than perturbing an observation stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from skyweave2.contracts import CameraModel
from skyweave2.faults import injectors as inj
from skyweave2.faults.bookkeeping import LayerTable, build_layer_table
from skyweave2.faults.config import FaultManifest, flag_name
from skyweave2.faults.honesty import HonestyVerdict, decisions_fingerprint, evaluate_honesty
from skyweave2.fusion.aligner import obs_key
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.engine import run_stream

FPS = 30.0
SLOW_BIAS_PERIOD_S = 5.0  # manifest comment: "sinusoid, period 5 s"


@dataclass
class CellResult:
    tier: str
    axis: str
    magnitude: str
    layer_table: LayerTable
    honesty: HonestyVerdict
    published: int = 0
    cross_p95_m: float = float("nan")
    range_p95_m: float = float("nan")
    mean_sigma_m: float = float("nan")
    fingerprint: str = ""
    notes: dict = field(default_factory=dict)


def _score(run, truth_at_batch) -> tuple[int, float, float]:
    cross, rng_err = [], []
    for batch_index, track in run.published:
        truth = truth_at_batch(batch_index)
        if truth is None:
            continue
        err = np.asarray(track.state[0:3]) - np.asarray(truth)
        if float(np.linalg.norm(err)) > 50.0:
            continue
        cross.append(float(np.hypot(err[0], err[2])))
        rng_err.append(abs(float(err[1])))
    if not cross:
        return len(run.published), float("nan"), float("nan")
    return (len(run.published), float(np.percentile(cross, 95)),
            float(np.percentile(rng_err, 95)))


def run_cell(
    tier: str,
    axis: str,
    magnitude,
    faulted_stream,
    ledger,
    cameras: dict[int, CameraModel],
    truth_at_batch,
    manifest: FaultManifest,
    config: FusionConfig | None = None,
    notes: dict | None = None,
) -> CellResult:
    """Run one campaign cell end to end and score every gate."""
    config = config or FusionConfig()
    run = run_stream(faulted_stream, cameras, config,
                     session_uuid=f"d6-{tier}-{axis}-{magnitude}")
    keys = {obs_key(o) for o in faulted_stream}
    table = build_layer_table(axis, str(magnitude), ledger, run, keys)
    verdict = evaluate_honesty(run, truth_at_batch,
                               sigma_threshold=manifest.overconfidence_sigma)
    published, cross_p95, range_p95 = _score(run, truth_at_batch)
    return CellResult(
        tier=tier, axis=axis, magnitude=str(magnitude),
        layer_table=table, honesty=verdict,
        published=published, cross_p95_m=cross_p95, range_p95_m=range_p95,
        mean_sigma_m=verdict.mean_position_sigma_m,
        fingerprint=decisions_fingerprint(run),
        notes=notes or {},
    )


def run_tier2(base_stream, cameras, truth_at_batch, manifest) -> list[CellResult]:
    """Tier II: detection-stream faults."""
    seed = manifest.base_seed
    out: list[CellResult] = []
    for value in manifest.axis("tier2_detection", "false_blobs_per_camera_frame"):
        stream, ledger = inj.inject_false_blobs(base_stream, float(value), seed, cameras)
        out.append(run_cell("II", "false_blobs_per_camera_frame", value, stream,
                            ledger, cameras, truth_at_batch, manifest))
    for value in manifest.axis("tier2_detection", "deleted_detections_pct"):
        stream, ledger = inj.inject_deleted_detections(base_stream, float(value), seed)
        out.append(run_cell("II", "deleted_detections_pct", value, stream,
                            ledger, cameras, truth_at_batch, manifest))
    for value in manifest.axis("tier2_detection", "split_blobs_pct"):
        stream, ledger = inj.inject_split_blobs(base_stream, float(value), seed)
        out.append(run_cell("II", "split_blobs_pct", value, stream,
                            ledger, cameras, truth_at_batch, manifest))
    for value in manifest.axis("tier2_detection", "merged_blobs_pct"):
        stream, ledger = inj.inject_merged_blobs(base_stream, float(value), seed)
        out.append(run_cell("II", "merged_blobs_pct", value, stream,
                            ledger, cameras, truth_at_batch, manifest))
    for value in manifest.nested_axis("tier2_detection", "centroid_bias_px", "constant"):
        stream, ledger = inj.inject_centroid_bias(base_stream, float(value), 0.0,
                                                  FPS, SLOW_BIAS_PERIOD_S)
        out.append(run_cell("II", "centroid_bias_constant_px", value, stream,
                            ledger, cameras, truth_at_batch, manifest))
    for value in manifest.nested_axis("tier2_detection", "centroid_bias_px",
                                      "slow_varying"):
        stream, ledger = inj.inject_centroid_bias(base_stream, 0.0, float(value),
                                                  FPS, SLOW_BIAS_PERIOD_S)
        out.append(run_cell("II", "centroid_bias_slow_px", value, stream,
                            ledger, cameras, truth_at_batch, manifest))
    return out


def run_tier4(base_stream, cameras, truth_at_batch, manifest) -> list[CellResult]:
    """Tier IV: time/stream faults, each clock axis run honest AND dishonest."""
    seed = manifest.base_seed
    honesty_modes = [flag_name(m) for m in
                     manifest.axis("tier4_time_stream", "time_sync_honesty")]
    dishonest_claim = 0.5  # the manifest's own comment: "dishonest claims 0.5 ms"
    out: list[CellResult] = []

    for axis, kwargs in (
        ("clock_offset_ms", lambda v: dict(offset_ms=float(v), drift_ppm=0.0,
                                           jitter_ms_sigma=0.0)),
        ("drift_ppm", lambda v: dict(offset_ms=0.0, drift_ppm=float(v),
                                     jitter_ms_sigma=0.0)),
        ("jitter_ms_sigma", lambda v: dict(offset_ms=0.0, drift_ppm=0.0,
                                           jitter_ms_sigma=float(v))),
    ):
        for value in manifest.axis("tier4_time_stream", axis):
            for mode in honesty_modes:
                stream, ledger = inj.inject_clock_error(
                    base_stream, seed=seed, honest=(mode == "honest"),
                    dishonest_claim_ms=dishonest_claim, camera_ids=(0,),
                    **kwargs(value),
                )
                out.append(run_cell("IV", f"{axis}[{mode}]", value, stream, ledger,
                                    cameras, truth_at_batch, manifest,
                                    notes={"honesty": mode}))

    for mode in [flag_name(m) for m in manifest.axis("tier4_time_stream", "loss_mode")]:
        for value in manifest.axis("tier4_time_stream", "observation_loss_pct"):
            stream, ledger = inj.inject_observation_loss(base_stream, float(value),
                                                         str(mode), seed)
            out.append(run_cell("IV", f"observation_loss_pct[{mode}]", value, stream,
                                ledger, cameras, truth_at_batch, manifest))

    for flag in manifest.axis("tier4_time_stream", "reorder"):
        name = flag_name(flag)
        if name == "off":
            stream, ledger = base_stream, inj.PlantLedger()
        else:
            stream, ledger = inj.inject_reorder(base_stream, seed)
        out.append(run_cell("IV", "reorder", name, stream, ledger, cameras,
                            truth_at_batch, manifest))
    for flag in manifest.axis("tier4_time_stream", "duplication"):
        name = flag_name(flag)
        if name == "off":
            stream, ledger = base_stream, inj.PlantLedger()
        else:
            stream, ledger = inj.inject_duplication(base_stream, seed)
        out.append(run_cell("IV", "duplication", name, stream, ledger, cameras,
                            truth_at_batch, manifest))
    for flag in manifest.axis("tier4_time_stream", "lateness"):
        name = flag_name(flag)
        if name == "off":
            stream, ledger = base_stream, inj.PlantLedger()
        else:
            stream, ledger = inj.inject_lateness(base_stream, seed)
        out.append(run_cell("IV", "lateness", name, stream, ledger, cameras,
                            truth_at_batch, manifest))

    restart_at = float(manifest.scalar("tier4_time_stream", "node_restart_at_s"))
    stream, ledger = inj.inject_node_restart(base_stream, restart_at, FPS, camera_id=0)
    out.append(run_cell("IV", "node_restart_at_s", restart_at, stream, ledger,
                        cameras, truth_at_batch, manifest))

    for value in [flag_name(v) for v in
                  manifest.axis("tier4_time_stream", "camera_dropout")]:
        if value == "none":
            stream, ledger = base_stream, inj.PlantLedger()
        else:
            camera_id = int(str(value).replace("cam", ""))
            # Second half of the clip, per the manifest's axis comment.
            frames = [o.envelope.frame_seq for o in base_stream]
            half_s = (max(frames) / FPS) / 2.0
            stream, ledger = inj.inject_camera_dropout(base_stream, camera_id,
                                                       half_s, FPS)
        out.append(run_cell("IV", "camera_dropout", value, stream, ledger, cameras,
                            truth_at_batch, manifest))
    return out


DECLARATION_MODES = ("honest", "dishonest")
DISHONEST_DECLARED = 1e-6  # "declares near-zero while injected is large"


def _declared_config(mode: str, rotation_deg: float, position_m: float,
                     target_width_m: float, speed_mps: float) -> FusionConfig:
    """Config whose DECLARED uncertainty is honest (>= injected) or a lie."""
    config = FusionConfig()
    if mode == "honest":
        declared_rot, declared_pos = rotation_deg, position_m
    else:
        declared_rot = declared_pos = DISHONEST_DECLARED
    return config.model_copy(update={"systematic": config.systematic.model_copy(
        update={
            "calibration_rotation_sigma_deg": declared_rot,
            "calibration_position_sigma_m": declared_pos,
            "target_width_m": target_width_m,
            "estimated_speed_mps": speed_mps,
        })})


def run_tier3(base_stream, cameras, truth_at_batch, manifest,
              target_width_m: float = 0.75, speed_mps: float = 30.0
              ) -> list[CellResult]:
    """Tier III: estimator-calibration faults; truth untouched.

    Each calibration axis runs under BOTH declaration modes (D6.1): honest
    (declared sigma >= injected error) must show zero overconfidence;
    dishonest (declares near-zero) is a labeled known-lie case.
    """
    seed = manifest.base_seed
    out: list[CellResult] = []
    simple = (
        ("rotation_err_deg", "rotation_err_deg"),
        ("position_err_m", "position_err_m"),
        ("focal_err_pct", "focal_err_pct"),
        ("principal_point_err_px", "principal_point_err_px"),
    )
    for axis, kw in simple:
        for value in manifest.axis("tier3_model", axis):
            perturbed, ledger = inj.perturb_cameras(cameras, seed, **{kw: float(value)})
            for mode in DECLARATION_MODES:
                # Focal/principal-point errors are declared through the
                # rotation channel: both act as an angular pointing error
                # at the target, which is what the bound propagates.
                rot = float(value) if kw in ("rotation_err_deg",) else 0.0
                pos = float(value) if kw == "position_err_m" else 0.0
                if kw == "focal_err_pct":
                    rot = float(value) / 100.0 * 30.0  # pct of a 60 deg FOV half-angle
                if kw == "principal_point_err_px":
                    rot = np.degrees(float(value) / 1995.3)  # px / focal length
                config = _declared_config(mode, rot, pos, target_width_m, speed_mps)
                out.append(run_cell("III", f"{axis}[{mode}]", value, base_stream,
                                    ledger, perturbed, truth_at_batch, manifest,
                                    config=config, notes={"declaration": mode}))
    for value in manifest.axis("tier3_model", "distortion_mismatch"):
        name = flag_name(value)
        dist = {
            "off": None,
            "mild": (-0.02, 0.0, 0.0, 0.0, 0.0),
            "measured_like": (-0.09, 0.015, 0.0, 0.0, 0.0),
        }[name]
        perturbed, ledger = inj.perturb_cameras(cameras, seed, distortion=dist)
        out.append(run_cell("III", "distortion_mismatch", name, base_stream, ledger,
                            perturbed, truth_at_batch, manifest))
    for value in manifest.axis("tier3_model", "rolling_readout_ms"):
        stream, ledger = inj.inject_rolling_shutter(base_stream, float(value))
        out.append(run_cell("III", "rolling_readout_ms", value, stream, ledger,
                            cameras, truth_at_batch, manifest))
    return out


def run_combined_holdout(base_stream, cameras, truth_at_batch, manifest) -> CellResult:
    """The held-out recipe, frozen in the manifest, run EXACTLY once.

    Image-tier terms (read noise, blur) are applied by the Tier I path when
    a radiance-backed clip is used; on the observation-stream path they are
    recorded in the notes as declared-but-not-applicable so the report never
    implies a fault that was not injected.
    """
    recipe = manifest.combined_holdout
    seed = manifest.base_seed
    stream = base_stream
    ledger = inj.PlantLedger()

    s, l1 = inj.inject_false_blobs(
        stream, float(recipe["false_blobs_per_camera_frame"]), seed, cameras)
    ledger.merge(l1)
    s, l2 = inj.inject_deleted_detections(s, float(recipe["deleted_detections_pct"]), seed)
    ledger.merge(l2)
    s, l3 = inj.inject_clock_error(
        s, float(recipe["clock_offset_ms"]), 0.0, float(recipe["jitter_ms_sigma"]),
        seed=seed, honest=(recipe["time_sync_honesty"] == "honest"),
        dishonest_claim_ms=0.5, camera_ids=(0,))
    ledger.merge(l3)
    s, l4 = inj.inject_observation_loss(
        s, float(recipe["observation_loss_pct"]), "random", seed)
    ledger.merge(l4)
    perturbed, l5 = inj.perturb_cameras(
        cameras, seed, rotation_err_deg=float(recipe["rotation_err_deg"]),
        position_err_m=float(recipe["position_err_m"]))
    ledger.merge(l5)

    return run_cell("combined", "holdout", "frozen", s, ledger, perturbed,
                    truth_at_batch, manifest,
                    notes={"image_terms_declared_not_applied":
                           f"read_noise_sigma_dn={recipe['read_noise_sigma_dn']}, "
                           f"blur_px={recipe['blur_px']} (Tier I path)"})
