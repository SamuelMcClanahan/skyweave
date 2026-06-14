import json

from skyweave.config import SimCheckConfig
from skyweave.viz.exporter import export_synthetic_viz_bundle


def test_export_synthetic_viz_bundle_writes_contract_files(tmp_path) -> None:
    config = SimCheckConfig()
    config.simulation.frames = 6
    config.logging.console_every = 10**9

    bundle = export_synthetic_viz_bundle(config, tmp_path / "viz", config_path="test-config.yaml", max_frames=4)

    for name in (
        "manifest.json",
        "grid.json",
        "cameras.json",
        "frames.jsonl",
        "weavefields.jsonl",
        "measurements.jsonl",
        "tracks.jsonl",
        "summary.json",
    ):
        assert (bundle / name).exists()

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "skyweave_viz_bundle"

    cameras = json.loads((bundle / "cameras.json").read_text(encoding="utf-8"))
    assert len(cameras) == 3
    assert "rotation_quat" in cameras[0]["viz"]

    frames = [json.loads(line) for line in (bundle / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(frames) == 4
    assert frames[0]["frame_seq"] == 0
    assert frames[0]["cameras"]
    assert "truth_position" in frames[0]
    assert "latency_ms" in frames[0]["stats"]
