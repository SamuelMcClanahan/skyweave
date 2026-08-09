"""Loader for the frozen D6 fault manifest.

Rule (brief): code READS ``v2/configs/d6_faults.yaml`` and never restates
its values. This module therefore contains no magnitudes — only accessors
that raise if a requested axis is absent from the manifest, so a typo can
never silently become a default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MANIFEST = Path(__file__).resolve().parents[3] / "configs" / "d6_faults.yaml"


@dataclass(frozen=True)
class FaultManifest:
    raw: dict[str, Any]
    path: str

    # -- generic access ---------------------------------------------------
    def tier(self, name: str) -> dict[str, Any]:
        if name not in self.raw:
            raise KeyError(f"{self.path}: no tier {name!r}")
        return self.raw[name]

    def axis(self, tier: str, axis: str) -> list:
        """Magnitudes for one axis, exactly as frozen."""
        block = self.tier(tier)
        if axis not in block:
            raise KeyError(f"{self.path}: tier {tier!r} has no axis {axis!r}")
        value = block[axis]
        if isinstance(value, dict):
            raise KeyError(
                f"{self.path}: {tier}.{axis} is a group; use nested_axis()"
            )
        return value if isinstance(value, list) else [value]

    def nested_axis(self, tier: str, axis: str, sub: str) -> list:
        block = self.tier(tier)[axis]
        if sub not in block:
            raise KeyError(f"{self.path}: {tier}.{axis} has no {sub!r}")
        return block[sub]

    def scalar(self, tier: str, axis: str) -> Any:
        block = self.tier(tier)
        if axis not in block:
            raise KeyError(f"{self.path}: tier {tier!r} has no axis {axis!r}")
        return block[axis]

    # -- named blocks the brief calls out ---------------------------------
    @property
    def base_seed(self) -> int:
        return int(self.raw["base_seed"])

    @property
    def overconfidence_sigma(self) -> float:
        return float(self.raw["honesty_gates"]["overconfidence_sigma"])

    @property
    def overconfidence_tolerance(self) -> int:
        return int(self.raw["honesty_gates"]["overconfidence_tolerance"])

    @property
    def combined_holdout(self) -> dict[str, Any]:
        return dict(self.raw["combined_holdout"])


def flag_name(value: Any) -> str:
    """Canonical name for a manifest flag value.

    YAML 1.1 parses the manifest's bare ``off``/``on`` as booleans. The
    manifest is FROZEN, so the code adapts: booleans are rendered back to
    the words the manifest author wrote, everything else stringifies.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def load_manifest(path: str | Path | None = None) -> FaultManifest:
    path = Path(path) if path is not None else DEFAULT_MANIFEST
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "d6-faults/1":
        raise ValueError(f"{path}: not a d6-faults/1 manifest")
    return FaultManifest(raw=data, path=str(path))
