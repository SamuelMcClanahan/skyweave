"""Truth label records for scoring: the target now, confusers later.

Labeled negatives are a first-class output of the synthetic pipeline
(design principle 4): a confuser carries a truth label exactly like the
target, distinguished only by ``object_class``. Storage is JSONL, one
record per line, append-friendly.

Positions are FULL-RESOLUTION pixel coordinates in the D0 pixel-center
convention, matching `Observation2D`.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class TruthLabel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    frame_seq: int = Field(ge=0)
    camera_id: int = Field(ge=0, default=0)
    object_id: int = Field(ge=0, default=0)
    object_class: str = "target"  # "target" or "confuser:<kind>"
    u: float
    v: float
    visible: bool = True
    size_px: float | None = None  # approximate FWHM at full resolution


def write_labels(labels: list[TruthLabel], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for label in labels:
            fh.write(json.dumps(label.model_dump(), sort_keys=True) + "\n")


def read_labels(path: str | Path) -> list[TruthLabel]:
    labels: list[TruthLabel] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                labels.append(TruthLabel.model_validate(json.loads(line)))
    return labels
