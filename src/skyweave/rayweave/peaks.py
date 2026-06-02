from __future__ import annotations

from collections import deque

import numpy as np

from skyweave.config import PeakConfig
from skyweave.messages import Measurement3D, VoxelPeak
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.scorer import ScoredWeavefield


class PeakExtractor:
    def __init__(self, grid: VoxelGrid, config: PeakConfig) -> None:
        self.grid = grid
        self.config = config

    def extract(self, scored: ScoredWeavefield) -> tuple[list[VoxelPeak], list[Measurement3D]]:
        scores = scored.dense_scores
        positive = scores[scores > 0]
        if positive.size == 0:
            return [], []
        threshold = float(np.percentile(positive, self.config.threshold_percentile))
        mask = scores >= threshold
        components = _connected_components(mask)

        peaks: list[VoxelPeak] = []
        for component in components:
            component = sorted(component, key=lambda idx: scores[idx], reverse=True)
            weights = np.asarray([scores[idx] for idx in component], dtype=np.float64)
            centers = np.asarray([self.grid.index_to_center(idx) for idx in component], dtype=np.float64)
            total = float(np.sum(weights))
            if total <= 0:
                continue
            position = np.sum(centers * weights[:, None], axis=0) / total
            spread = centers - position
            cov = (spread.T * weights) @ spread / total
            cov += np.eye(3, dtype=np.float64) * (self.grid.voxel_size**2 / 12.0)
            supporting = _supporting_cameras(component, scored.camera_scores)
            peaks.append(
                VoxelPeak(
                    position=tuple(float(x) for x in position),
                    score=float(np.max(weights)),
                    covariance=cov.tolist(),
                    supporting_camera_ids=supporting,
                    n_voxels=len(component),
                )
            )

        peaks = sorted(peaks, key=lambda peak: peak.score, reverse=True)[: self.config.max_peaks]
        measurements = [
            Measurement3D(
                ts_ns=scored.volume.ts_ns,
                source="voxel_peak",
                position=peak.position,
                covariance=peak.covariance,
                score=peak.score,
                supporting_camera_ids=peak.supporting_camera_ids,
            )
            for peak in peaks
        ]
        return peaks, measurements


def _connected_components(mask: np.ndarray) -> list[list[tuple[int, int, int]]]:
    visited = np.zeros(mask.shape, dtype=bool)
    components: list[list[tuple[int, int, int]]] = []
    starts = np.argwhere(mask)
    dims = mask.shape
    for start_arr in starts:
        start = tuple(int(x) for x in start_arr)
        if visited[start]:
            continue
        queue: deque[tuple[int, int, int]] = deque([start])
        visited[start] = True
        component: list[tuple[int, int, int]] = []
        while queue:
            idx = queue.popleft()
            component.append(idx)
            for neighbor in _neighbors(idx, dims):
                if mask[neighbor] and not visited[neighbor]:
                    visited[neighbor] = True
                    queue.append(neighbor)
        components.append(component)
    return components


def _neighbors(idx: tuple[int, int, int], dims: tuple[int, int, int]):
    ix, iy, iz = idx
    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        nx, ny, nz = ix + dx, iy + dy, iz + dz
        if 0 <= nx < dims[0] and 0 <= ny < dims[1] and 0 <= nz < dims[2]:
            yield nx, ny, nz


def _supporting_cameras(
    component: list[tuple[int, int, int]],
    camera_scores: dict[int, np.ndarray],
) -> list[int]:
    supporting = []
    for camera_id, scores in camera_scores.items():
        if any(scores[idx] > 0 for idx in component):
            supporting.append(camera_id)
    return supporting

