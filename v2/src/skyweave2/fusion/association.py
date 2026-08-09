"""Cross-camera association: candidate groups from one event-time batch.

Frozen mechanics (D5 brief): deterministic camera-pair enumeration plus
consensus against the remaining cameras — exact and seedless — with
track-seeded gating once tracks exist. The output type ALWAYS carries
multiple candidate groups, so the single-target shortcut can never become
the architecture (working plan §6).

Post-review architecture (D5 adversarial review, confirmed critical): a
group only CLAIMS its observations after its D1 solve passes the residual
gate, and an over-residual group is pruned by the D1 leave-one-out
diagnostic (working plan §9 layer 5) before rejection. Consequences the
old claim-first design got wrong:

- one static decoy inside a track's gate no longer poisons the whole
  group and starve the track — the decoy is pruned, the survivors pass;
- a larger mixed ghost group that fails its solve no longer steals a true
  observation from a smaller true group — failed groups claim nothing;
- every pruned member and rejected group leaves an audit record.

The pair gate is the closest-approach point of the two bearing rays
reprojected into both cameras: algebraically equivalent to an epipolar
line distance for calibrated cameras, in the config's pixel units.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from skyweave2.contracts import CameraModel, LocalizationResult, Observation2D, ResultStatus
from skyweave2.fusion.aligner import obs_key
from skyweave2.fusion.config import FusionConfig
from skyweave2.geometry import (
    GeometryConfig,
    bearing_from_observation,
    leave_one_out,
    localize,
)


@dataclass(frozen=True)
class TrackSeed:
    """Predicted position of an existing track, used to claim observations."""

    track_id: int
    position: np.ndarray  # (3,) predicted at batch time
    suppressed: bool = False


@dataclass(frozen=True)
class CandidateGroup:
    observations: tuple[Observation2D, ...]  # sorted by camera_id
    camera_ids: tuple[int, ...]
    source: str  # "track" | "direct" | "voxel"
    result: LocalizationResult | None = None  # the PASSING solve (engine reuses)
    seed_track_id: int | None = None
    suppressed_seed: bool = False
    pair_residual_px: float = 0.0
    seed_position: np.ndarray | None = None

    def keys(self) -> tuple[str, ...]:
        return tuple(obs_key(o) for o in self.observations)


@dataclass(frozen=True)
class AssociationRejection:
    reason: str  # "weak_geometry" | "residual" | "pruned_member"
    obs_ids: tuple[str, ...]
    detail: float


@dataclass
class AssociationResult:
    groups: list[CandidateGroup] = field(default_factory=list)
    unassigned: list[Observation2D] = field(default_factory=list)
    rejections: list[AssociationRejection] = field(default_factory=list)


def _closest_point_between_rays(o1, d1, o2, d2) -> tuple[np.ndarray, float]:
    """Midpoint of the common perpendicular and the gap between the rays."""
    cross = np.cross(d1, d2)
    denom = float(cross @ cross)
    w0 = o1 - o2
    if denom < 1e-12:  # parallel rays: no usable pair point
        return (o1 + o2) / 2.0, float(np.inf)
    t1 = float(np.cross(w0, d2) @ cross) / -denom
    t2 = float(np.cross(w0, d1) @ cross) / -denom
    p1 = o1 + t1 * d1
    p2 = o2 + t2 * d2
    return (p1 + p2) / 2.0, float(np.linalg.norm(p1 - p2))


def _reprojection_px(camera: CameraModel, point: np.ndarray, obs: Observation2D) -> float:
    proj = camera.project(point)
    if proj is None:
        return float(np.inf)
    return float(np.hypot(proj[0] - obs.u, proj[1] - obs.v))


def _sorted_obs(observations: list[Observation2D]) -> list[Observation2D]:
    return sorted(
        observations, key=lambda o: (o.envelope.camera_id, o.envelope.frame_seq, o.obs_id)
    )


def solve_and_prune(
    members: list[Observation2D],
    cameras: dict[int, CameraModel],
    config: FusionConfig,
    gcfg: GeometryConfig,
) -> tuple[LocalizationResult | None, list[Observation2D], list[AssociationRejection]]:
    """Localize a member set; prune LOO-flagged members until it passes.

    Returns (passing result or None, surviving members, audit rejections).
    Pruned members are NOT claimed by anyone — they stay available to other
    hypotheses (the claim-release semantics the review demanded).
    """
    acfg = config.association
    members = _sorted_obs(members)
    rejections: list[AssociationRejection] = []
    while len(members) >= acfg.min_cameras:
        result = localize(members, cameras, gcfg)
        if result.status == ResultStatus.WEAK_GEOMETRY:
            rejections.append(
                AssociationRejection(
                    "weak_geometry", tuple(obs_key(o) for o in members), 0.0
                )
            )
            return None, members, rejections
        if result.residual_px_rms <= acfg.residual_px_max:
            return result, members, rejections
        if len(members) <= acfg.min_cameras:
            rejections.append(
                AssociationRejection(
                    "residual", tuple(obs_key(o) for o in members),
                    result.residual_px_rms,
                )
            )
            return None, members, rejections
        # Leave-one-out (D1 diagnostic, consumed as-is): drop the member the
        # remaining cameras most disagree with; deterministic worst-first.
        cams_list = [cameras[o.envelope.camera_id] for o in members]
        pixels = np.array([[o.u, o.v] for o in members])
        covs = np.array(
            [[[o.cov_uu, o.cov_uv], [o.cov_uv, o.cov_vv]] for o in members]
        )
        entries = leave_one_out(cams_list, pixels, covs, gcfg)
        worst = max(entries, key=lambda e: (e.residual_px, e.index))
        pruned = members[worst.index]
        rejections.append(
            AssociationRejection(
                "pruned_member", (obs_key(pruned),), worst.residual_px
            )
        )
        members = [o for i, o in enumerate(members) if i != worst.index]
    return None, members, rejections


def associate(
    observations: list[Observation2D],
    cameras: dict[int, CameraModel],
    track_seeds: list[TrackSeed],
    config: FusionConfig,
    geometry_config: GeometryConfig | None = None,
) -> AssociationResult:
    """One batch in, solve-validated candidate groups out. Deterministic."""
    acfg = config.association
    gcfg = geometry_config or GeometryConfig()
    known = [o for o in _sorted_obs(observations) if o.envelope.camera_id in cameras]
    out = AssociationResult()
    bearings = {
        obs_key(o): bearing_from_observation(o, cameras[o.envelope.camera_id], gcfg)
        for o in known
    }
    claimed: set[str] = set()

    def _accept(members, source, seed=None):
        result, survivors, rejections = solve_and_prune(members, cameras, config, gcfg)
        out.rejections.extend(rejections)
        if result is None:
            return False
        for member in survivors:
            claimed.add(obs_key(member))
        out.groups.append(
            CandidateGroup(
                observations=tuple(survivors),
                camera_ids=tuple(o.envelope.camera_id for o in survivors),
                source=source,
                result=result,
                seed_track_id=None if seed is None else seed.track_id,
                suppressed_seed=False if seed is None else seed.suppressed,
                seed_position=np.asarray(result.position, dtype=np.float64),
            )
        )
        return True

    # ---- 1. Track-seeded gating (sorted by track_id: deterministic) -----
    for seed in sorted(track_seeds, key=lambda s: s.track_id):
        members: list[Observation2D] = []
        for camera_id in sorted(cameras):
            proj = cameras[camera_id].project(seed.position)
            if proj is None:
                continue
            best: Observation2D | None = None
            best_px = float(np.inf)
            for obs in known:
                if obs.envelope.camera_id != camera_id or obs_key(obs) in claimed:
                    continue
                bearing = bearings[obs_key(obs)]
                offset = seed.position - bearing.origin
                perp = offset - (offset @ bearing.direction) * bearing.direction
                if float(np.linalg.norm(perp)) > acfg.track_gate_m:
                    continue
                px = float(np.hypot(proj[0] - obs.u, proj[1] - obs.v))
                if px < best_px:
                    best, best_px = obs, px
            if best is not None:
                members.append(best)
        if len(members) >= acfg.min_cameras:
            _accept(members, "track", seed=seed)

    # ---- 2. Direct hypotheses: pair enumeration + consensus -------------
    def _propose_direct():
        remaining = [o for o in known if obs_key(o) not in claimed]
        by_camera: dict[int, list[Observation2D]] = {}
        for obs in remaining:
            by_camera.setdefault(obs.envelope.camera_id, []).append(obs)
        camera_ids = sorted(by_camera)
        proposals: list[tuple[tuple, list[Observation2D], float]] = []
        epipolar_failures: list[AssociationRejection] = []
        for i, cam_a in enumerate(camera_ids):
            for cam_b in camera_ids[i + 1 :]:
                for obs_a in by_camera[cam_a]:
                    for obs_b in by_camera[cam_b]:
                        bearing_a = bearings[obs_key(obs_a)]
                        bearing_b = bearings[obs_key(obs_b)]
                        point, _gap = _closest_point_between_rays(
                            bearing_a.origin, bearing_a.direction,
                            bearing_b.origin, bearing_b.direction,
                        )
                        if not np.all(np.isfinite(point)):
                            continue
                        pair_px = max(
                            _reprojection_px(cameras[cam_a], point, obs_a),
                            _reprojection_px(cameras[cam_b], point, obs_b),
                        )
                        if pair_px > acfg.epipolar_gate_px:
                            # D6-F4: record the epipolar rejection per
                            # observation so layer 2 is visible in the D6
                            # layer table. A pair failing the gate is not
                            # a terminal verdict for its members (either may
                            # still pair with another camera), so this is an
                            # audit record, not a claim.
                            epipolar_failures.append(
                                AssociationRejection(
                                    "epipolar",
                                    (obs_key(obs_a), obs_key(obs_b)),
                                    pair_px,
                                )
                            )
                            continue
                        members = [obs_a, obs_b]
                        worst_px = pair_px
                        for cam_c in camera_ids:
                            if cam_c in (cam_a, cam_b):
                                continue
                            best_c: Observation2D | None = None
                            best_px = acfg.consensus_gate_px
                            for obs_c in by_camera[cam_c]:
                                px = _reprojection_px(cameras[cam_c], point, obs_c)
                                if px <= best_px:
                                    best_c, best_px = obs_c, px
                            if best_c is not None:
                                members.append(best_c)
                                worst_px = max(worst_px, best_px)
                        members = _sorted_obs(members)
                        order = (
                            -len(members),
                            round(worst_px / max(acfg.consensus_margin_px, 1e-9)),
                            tuple(obs_key(o) for o in members),
                        )
                        proposals.append((order, members, worst_px))
        out.rejections.extend(epipolar_failures)
        return sorted(proposals, key=lambda item: item[0])

    for _, members, _worst in _propose_direct():
        if any(obs_key(o) in claimed for o in members):
            continue
        # Solve-validated claim: a failing group blocks nothing (its members
        # stay free for later, smaller proposals in this same ordering).
        _accept(members, "direct")

    out.unassigned = [o for o in known if obs_key(o) not in claimed]
    # Observations from cameras without a model are unassignable by design.
    out.unassigned += [
        o for o in _sorted_obs(observations) if o.envelope.camera_id not in cameras
    ]
    return out
