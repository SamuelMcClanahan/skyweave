import math

from skyweave.cli.benchmark import STAGES, run_benchmark


def test_benchmark_collects_stage_timings() -> None:
    result = run_benchmark("configs/sim.yaml", frames=4, warmup=1)

    assert result.frames == 4
    assert result.warmup == 1
    assert result.measurement_frames == 4
    assert math.isfinite(result.peak_rmse_m)
    assert math.isfinite(result.track_rmse_m)
    for stage in STAGES:
        assert len(result.stage_ms[stage]) == 4
        assert all(value >= 0.0 for value in result.stage_ms[stage])
