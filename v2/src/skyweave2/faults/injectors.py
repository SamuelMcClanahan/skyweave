"""Fault injectors for Tiers II (detection), III (model), IV (time/stream).

Every injector is a pure function of (input, magnitude, seed) and returns
both the faulted data AND the ledger of what it planted, so bookkeeping can
attribute each injected item to the layer that terminates it. An injector
that reported a plant it did not make would corrupt the whole campaign, so
each has a can-fail fixture (N1) proving the fault is really present.

Magnitudes are passed in by the campaign from the frozen manifest; nothing
here has a default magnitude.
"""

from __future__ import annotations

import uuid
import zlib
from dataclasses import dataclass, field

import numpy as np

from skyweave2.contracts import CameraModel, Observation2D
from skyweave2.fusion.aligner import obs_key


@dataclass
class PlantLedger:
    """What an injector planted, keyed the way bookkeeping sees it."""

    # obs_key -> label ("false_blob", "split", "merged", "biased", ...)
    planted: dict[str, str] = field(default_factory=dict)
    # obs_keys of TRUE observations removed from the stream
    deleted: list[str] = field(default_factory=list)
    # obs_keys of TRUE observations perturbed in place (still true items)
    perturbed: dict[str, str] = field(default_factory=dict)
    # Injected COPIES of true observations. A verbatim duplicate shares its
    # obs_key with the original by construction, so it can never go in
    # `planted` (that would brand the true original a false item and invert
    # the false-accept rate — D6 review, critical). Counted, not keyed.
    duplicated: list[str] = field(default_factory=list)
    notes: dict[str, float] = field(default_factory=dict)

    def merge(self, other: PlantLedger) -> None:
        self.planted.update(other.planted)
        self.deleted.extend(other.deleted)
        self.perturbed.update(other.perturbed)
        self.duplicated.extend(other.duplicated)
        self.notes.update(other.notes)


def _stable_tag(tag: int | str) -> int:
    """Process-stable integer for a seed tag.

    Python's ``hash()`` on str is salted per process (PYTHONHASHSEED), so
    using it here made the campaign non-reproducible ACROSS RUNS while
    looking deterministic within one — caught by the report's own
    byte-identity gate. zlib.crc32 is stable forever.
    """
    if isinstance(tag, int):
        return tag
    return zlib.crc32(tag.encode("utf-8"))


def _rng(seed: int, *tags: int | str) -> np.random.Generator:
    entropy = [seed] + [_stable_tag(tag) for tag in tags]
    return np.random.default_rng(np.random.SeedSequence(entropy))


def _replace(obs: Observation2D, **updates) -> Observation2D:
    return obs.model_copy(update=updates)


# ---------------------------------------------------------------------------
# Tier II: detection-stream faults
# ---------------------------------------------------------------------------


def inject_false_blobs(
    observations: list[Observation2D],
    per_camera_frame: float,
    seed: int,
    cameras: dict[int, CameraModel],
) -> tuple[list[Observation2D], PlantLedger]:
    """Add Poisson(per_camera_frame) spurious detections per camera-frame."""
    ledger = PlantLedger()
    if per_camera_frame <= 0.0:
        return list(observations), ledger
    by_frame: dict[tuple[int, int], list[Observation2D]] = {}
    for obs in observations:
        by_frame.setdefault((obs.envelope.camera_id, obs.envelope.frame_seq), []).append(obs)
    out = list(observations)
    for (camera_id, frame_seq), members in sorted(by_frame.items()):
        rng = _rng(seed, camera_id, frame_seq, "false")
        count = int(rng.poisson(per_camera_frame))
        template = members[0]
        max_obs_id = max(o.obs_id for o in members)
        for k in range(count):
            width = template.envelope.full_width
            height = template.envelope.full_height
            u = float(rng.uniform(0.05 * width, 0.95 * width))
            v = float(rng.uniform(0.05 * height, 0.95 * height))
            fake = _replace(
                template,
                obs_id=max_obs_id + 1 + k,
                u=u,
                v=v,
                local_blob_id=None,
            )
            out.append(fake)
            ledger.planted[obs_key(fake)] = "false_blob"
    # Arrival order matters: appending plants after the whole clip made the
    # aligner kill 97-100% of them as "late", so the clutter axis never
    # reached layers 2-7 (D6 review, critical). Deliver in capture order.
    out.sort(key=lambda o: (o.envelope.capture_ts_ns, o.envelope.camera_id, o.obs_id))
    return out, ledger


