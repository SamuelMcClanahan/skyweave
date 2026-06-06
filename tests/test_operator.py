from __future__ import annotations

import json
from pathlib import Path

import yaml

from skyweave.calibration.charuco import CharucoBoardSpec
from skyweave.operator.calibration import load_extrinsic_camera_calibs, scale_camera_calibs
from skyweave.operator.profiles import load_profile, normalize_profile_name, save_profile
from skyweave.operator.runtime import OperatorRuntime
from skyweave.operator.state import OperatorState, PipelineStatus


def test_operator_state_updates_settings_tracking_and_room(tmp_path: Path) -> None:
    state = OperatorState(
        devices=["/dev/video4", "/dev/video2"],
        labels=["cam1", "cam2"],
        config_path="configs/sim.yaml",
        extrinsics_path="configs/extrinsics.yaml",
        profile_dir=tmp_path,
    )

    snapshot = state.apply_payload(
        {
            "camera": {"fps": 60, "width": 640, "height": 480},
            "motion": {"threshold": 48, "backend": "python"},
            "kalman": {"sigma_accel_mps2": 3.5},
            "tracking": {"requested_mode": "stress"},
            "room": {"mesh_url": "/room-assets/room.glb", "translation_m": [1, 2, 3]},
        }
    )

    assert snapshot["settings"]["camera"]["fps"] == 60
    assert snapshot["settings"]["motion"]["threshold"] == 48
    assert snapshot["settings"]["kalman"]["sigma_accel_mps2"] == 3.5
    assert snapshot["tracking"]["requested_mode"] == "stress"
    assert snapshot["tracking"]["revision"] == 1
    assert snapshot["room"]["mesh_url"] == "/room-assets/room.glb"
    assert snapshot["cameras"][0]["label"] == "cam1"

    room_only = state.apply_payload({"room": {"opacity": 0.5}})
    assert room_only["tracking"]["revision"] == 1


def test_operator_profiles_round_trip(tmp_path: Path) -> None:
    state = OperatorState(
        devices=["/dev/video0"],
        labels=["cam1"],
        config_path="configs/sim.yaml",
        extrinsics_path="configs/extrinsics.yaml",
        profile_dir=tmp_path,
    )
    state.apply_payload({"camera": {"fps": 45}, "tracking": {"requested_mode": "stress"}})

    saved = save_profile(state, "room-test")
    state.apply_payload({"camera": {"fps": 20}, "tracking": {"requested_mode": "auto"}})
    loaded = load_profile(state, "room-test")

    assert saved["name"] == "room-test"
    assert loaded["status"]["settings"]["camera"]["fps"] == 45
    assert loaded["status"]["tracking"]["requested_mode"] == "stress"
    assert normalize_profile_name("room-test.yaml") == "room-test"


def test_operator_rejects_unsafe_profile_names() -> None:
    try:
        normalize_profile_name("../escape")
    except ValueError as exc:
        assert "profile name" in str(exc)
    else:
        raise AssertionError("unsafe profile name should fail")


