from __future__ import annotations

import argparse
from pathlib import Path

from skyweave.calibration.charuco import (
    DEFAULT_CHARUCO_DICTIONARY,
    DEFAULT_CHARUCO_MARKER_MM,
    DEFAULT_CHARUCO_SQUARES_X,
    DEFAULT_CHARUCO_SQUARES_Y,
    DEFAULT_CHARUCO_SQUARE_MM,
    CharucoBoardSpec,
)
from skyweave.calibration.charuco_live_state import LiveCameraSettings, LiveTuningSettings
from skyweave.camera.live_benchmark import DEFAULT_LIVE_BENCHMARK_CONFIG
from skyweave.config import load_config
from skyweave.operator.runtime import OperatorRuntime, OperatorRuntimeOptions
from skyweave.operator.server import OperatorServer
from skyweave.operator.state import OperatorState, TRACKING_MODES
from skyweave.sim.scene import SCENE_CHOICES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Skyweave live operator dashboard and visualizer.")
    parser.add_argument("--device", default=None)
    parser.add_argument("--devices", default=None, help="Comma-separated camera devices.")
    parser.add_argument("--labels", default=None, help="Comma-separated camera labels.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--config", default=DEFAULT_LIVE_BENCHMARK_CONFIG)
    parser.add_argument("--extrinsics", default="configs/extrinsics.yaml")
    parser.add_argument("--profile-dir", default="data/profiles")
    parser.add_argument("--mode", choices=TRACKING_MODES, default="auto")
    parser.add_argument("--target-hz", type=float, default=30.0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--warmup-frames", type=int, default=10)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    parser.add_argument("--display-scale", type=float, default=0.5)
    parser.add_argument("--detect-every", type=int, default=2)
    parser.add_argument("--min-lock-corners", type=int, default=12)
    parser.add_argument("--reopen-after-failures", type=int, default=5)
    parser.add_argument("--squares-x", type=int, default=DEFAULT_CHARUCO_SQUARES_X)
    parser.add_argument("--squares-y", type=int, default=DEFAULT_CHARUCO_SQUARES_Y)
    parser.add_argument("--square-mm", type=float, default=DEFAULT_CHARUCO_SQUARE_MM)
    parser.add_argument("--marker-mm", type=float, default=DEFAULT_CHARUCO_MARKER_MM)
    parser.add_argument("--dictionary", default=DEFAULT_CHARUCO_DICTIONARY)
    parser.add_argument("--viz-dir", default="viz_web")
    parser.add_argument("--room-assets-dir", default="data/room")
    parser.add_argument("--room-mesh", default=None, help="Optional GLB/GLTF path or URL for the visualizer room mesh.")
    parser.add_argument("--room-translation", default=None, help="Room mesh translation as x,y,z meters.")
    parser.add_argument("--room-rotation", default=None, help="Room mesh rotation as x,y,z degrees.")
    args = parser.parse_args(argv)

    if args.device and args.devices:
        parser.error("use either --device or --devices, not both")
    if len(args.fourcc) != 4:
        parser.error("--fourcc must be exactly 4 characters")
    if args.target_hz <= 0.0:
        parser.error("--target-hz must be positive")

    devices = _parse_devices(args.device, args.devices)
    labels = _parse_labels(args.labels, len(devices))
    config = load_config(args.config)
    state = OperatorState(
        devices=devices,
        labels=labels,
        config_path=args.config,
        extrinsics_path=args.extrinsics,
        profile_dir=Path(args.profile_dir),
        requested_mode=args.mode,
        simulation_scene=_operator_simulation_scene(config.simulation.scene),
    )
    state.live.tuning = LiveTuningSettings(
        camera=LiveCameraSettings(
            width=args.width,
            height=args.height,
            fps=args.fps,
            fourcc=args.fourcc,
            warmup_frames=args.warmup_frames,
            jpeg_quality=args.jpeg_quality,
            display_scale=args.display_scale,
            detect_every=args.detect_every,
            min_lock_corners=args.min_lock_corners,
            reopen_after_failures=args.reopen_after_failures,
        )
    )
    if args.room_mesh:
        state.room.mesh_url = _room_mesh_url(args.room_mesh, Path(args.room_assets_dir))
    if args.room_translation:
        state.room.translation_m = _parse_triplet(args.room_translation, "--room-translation")
    if args.room_rotation:
        state.room.rotation_deg = _parse_triplet(args.room_rotation, "--room-rotation")

    board = CharucoBoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_length_m=args.square_mm / 1000.0,
        marker_length_m=args.marker_mm / 1000.0,
        dictionary=args.dictionary,
    )
    runtime = OperatorRuntime(
        state,
        board,
        OperatorRuntimeOptions(config_path=args.config, extrinsics_path=args.extrinsics, target_hz=args.target_hz),
    )
    server = OperatorServer(
        state,
        viz_dir=Path(args.viz_dir),
        room_assets_dir=Path(args.room_assets_dir),
        host=args.host,
        port=args.port,
    )
    runtime.start()
    try:
        server.run()
    finally:
        runtime.stop()
    return 0


def _parse_devices(device: str | None, devices: str | None) -> list[str]:
    if devices:
        parsed = [item.strip() for item in devices.split(",") if item.strip()]
        if parsed:
            return parsed
    return [device or "/dev/video0"]


def _parse_labels(labels: str | None, count: int) -> list[str]:
    if labels:
        parsed = [item.strip() for item in labels.split(",") if item.strip()]
    else:
        parsed = []
    while len(parsed) < count:
        parsed.append(f"cam{len(parsed) + 1}")
    return parsed[:count]


def _parse_triplet(value: str, name: str) -> list[float]:
    parts = [item.strip() for item in value.split(",")]
    if len(parts) != 3:
        raise SystemExit(f"{name} must be exactly three comma-separated numbers")
    try:
        return [float(item) for item in parts]
    except ValueError as exc:
        raise SystemExit(f"{name} must contain only numbers") from exc


def _operator_simulation_scene(scene: str) -> str:
    if scene in SCENE_CHOICES:
        return scene
    return SCENE_CHOICES[0]


def _room_mesh_url(value: str, assets_dir: Path) -> str:
    if value.startswith(("http://", "https://", "/")):
        return value
    path = Path(value)
    try:
        relative = path.relative_to(assets_dir)
        return f"/room-assets/{relative.as_posix()}"
    except ValueError:
        return f"/room-assets/{path.name}"


if __name__ == "__main__":
    raise SystemExit(main())
