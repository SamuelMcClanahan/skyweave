"""Bounded deterministic voxel proposal oracle (working plan §7.2/§8).

A candidate GENERATOR, never the accuracy mechanism: small local grids
seeded from candidate regions or track predictions, camera-support bitmask
per cell (a camera votes a cell at most once, however many of its rays pass
nearby), local peaks only. Cells snap to a world-aligned lattice so
overlapping seed grids agree exactly and the output is deterministic with
no seeds or floating-point order sensitivity.

The voxel center NEVER enters the filter (frozen decision): peaks only
nominate observation groups, which the D1 solver then refines.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from skyweave2.fusion.config import VoxelConfig


@dataclass(frozen=True)
class VoxelRay:
    camera_id: int
    origin: np.ndarray  # (3,)
    direction: np.ndarray  # (3,) unit


@dataclass(frozen=True)
class VoxelPeak:
    position: np.ndarray  # (3,) cell center, world
    cell: tuple[int, int, int]  # world lattice index
    support_mask: int  # bit per camera_id
    support_count: int


def _cell_center(cell: tuple[int, int, int], cell_m: float) -> np.ndarray:
    return (np.asarray(cell, dtype=np.float64) + 0.5) * cell_m


def _cells_around(seed: np.ndarray, config: VoxelConfig) -> list[tuple[int, int, int]]:
    base = np.floor(np.asarray(seed, dtype=np.float64) / config.cell_m).astype(int)
    h = config.half_extent_cells
    cells = []
    for dx in range(-h, h + 1):
        for dy in range(-h, h + 1):
            for dz in range(-h, h + 1):
                cells.append((int(base[0] + dx), int(base[1] + dy), int(base[2] + dz)))
    return cells


def vote(
    seeds: list[np.ndarray], rays: list[VoxelRay], config: VoxelConfig
) -> dict[tuple[int, int, int], int]:
    """Camera-support bitmask per world cell across all seed regions."""
    cells: set[tuple[int, int, int]] = set()
    for seed in seeds:
        cells.update(_cells_around(seed, config))
    if not cells:
        return {}
    ordered = sorted(cells)
    centers = np.array([_cell_center(c, config.cell_m) for c in ordered])
    radius = config.vote_radius_cells * config.cell_m
    support = np.zeros(len(ordered), dtype=np.int64)
    for ray in sorted(rays, key=lambda r: r.camera_id):
        offset = centers - ray.origin
        # einsum, not @: Accelerate raises spurious FP flags on strided
        # matvec (same platform quirk the D3 sensor model hit).
        along = np.einsum("ij,j->i", offset, ray.direction)
        perp = offset - along[:, None] * ray.direction[None, :]
        near = (np.linalg.norm(perp, axis=1) <= radius) & (along > 0.0)
        support[near] |= 1 << ray.camera_id
    return {cell: int(mask) for cell, mask in zip(ordered, support, strict=True) if mask}


def _ray_distance_sum(
    cell: tuple[int, int, int], mask: int, rays: list[VoxelRay], cell_m: float
) -> float:
    """Sum over supporting cameras of the nearest-ray distance to the cell.

    The plateau tie-break: a converging ray bundle supports a cone of
    cells; the best representative is the one closest to its rays (the
    intersection), not the lexicographically smallest lattice index.
    """
    center = _cell_center(cell, cell_m)
    best: dict[int, float] = {}
    for ray in rays:
        if not (mask >> ray.camera_id) & 1:
            continue
        offset = center - ray.origin
        along = float(offset @ ray.direction)
        if along <= 0.0:
            continue
        distance = float(np.linalg.norm(offset - along * ray.direction))
        if ray.camera_id not in best or distance < best[ray.camera_id]:
            best[ray.camera_id] = distance
    return sum(best.values())


def propose(
    seeds: list[np.ndarray], rays: list[VoxelRay], config: VoxelConfig
) -> list[VoxelPeak]:
    """Local peaks of camera support, popcount-gated, deterministic order."""
    votes = vote(seeds, rays, config)
    counts = {cell: bin(mask).count("1") for cell, mask in votes.items()}
    # Tie-break score for plateau cells: distance of the cell to its
    # supporting rays (smaller = closer to the true intersection).
    scores = {
        cell: _ray_distance_sum(cell, mask, rays, config.cell_m)
        for cell, mask in votes.items()
        if counts[cell] >= config.min_camera_support
    }
    peaks: list[VoxelPeak] = []
    for cell in sorted(scores):
        count = counts[cell]
        rank = (-count, scores[cell], cell)
        is_peak = True
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if dx == dy == dz == 0:
                        continue
                    neighbor = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
                    if neighbor not in scores:
                        continue
                    neighbor_rank = (-counts[neighbor], scores[neighbor], neighbor)
                    if neighbor_rank < rank:
                        # A strictly better neighbor (more support, then
                        # closer to its rays, then lattice order) owns this
                        # plateau: exactly one representative, near the
                        # ray intersection, deterministically.
                        is_peak = False
                        break
                if not is_peak:
                    break
            if not is_peak:
                break
        if is_peak:
            peaks.append(
                VoxelPeak(
                    position=_cell_center(cell, config.cell_m),
                    cell=cell,
                    support_mask=votes[cell],
                    support_count=count,
                )
            )
    peaks.sort(key=lambda p: (-p.support_count, scores[p.cell], p.cell))
    return peaks[: config.max_peaks]
