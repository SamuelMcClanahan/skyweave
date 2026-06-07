from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skyweave.calibration.charuco_live_state import LiveState, LiveTuningSettings

TRACKING_MODES = ("auto", "real", "stress", "rendered")


@dataclass
class RoomSettings:
    mesh_url: str = ""
    visible: bool = True
    opacity: float = 0.42
    scale: float = 1.0
    translation_m: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation_deg: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    fallback_visible: bool = True
    fallback_size_m: list[float] = field(default_factory=lambda: [4.0, 4.0, 2.6])
    revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mesh_url": self.mesh_url,
            "visible": self.visible,
            "opacity": self.opacity,
            "scale": self.scale,
            "translation_m": list(self.translation_m),
            "rotation_deg": list(self.rotation_deg),
            "fallback_visible": self.fallback_visible,
            "fallback_size_m": list(self.fallback_size_m),
            "revision": self.revision,
        }

    def update(self, payload: dict[str, Any]) -> None:
        if "mesh_url" in payload:
            self.mesh_url = str(payload["mesh_url"]).strip()
        if "visible" in payload:
            self.visible = bool(payload["visible"])
        if "opacity" in payload:
            self.opacity = _bounded_float(payload["opacity"], "room.opacity", 0.0, 1.0)
        if "scale" in payload:
            self.scale = _positive_float(payload["scale"], "room.scale")
        if "translation_m" in payload:
            self.translation_m = _float_triplet(payload["translation_m"], "room.translation_m")
        if "rotation_deg" in payload:
            self.rotation_deg = _float_triplet(payload["rotation_deg"], "room.rotation_deg")
        if "fallback_visible" in payload:
            self.fallback_visible = bool(payload["fallback_visible"])
        if "fallback_size_m" in payload:
            self.fallback_size_m = _positive_float_triplet(payload["fallback_size_m"], "room.fallback_size_m")
        self.revision += 1


@dataclass
class FusionRuntimeSettings:
    min_cameras_per_frame: int = 2
    pixel_noise_px: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_cameras_per_frame": self.min_cameras_per_frame,
            "pixel_noise_px": self.pixel_noise_px,
        }

    def update(self, payload: dict[str, Any]) -> None:
        if "min_cameras_per_frame" in payload:
            self.min_cameras_per_frame = _positive_int(payload["min_cameras_per_frame"], "fusion.min_cameras_per_frame")
        if "pixel_noise_px" in payload:
            self.pixel_noise_px = _positive_float(payload["pixel_noise_px"], "fusion.pixel_noise_px")


@dataclass
class RayweaveScorerRuntimeSettings:
    min_supporting_cameras: int = 2
    top_k_voxels: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"min_supporting_cameras": self.min_supporting_cameras}
        if self.top_k_voxels is not None:
            payload["top_k_voxels"] = self.top_k_voxels
        return payload

    def update(self, payload: dict[str, Any]) -> None:
        if "min_supporting_cameras" in payload:
            self.min_supporting_cameras = _positive_int(
                payload["min_supporting_cameras"],
                "rayweave.scorer.min_supporting_cameras",
            )
        if "top_k_voxels" in payload:
            self.top_k_voxels = _positive_int(payload["top_k_voxels"], "rayweave.scorer.top_k_voxels")


@dataclass
class RayweavePeakRuntimeSettings:
    threshold_percentile: float = 99.5
    max_peaks: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_percentile": self.threshold_percentile,
            "max_peaks": self.max_peaks,
        }

    def update(self, payload: dict[str, Any]) -> None:
        if "threshold_percentile" in payload:
            self.threshold_percentile = _bounded_float(
                payload["threshold_percentile"],
                "rayweave.peaks.threshold_percentile",
                0.0,
                100.0,
            )
        if "max_peaks" in payload:
            self.max_peaks = _positive_int(payload["max_peaks"], "rayweave.peaks.max_peaks")