def inject_deleted_detections(
    observations: list[Observation2D], pct: float, seed: int
) -> tuple[list[Observation2D], PlantLedger]:
    """Delete pct% of detections uniformly at random."""
    ledger = PlantLedger()
    if pct <= 0.0:
        return list(observations), ledger
    out = []
    for obs in observations:
        rng = _rng(seed, obs.envelope.camera_id, obs.envelope.frame_seq, obs.obs_id)
        if float(rng.uniform(0.0, 100.0)) < pct:
            ledger.deleted.append(obs_key(obs))
            continue
        out.append(obs)
    return out, ledger


def inject_split_blobs(
    observations: list[Observation2D], pct: float, seed: int, split_px: float = 6.0
) -> tuple[list[Observation2D], PlantLedger]:
    """Replace pct% of detections with two half-area detections astride the
    original centroid (the classic mask-fragmentation fault)."""
    ledger = PlantLedger()
    if pct <= 0.0:
        return list(observations), ledger
    out = []
    for obs in observations:
        rng = _rng(seed, obs.envelope.camera_id, obs.envelope.frame_seq, obs.obs_id, "split")
        if float(rng.uniform(0.0, 100.0)) >= pct:
            out.append(obs)
            continue
        offset = split_px / 2.0
        area = max(obs.area_px // 2, 1)
        left = _replace(obs, u=obs.u - offset, area_px=area)
        right = _replace(obs, obs_id=obs.obs_id + 1000, u=obs.u + offset, area_px=area)
        out.extend([left, right])
        ledger.planted[obs_key(left)] = "split"
        ledger.planted[obs_key(right)] = "split"
        ledger.deleted.append(obs_key(obs))
    return out, ledger


def inject_merged_blobs(
    observations: list[Observation2D], pct: float, seed: int, merge_px: float = 10.0
) -> tuple[list[Observation2D], PlantLedger]:
    """Merge a detection with a nearby planted neighbour: the surviving
    detection sits at the area-weighted midpoint (centroid pulled off truth)."""
    ledger = PlantLedger()
    if pct <= 0.0:
        return list(observations), ledger
    out = []
    for obs in observations:
        rng = _rng(seed, obs.envelope.camera_id, obs.envelope.frame_seq, obs.obs_id, "merge")
        if float(rng.uniform(0.0, 100.0)) >= pct:
            out.append(obs)
            continue
        merged = _replace(
            obs,
            u=obs.u + merge_px / 2.0,
            area_px=obs.area_px * 2,
            bbox_w=obs.bbox_w * 2,
        )
        out.append(merged)
        ledger.perturbed[obs_key(merged)] = "merged"
        ledger.notes[f"merge_shift_px:{obs_key(merged)}"] = merge_px / 2.0
    return out, ledger


def inject_centroid_bias(
    observations: list[Observation2D],
    constant_px: float,
    slow_varying_px: float,
    fps: float,
    slow_period_s: float,
) -> tuple[list[Observation2D], PlantLedger]:
    """Add a constant and/or slow sinusoidal centroid bias (u axis).

    ``slow_period_s`` comes from the manifest comment (period 5 s); the
    campaign passes it, this function never assumes it.
    """
    ledger = PlantLedger()
    if constant_px == 0.0 and slow_varying_px == 0.0:
        return list(observations), ledger
    out = []
    for obs in observations:
        t = obs.envelope.frame_seq / fps
        shift = constant_px + slow_varying_px * float(
            np.sin(2.0 * np.pi * t / slow_period_s)
        )
        biased = _replace(obs, u=obs.u + shift)
        out.append(biased)
        ledger.perturbed[obs_key(biased)] = "centroid_bias"
        ledger.notes[f"bias_px:{obs_key(biased)}"] = shift
    return out, ledger


# ---------------------------------------------------------------------------
# Tier III: model (estimator calibration) faults — truth untouched
# ---------------------------------------------------------------------------


def _rotation(axis: np.ndarray, angle_rad: float) -> np.ndarray:
    kx, ky, kz = axis
    k = np.array([[0.0, -kz, ky], [kz, 0.0, -kx], [-ky, kx, 0.0]])
    return np.eye(3) + np.sin(angle_rad) * k + (1.0 - np.cos(angle_rad)) * (k @ k)


def perturb_cameras(
    cameras: dict[int, CameraModel],
    seed: int,
    rotation_err_deg: float = 0.0,
    position_err_m: float = 0.0,
    focal_err_pct: float = 0.0,
    principal_point_err_px: float = 0.0,
    distortion: tuple[float, float, float, float, float] | None = None,
) -> tuple[dict[int, CameraModel], PlantLedger]:
    """Perturb the ESTIMATOR's calibration only (truth/world untouched)."""
    ledger = PlantLedger()
    out: dict[int, CameraModel] = {}
    for camera_id in sorted(cameras):
        camera = cameras[camera_id]
        rng = _rng(seed, camera_id, "calib")
        t = np.array(camera.t_world_cam, dtype=np.float64)
        if rotation_err_deg > 0.0:
            axis = rng.normal(size=3)
            axis /= np.linalg.norm(axis)
            t[:3, :3] = _rotation(axis, np.radians(rotation_err_deg)) @ t[:3, :3]
        if position_err_m > 0.0:
            direction = rng.normal(size=3)
            direction /= np.linalg.norm(direction)
            t[:3, 3] += position_err_m * direction
        k = np.array(camera.k, dtype=np.float64)
        if focal_err_pct != 0.0:
            k[0, 0] *= 1.0 + focal_err_pct / 100.0
            k[1, 1] *= 1.0 + focal_err_pct / 100.0
        if principal_point_err_px > 0.0:
            offset = rng.normal(size=2)
            offset /= np.linalg.norm(offset)
            k[0, 2] += principal_point_err_px * offset[0]
            k[1, 2] += principal_point_err_px * offset[1]
        out[camera_id] = CameraModel(
            camera_id=camera.camera_id,
            width=camera.width,
            height=camera.height,
            k=tuple(tuple(float(v) for v in row) for row in k),
            dist=tuple(distortion) if distortion is not None else camera.dist,
            t_world_cam=tuple(tuple(float(v) for v in row) for row in t),
            calibration_rev=camera.calibration_rev,
        )
        ledger.notes[f"calib_rot_deg:{camera_id}"] = rotation_err_deg
        ledger.notes[f"calib_pos_m:{camera_id}"] = position_err_m
    return out, ledger


def inject_rolling_shutter(
    observations: list[Observation2D], readout_ms: float
) -> tuple[list[Observation2D], PlantLedger]:
    """Per-row capture-time shift: a detection's true event time depends on
    its image row. Applied as a capture_ts_ns shift, exactly what a rolling
    sensor does to the D0 exposure-midpoint claim."""
    ledger = PlantLedger()
    if readout_ms <= 0.0:
        return list(observations), ledger
    out = []
    for obs in observations:
        row_fraction = obs.v / max(obs.envelope.full_height - 1, 1)
        shift_ns = int(round(row_fraction * readout_ms * 1e6))
        envelope = obs.envelope.model_copy(
            update={"capture_ts_ns": obs.envelope.capture_ts_ns + shift_ns,
                    "line_readout_us": readout_ms * 1000.0
                    / max(obs.envelope.full_height, 1)}
        )
        shifted = _replace(obs, envelope=envelope)
        out.append(shifted)
        ledger.perturbed[obs_key(shifted)] = "rolling_shutter"
        ledger.notes[f"rs_shift_ns:{obs_key(shifted)}"] = float(shift_ns)
    return out, ledger


# ---------------------------------------------------------------------------
# Tier IV: time / stream faults at the aligner boundary
# ---------------------------------------------------------------------------


def inject_clock_error(
    observations: list[Observation2D],
    offset_ms: float,
    drift_ppm: float,
    jitter_ms_sigma: float,
    seed: int,
    honest: bool,
    dishonest_claim_ms: float,
    camera_ids: tuple[int, ...] | None = None,
) -> tuple[list[Observation2D], PlantLedger]:
    """Offset/drift/jitter a camera's clock, declaring the error honestly or
    dishonestly in ``time_sync_error_ms`` (the brief's honesty rule)."""
    ledger = PlantLedger()
    if offset_ms == 0.0 and drift_ppm == 0.0 and jitter_ms_sigma == 0.0:
        return list(observations), ledger
    out = []
    for obs in observations:
        camera_id = obs.envelope.camera_id
        if camera_ids is not None and camera_id not in camera_ids:
            out.append(obs)
            continue
        ts = obs.envelope.capture_ts_ns
        shift = offset_ms * 1e6 + ts * drift_ppm * 1e-6
        if jitter_ms_sigma > 0.0:
            rng = _rng(seed, camera_id, obs.envelope.frame_seq, "jitter")
            shift += float(rng.normal(0.0, jitter_ms_sigma * 1e6))
        magnitude_ms = abs(shift) / 1e6
        declared = magnitude_ms if honest else dishonest_claim_ms
        envelope = obs.envelope.model_copy(
            update={"capture_ts_ns": int(round(ts + shift)),
                    "time_sync_error_ms": float(declared)}
        )
        faulted = _replace(obs, envelope=envelope)
        out.append(faulted)
        ledger.perturbed[obs_key(faulted)] = "clock"
        ledger.notes[f"clock_shift_ns:{obs_key(faulted)}"] = float(shift)
    return out, ledger


def inject_observation_loss(
    observations: list[Observation2D],
    pct: float,
    mode: str,
    seed: int,
    burst_len: int = 10,
) -> tuple[list[Observation2D], PlantLedger]:
    """Drop pct% of observations, uniformly (``random``) or in bursts."""
    ledger = PlantLedger()
    if pct <= 0.0:
        return list(observations), ledger
    if mode not in ("random", "burst"):
        raise ValueError(f"unknown loss mode {mode!r}")
    out = []
    if mode == "random":
        for obs in observations:
            rng = _rng(seed, obs.envelope.camera_id, obs.envelope.frame_seq, obs.obs_id, "loss")
            if float(rng.uniform(0.0, 100.0)) < pct:
                ledger.deleted.append(obs_key(obs))
                continue
            out.append(obs)
        return out, ledger
    # burst: contiguous frame ranges per camera
    frames_by_camera: dict[int, list[int]] = {}
    for obs in observations:
        frames_by_camera.setdefault(obs.envelope.camera_id, []).append(
            obs.envelope.frame_seq
        )
    dropped: set[tuple[int, int]] = set()
    for camera_id, frames in sorted(frames_by_camera.items()):
        unique = sorted(set(frames))
        n_drop = int(round(len(unique) * pct / 100.0))
        rng = _rng(seed, camera_id, "burst")
        remaining = n_drop
        while remaining > 0 and unique:
            start = int(rng.integers(0, len(unique)))
            length = min(burst_len, remaining, len(unique) - start)
            for f in unique[start : start + length]:
                dropped.add((camera_id, f))
            remaining -= length
    for obs in observations:
        if (obs.envelope.camera_id, obs.envelope.frame_seq) in dropped:
            ledger.deleted.append(obs_key(obs))
            continue
        out.append(obs)
    return out, ledger


def inject_reorder(
    observations: list[Observation2D], seed: int, window: int = 12
) -> tuple[list[Observation2D], PlantLedger]:
    """Shuffle ARRIVAL order within a sliding window (event time untouched)."""
    ledger = PlantLedger()
    out: list[Observation2D] = []
    rng = _rng(seed, "reorder")
    block: list[Observation2D] = []
    for obs in observations:
        block.append(obs)
        if len(block) >= window:
            order = rng.permutation(len(block))
            out.extend(block[i] for i in order)
            block = []
    if block:
        order = rng.permutation(len(block))
        out.extend(block[i] for i in order)
    ledger.notes["reordered"] = float(len(out))
    return out, ledger


def inject_duplication(
    observations: list[Observation2D], seed: int, pct: float = 5.0
) -> tuple[list[Observation2D], PlantLedger]:
    """Duplicate pct% of observations verbatim (transport retransmission)."""
    ledger = PlantLedger()
    out = []
    for obs in observations:
        out.append(obs)
        rng = _rng(seed, obs.envelope.camera_id, obs.envelope.frame_seq, obs.obs_id, "dup")
        if float(rng.uniform(0.0, 100.0)) < pct:
            out.append(obs)
            ledger.duplicated.append(obs_key(obs))
    return out, ledger


def inject_lateness(
    observations: list[Observation2D], seed: int, pct: float = 5.0,
    delay_batches: int = 30, batch_period_ns: int = 33_333_333,
) -> tuple[list[Observation2D], PlantLedger]:
    """Move pct% of observations LATE in arrival order (event time intact),
    far enough back that the aligner's lateness policy must catch them."""
    ledger = PlantLedger()
    normal, late = [], []
    for obs in observations:
        rng = _rng(seed, obs.envelope.camera_id, obs.envelope.frame_seq, obs.obs_id, "late")
        if float(rng.uniform(0.0, 100.0)) < pct:
            late.append(obs)
            # A delayed TRUE observation is perturbed, not planted: booking
            # it as a plant inverted the false-accept accounting on this
            # axis (D6 review).
            ledger.perturbed[obs_key(obs)] = "late"
        else:
            normal.append(obs)
    # Append the late ones at the very end of the arrival order.
    _ = delay_batches, batch_period_ns
    return normal + late, ledger


def inject_node_restart(
    observations: list[Observation2D], restart_at_s: float, fps: float,
    camera_id: int = 0,
) -> tuple[list[Observation2D], PlantLedger]:
    """Mid-clip node reboot: new session_uuid and frame_seq reset to 0.

    Event times are NOT rewound — a reboot changes identity, not physics.
    """
    ledger = PlantLedger()
    restart_frame = int(round(restart_at_s * fps))
    new_session = str(uuid.uuid5(uuid.NAMESPACE_URL, f"d6-restart:{camera_id}"))
    out = []
    for obs in observations:
        if obs.envelope.camera_id == camera_id and obs.envelope.frame_seq >= restart_frame:
            envelope = obs.envelope.model_copy(
                update={"session_uuid": new_session,
                        "frame_seq": obs.envelope.frame_seq - restart_frame}
            )
            rebooted = _replace(obs, envelope=envelope)
            out.append(rebooted)
            ledger.perturbed[obs_key(rebooted)] = "node_restart"
        else:
            out.append(obs)
    ledger.notes["restart_frame"] = float(restart_frame)
    return out, ledger


def inject_camera_dropout(
    observations: list[Observation2D], camera_id: int, from_s: float, fps: float
) -> tuple[list[Observation2D], PlantLedger]:
    """Total loss of one camera from ``from_s`` to the end of the clip."""
    ledger = PlantLedger()
    cutoff = int(round(from_s * fps))
    out = []
    for obs in observations:
        if obs.envelope.camera_id == camera_id and obs.envelope.frame_seq >= cutoff:
            ledger.deleted.append(obs_key(obs))
            continue
        out.append(obs)
    ledger.notes["dropout_from_frame"] = float(cutoff)
    return out, ledger
