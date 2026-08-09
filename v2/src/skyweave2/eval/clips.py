"""Clip storage: a directory of U8 Y frames plus a small JSON descriptor.

Format (Provisional, D3): ``clip.json`` beside ``frame_000000.npy`` ...
one uint8 (H, W) array per frame. Chosen over video files so the scorer
and hybrid compositor stay numpy-only and byte-deterministic; an ffmpeg
ingest helper in ``hybrid.py`` converts real recordings into this layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

FRAME_TEMPLATE = "frame_{:06d}.npy"


class ClipMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_name: str = "skyweave2-clip/1"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(ge=0)
    fps: float = Field(gt=0.0, default=30.0)
    source: str = ""  # free-text provenance (real capture, hybrid, synthetic)


def write_clip(
    frames: list[np.ndarray], path: str | Path, fps: float = 30.0, source: str = ""
) -> ClipMeta:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if not frames:
        raise ValueError("clip needs at least one frame")
    height, width = frames[0].shape
    for i, frame in enumerate(frames):
        if frame.shape != (height, width) or frame.dtype != np.uint8:
            raise ValueError(f"frame {i}: expected uint8 ({height}, {width}), got "
                             f"{frame.dtype} {frame.shape}")
        np.save(path / FRAME_TEMPLATE.format(i), frame)
    meta = ClipMeta(width=width, height=height, frame_count=len(frames), fps=fps, source=source)
    (path / "clip.json").write_text(
        json.dumps(meta.model_dump(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def read_clip_meta(path: str | Path) -> ClipMeta:
    return ClipMeta.model_validate(json.loads((Path(path) / "clip.json").read_text()))


def read_frame(path: str | Path, frame_seq: int) -> np.ndarray:
    frame = np.load(Path(path) / FRAME_TEMPLATE.format(frame_seq))
    if frame.dtype != np.uint8 or frame.ndim != 2:
        raise ValueError(f"frame {frame_seq}: expected 2-D uint8, got {frame.dtype} {frame.shape}")
    return frame


def iter_frames(path: str | Path):
    """Yield (frame_seq, frame) in sequence order."""
    meta = read_clip_meta(path)
    for seq in range(meta.frame_count):
        yield seq, read_frame(path, seq)