@dataclass
class RayweaveRuntimeSettings:
    scorer: RayweaveScorerRuntimeSettings = field(default_factory=RayweaveScorerRuntimeSettings)
    peaks: RayweavePeakRuntimeSettings = field(default_factory=RayweavePeakRuntimeSettings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer": self.scorer.to_dict(),
            "peaks": self.peaks.to_dict(),
        }

    def update(self, payload: dict[str, Any]) -> None:
        if "scorer" in payload:
            self.scorer.update(_dict_payload(payload["scorer"], "rayweave.scorer"))
        if "peaks" in payload:
            self.peaks.update(_dict_payload(payload["peaks"], "rayweave.peaks"))


@dataclass
class CalibrationStatus:
    extrinsics_path: str
    loaded: bool = False
    camera_count: int = 0
    rms_reprojection_error_px: float | None = None
    message: str = "not loaded"
    cameras: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extrinsics_path": self.extrinsics_path,
            "loaded": self.loaded,
            "camera_count": self.camera_count,
            "rms_reprojection_error_px": self.rms_reprojection_error_px,
            "message": self.message,
            "cameras": copy.deepcopy(self.cameras),
        }


@dataclass
class PipelineStatus:
    mode: str = "stress"
    reason: str = "starting"
    frame_seq: int = -1
    aligned: bool = False
    packet_count: int = 0
    blob_count: int = 0
    patch_count: int = 0
    measurement_count: int = 0
    track_count: int = 0
    camera_read_ms: float = 0.0
    motion_ms: float = 0.0
    preview_ms: float = 0.0
    alignment_ms: float = 0.0
    scoring_ms: float = 0.0
    peaks_ms: float = 0.0
    kalman_ms: float = 0.0
    total_ms: float = 0.0
    target_sleep_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "frame_seq": self.frame_seq,
            "aligned": self.aligned,
            "packet_count": self.packet_count,
            "blob_count": self.blob_count,
            "patch_count": self.patch_count,
            "measurement_count": self.measurement_count,
            "track_count": self.track_count,
            "camera_read_ms": self.camera_read_ms,
            "motion_ms": self.motion_ms,
            "preview_ms": self.preview_ms,
            "alignment_ms": self.alignment_ms,
            "scoring_ms": self.scoring_ms,
            "peaks_ms": self.peaks_ms,
            "kalman_ms": self.kalman_ms,
            "total_ms": self.total_ms,
            "target_sleep_ms": self.target_sleep_ms,
        }


@dataclass
class RecordingStatus:
    active: bool = False
    session_id: str | None = None
    output_dir: str | None = None
    frame_count: int = 0
    image_count: int = 0
    started_ts_ns: int | None = None
    stopped_ts_ns: int | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "session_id": self.session_id,
            "output_dir": self.output_dir,
            "frame_count": self.frame_count,
            "image_count": self.image_count,
            "started_ts_ns": self.started_ts_ns,
            "stopped_ts_ns": self.stopped_ts_ns,
            "last_error": self.last_error,
        }


