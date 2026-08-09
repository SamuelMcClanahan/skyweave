"""F4: voxel oracle matches brute-force voting; deterministic; bounded."""

from __future__ import annotations

import numpy as np

from skyweave2.fusion.config import VoxelConfig
from skyweave2.fusion.voxel_oracle import VoxelRay, propose, vote


def _rays_toward(point, origins):
    rays = []
    for camera_id, origin in enumerate(origins):
        origin = np.asarray(origin, dtype=np.float64)
        direction = np.asarray(point, dtype=np.float64) - origin
        direction /= np.linalg.norm(direction)
        rays.append(VoxelRay(camera_id=camera_id, origin=origin, direction=direction))
    return rays


def _brute_force_vote(seeds, rays, config: VoxelConfig):
    """Independent re-implementation: loop every cell and every ray."""
    cells = set()
    for seed in seeds:
        base = np.floor(np.asarray(seed) / config.cell_m).astype(int)
        h = config.half_extent_cells
        for dx in range(-h, h + 1):
            for dy in range(-h, h + 1):
                for dz in range(-h, h + 1):
                    cells.add((int(base[0] + dx), int(base[1] + dy), int(base[2] + dz)))
    out = {}
    radius = config.vote_radius_cells * config.cell_m
    for cell in cells:
        center = (np.asarray(cell, dtype=np.float64) + 0.5) * config.cell_m
        mask = 0
        for ray in rays:
            offset = center - ray.origin
            along = float(offset @ ray.direction)
            if along <= 0.0:
                continue
            perp = offset - along * ray.direction
            if float(np.linalg.norm(perp)) <= radius:
                mask |= 1 << ray.camera_id
        if mask:
            out[cell] = mask
    return out


def test_f4_vote_matches_brute_force():
    config = VoxelConfig(enabled=True, cell_m=2.0, half_extent_cells=4)
    point = np.array([10.0, 100.0, 20.0])
    rays = _rays_toward(point, [(-10.0, 0.0, 0.0), (0.0, 0.0, 0.0), (10.0, 0.0, 2.0)])
    seeds = [point + np.array([1.0, -2.0, 0.5])]
    assert vote(seeds, rays, config) == _brute_force_vote(seeds, rays, config)


def test_f4_peak_at_the_intersection_with_full_support():
    config = VoxelConfig(enabled=True, cell_m=2.0, half_extent_cells=5)
    point = np.array([10.0, 100.0, 20.0])
    rays = _rays_toward(point, [(-10.0, 0.0, 0.0), (0.0, 0.0, 0.0), (10.0, 0.0, 2.0)])
    peaks = propose([point + 0.4], rays, config)
    assert peaks
    top = peaks[0]
    assert top.support_count == 3
    assert top.support_mask == 0b111
    # Proposal-grade accuracy: a voting oracle over a shallow convergence
    # bundle is cell-quantized (~3 cells here); the D1 solver refines it.
    assert np.linalg.norm(top.position - point) <= 3.0 * config.cell_m


def test_f4_min_support_gate_can_reject():
    """A single-camera region produces NO proposals: the popcount gate works."""
    config = VoxelConfig(enabled=True, cell_m=2.0, half_extent_cells=3)
    point = np.array([10.0, 100.0, 20.0])
    rays = _rays_toward(point, [(0.0, 0.0, 0.0)])  # one camera only
    assert propose([point], rays, config) == []


def test_f4_deterministic_and_bounded():
    config = VoxelConfig(enabled=True, cell_m=2.0, half_extent_cells=6, max_peaks=3)
    rng = np.random.default_rng(4)
    points = [np.array([10.0, 100.0, 20.0]) + rng.normal(0, 15, 3) for _ in range(4)]
    origins = [(-10.0, 0.0, 0.0), (0.0, 0.0, 0.0), (10.0, 0.0, 2.0)]
    rays = [r for p in points for r in _rays_toward(p, origins)]
    seeds = [p + rng.normal(0, 1, 3) for p in points]
    a = propose(seeds, rays, config)
    b = propose(list(seeds), list(rays), config)
    assert [(p.cell, p.support_mask) for p in a] == [(p.cell, p.support_mask) for p in b]
    assert len(a) <= config.max_peaks


def test_f4_overlapping_seed_grids_agree():
    """World-lattice snapping: two seeds covering the same region produce the
    same cells as either alone (no seed-relative drift)."""
    config = VoxelConfig(enabled=True, cell_m=2.0, half_extent_cells=4)
    point = np.array([10.0, 100.0, 20.0])
    rays = _rays_toward(point, [(-10.0, 0.0, 0.0), (0.0, 0.0, 0.0)])
    single = vote([point], rays, config)
    double = vote([point, point + 0.3], rays, config)
    for cell, mask in single.items():
        assert double.get(cell) == mask
