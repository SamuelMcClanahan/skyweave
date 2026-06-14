from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave.config import GridConfig
from skyweave.messages import VoxelGridSpec


@dataclass(frozen=True)
class VoxelGrid:
    spec: VoxelGridSpec

    @classmethod
    def from_config(cls, config: GridConfig) -> "VoxelGrid":
        return cls(
            VoxelGridSpec(
                frame_id=config.frame_id,
                origin=config.origin_m,
                dims=config.dims,
                voxel_size_m=config.voxel_size_m,
            )
        )

    @property
    def origin(self) -> np.ndarray:
        return np.asarray(self.spec.origin, dtype=np.float64)

    @property
    def dims(self) -> tuple[int, int, int]:
        return self.spec.dims

    @property
    def voxel_size(self) -> float:
        return self.spec.voxel_size_m

    @property
    def bounds_min(self) -> np.ndarray:
        return self.origin

    @property
    def bounds_max(self) -> np.ndarray:
        return self.origin + np.asarray(self.dims, dtype=np.float64) * self.voxel_size

    def zeros(self) -> np.ndarray:
        return np.zeros(self.dims, dtype=np.float32)

    def index_to_center(self, index: tuple[int, int, int]) -> np.ndarray:
        return self.origin + (np.asarray(index, dtype=np.float64) + 0.5) * self.voxel_size

    def point_to_index(self, point: np.ndarray) -> tuple[int, int, int] | None:
        rel = (point - self.origin) / self.voxel_size
        idx = np.floor(rel).astype(int)
        if np.any(idx < 0) or np.any(idx >= np.asarray(self.dims)):
            return None
        return int(idx[0]), int(idx[1]), int(idx[2])

