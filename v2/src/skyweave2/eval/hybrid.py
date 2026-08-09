"""Hybrid clips: composite a synthetic blob into real footage, emit truth.

The cheap sim2real bridge (design §7.4): real clouds, real AE transients,
real sensor noise and compression — with KNOWN target pixel positions. The
injected blob is an analytic Gaussian whose sigma combines the requested
blob size with the sensor-model PSF, so it matches the footage's blur; the
truth label IS the injected center, so label accuracy holds by
construction (test E3).

Optional matched noise: shot-like Gaussian noise proportional to the added
signal, seeded per (dataset_seed, frame). Noise is added AFTER the label is
recorded — it perturbs pixels, never truth.

Real recordings are converted into the clip layout with ``ingest_video``
(ffmpeg subprocess; not exercised by tests).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from skyweave2.eval.clips import iter_frames, read_clip_meta, write_clip
from skyweave2.eval.labels import TruthLabel, write_labels
from skyweave2.sensor_model.config import SensorModelSpec

_FWHM_TO_SIGMA = 1.0 / 2.354820045


class TrajectorySpec(BaseModel):
    """Linear constant-velocity injected target, in full-res pixel coords."""

    model_config = ConfigDict(extra="forbid")

    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)  # inclusive
    u0: float
    v0: float
    du_per_frame: float = 0.0
    dv_per_frame: float = 0.0
    fwhm_px: float = Field(default=3.0, gt=0.0)
    peak_dn: float = Field(default=60.0, gt=0.0)  # added U8 luma at blob center
    object_id: int = Field(default=0, ge=0)
    object_class: str = "target"
    camera_id: int = Field(default=0, ge=0)
    add_matched_noise: bool = False
    dataset_seed: int = 0


def position_at(spec: TrajectorySpec, frame_seq: int) -> tuple[float, float]:
    step = frame_seq - spec.start_frame
    return spec.u0 + spec.du_per_frame * step, spec.v0 + spec.dv_per_frame * step


def blob_image(
    shape: tuple[int, int],
    center_uv: tuple[float, float],
    fwhm_px: float,
    peak: float,
    psf_sigma_px: float,
) -> np.ndarray:
    """Analytic Gaussian blob at a sub-pixel center, PSF-matched.

    Gaussian blob convolved with a Gaussian PSF is another Gaussian:
    sigma_total = sqrt(sigma_blob^2 + sigma_psf^2). Evaluating the closed
    form at pixel centers keeps the intensity centroid exactly at
    ``center_uv`` — the by-construction guarantee E3 verifies.
    """
    sigma = float(np.hypot(fwhm_px * _FWHM_TO_SIGMA, psf_sigma_px))
    height, width = shape
    v, u = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    cu, cv = center_uv
    return peak * np.exp(-0.5 * (((u - cu) / sigma) ** 2 + ((v - cv) / sigma) ** 2))


def composite_clip(
    real_clip_dir: str | Path,
    out_dir: str | Path,
    trajectory: TrajectorySpec,
    sensor_spec: SensorModelSpec | None = None,
) -> list[TruthLabel]:
    """Write the composited clip and its labels.jsonl; return the labels."""
    sensor_spec = sensor_spec or SensorModelSpec()
    meta = read_clip_meta(real_clip_dir)
    psf_sigma = sensor_spec.psf_sigma_px if sensor_spec.enable_psf else 0.0

    frames: list[np.ndarray] = []
    labels: list[TruthLabel] = []
    for seq, frame in iter_frames(real_clip_dir):
        if trajectory.start_frame <= seq <= trajectory.end_frame:
            center = position_at(trajectory, seq)
            added = blob_image(
                frame.shape, center, trajectory.fwhm_px, trajectory.peak_dn, psf_sigma
            )
            if trajectory.add_matched_noise:
                rng = np.random.default_rng(
                    np.random.SeedSequence((trajectory.dataset_seed, trajectory.camera_id, seq))
                )
                # Shot-like: sigma grows with the sqrt of added electrons,
                # expressed back in DN through the conversion gain.
                dn_per_e = 1.0 / sensor_spec.conversion_gain_e_per_dn
                sigma_dn = np.sqrt(np.clip(added / dn_per_e, 0.0, None)) * dn_per_e
                added = added + rng.normal(0.0, 1.0, size=added.shape) * sigma_dn
            composited = np.clip(frame.astype(np.float64) + added, 0.0, 255.0)
            frames.append(np.rint(composited).astype(np.uint8))
            labels.append(
                TruthLabel(
                    frame_seq=seq,
                    camera_id=trajectory.camera_id,
                    object_id=trajectory.object_id,
                    object_class=trajectory.object_class,
                    u=center[0],
                    v=center[1],
                    size_px=trajectory.fwhm_px,
                )
            )
        else:
            frames.append(frame.copy())

    out_dir = Path(out_dir)
    write_clip(frames, out_dir, fps=meta.fps, source=f"hybrid({meta.source or real_clip_dir})")
    write_labels(labels, out_dir / "labels.jsonl")
    return labels


def ingest_video(
    video_path: str | Path,
    out_dir: str | Path,
    fps: float | None = None,
    max_frames: int | None = None,
) -> int:
    """Convert a real recording to the clip layout via ffmpeg (gray8 rawvideo).

    Streams the decode one frame at a time and writes each .npy immediately,
    so multi-minute sky recordings (the designed input — bench task 3) ingest
    in constant memory instead of buffering gigabytes of raw video. Bench-side
    utility; requires ffmpeg on PATH and is not exercised by the test suite.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "json", str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    if fps is None:
        num, den = stream["r_frame_rate"].split("/")
        fps = float(num) / float(den)

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    frame_bytes = width * height
    from skyweave2.eval.clips import FRAME_TEMPLATE, ClipMeta

    proc = subprocess.Popen(
        [
            "ffmpeg", "-v", "error", "-i", str(video_path),
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        stdout=subprocess.PIPE,
    )
    count = 0
    assert proc.stdout is not None
    try:
        while max_frames is None or count < max_frames:
            chunk = proc.stdout.read(frame_bytes)
            if len(chunk) < frame_bytes:
                break
            frame = np.frombuffer(chunk, dtype=np.uint8).reshape(height, width)
            np.save(out_path / FRAME_TEMPLATE.format(count), frame)
            count += 1
    finally:
        proc.stdout.close()
        proc.terminate()
        proc.wait()
    if count == 0:
        raise RuntimeError(f"no frames decoded from {video_path}")
    meta = ClipMeta(
        width=width, height=height, frame_count=count, fps=fps, source=str(video_path)
    )
    (out_path / "clip.json").write_text(
        json.dumps(meta.model_dump(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return count


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Composite a synthetic blob into a real clip")
    parser.add_argument("clip", help="input clip directory")
    parser.add_argument("trajectory", help="TrajectorySpec JSON file")
    parser.add_argument("out", help="output clip directory")
    args = parser.parse_args(argv)
    trajectory = TrajectorySpec.model_validate(json.loads(Path(args.trajectory).read_text()))
    labels = composite_clip(args.clip, args.out, trajectory)
    print(f"wrote {args.out} ({len(labels)} labeled frames)")


if __name__ == "__main__":
    main()
