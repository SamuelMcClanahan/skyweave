"""Centroid repeatability and bias harness (seeded, deterministic).

Produces the centroid sigma and bias at a processing resolution, expressed
as full-resolution-equivalent pixels, feeding the verdict against the
0.75 px edge-only tripwire (D2 rule). Modeled until bench-anchored.

Method: a DRIFTING blob with a known per-frame full-resolution truth
position, appearing only after the warm-up schedule, re-imaged through the
D3 sensor model with fresh per-frame noise (mid-bracket until the
conversion-gain bench lands — the verdict is therefore Modeled and
re-checked after). Two harness properties are load-bearing and were found
the hard way:

- the blob must be ABSENT during warm-up, or the background model learns it;
- the drift must be fast enough (>= ~1.5 px/frame for a 5 px blob) that the
  leading-edge intensity change exceeds the model's variance gate — a
  sub-pixel drift changes each pixel too slowly to ever become foreground.

Because the error is measured against each frame's own truth, the drift
contributes nothing to the scatter: the scatter of matched centroids IS the
repeatability, the mean offset is the bias. An optional known jitter is
RENDERED but NOT recorded in the truth (the truth stays on the nominal
drift path), so the injected jitter appears as genuine measurement error by
construction and the harness validates itself (test K4: measured sigma must
recover the injected sigma regardless of how well the detector tracks
sub-pixel motion — recording jittered truth instead would cancel it).

Labels: this harness produces the number that becomes the Measured
covariance floor once the conversion-gain bench anchors the noise scale;
until then every sigma it reports is Modeled (mid-bracket noise).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from skyweave2.detector.config import DetectorConfig
from skyweave2.detector.runner import detect_clip
from skyweave2.eval.clips import write_clip
from skyweave2.sensor_model import SensorModelSpec, TruthCameraOptics, render_frame

_FWHM_TO_SIGMA = 1.0 / 2.354820045


@dataclass(frozen=True)
class RepeatabilityResult:
    sigma_u_px: float  # full-resolution-equivalent, 1-sigma
    sigma_v_px: float
    sigma_px: float  # rms of the two axes
    bias_u_px: float
    bias_v_px: float
    n_matched: int
    n_frames: int
    matched_fraction: float


def build_noise_fixture_clip(
    out_dir: str | Path,
    width: int,
    height: int,
    truth_uv: tuple[float, float],
    fwhm_px: float,
    n_frames: int,
    dataset_seed: int,
    spec: SensorModelSpec | None = None,
    jitter_sigma_px: float = 0.0,
    background_radiance: float = 0.35,
    blob_radiance: float = 1.1,
    drift_uv_per_frame: tuple[float, float] = (1.5, 0.3),
    appear_after: int = 0,
) -> list[tuple[float, float] | None]:
    """Sensor-model fixture: drifting (optionally jittered) blob, fresh
    per-frame noise; blob absent before ``appear_after``.

    Returns the per-frame truth center (None for blob-free frames). Jitter
    positions are drawn from a generator seeded by (dataset_seed, tag) so
    the fixture is deterministic. ``truth_uv`` is the center at the
    appearance frame; drift accumulates from there.
    """
    spec = spec or SensorModelSpec()
    camera = TruthCameraOptics(
        width=width, height=height, fx=4.0 * width, fy=4.0 * width,
        cx=(width - 1) / 2.0, cy=(height - 1) / 2.0,
    )
    sigma = fwhm_px * _FWHM_TO_SIGMA
    v_grid, u_grid = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    jitter_rng = np.random.default_rng(np.random.SeedSequence((dataset_seed, 0x71E7)))
    truths: list[tuple[float, float] | None] = []
    frames: list[np.ndarray] = []
    ae_state = None
    for seq in range(n_frames):
        if seq < appear_after:
            truths.append(None)
            radiance = np.full((height, width), background_radiance)
        else:
            step = seq - appear_after
            cu = truth_uv[0] + drift_uv_per_frame[0] * step
            cv = truth_uv[1] + drift_uv_per_frame[1] * step
            truths.append((cu, cv))  # truth = NOMINAL path; jitter is error
            if jitter_sigma_px > 0.0:
                cu += float(jitter_rng.normal(0.0, jitter_sigma_px))
                cv += float(jitter_rng.normal(0.0, jitter_sigma_px))
            blob = np.exp(
                -0.5 * (((u_grid - cu) / sigma) ** 2 + ((v_grid - cv) / sigma) ** 2)
            )
            radiance = background_radiance + blob_radiance * blob
        y_u8, ae_state = render_frame(
            radiance, camera, spec, dataset_seed, camera_id=0, frame_seq=seq,
            ae_state=ae_state,
        )
        frames.append(y_u8)
    write_clip(frames, out_dir, fps=30.0, source="repeatability-fixture")
    return truths


def measure_repeatability(
    clip_dir: str | Path,
    config: DetectorConfig,
    truths: list[tuple[float, float] | None],
    match_radius_px: float = 8.0,
) -> RepeatabilityResult:
    """Run the detector; scatter of matched centroids = repeatability.

    ``truths`` holds the per-frame FULL-RESOLUTION truth center; frames
    inside the warm-up schedule are skipped. Errors are measured per frame
    against that frame's truth, so injected jitter contributes exactly its
    own sigma (K4's recovery property).
    """
    observations, results = detect_clip(clip_dir, config)
    by_frame: dict[int, list] = {}
    for obs in observations:
        by_frame.setdefault(obs.envelope.frame_seq, []).append(obs)

    du: list[float] = []
    dv: list[float] = []
    n_eligible = 0
    for seq in range(len(truths)):
        if seq < config.warmup_frames or truths[seq] is None:
            continue
        n_eligible += 1
        truth_u, truth_v = truths[seq]
        best = None
        best_d = match_radius_px
        for obs in by_frame.get(seq, []):
            d = float(np.hypot(obs.u - truth_u, obs.v - truth_v))
            if d <= best_d:
                best, best_d = obs, d
        if best is not None:
            du.append(best.u - truth_u)
            dv.append(best.v - truth_v)

    if len(du) < 2:
        raise ValueError(
            f"repeatability needs >= 2 matched frames, got {len(du)} "
            f"of {n_eligible} eligible"
        )
    du_arr, dv_arr = np.asarray(du), np.asarray(dv)
    sigma_u = float(du_arr.std(ddof=1))
    sigma_v = float(dv_arr.std(ddof=1))
    return RepeatabilityResult(
        sigma_u_px=sigma_u,
        sigma_v_px=sigma_v,
        sigma_px=float(np.sqrt(0.5 * (sigma_u**2 + sigma_v**2))),
        bias_u_px=float(du_arr.mean()),
        bias_v_px=float(dv_arr.mean()),
        n_matched=len(du),
        n_frames=n_eligible,
        matched_fraction=len(du) / max(n_eligible, 1),
    )
