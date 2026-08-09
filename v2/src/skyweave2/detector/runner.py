"""Detector runner: clip -> per-frame detections -> Observation2D stream.

The pipeline per the D4 brief: luma frame -> background model update
(warm-up schedule) -> foreground mask -> threshold (backend-internal) ->
small morphology -> connected components -> component measurements ->
short persistence -> Observation2D.

Frozen-contract rules honored here:
- centroids reported in FULL-resolution coordinates via the D0 scaling law
  (``proc_to_full``); the envelope declares the proc resolution;
- centroid covariance uses the configured floor (Measured by this phase's
  repeatability harness; Provisional until that number is recorded);
- ``evidence_ref`` stays null — the host path has no patches.

Determinism (test K2): identical clip + config produce identical
observation streams. The session_uuid is derived from (clip meta, config,
camera id), timestamps from frame_seq/fps in the SYNTHETIC clock domain.

CLI:

    uv run skyweave2-detect <clip-dir> [config.json] --out observations.jsonl
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from skyweave2.contracts import (
    ClockDomain,
    FrameEnvelope,
    Observation2D,
    proc_to_full,
)
from skyweave2.detector.backends import make_backend
from skyweave2.detector.components import (
    MaskComponent,
    _require_cv2,
    find_components,
    open_mask,
)
from skyweave2.detector.config import DetectorConfig
from skyweave2.detector.persistence import PersistenceFilter
from skyweave2.eval.clips import iter_frames, read_clip_meta
from skyweave2.eval.labels import TruthLabel
from skyweave2.eval.scorecard import (
    Component,
    FrameResult,
    ScorecardConfig,
    assemble_scorecard,
)


@dataclass
class FrameDetections:
    frame_seq: int
    components: list[MaskComponent]  # proc resolution, post-morphology/area gate
    emitted: list[tuple[MaskComponent, int, int]]  # (component, persistence, blob_id)
    occupancy: float  # foreground fraction at proc resolution
    elapsed_s: float


def run_detector(clip_dir: str | Path, config: DetectorConfig) -> list[FrameDetections]:
    """Run the configured backend over a clip; components at proc resolution."""
    cv2 = _require_cv2()
    meta = read_clip_meta(clip_dir)
    backend = make_backend(config)
    persistence = PersistenceFilter(config)
    proc_shape = (config.proc_width, config.proc_height)
    results: list[FrameDetections] = []
    for seq, frame in iter_frames(clip_dir):
        start = time.perf_counter()
        if (meta.width, meta.height) != proc_shape:
            proc = cv2.resize(frame, proc_shape, interpolation=cv2.INTER_AREA)
        else:
            proc = frame
        warming = seq < config.warmup_frames
        mask = backend.apply(proc, warming_up=warming)
        if warming:
            results.append(FrameDetections(seq, [], [], 0.0, time.perf_counter() - start))
            continue
        mask = open_mask(mask, config.open_radius_px)
        components = find_components(mask, config.min_area_px, config.max_area_px)
        emitted = persistence.update(components)
        results.append(
            FrameDetections(
                seq, components, emitted, float(mask.mean()), time.perf_counter() - start
            )
        )
    return results


def _session_uuid(meta_json: str, config: DetectorConfig, camera_id: int) -> str:
    key = f"skyweave2-detect:{meta_json}:{config.model_dump_json()}:{camera_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def detect_clip(
    clip_dir: str | Path,
    config: DetectorConfig,
    camera_id: int = 0,
) -> tuple[list[Observation2D], list[FrameDetections]]:
    """Full pipeline: run the backend and emit frozen-contract observations."""
    meta = read_clip_meta(clip_dir)
    results = run_detector(clip_dir, config)
    scale_x = meta.width / config.proc_width
    scale_y = meta.height / config.proc_height
    session = _session_uuid(json.dumps(meta.model_dump(), sort_keys=True), config, camera_id)

    observations: list[Observation2D] = []
    for frame in results:
        if not frame.emitted:
            continue
        envelope = FrameEnvelope(
            camera_id=camera_id,
            session_uuid=session,
            frame_seq=frame.frame_seq,
            # The clip's declared fps is authoritative; config.fps is only
            # the fallback for clips predating the fps field.
            capture_ts_ns=int(round(frame.frame_seq / (meta.fps or config.fps) * 1e9)),
            clock_domain=ClockDomain.SYNTHETIC,
            time_sync_error_ms=config.time_sync_error_ms,
            exposure_us=config.exposure_us,
            full_width=meta.width,
            full_height=meta.height,
            proc_width=config.proc_width,
            proc_height=config.proc_height,
            calibration_rev=config.calibration_rev,
            detector_rev=config.detector_rev,
        )
        for obs_id, (comp, count, blob_id) in enumerate(frame.emitted):
            observations.append(
                Observation2D(
                    envelope=envelope,
                    obs_id=obs_id,
                    u=proc_to_full(comp.centroid_u, scale_x),
                    v=proc_to_full(comp.centroid_v, scale_y),
                    cov_uu=config.centroid_cov_floor_px2,
                    cov_uv=0.0,
                    cov_vv=config.centroid_cov_floor_px2,
                    bbox_x=int(np.floor(comp.bbox_x * scale_x)),
                    bbox_y=int(np.floor(comp.bbox_y * scale_y)),
                    bbox_w=max(1, int(np.ceil(comp.bbox_w * scale_x))),
                    bbox_h=max(1, int(np.ceil(comp.bbox_h * scale_y))),
                    area_px=comp.area_px,
                    persistence_count=count,
                    confidence=1.0,
                    local_blob_id=blob_id,
                    evidence_ref=None,
                )
            )
    return observations, results


def scorecard_for_detections(
    clip_dir: str | Path,
    detector_config: DetectorConfig,
    frame_results: list[FrameDetections],
    scorecard_config: ScorecardConfig | None = None,
    labels: list[TruthLabel] | None = None,
    deterministic_perf: bool = False,
    camera_id: int | None = None,
) -> dict:
    """Score a detector run with the D3 scorecard machinery.

    Emitted (persistence-passing) components are mapped to full resolution so
    quality metrics match truth labels in full-res coordinates. The
    scorecard's warm-up horizon is the detector's warm-up schedule.
    """
    meta = read_clip_meta(clip_dir)
    scale_x = meta.width / detector_config.proc_width
    scale_y = meta.height / detector_config.proc_height
    config = (scorecard_config or ScorecardConfig()).model_copy(
        update={"background_frames": detector_config.warmup_frames}
    )
    rows = [
        FrameResult(
            frame_seq=r.frame_seq,
            components=[
                Component(
                    centroid_u=proc_to_full(comp.centroid_u, scale_x),
                    centroid_v=proc_to_full(comp.centroid_v, scale_y),
                    area_px=comp.area_px,
                )
                for comp, _, _ in r.emitted
            ],
            occupancy=r.occupancy,
            elapsed_s=r.elapsed_s,
        )
        for r in frame_results
    ]
    return assemble_scorecard(
        rows,
        config,
        labels,
        clip_name=str(clip_dir),
        detector_name=(
            f"{detector_config.backend.value} @ "
            f"{detector_config.proc_width}x{detector_config.proc_height}"
        ),
        deterministic_perf=deterministic_perf,
        camera_id=camera_id,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a detector backend over a clip")
    parser.add_argument("clip")
    parser.add_argument("config", nargs="?", help="DetectorConfig JSON (optional)")
    parser.add_argument("--out", default="observations.jsonl")
    parser.add_argument("--camera-id", type=int, default=0)
    args = parser.parse_args(argv)
    config = (
        DetectorConfig.model_validate(json.loads(Path(args.config).read_text()))
        if args.config
        else DetectorConfig()
    )
    observations, results = detect_clip(args.clip, config, camera_id=args.camera_id)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for obs in observations:
            fh.write(json.dumps(obs.model_dump(mode="json"), sort_keys=True) + "\n")
    emitted_frames = sum(1 for r in results if r.emitted)
    print(
        f"wrote {args.out}: {len(observations)} observations over "
        f"{emitted_frames}/{len(results)} frames ({config.backend.value})"
    )


if __name__ == "__main__":
    main()
