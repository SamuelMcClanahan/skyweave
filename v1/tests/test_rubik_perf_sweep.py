from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_sweep_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "rubik_perf_sweep.py"
    spec = importlib.util.spec_from_file_location("rubik_perf_sweep", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sweep_parser_coerces_ms_fields_to_floats() -> None:
    sweep = _load_sweep_module()

    parsed = sweep._parse_output(
        "\n".join(
            [
                "camera_check_live camera_id=0 frames=20 read_failures=0 effective_fps=103.46 packet_latency_p95=9.181ms",
                "live_benchmark_run frames=20 total_p50=9.53ms read_failures=0 aligned=20 measurements=20",
                "stage,p50_ms,p95_ms,p99_ms,total_ms,share_pct",
                "total,9.528,10.827,14.064,194.576,100.0",
            ]
        )
    )

    camera = parsed["cameras"][0]
    live = parsed["live"]
    total_stage = parsed["voxel_stages"]["total"]
    assert camera["packet_latency_p95"] == 9.181
    assert isinstance(camera["packet_latency_p95"], float)
    assert live["total_p50"] == 9.53
    assert isinstance(live["total_p50"], float)
    assert total_stage["p99_ms"] == 14.064
