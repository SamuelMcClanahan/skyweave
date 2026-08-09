"""Stage A -> D bridge: exp001 render output -> U8 clips the scorer consumes.

This is the missing link the D4 adversarial review flagged: the Blender
generator emits per-camera radiance (EXR + optional fp16 .npy companions at
the margined render resolution) plus truth sidecars, while the detector and
report consume the D3 clip layout (clip.json + frame_*.npy uint8) with
per-camera labels. This module performs that conversion deterministically:

  radiance (margined grid) -> D3 sensor model (warp margin consumed,
  per-frame seeds from (dataset_seed, camera_id, frame_seq), AE threaded in
  sequence order) -> U8 Y clip per camera + labels.jsonl filtered to that
  camera + provenance recorded in clip.json's source field.

Radiance input: .npy companions when present; otherwise EXRs are converted
through the LOCAL pinned Blender binary in one headless batch (no new
Python dependencies; the pinned renderer is already the EXR authority).

Usage (from ``v2/``):

    uv run python -m skyweave2.render_ingest <render-dir> <out-dir> \
        [--spec sensor_spec.json] [--blender /Applications/Blender.app/...]

Determinism: identical render dir + spec + dataset seed produce
byte-identical clips (the sensor model's S2 property, threaded through).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from skyweave2.eval.clips import write_clip
from skyweave2.eval.labels import read_labels, write_labels
from skyweave2.sensor_model import (
    SENSOR_MODEL_VERSION,
    SensorModelSpec,
    TruthCameraOptics,
    render_frame,
)

_EXR_TO_NPY_SCRIPT = """
import sys
import bpy
import numpy as np

paths = sys.argv[sys.argv.index("--") + 1 :]
for exr in paths:
    image = bpy.data.images.load(exr)
    try:
        pixels = np.array(image.pixels[:], dtype=np.float32)
        w, h = image.size
        rgb = pixels.reshape(h, w, image.channels)[::-1, :, :3]
        np.save(exr[:-4] + ".npy", rgb.astype(np.float16))
    finally:
        bpy.data.images.remove(image)
print("CONVERTED", len(paths))
"""

DEFAULT_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"


def _ensure_npy(camera_dir: Path, blender_binary: str, batch: int = 200) -> list[Path]:
    """Return ordered radiance .npy paths, converting from EXR where missing."""
    exrs = sorted(camera_dir.glob("radiance_*.exr"))
    npys = {p.with_suffix(".npy") for p in camera_dir.glob("radiance_*.npy")}
    missing = [p for p in exrs if p.with_suffix(".npy") not in npys]
    if missing:
        if not shutil.which(blender_binary) and not Path(blender_binary).exists():
            raise RuntimeError(
                f"{len(missing)} EXRs need conversion but Blender binary "
                f"{blender_binary!r} was not found; pass --blender"
            )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(_EXR_TO_NPY_SCRIPT)
            script = fh.name
        for start in range(0, len(missing), batch):
            chunk = [str(p) for p in missing[start : start + batch]]
            result = subprocess.run(
                [blender_binary, "-b", "-P", script, "--", *chunk],
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if f"CONVERTED {len(chunk)}" not in result.stdout:
                raise RuntimeError(
                    f"EXR conversion failed near {chunk[0]}: {result.stdout[-500:]}"
                )
    out = sorted(camera_dir.glob("radiance_*.npy"))
    if len(out) != len(exrs) and exrs:
        raise RuntimeError(f"{camera_dir}: {len(exrs)} EXRs but {len(out)} npy files")
    return out


def ingest_render(
    render_dir: str | Path,
    out_dir: str | Path,
    spec: SensorModelSpec | None = None,
    blender_binary: str = DEFAULT_BLENDER,
    only_camera: int | None = None,
) -> dict:
    """Convert one rendered dataset into per-camera U8 clips + labels.

    Returns a manifest of what was produced. The sensor spec's warp margin
    is forced to the render's recorded margin — the whole point of the
    margined render is that the warp consumes it here.
    """
    render_dir = Path(render_dir)
    out_dir = Path(out_dir)
    dataset = json.loads((render_dir / "dataset.json").read_text())
    cameras = json.loads((render_dir / "truth" / "cameras.json").read_text())
    labels_path = render_dir / "truth" / "labels.jsonl"
    all_labels = read_labels(labels_path) if labels_path.exists() else []

    spec = spec or SensorModelSpec()
    # Camera-filtered runs (resumable chunked ingest) merge into any
    # existing manifest instead of clobbering the other cameras' entries.
    manifest_path = Path(out_dir) / "ingest.json"
    if only_camera is not None and manifest_path.exists():
        produced = json.loads(manifest_path.read_text())
    else:
        produced = {
            "schema": "skyweave2-ingest/1",
            "dataset_id": dataset["dataset_id"],
            "sensor_model_version": SENSOR_MODEL_VERSION,
            "negative": dataset.get("negative", False),
            "clips": {},
        }
    for record in cameras:
        camera_id = record["camera_id"]
        if only_camera is not None and camera_id != only_camera:
            continue
        camera_dir = render_dir / f"cam{camera_id}"
        if not camera_dir.exists():
            continue
        margin = int(record["margin_px"])
        cam_spec = spec.model_copy(update={"warp_margin_px": margin})
        optics = TruthCameraOptics(
            width=record["nominal_width"],
            height=record["nominal_height"],
            fx=record["fx"],
            fy=record["fy"],
            cx=record["cx"],
            cy=record["cy"],
        )
        frames: list[np.ndarray] = []
        ae_state = None
        npys = _ensure_npy(camera_dir, blender_binary)
        for path in npys:
            frame_seq = int(path.stem.split("_")[1])
            radiance = np.load(path).astype(np.float64)
            y_u8, ae_state = render_frame(
                radiance,
                optics,
                cam_spec,
                dataset_seed=int(dataset["seed"]),
                camera_id=camera_id,
                frame_seq=frame_seq,
                ae_state=ae_state,
            )
            frames.append(y_u8)
        if not frames:
            continue
        clip_dir = out_dir / f"cam{camera_id}"
        write_clip(
            frames,
            clip_dir,
            fps=30.0,
            source=(
                f"exp001 dataset {dataset['dataset_id']} cam{camera_id} "
                f"({SENSOR_MODEL_VERSION})"
            ),
        )
        cam_labels = [
            label for label in all_labels if label.camera_id == camera_id
        ]
        write_labels(cam_labels, clip_dir / "labels.jsonl")
        produced["clips"][str(camera_id)] = {
            "path": str(clip_dir),
            "frames": len(frames),
            "labels": len(cam_labels),
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ingest.json").write_text(
        json.dumps(produced, indent=2, sort_keys=True) + "\n"
    )
    return produced


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render output -> U8 clips")
    parser.add_argument("render_dir")
    parser.add_argument("out_dir")
    parser.add_argument("--spec", help="SensorModelSpec JSON (optional)")
    parser.add_argument("--blender", default=DEFAULT_BLENDER)
    parser.add_argument("--camera", type=int, default=None,
                        help="ingest only this camera (chunked/resumable runs)")
    args = parser.parse_args(argv)
    spec = (
        SensorModelSpec.model_validate(json.loads(Path(args.spec).read_text()))
        if args.spec
        else None
    )
    produced = ingest_render(args.render_dir, args.out_dir, spec, args.blender,
                             only_camera=args.camera)
    for cam, info in sorted(produced["clips"].items()):
        print(f"cam{cam}: {info['frames']} frames, {info['labels']} labels -> {info['path']}")


if __name__ == "__main__":
    main()
