from skyweave.sim.check import run_sim_check
from skyweave.config import SimCheckConfig
from skyweave.log import JsonlLogger
from skyweave.recording.recorder import STREAM_FILES, Recorder
from skyweave.recording.replayer import replay_session


def test_record_and_replay_session(tmp_path) -> None:
    config = SimCheckConfig()
    config.simulation.frames = 30
    config.logging.console_every = 10**9
    config.logging.log_dir = str(tmp_path / "logs")

    recorder = Recorder.create(tmp_path / "recordings", config, "test-config.yaml")
    logger = JsonlLogger(config.logging.log_dir, run_name="test-recording")
    try:
        summary = run_sim_check(config, logger, recorder=recorder, config_path="test-config.yaml")
    finally:
        recorder.close()
        logger.close()

    assert summary.passed
    assert (recorder.session_dir / "manifest.json").exists()
    assert (recorder.session_dir / "summary.json").exists()
    for filename in STREAM_FILES.values():
        assert (recorder.session_dir / filename).exists()

    replayed = replay_session(recorder.session_dir)
    assert replayed.passed
    assert replayed.frames == summary.frames
    assert replayed.peak_rmse_m < 0.20
    assert replayed.track_rmse_m < 0.20