@dataclass
class OperatorState:
    devices: list[str]
    labels: list[str]
    config_path: str
    extrinsics_path: str
    profile_dir: Path
    requested_mode: str = "auto"
    record_dir: Path = Path("data/operator_recordings")
    live: LiveState = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(init=False)
    profile_name: str | None = None
    tracking_revision: int = 0
    runtime_status: str = "starting"
    runtime_error: str | None = None
    calibration: CalibrationStatus = field(init=False)
    pipeline: PipelineStatus = field(default_factory=PipelineStatus)
    room: RoomSettings = field(default_factory=RoomSettings)
    fusion: FusionRuntimeSettings = field(default_factory=FusionRuntimeSettings)
    rayweave: RayweaveRuntimeSettings = field(default_factory=RayweaveRuntimeSettings)
    recording: RecordingStatus = field(default_factory=RecordingStatus)
    latest_viz_frame: dict[str, Any] | None = None
    viz_version: int = 0
    _record_events_fh: Any = field(default=None, init=False, repr=False)
    _record_last_frame_versions: list[int] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.requested_mode not in TRACKING_MODES:
            raise ValueError(f"requested_mode must be one of {', '.join(TRACKING_MODES)}")
        self.condition = threading.Condition(self.lock)
        self.live = LiveState(devices=self.devices, tuning=LiveTuningSettings())
        self.calibration = CalibrationStatus(extrinsics_path=self.extrinsics_path)

    def snapshot(self) -> dict[str, Any]:
        live_snapshot = self.live.snapshot()
        with self.lock:
            settings = _dict_payload(live_snapshot.get("settings", {}), "settings")
            settings["fusion"] = self.fusion.to_dict()
            settings["rayweave"] = self.rayweave.to_dict()
            live_snapshot["settings"] = settings
            cameras = []
            for camera in live_snapshot["cameras"]:
                item = dict(camera)
                index = int(item["index"])
                item["label"] = self.labels[index] if index < len(self.labels) else f"cam{index + 1}"
                cameras.append(item)
            live_snapshot["cameras"] = cameras
            return {
                **live_snapshot,
                "operator": {
                    "status": self.runtime_status,
                    "error": self.runtime_error,
                    "config_path": self.config_path,
                    "profile_dir": str(self.profile_dir),
                },
                "tracking": {
                    "requested_mode": self.requested_mode,
                    "effective_mode": self.pipeline.mode,
                    "mode_choices": list(TRACKING_MODES),
                    "reason": self.pipeline.reason,
                    "revision": self.tracking_revision,
                },
                "profile": {
                    "active": self.profile_name,
                },
                "calibration": self.calibration.to_dict(),
                "pipeline": self.pipeline.to_dict(),
                "room": self.room.to_dict(),
                "recording": self.recording.to_dict(),
            }

    def settings_payload(self) -> dict[str, Any]:
        settings = self.live.settings_snapshot()[0].to_dict()
        with self.lock:
            settings["fusion"] = self.fusion.to_dict()
            settings["rayweave"] = self.rayweave.to_dict()
            return {
                "version": 1,
                "tracking": {"requested_mode": self.requested_mode},
                "settings": settings,
                "room": self.room.to_dict(),
            }

    def apply_payload(self, payload: dict[str, Any], profile_name: str | None = None) -> dict[str, Any]:
        settings_payload: dict[str, Any] = {}
        if "settings" in payload:
            settings_payload.update(_dict_payload(payload["settings"], "settings"))
        for key in ("camera", "motion", "kalman"):
            if key in payload:
                settings_payload[key] = payload[key]
        fusion_payload = settings_payload.pop("fusion", None)
        rayweave_payload = settings_payload.pop("rayweave", None)
        if settings_payload:
            self.live.update_settings(settings_payload)

        with self.condition:
            tracking_changed = False
            if "tracking" in payload:
                tracking = _dict_payload(payload["tracking"], "tracking")
                if "requested_mode" in tracking:
                    next_mode = _tracking_mode(tracking["requested_mode"])
                    tracking_changed = tracking_changed or next_mode != self.requested_mode
                    self.requested_mode = next_mode
                if "mode" in tracking:
                    next_mode = _tracking_mode(tracking["mode"])
                    tracking_changed = tracking_changed or next_mode != self.requested_mode
                    self.requested_mode = next_mode
            if "mode" in payload:
                next_mode = _tracking_mode(payload["mode"])
                tracking_changed = tracking_changed or next_mode != self.requested_mode
                self.requested_mode = next_mode
            if "room" in payload:
                self.room.update(_dict_payload(payload["room"], "room"))
            if "fusion" in payload:
                fusion_payload = payload["fusion"]
            if "rayweave" in payload:
                rayweave_payload = payload["rayweave"]
            if fusion_payload is not None:
                self.fusion.update(_dict_payload(fusion_payload, "fusion"))
                tracking_changed = True
            if rayweave_payload is not None:
                self.rayweave.update(_dict_payload(rayweave_payload, "rayweave"))
                tracking_changed = True
            if profile_name is not None:
                self.profile_name = profile_name
            if tracking_changed:
                self.tracking_revision += 1
            self.condition.notify_all()
        return self.snapshot()

    def set_runtime_status(self, status: str, error: str | None = None) -> None:
        with self.condition:
            self.runtime_status = status
            self.runtime_error = error
            self.condition.notify_all()

    def tracking_snapshot(self) -> tuple[str, int]:
        with self.lock:
            return self.requested_mode, self.tracking_revision

    def set_calibration(self, calibration: CalibrationStatus) -> None:
        with self.condition:
            self.calibration = calibration
            self.condition.notify_all()

    def set_pipeline(self, pipeline: PipelineStatus) -> None:
        with self.condition:
            self.pipeline = pipeline
            self.condition.notify_all()

    def set_viz_frame(self, frame: dict[str, Any]) -> None:
        with self.condition:
            self.latest_viz_frame = frame
            self.viz_version += 1
            self.condition.notify_all()
        self._record_viz_frame(frame)

    def wait_viz_frame(self, last_version: int, timeout_s: float) -> tuple[int, dict[str, Any] | None]:
        with self.condition:
            self.condition.wait_for(
                lambda: self.viz_version != last_version or not self.live.running,
                timeout=timeout_s,
            )
            return self.viz_version, copy.deepcopy(self.latest_viz_frame)

    def stop(self) -> None:
        self.stop_recording()
        with self.live.condition:
            self.live.running = False
            self.live.condition.notify_all()
        with self.condition:
            self.condition.notify_all()

    def start_recording(self, name: str | None = None) -> dict[str, Any]:
        created = datetime.now(timezone.utc)
        session_id = _recording_session_id(created, name)
        settings = self.live.settings_snapshot()[0].to_dict()
        live_snapshot = self.live.snapshot()
        live_cameras = live_snapshot.get("cameras", [])
        camera_count = len(live_cameras) if isinstance(live_cameras, list) else len(self.devices)
        live_devices = [
            str(camera.get("device", f"camera{index}"))
            for index, camera in enumerate(live_cameras)
            if isinstance(camera, dict)
        ]
        live_labels = [
            self.labels[index] if index < len(self.labels) else f"cam{index + 1}"
            for index in range(camera_count)
        ]
        with self.condition:
            if self.recording.active:
                raise ValueError("recording is already active")
            session_dir = _unique_dir(self.record_dir, session_id)
            session_id = session_dir.name
            (session_dir / "frames").mkdir(parents=True, exist_ok=False)
            for index in range(camera_count):
                (session_dir / "frames" / f"cam{index + 1}").mkdir()
            manifest = {
                "schema_version": 1,
                "created_utc": created.isoformat(),
                "session_id": session_id,
                "devices": live_devices,
                "labels": live_labels,
                "config_path": self.config_path,
                "extrinsics_path": self.extrinsics_path,
                "settings": settings,
                "tracking": {"requested_mode": self.requested_mode},
                "calibration": self.calibration.to_dict(),
            }
            _write_json(session_dir / "manifest.json", manifest)
            self._record_events_fh = (session_dir / "events.jsonl").open("a", encoding="utf-8")
            self._record_last_frame_versions = [-1 for _ in range(camera_count)]
            self.recording = RecordingStatus(
                active=True,
                session_id=session_id,
                output_dir=str(session_dir),
                started_ts_ns=time.time_ns(),
            )
            self.condition.notify_all()
            return self.recording.to_dict()

    def stop_recording(self) -> dict[str, Any]:
        with self.condition:
            if not self.recording.active:
                return self.recording.to_dict()
            self.recording.active = False
            self.recording.stopped_ts_ns = time.time_ns()
            summary = self.recording.to_dict()
            output_dir = self.recording.output_dir
            if self._record_events_fh is not None:
                self._record_events_fh.close()
                self._record_events_fh = None
            if output_dir:
                _write_json(Path(output_dir) / "summary.json", summary)
            self.condition.notify_all()
            return summary

    def save_recording_snapshot(self, name: str | None = None) -> dict[str, Any]:
        created = datetime.now(timezone.utc)
        session_id = _recording_session_id(created, name or "snapshot")
        session_dir = _unique_dir(self.record_dir, session_id)
        frames_dir = session_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=False)
        live_snapshot = self.live.snapshot()
        live_cameras = live_snapshot.get("cameras", [])
        camera_count = len(live_cameras) if isinstance(live_cameras, list) else len(self.devices)
        labels = [self.labels[index] if index < len(self.labels) else f"cam{index + 1}" for index in range(camera_count)]
        for index in range(camera_count):
            (frames_dir / f"cam{index + 1}").mkdir()
        live_snapshot, frame, jpeg_items = self._recording_payload_inputs()
        images = _write_record_images(frames_dir, labels, live_snapshot, jpeg_items, [-1 for _ in range(camera_count)], 0)
        payload = {
            "schema_version": 1,
            "event": "snapshot",
            "created_utc": created.isoformat(),
            "session_id": session_dir.name,
            "status": self.snapshot(),
            "viz_frame": _compact_viz_frame(frame),
            "images": images,
        }
        _write_json(session_dir / "snapshot.json", payload)
        return {
            "active": False,
            "session_id": session_dir.name,
            "output_dir": str(session_dir),
            "frame_count": 1,
            "image_count": len(images),
            "started_ts_ns": None,
            "stopped_ts_ns": time.time_ns(),
            "last_error": None,
        }

    def _record_viz_frame(self, frame: dict[str, Any]) -> None:
        live_snapshot, _frame, jpeg_items = self._recording_payload_inputs(frame)
        with self.condition:
            if not self.recording.active or self._record_events_fh is None or not self.recording.output_dir:
                return
            try:
                session_dir = Path(self.recording.output_dir)
                frame_seq = int(frame.get("frame_seq", self.recording.frame_count))
                images = _write_record_images(
                    session_dir / "frames",
                    self.labels,
                    live_snapshot,
                    jpeg_items,
                    self._record_last_frame_versions,
                    frame_seq,
                )
                event = {
                    "event": "viz_frame",
                    "record_seq": self.recording.frame_count,
                    "event_ts_ns": time.time_ns(),
                    "frame_seq": frame.get("frame_seq"),
                    "ts_ns": frame.get("ts_ns"),
                    "pipeline": self.pipeline.to_dict(),
                    "track": live_snapshot.get("track", {}),
                    "cameras": _compact_camera_statuses(live_snapshot.get("cameras", [])),
                    "measurements": frame.get("measurements", []),
                    "tracks": frame.get("tracks", []),
                    "stats": frame.get("stats", {}),
                    "images": images,
                }
                self._record_events_fh.write(json.dumps(event, sort_keys=True) + "\n")
                self._record_events_fh.flush()
                self.recording.frame_count += 1
                self.recording.image_count += len(images)
                self.recording.last_error = None
            except Exception as exc:
                self.recording.last_error = str(exc)
            self.condition.notify_all()

    def _recording_payload_inputs(
        self,
        frame: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, list[tuple[int, int, int, bytes]]]:
        live_snapshot = self.live.snapshot()
        with self.condition:
            selected_frame = copy.deepcopy(frame if frame is not None else self.latest_viz_frame)
        jpeg_items: list[tuple[int, int, int, bytes]] = []
        with self.live.lock:
            for index, jpeg in enumerate(self.live.frame_jpegs):
                if jpeg is None:
                    continue
                camera_frame_seq = self.live.cameras[index].frame_seq
                version = self.live.frame_versions[index]
                jpeg_items.append((index, version, camera_frame_seq, jpeg))
        return live_snapshot, selected_frame, jpeg_items


