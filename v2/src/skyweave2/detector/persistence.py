"""Short temporal stabilization: single-frame flicker never becomes an
observation; a component must reappear within the gate radius for
``persistence_frames`` consecutive frames (test K6).

Greedy nearest-neighbor association at proc resolution — deliberately
simple; real multi-target association is D5's problem, this only
stabilizes emission.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from skyweave2.detector.components import MaskComponent
from skyweave2.detector.config import DetectorConfig


@dataclass
class _Track:
    u: float
    v: float
    count: int
    blob_id: int


@dataclass
class PersistenceFilter:
    config: DetectorConfig
    _tracks: list[_Track] = field(default_factory=list)
    _next_id: int = 0

    def update(self, components: list[MaskComponent]) -> list[tuple[MaskComponent, int, int]]:
        """Return (component, persistence_count, local_blob_id) for each
        component whose count has reached the persistence threshold.

        Association is globally nearest-pair-first (not raster order): all
        (component, track) pairs inside the gate are sorted by distance and
        claimed closest-first. Raster-order greedy allowed a one-frame
        flicker that happened to sort earlier to STEAL an adjacent track and
        inherit its count — emitting a single-frame blob as persistent
        (found by adversarial review; regression-tested).
        """
        gate = self.config.persistence_gate_px
        pairs: list[tuple[float, int, int]] = []
        for ci, comp in enumerate(components):
            for ti, track in enumerate(self._tracks):
                d = float(np.hypot(comp.centroid_u - track.u, comp.centroid_v - track.v))
                if d <= gate:
                    pairs.append((d, ci, ti))
        pairs.sort()
        comp_to_track: dict[int, int] = {}
        used_tracks: set[int] = set()
        for _, ci, ti in pairs:
            if ci in comp_to_track or ti in used_tracks:
                continue
            comp_to_track[ci] = ti
            used_tracks.add(ti)

        new_tracks: list[_Track] = []
        results: list[tuple[MaskComponent, int, int]] = []
        for ci, comp in enumerate(components):
            if ci in comp_to_track:
                prior = self._tracks[comp_to_track[ci]]
                track = _Track(comp.centroid_u, comp.centroid_v, prior.count + 1, prior.blob_id)
            else:
                track = _Track(comp.centroid_u, comp.centroid_v, 1, self._next_id)
                self._next_id += 1
            new_tracks.append(track)
            if track.count >= self.config.persistence_frames:
                results.append((comp, track.count, track.blob_id))
        self._tracks = new_tracks  # unmatched old tracks die immediately
        return results
