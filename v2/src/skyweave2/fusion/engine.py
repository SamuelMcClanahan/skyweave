"""Fusion engine: batch -> candidate groups -> D1 localize() -> results.

The D1 solver is consumed AS-IS; one LocalizationResult per surviving
candidate group, ``obs_ids`` filled by the solver from the exact
observations consumed. The voxel path sits behind ``config.voxel.enabled``
and only NOMINATES additional observation groups (peak centers never enter
the filter). Weak-geometry and over-residual groups are recorded as
rejections with their obs_ids — layered rejection, working plan §9.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from skyweave2.contracts import CameraModel, LocalizationResult
from skyweave2.fusion.aligner import Batch, obs_key
from skyweave2.fusion.association import (
    AssociationResult,
    CandidateGroup,
    TrackSeed,
    associate,
    solve_and_prune,
)
from skyweave2.fusion.config import FusionConfig
from skyweave2.fusion.systematic import estimate_systematic
from skyweave2.fusion.voxel_oracle import VoxelRay, propose
from skyweave2.geometry import GeometryConfig, bearing_from_observation


@dataclass(frozen=True)
class RejectionRecord:
    reason: str  # "weak_geometry" | "residual" | "solver_status"
    obs_ids: tuple[str, ...]
    detail: float  # residual px for "residual", else 0.0


@dataclass
class BatchOutput:
    batch_index: int
    ts_ns: int
    results: list[tuple[CandidateGroup, LocalizationResult]] = field(default_factory=list)
    rejections: list[RejectionRecord] = field(default_factory=list)
    unassigned_count: int = 0
    voxel_groups: int = 0
    voxel_peaks: int = 0


@dataclass
class RunResult:
    """One full pipeline run over an arrival-ordered observation stream."""

    batches: list[BatchOutput] = field(default_factory=list)
    published: list = field(default_factory=list)  # (batch_index, Track) pairs
    excluded: list = field(default_factory=list)  # aligner exclusions
    records: list = field(default_factory=list)  # TrackUpdateRecords (audit)
    statuses: dict = field(default_factory=dict)  # final track statuses
    # (batch_index, track_id) -> (systematic_bound, sources). The frozen
    # Track contract has no systematic field, so the second error channel
    # travels beside the published states rather than inside them.
    systematic: dict = field(default_factory=dict)


def run_stream(
    observations,
    cameras: dict[int, CameraModel],
    config: FusionConfig,
    session_uuid: str,
    geometry_config: GeometryConfig | None = None,
    tracker=None,
) -> RunResult:
    """Arrival-ordered observations -> batches -> groups -> tracks.

    The full deterministic chain used by tests, the report, and replay
    verification (F5). A caller-supplied tracker allows suppression tests.
    """
    from skyweave2.fusion.aligner import EventAligner
    from skyweave2.fusion.tracker import Tracker

    aligner = EventAligner(config)
    engine = FusionEngine(cameras, config, geometry_config)
    if tracker is None:
        tracker = Tracker(config, session_uuid)
    run = RunResult()

    def _step(batch: Batch) -> None:
        seeds = tracker.seeds(batch.ts_ns)
        output = engine.process_batch(batch, seeds)
        run.batches.append(output)
        published = tracker.update_batch(
            batch.ts_ns, output.results, batch_index=batch.batch_index
        )
        for track in published:
            run.published.append((batch.batch_index, track))
            entry = getattr(tracker, "published_systematic", {}).get(track.track_id)
            if entry is not None:
                run.systematic[(batch.batch_index, track.track_id)] = entry

    for obs in observations:
        aligner.offer(obs)
        for batch in aligner.ready_batches():
            _step(batch)
    for batch in aligner.flush():
        _step(batch)
    run.excluded = list(aligner.excluded)
    run.records = list(tracker.records)
    run.statuses = {tid: status.value for tid, status in tracker.statuses().items()}
    return run


class FusionEngine:
    def __init__(
        self,
        cameras: dict[int, CameraModel],
        config: FusionConfig,
        geometry_config: GeometryConfig | None = None,
    ):
        self._cameras = cameras
        self._config = config
        self._gcfg = geometry_config or GeometryConfig()

    def _voxel_groups(
        self, assoc: AssociationResult, track_seeds: list[TrackSeed], batch: Batch
    ) -> tuple[list[CandidateGroup], int]:
        """Voxel-nominated groups from observations association left behind."""
        vcfg = self._config.voxel
        acfg = self._config.association
        observations = batch.sorted_observations()
        rays = []
        for obs in observations:
            camera = self._cameras.get(obs.envelope.camera_id)
            if camera is None:
                continue
            bearing = bearing_from_observation(obs, camera, self._gcfg)
            rays.append(
                VoxelRay(
                    camera_id=obs.envelope.camera_id,
                    origin=bearing.origin,
                    direction=bearing.direction,
                )
            )
        seeds = [g.seed_position for g in assoc.groups if g.seed_position is not None]
        seeds += [np.asarray(s.position, dtype=np.float64) for s in track_seeds]
        if not seeds:
            return [], 0
        peaks = propose(seeds, rays, vcfg)

        claimed: set[str] = set()
        for group in assoc.groups:
            claimed.update(group.keys())
        existing_sets = {group.keys() for group in assoc.groups}
        out: list[CandidateGroup] = []
        for peak in peaks:
            members = []
            for camera_id in sorted(self._cameras):
                camera = self._cameras[camera_id]
                proj = camera.project(peak.position)
                if proj is None:
                    continue
                best, best_px = None, vcfg.gather_gate_px
                for obs in observations:
                    if obs.envelope.camera_id != camera_id or obs_key(obs) in claimed:
                        continue
                    px = float(np.hypot(proj[0] - obs.u, proj[1] - obs.v))
                    if px <= best_px:
                        best, best_px = obs, px
                if best is not None:
                    members.append(best)
            if len(members) < acfg.min_cameras:
                continue
            members = sorted(
                members, key=lambda o: (o.envelope.camera_id, o.envelope.frame_seq, o.obs_id)
            )
            group = CandidateGroup(
                observations=tuple(members),
                camera_ids=tuple(o.envelope.camera_id for o in members),
                source="voxel",
                seed_position=peak.position,
            )
            if group.keys() in existing_sets:
                continue
            for key in group.keys():
                claimed.add(key)
            existing_sets.add(group.keys())
            out.append(group)
        return out, len(peaks)

    def process_batch(self, batch: Batch, track_seeds: list[TrackSeed]) -> BatchOutput:
        assoc = associate(
            batch.sorted_observations(), self._cameras, track_seeds, self._config, self._gcfg
        )
        groups = list(assoc.groups)
        voxel_added = 0
        voxel_peaks = 0
        if self._config.voxel.enabled:
            extra, voxel_peaks = self._voxel_groups(assoc, track_seeds, batch)
            groups.extend(extra)
            voxel_added = len(extra)

        output = BatchOutput(
            batch_index=batch.batch_index,
            ts_ns=batch.ts_ns,
            unassigned_count=len(assoc.unassigned) - sum(
                len(g.observations) for g in groups if g.source == "voxel"
            ),
            voxel_groups=voxel_added,
            voxel_peaks=voxel_peaks,
        )
        # Association groups arrive solve-validated with their passing
        # result attached (post-review architecture); its prune/reject audit
        # records are carried through verbatim.
        output.rejections.extend(
            RejectionRecord(r.reason, r.obs_ids, r.detail) for r in assoc.rejections
        )
        for group in groups:
            if group.result is not None:
                output.results.append(
                    (group, self._with_systematic(group, group.result))
                )
                continue
            # Voxel-nominated groups are solved (and pruned) here.
            result, survivors, rejections = solve_and_prune(
                list(group.observations), self._cameras, self._config, self._gcfg
            )
            output.rejections.extend(
                RejectionRecord(r.reason, r.obs_ids, r.detail) for r in rejections
            )
            if result is None:
                continue
            pruned_group = CandidateGroup(
                observations=tuple(survivors),
                camera_ids=tuple(o.envelope.camera_id for o in survivors),
                source=group.source,
                result=result,
                seed_position=group.seed_position,
            )
            output.results.append(
                (pruned_group, self._with_systematic(pruned_group, result))
            )
        return output

    def _with_systematic(self, group: CandidateGroup, result: LocalizationResult):
        """Fill the D0 systematic channel from DECLARED uncertainty (D6-F1).

        The conditional covariance is untouched: a bias never enters the
        filter's noise model (D0 two-channel rule).
        """
        declared = self._config.systematic
        observations = list(group.observations)
        cameras = [self._cameras[o.envelope.camera_id] for o in observations
                   if o.envelope.camera_id in self._cameras]
        bound, sources = estimate_systematic(
            np.asarray(result.position, dtype=np.float64),
            observations,
            cameras,
            declared.calibration_rotation_sigma_deg,
            declared.calibration_position_sigma_m,
            declared.target_width_m,
            declared.estimated_speed_mps,
        )
        return result.model_copy(update={"systematic_bound": bound,
                                         "systematic_sources": sources})