def test_extrinsics_loader_builds_scaled_camera_calibs(tmp_path: Path) -> None:
    intrinsics = tmp_path / "intrinsics_cam1.yaml"
    intrinsics.write_text(
        yaml.safe_dump(
            {
                "label": "cam1",
                "image_width": 64,
                "image_height": 48,
                "camera_matrix": [[32.0, 0.0, 32.0], [0.0, 32.0, 24.0], [0.0, 0.0, 1.0]],
                "dist_coeffs": [0, 0, 0, 0, 0],
            }
        ),
        encoding="utf-8",
    )
    extrinsics = tmp_path / "extrinsics.yaml"
    extrinsics.write_text(
        yaml.safe_dump(
            {
                "rms_reprojection_error_px": 0.42,
                "cameras": [
                    {
                        "camera_id": 0,
                        "label": "cam1",
                        "device": "/dev/video4",
                        "image_width": 64,
                        "image_height": 48,
                        "intrinsics_file": str(intrinsics),
                        "T_world_cam": [[1, 0, 0, 1], [0, 1, 0, 2], [0, 0, 1, 3], [0, 0, 0, 1]],
                        "rms_reprojection_error_px": 0.42,
                        "max_reprojection_error_px": 0.9,
                        "t_world_cam_m": [1, 2, 3],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cameras, status = load_extrinsic_camera_calibs(extrinsics)
    scaled = scale_camera_calibs(cameras, 128, 96)

    assert status.loaded
    assert status.camera_count == 1
    assert cameras[0].position.tolist() == [1, 2, 3]
    assert scaled[0].width == 128
    assert scaled[0].height == 96
    assert scaled[0].K[0, 0] == 64.0
    assert scaled[0].K[1, 1] == 64.0


def test_runtime_build_pipeline_auto_falls_back_to_stress_without_extrinsics(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "simulation": {
                    "frames": 4,
                    "image_width": 32,
                    "image_height": 24,
                    "focal_length_px": 18,
                    "patch_size_px": 3,
                },
                "rayweave": {
                    "grid": {"origin_m": [-1, -1, 0], "dims": [8, 8, 6], "voxel_size_m": 0.25},
                    "scorer": {"backend": "python_numpy", "min_supporting_cameras": 2, "top_k_voxels": 50},
                },
            }
        ),
        encoding="utf-8",
    )
    state = OperatorState(
        devices=["/dev/video0"],
        labels=["cam1"],
        config_path=str(config),
        extrinsics_path=str(tmp_path / "missing.yaml"),
        profile_dir=tmp_path / "profiles",
        requested_mode="auto",
    )
    runtime = OperatorRuntime(state, CharucoBoardSpec(8, 6, 0.03, 0.0216, "DICT_4X4"))

    pipeline = runtime._build_pipeline(state.live.settings_snapshot()[0])

    assert pipeline.effective_mode == "stress"
    assert "fallback" in pipeline.reason
    assert state.snapshot()["calibration"]["loaded"] is False


def test_operator_recording_writes_events_and_images(tmp_path: Path) -> None:
    state = OperatorState(
        devices=["/dev/video4"],
        labels=["cam1"],
        config_path="configs/sim.yaml",
        extrinsics_path="configs/extrinsics.yaml",
        profile_dir=tmp_path / "profiles",
        record_dir=tmp_path / "operator_recordings",
    )
    state.live.record_camera_frame(
        camera_index=0,
        frame_seq=12,
        detection_dictionary="none",
        marker_count=0,
        corner_count=0,
        latency_ms=1.0,
        sharpness=10.0,
        capture_fps=20.0,
        frame_jpeg=b"\xff\xd8\xff\xd9",
    )
    state.set_pipeline(PipelineStatus(mode="real", frame_seq=7, aligned=True, packet_count=1, blob_count=1))

    started = state.start_recording("throw-test")
    state.set_viz_frame(
        {
            "frame_seq": 7,
            "ts_ns": 123,
            "tracks": [{"id": 1}],
            "measurements": [{"position": [1, 2, 3]}],
            "stats": {"fps": 20},
        }
    )
    stopped = state.stop_recording()

    session_dir = Path(started["output_dir"])
    assert stopped["frame_count"] == 1
    assert stopped["image_count"] == 1
    assert (session_dir / "manifest.json").exists()
    assert (session_dir / "summary.json").exists()
    events = (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    payload = json.loads(events[0])
    assert payload["pipeline"]["blob_count"] == 1
    assert payload["images"][0]["camera_index"] == 0
    assert (session_dir / payload["images"][0]["path"]).exists()

    snapshot = state.save_recording_snapshot("single-frame")
    snapshot_dir = Path(snapshot["output_dir"])
    assert (snapshot_dir / "snapshot.json").exists()
    assert snapshot["image_count"] == 1
