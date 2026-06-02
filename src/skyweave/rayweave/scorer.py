from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave.config import ScorerConfig
from skyweave.fusion.aligner import AlignedEvidence
from skyweave.fusion.geom import CameraCalib, ray_from_pixel
from skyweave.messages import SparseVoxel, WeavefieldVolume
from skyweave.rayweave.dda import trace_ray_indices
from skyweave.rayweave.grid import VoxelGrid
from skyweave.rayweave.patches import decode_rle_u8


@dataclass(frozen=True)
class ScoredWeavefield:
    volume: WeavefieldVolume
    dense_scores: np.ndarray
    support_counts: np.ndarray
    camera_scores: dict[int, np.ndarray]


class RayweaveScorer:
    def __init__(
        self,
        grid: VoxelGrid,
        cameras: dict[int, CameraCalib],
        config: ScorerConfig,
    ) -> None:
        self.grid = grid
        self.cameras = cameras
        self.config = config

    def score(self, aligned: AlignedEvidence) -> ScoredWeavefield:
        camera_scores: dict[int, np.ndarray] = {}
        for packet in aligned.motion_packets:
            camera = self.cameras[packet.camera_id]
            score_grid = self.grid.zeros()
            pixels = list(_iter_patch_pixels(packet.motion_patches))
            if not pixels and packet.blobs:
                blob = max(packet.blobs, key=lambda item: item.confidence)
                pixels = [(blob.cx, blob.cy, blob.confidence)]
            if not pixels:
                camera_scores[packet.camera_id] = score_grid
                continue

            normalizer = max(sum(weight for *_uv, weight in pixels), 1e-9)
            for u, v, weight in pixels:
                origin, direction = ray_from_pixel(u, v, camera)
                ray_weight = float(weight / normalizer)
                for ix, iy, iz in trace_ray_indices(origin, direction, self.grid):
                    score_grid[ix, iy, iz] += ray_weight
            camera_scores[packet.camera_id] = score_grid

        support_counts = np.zeros(self.grid.dims, dtype=np.uint8)
        combined = self.grid.zeros()
        for score_grid in camera_scores.values():
            support_counts += (score_grid > 0).astype(np.uint8)
            combined += score_grid
        combined = np.where(support_counts >= self.config.min_supporting_cameras, combined, 0.0)

        sparse = _top_k_sparse(combined, self.config.top_k_voxels)
        volume = WeavefieldVolume(
            ts_ns=aligned.ts_ns,
            grid=self.grid.spec,
            voxels=sparse,
            peaks=[],
            decay_s=1.0,
            source_packet_ids=aligned.source_packet_ids,
        )
        return ScoredWeavefield(volume, combined, support_counts, camera_scores)


def _iter_patch_pixels(patches) -> list[tuple[float, float, float]]:
    pixels: list[tuple[float, float, float]] = []
    for patch in patches:
        if patch.encoding != "rle_u8":
            continue
        mask = decode_rle_u8(patch.payload, patch.bbox_w, patch.bbox_h)
        ys, xs = np.nonzero(mask)
        for y, x in zip(ys, xs):
            value = float(mask[y, x]) / 255.0 * patch.value_scale
            pixels.append((patch.bbox_x + float(x), patch.bbox_y + float(y), value))
    return pixels


def _top_k_sparse(scores: np.ndarray, top_k: int) -> list[SparseVoxel]:
    flat = scores.ravel()
    positive = np.flatnonzero(flat > 0)
    if positive.size == 0:
        return []
    if positive.size > top_k:
        local = np.argpartition(flat[positive], -top_k)[-top_k:]
        positive = positive[local]
    order = positive[np.argsort(flat[positive])[::-1]]
    coords = np.column_stack(np.unravel_index(order, scores.shape))
    return [
        SparseVoxel(ix=int(ix), iy=int(iy), iz=int(iz), score=float(scores[ix, iy, iz]))
        for ix, iy, iz in coords
    ]

