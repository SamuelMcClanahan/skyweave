from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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


class ScorerBackend(Protocol):
    def score(self, aligned: AlignedEvidence) -> ScoredWeavefield:
        ...


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
        self.backend = _build_backend(grid, cameras, config)

    def score(self, aligned: AlignedEvidence) -> ScoredWeavefield:
        return self.backend.score(aligned)


class PythonNumpyScorerBackend:
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


class NumbaScorerBackend:
    def __init__(
        self,
        grid: VoxelGrid,
        cameras: dict[int, CameraCalib],
        config: ScorerConfig,
    ) -> None:
        from skyweave.rayweave.numba_scorer import combine_touched_scores_numba, score_rays_numba

        self.grid = grid
        self.cameras = cameras
        self.config = config
        self._score_rays = score_rays_numba
        self._combine_scores = combine_touched_scores_numba
        self._camera_ids = sorted(cameras)
        self._camera_slots = {camera_id: idx for idx, camera_id in enumerate(self._camera_ids)}
        self._camera_positions = np.asarray([cameras[camera_id].position for camera_id in self._camera_ids], dtype=np.float64)
        self._camera_rotations = np.asarray(
            [cameras[camera_id].T_world_cam[:3, :3] for camera_id in self._camera_ids],
            dtype=np.float64,
        )
        self._camera_intrinsics = np.asarray(
            [
                [
                    cameras[camera_id].K[0, 0],
                    cameras[camera_id].K[1, 1],
                    cameras[camera_id].K[0, 2],
                    cameras[camera_id].K[1, 2],
                ]
                for camera_id in self._camera_ids
            ],
            dtype=np.float64,
        )
        self._grid_origin = self.grid.origin.astype(np.float64)
        self._dims = np.asarray(self.grid.dims, dtype=np.int64)
        self._max_voxels_per_ray = int(self._dims.sum() + 3)
        self._touched_stamps = np.zeros(int(np.prod(self._dims)), dtype=np.int32)
        self._frame_stamp = 0

    def score(self, aligned: AlignedEvidence) -> ScoredWeavefield:
        ray_camera_slots, ray_u, ray_v, ray_weight = self._rays_from_packets(aligned)
        score_by_camera = np.zeros((len(self._camera_ids), *self.grid.dims), dtype=np.float32)
        touched = np.empty(max(ray_u.size * self._max_voxels_per_ray, 1), dtype=np.int64)
        touched_indices = np.empty(0, dtype=np.int64)
        active_slots: set[int] = set()
        if ray_u.size:
            active_slots = {int(slot) for slot in ray_camera_slots}
            frame_stamp = self._next_frame_stamp()
            touched_count = self._score_rays(
                score_by_camera,
                touched,
                self._touched_stamps,
                frame_stamp,
                ray_camera_slots,
                ray_u,
                ray_v,
                ray_weight,
                self._camera_positions,
                self._camera_rotations,
                self._camera_intrinsics,
                self._grid_origin,
                self._dims,
                float(self.grid.voxel_size),
            )
            touched_indices = touched[:touched_count]

        combined = self.grid.zeros()
        support_counts = np.zeros(self.grid.dims, dtype=np.uint8)
        self._combine_scores(score_by_camera, combined, support_counts, touched_indices, int(self.config.min_supporting_cameras))
        sparse = _top_k_sparse_from_flat_indices(combined, touched_indices, self.config.top_k_voxels)
        volume = WeavefieldVolume(
            ts_ns=aligned.ts_ns,
            grid=self.grid.spec,
            voxels=sparse,
            peaks=[],
            decay_s=1.0,
            source_packet_ids=aligned.source_packet_ids,
        )
        camera_scores = {
            camera_id: score_by_camera[slot]
            for camera_id, slot in self._camera_slots.items()
            if slot in active_slots
        }
        return ScoredWeavefield(volume, combined, support_counts, camera_scores)

    def _next_frame_stamp(self) -> int:
        if self._frame_stamp >= np.iinfo(np.int32).max:
            self._touched_stamps.fill(0)
            self._frame_stamp = 0
        self._frame_stamp += 1
        return self._frame_stamp

    def _rays_from_packets(self, aligned: AlignedEvidence) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        camera_slots: list[int] = []
        us: list[float] = []
        vs: list[float] = []
        weights: list[float] = []
        for packet in aligned.motion_packets:
            pixels = list(_iter_patch_pixels(packet.motion_patches))
            if not pixels and packet.blobs:
                blob = max(packet.blobs, key=lambda item: item.confidence)
                pixels = [(blob.cx, blob.cy, blob.confidence)]
            if not pixels:
                continue

            normalizer = max(sum(weight for *_uv, weight in pixels), 1e-9)
            slot = self._camera_slots[packet.camera_id]
            for u, v, weight in pixels:
                camera_slots.append(slot)
                us.append(u)
                vs.append(v)
                weights.append(float(weight / normalizer))

        return (
            np.asarray(camera_slots, dtype=np.int64),
            np.asarray(us, dtype=np.float64),
            np.asarray(vs, dtype=np.float64),
            np.asarray(weights, dtype=np.float32),
        )


def _build_backend(
    grid: VoxelGrid,
    cameras: dict[int, CameraCalib],
    config: ScorerConfig,
) -> ScorerBackend:
    if config.backend == "python_numpy":
        return PythonNumpyScorerBackend(grid, cameras, config)
    if config.backend == "numba":
        return NumbaScorerBackend(grid, cameras, config)
    raise ValueError(f"Unsupported Rayweave scorer backend: {config.backend!r}")


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


def _top_k_sparse_from_flat_indices(scores: np.ndarray, indices: np.ndarray, top_k: int) -> list[SparseVoxel]:
    if indices.size == 0:
        return []
    flat = scores.ravel()
    positive = indices[flat[indices] > 0]
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