def _dict_payload(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _tracking_mode(value: Any) -> str:
    mode = str(value).strip().lower()
    if mode not in TRACKING_MODES:
        raise ValueError(f"tracking mode must be one of {', '.join(TRACKING_MODES)}")
    return mode


def _bounded_float(value: Any, name: str, min_value: float, max_value: float) -> float:
    parsed = float(value)
    if not min_value <= parsed <= max_value:
        raise ValueError(f"{name} must be between {min_value} and {max_value}")
    return parsed


def _positive_int(value: Any, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_float(value: Any, name: str) -> float:
    parsed = float(value)
    if parsed <= 0.0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _float_triplet(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must be a 3-number list")
    return [float(item) for item in value]


def _positive_float_triplet(value: Any, name: str) -> list[float]:
    triplet = _float_triplet(value, name)
    if any(item <= 0.0 for item in triplet):
        raise ValueError(f"{name} values must be positive")
    return triplet


def _recording_session_id(created: datetime, name: str | None) -> str:
    stamp = created.strftime("%Y%m%d-%H%M%S")
    suffix = _safe_slug(name or "operator")
    return f"{stamp}-{suffix}" if suffix else stamp


def _safe_slug(value: str) -> str:
    output = []
    for char in value.strip().lower().replace(" ", "-"):
        if char.isalnum() or char in {"-", "_", "."}:
            output.append(char)
        else:
            output.append("-")
    slug = "".join(output).strip("-._")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:64]


def _unique_dir(root: Path, session_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / session_id
    if not candidate.exists():
        return candidate
    for suffix in range(2, 1000):
        candidate = root / f"{session_id}-{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate recording directory for {session_id}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _compact_viz_frame(frame: dict[str, Any] | None) -> dict[str, Any] | None:
    if frame is None:
        return None
    return {
        "frame_seq": frame.get("frame_seq"),
        "ts_ns": frame.get("ts_ns"),
        "tracks": frame.get("tracks", []),
        "measurements": frame.get("measurements", []),
        "stats": frame.get("stats", {}),
        "room": frame.get("room", {}),
    }


def _compact_camera_statuses(cameras: Any) -> list[dict[str, Any]]:
    if not isinstance(cameras, list):
        return []
    keys = (
        "index",
        "label",
        "device",
        "status",
        "frame_seq",
        "capture_fps",
        "read_failures",
        "sharpness",
        "latency_ms",
        "stale_age_ms",
    )
    return [
        {key: camera.get(key) for key in keys if isinstance(camera, dict) and key in camera}
        for camera in cameras
        if isinstance(camera, dict)
    ]


def _write_record_images(
    frames_dir: Path,
    labels: list[str],
    _live_snapshot: dict[str, Any],
    jpeg_items: list[tuple[int, int, int, bytes]],
    last_versions: list[int],
    record_frame_seq: int,
) -> list[dict[str, Any]]:
    images = []
    for camera_index, version, camera_frame_seq, jpeg in jpeg_items:
        while len(last_versions) <= camera_index:
            last_versions.append(-1)
        if version == last_versions[camera_index]:
            continue
        last_versions[camera_index] = version
        label = labels[camera_index] if camera_index < len(labels) else f"cam{camera_index + 1}"
        safe_label = _safe_slug(label) or f"cam{camera_index + 1}"
        filename = f"frame{record_frame_seq:06d}_src{camera_frame_seq:06d}_v{version:06d}.jpg"
        relative = Path(f"frames/cam{camera_index + 1}") / filename
        path = frames_dir / f"cam{camera_index + 1}" / filename
        path.write_bytes(jpeg)
        images.append(
            {
                "camera_index": camera_index,
                "label": label,
                "safe_label": safe_label,
                "camera_frame_seq": camera_frame_seq,
                "frame_version": version,
                "path": relative.as_posix(),
            }
        )
    return images
