from __future__ import annotations

from skyweave.calibration.charuco import (
    DEFAULT_CHARUCO_DICTIONARY,
    DEFAULT_CHARUCO_MARKER_MM,
    DEFAULT_CHARUCO_SQUARES_X,
    DEFAULT_CHARUCO_SQUARES_Y,
    DEFAULT_CHARUCO_SQUARE_MM,
    CharucoBoardSpec,
    dictionary_candidates,
)
from skyweave.cli.charuco_check import DetectionStats, _percentile
from skyweave.cli.charuco_live import (
    LiveState,
    _display_host,
    _fps_from_times,
    _html_page,
    _parse_devices,
    _start_status_logger,
)


def test_dictionary_candidates_expand_calib_io_family_names() -> None:
    assert dictionary_candidates("DICT_4X4") == [
        "DICT_4X4_50",
        "DICT_4X4_100",
        "DICT_4X4_250",
        "DICT_4X4_1000",
    ]
    assert dictionary_candidates("5x5") == [
        "DICT_5X5_50",
        "DICT_5X5_100",
        "DICT_5X5_250",
        "DICT_5X5_1000",
    ]
    assert dictionary_candidates("DICT_5X5_1000") == ["DICT_5X5_1000"]


def test_charuco_board_spec_stores_metric_lengths() -> None:
    spec = CharucoBoardSpec(
        squares_x=10,
        squares_y=7,
        square_length_m=0.024,
        marker_length_m=0.018,
        dictionary="DICT_4X4",
    )

    assert spec.squares_x == 10
    assert spec.squares_y == 7
    assert spec.square_length_m == 0.024
    assert spec.marker_length_m == 0.018


def test_default_charuco_board_spec_matches_measured_print() -> None:
    assert DEFAULT_CHARUCO_SQUARES_X == 8
    assert DEFAULT_CHARUCO_SQUARES_Y == 6
    assert DEFAULT_CHARUCO_SQUARE_MM == 30.0
    assert DEFAULT_CHARUCO_MARKER_MM == 21.6
    assert DEFAULT_CHARUCO_DICTIONARY == "DICT_4X4"


def test_detection_stats_track_best_frame_and_rate() -> None:
    stats = DetectionStats()

    assert stats.record(frame_seq=4, dictionary="DICT_4X4_50", corner_count=3, marker_count=5, latency_ms=2.0)
    assert stats.record(frame_seq=5, dictionary="DICT_4X4_100", corner_count=8, marker_count=7, latency_ms=1.0)
    assert not stats.record(frame_seq=6, dictionary="DICT_4X4_250", corner_count=7, marker_count=9, latency_ms=3.0)

    assert stats.frames == 3
    assert stats.detected_frames == 3
    assert stats.detection_rate == 1.0
    assert stats.best_corners == 8
    assert stats.best_markers == 7
    assert stats.best_dictionary == "DICT_4X4_100"
    assert stats.best_frame_seq == 5
    assert _percentile(stats.latencies_ms, 50.0) == 2.0


def test_charuco_live_helpers_are_stable(tmp_path) -> None:
    state = LiveState(devices=["/dev/video0", "/dev/video2"])

    assert _display_host("0.0.0.0") == "10.42.0.111"
    assert _display_host("127.0.0.1") == "127.0.0.1"
    assert _fps_from_times([1.0, 1.5, 2.0]) == 2.0
    assert "SKYWEAVE CHARUCO LIVE" in _html_page()
    snapshot = state.snapshot()
    assert snapshot["best_corner_count"] == 0
    assert snapshot["sharpness"] == 0.0
    assert snapshot["best_sharpness"] == 0.0
    assert snapshot["stale_age_ms"] == 0.0
    assert len(snapshot["cameras"]) == 2
    assert state.request_camera(1)
    assert not state.request_camera(3)
    assert state.snapshot()["requested_index"] == 1
    assert _parse_devices(None, "/dev/video0,/dev/video2") == ["/dev/video0", "/dev/video2"]

    with state.condition:
        state.running = False
        state.condition.notify_all()
    log_path = tmp_path / "charuco_live.jsonl"
    thread = _start_status_logger(state, str(log_path), 0.001)
    assert thread is not None
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert '"running": false' in log_path.read_text(encoding="utf-8")
