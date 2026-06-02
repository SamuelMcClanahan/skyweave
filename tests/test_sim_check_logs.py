import json

from skyweave.cli.sim_check import run_sim_check
from skyweave.config import SimCheckConfig
from skyweave.log import JsonlLogger


def test_sim_check_stage_logs_are_opt_in(tmp_path) -> None:
    config = SimCheckConfig()
    config.simulation.frames = 3
    config.logging.console_every = 10**9
    config.logging.log_dir = str(tmp_path / "logs-default")
    logger = JsonlLogger(config.logging.log_dir, run_name="stage-default")
    try:
        run_sim_check(config, logger, config_path="test-config.yaml")
    finally:
        logger.close()

    default_event_names = [event["event"] for event in _events(logger.path)]
    assert "frame_stage_timings" not in default_event_names

    config.logging.log_stage_timings = True
    config.logging.log_dir = str(tmp_path / "logs-stages")
    logger = JsonlLogger(config.logging.log_dir, run_name="stage-enabled")
    try:
        run_sim_check(config, logger, config_path="test-config.yaml")
    finally:
        logger.close()

    stage_events = [event for event in _events(logger.path) if event["event"] == "frame_stage_timings"]
    assert stage_events
    assert set(stage_events[0]["stage_ms"]) == {
        "alignment",
        "scoring",
        "peaks",
        "triangulation",
        "kalman",
        "total",
    }


def _events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
