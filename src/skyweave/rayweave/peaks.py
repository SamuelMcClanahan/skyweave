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
        sparse = scored.volume.voxels
        if not sparse:
            return [], []
        values = np.asarray([voxel.score for voxel in sparse], dtype=np.float32)
        threshold = float(np.percentile(values, self.config.threshold_percentile))
        candidates = {
            (voxel.ix, voxel.iy, voxel.iz): voxel.score
            for voxel in sparse
            if voxel.score >= threshold
        }
        components = _connected_components_sparse(candidates)

        peaks: list[VoxelPeak] = []
        for component in components:
            component = sorted(component, key=lambda idx: candidates[idx], reverse=True)
            peak_idx = component[0]
            local_indices, local_scores = _local_peak_scores(scored.dense_scores, peak_idx, self.config.soft_argmax_radius_voxels)
            if local_indices:
                centers = self.grid.origin + (np.asarray(local_indices, dtype=np.float64) + 0.5) * self.grid.voxel_size
                weights = _softmax_weights(local_scores, self.config.soft_argmax_beta)
            else:
                weights = np.asarray([candidates[idx] for idx in component], dtype=np.float64)
                centers = self.grid.origin + (np.asarray(component, dtype=np.float64) + 0.5) * self.grid.voxel_size
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


def _connected_components_sparse(scores: dict[tuple[int, int, int], float]) -> list[list[tuple[int, int, int]]]:
    visited: set[tuple[int, int, int]] = set()
    components: list[list[tuple[int, int, int]]] = []
    candidates = set(scores)
    for start in candidates:
        if start in visited:
            continue
        queue: deque[tuple[int, int, int]] = deque([start])
        visited.add(start)
        component: list[tuple[int, int, int]] = []
        while queue:
            idx = queue.popleft()
            component.append(idx)
            for neighbor in _neighbors(idx):
                if neighbor in candidates and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


def _neighbors(idx: tuple[int, int, int]):
    ix, iy, iz = idx
    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        yield ix + dx, iy + dy, iz + dz


def _local_peak_scores(
    scores: np.ndarray,
    peak_idx: tuple[int, int, int],
    radius: int,
) -> tuple[list[tuple[int, int, int]], np.ndarray]:
    radius = max(int(radius), 0)
    px, py, pz = peak_idx
    indices: list[tuple[int, int, int]] = []
    values: list[float] = []
    for ix in range(max(px - radius, 0), min(px + radius + 1, scores.shape[0])):
        for iy in range(max(py - radius, 0), min(py + radius + 1, scores.shape[1])):
            for iz in range(max(pz - radius, 0), min(pz + radius + 1, scores.shape[2])):
                value = float(scores[ix, iy, iz])
                if value <= 0.0:
                    continue
                indices.append((ix, iy, iz))
                values.append(value)
    return indices, np.asarray(values, dtype=np.float64)


def _softmax_weights(scores: np.ndarray, beta: float) -> np.ndarray:
    if scores.size == 0:
        return scores
    beta = max(float(beta), 0.0)
    if beta <= 0.0:
        return np.ones_like(scores, dtype=np.float64) / float(scores.size)
    shifted = scores - float(np.max(scores))
    weights = np.exp(beta * shifted)
    total = float(np.sum(weights))
    if total <= 0.0 or not np.isfinite(total):
        return np.ones_like(scores, dtype=np.float64) / float(scores.size)
    return weights / total


def _supporting_cameras(
    component: list[tuple[int, int, int]],
    camera_scores: dict[int, np.ndarray],
) -> list[int]:
    supporting = []
    for camera_id, scores in camera_scores.items():
        if any(scores[idx] > 0 for idx in component):
            supporting.append(camera_id)
    return supporting
