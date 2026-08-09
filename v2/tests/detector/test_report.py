"""Report generator: determinism and content on small fixtures."""

from __future__ import annotations

from skyweave2.detector.config import DetectorConfig
from skyweave2.detector.report import generate_report
from skyweave2.eval.clips import write_clip
from skyweave2.eval.labels import TruthLabel
from tests.detector.conftest import FULL_H, FULL_W, flat_frame, gaussian_blob_frame


def _small_base_config() -> DetectorConfig:
    return DetectorConfig(warmup_frames=10, persistence_frames=2)


def _fixture_clips(tmp_path):
    gate_frames = []
    labels = []
    for seq in range(45):
        if seq >= 15:
            center = (80.0 + 2.0 * (seq - 15), 120.0)
            gate_frames.append(gaussian_blob_frame(center))
            labels.append(TruthLabel(frame_seq=seq, u=center[0], v=center[1]))
        else:
            gate_frames.append(flat_frame())
    gate_dir = tmp_path / "gate"
    write_clip(gate_frames, gate_dir, fps=30.0, source="gate-fixture")
    negative_dir = tmp_path / "negative"
    write_clip([flat_frame() for _ in range(45)], negative_dir, fps=30.0,
               source="negative-fixture")
    return gate_dir, negative_dir, labels


def test_report_generates_and_is_deterministic(tmp_path, monkeypatch):
    import skyweave2.detector.report as report_mod

    # Fixture clips are 384x216: shrink the swept resolutions and the
    # repeatability fixture proportionally so the test stays fast.
    monkeypatch.setattr(report_mod, "RESOLUTIONS",
                        ((FULL_W, FULL_H), (256, 144), (192, 108)))
    monkeypatch.setattr(report_mod, "PRIMARY", (256, 144))
    gate_dir, negative_dir, labels = _fixture_clips(tmp_path)
    base = _small_base_config()

    a = generate_report(str(gate_dir), str(negative_dir), labels, seed=7,
                        base_config=base, scratch_dir=tmp_path / "s1")
    b = generate_report(str(gate_dir), str(negative_dir), labels, seed=7,
                        base_config=base, scratch_dir=tmp_path / "s2")
    assert a == b  # byte-identical for identical inputs and seed
    for heading in (
        "## Scorecards: gate clip",
        "## False proposals: negative clip",
        "## Centroid repeatability",
        "### Tripwire verdict",
        "## Warm-up",
    ):
        assert heading in a
    assert "Modeled" in a
    assert ("FIRES" in a) != ("CLEAR" in a)  # exactly one verdict


def test_report_evidence_jsons_written(tmp_path, monkeypatch):
    import skyweave2.detector.report as report_mod

    monkeypatch.setattr(report_mod, "RESOLUTIONS", ((256, 144),))
    monkeypatch.setattr(report_mod, "PRIMARY", (256, 144))
    gate_dir, negative_dir, labels = _fixture_clips(tmp_path)
    evidence = tmp_path / "evidence"
    generate_report(str(gate_dir), str(negative_dir), labels, seed=7,
                    base_config=_small_base_config(), scratch_dir=tmp_path / "s",
                    evidence_dir=evidence)
    names = sorted(p.name for p in evidence.glob("*.json"))
    assert names == [
        "ive_approx_gate_256x144.json",
        "ive_approx_negative_256x144.json",
        "mog2_gate_256x144.json",
        "mog2_negative_256x144.json",
    ]
