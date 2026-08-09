"""E3: hybrid compositor label centroid matches injected position < 0.1 px."""

from __future__ import annotations

import numpy as np

from skyweave2.eval.clips import iter_frames, read_clip_meta, write_clip
from skyweave2.eval.hybrid import TrajectorySpec, blob_image, composite_clip, position_at
from skyweave2.eval.labels import read_labels
from skyweave2.sensor_model import SensorModelSpec


def _real_clip(tmp_path, n_frames=12, shape=(60, 80)):
    """Stand-in for real footage: mid-gray with mild fixed texture."""
    rng = np.random.default_rng(19)
    texture = rng.integers(90, 110, size=shape).astype(np.uint8)
    frames = [texture.copy() for _ in range(n_frames)]
    clip_dir = tmp_path / "real"
    write_clip(frames, clip_dir, fps=30.0, source="stand-in")
    return clip_dir


def _added_centroid(before: np.ndarray, after: np.ndarray) -> tuple[float, float]:
    diff = after.astype(np.float64) - before.astype(np.float64)
    diff = np.clip(diff, 0.0, None)
    total = diff.sum()
    v, u = np.meshgrid(
        np.arange(diff.shape[0], dtype=np.float64),
        np.arange(diff.shape[1], dtype=np.float64),
        indexing="ij",
    )
    return float((diff * u).sum() / total), float((diff * v).sum() / total)


def test_e3_label_centroid_matches_injection(tmp_path):
    clip_dir = _real_clip(tmp_path)
    trajectory = TrajectorySpec(
        start_frame=2, end_frame=9, u0=25.37, v0=30.81,
        du_per_frame=1.6, dv_per_frame=-0.4, fwhm_px=3.0, peak_dn=70.0,
    )
    out_dir = tmp_path / "hybrid"
    labels = composite_clip(clip_dir, out_dir, trajectory)
    assert len(labels) == 8

    originals = {seq: frame for seq, frame in iter_frames(clip_dir)}
    composited = {seq: frame for seq, frame in iter_frames(out_dir)}
    for label in labels:
        # Label equals the injected trajectory position by construction...
        expected = position_at(trajectory, label.frame_seq)
        assert label.u == expected[0] and label.v == expected[1]
        # ...and the measured centroid of the added intensity confirms the
        # injection actually landed there, within the 0.1 px requirement.
        cu, cv = _added_centroid(originals[label.frame_seq], composited[label.frame_seq])
        err = float(np.hypot(cu - label.u, cv - label.v))
        assert err < 0.1, f"frame {label.frame_seq}: centroid error {err:.4f} px"


def test_e3_labels_file_round_trips(tmp_path):
    clip_dir = _real_clip(tmp_path)
    trajectory = TrajectorySpec(start_frame=0, end_frame=3, u0=40.0, v0=20.0,
                                du_per_frame=2.0)
    out_dir = tmp_path / "hybrid"
    labels = composite_clip(clip_dir, out_dir, trajectory)
    loaded = read_labels(out_dir / "labels.jsonl")
    assert [label.model_dump() for label in loaded] == [
        label.model_dump() for label in labels
    ]
    meta = read_clip_meta(out_dir)
    assert meta.frame_count == 12
    assert meta.source.startswith("hybrid(")


def test_blob_is_psf_matched():
    """Injected blob sigma must widen with the sensor-model PSF."""
    narrow = blob_image((41, 41), (20.0, 20.0), fwhm_px=3.0, peak=60.0, psf_sigma_px=0.0)
    wide = blob_image((41, 41), (20.0, 20.0), fwhm_px=3.0, peak=60.0, psf_sigma_px=1.5)
    assert wide[20, 20] == narrow[20, 20]  # same peak by construction
    assert wide[20, 26] > narrow[20, 26]  # heavier tails when PSF-matched


def test_matched_noise_is_deterministic_and_label_preserving(tmp_path):
    clip_dir = _real_clip(tmp_path)
    trajectory = TrajectorySpec(
        start_frame=1, end_frame=6, u0=30.0, v0=30.0, du_per_frame=1.0,
        add_matched_noise=True, dataset_seed=7,
    )
    labels_a = composite_clip(clip_dir, tmp_path / "a", trajectory)
    labels_b = composite_clip(clip_dir, tmp_path / "b", trajectory)
    assert [la.model_dump() for la in labels_a] == [lb.model_dump() for lb in labels_b]
    for seq in range(1, 7):
        fa = np.load(tmp_path / "a" / f"frame_{seq:06d}.npy")
        fb = np.load(tmp_path / "b" / f"frame_{seq:06d}.npy")
        assert np.array_equal(fa, fb)  # same seed: byte-identical composite


def test_composite_respects_sensor_spec_psf(tmp_path):
    clip_dir = _real_clip(tmp_path)
    trajectory = TrajectorySpec(start_frame=0, end_frame=0, u0=40.0, v0=30.0, fwhm_px=2.0)
    sharp = SensorModelSpec(enable_psf=False)
    soft = SensorModelSpec(enable_psf=True, psf_sigma_px=1.5)
    composite_clip(clip_dir, tmp_path / "sharp", trajectory, sensor_spec=sharp)
    composite_clip(clip_dir, tmp_path / "soft", trajectory, sensor_spec=soft)
    frame_sharp = np.load(tmp_path / "sharp" / "frame_000000.npy").astype(float)
    frame_soft = np.load(tmp_path / "soft" / "frame_000000.npy").astype(float)
    assert frame_soft.sum() > frame_sharp.sum()  # wider Gaussian, same peak
