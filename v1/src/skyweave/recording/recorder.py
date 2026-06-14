from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import msgpack

from skyweave.config import SimCheckConfig
from skyweave.messages import DetectionPacket, Measurement3D, MotionPacket, RunSummary, SkyweaveModel, Track, WeavefieldVolume


STREAM_FILES = {
    "motion_packets": "motion_packets.msgpack",
    "detection_packets": "detection_packets.msgpack",
    "weavefields": "weavefields.msgpack",
    "measurements": "measurements.msgpack",
    "tracks": "tracks.msgpack",
}


class Recorder:
    def __init__(self, session_dir: Path, manifest: dict) -> None:
        self.session_dir = session_dir
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self._packer = msgpack.Packer(use_bin_type=True)
        self._files = {
            stream: (self.session_dir / filename).open("ab")
            for stream, filename in STREAM_FILES.items()
        }
        _write_json(self.session_dir / "manifest.json", manifest)

    @classmethod
    def create(
        cls,
        output_dir: str | Path,
        config: SimCheckConfig,
        config_path: str,
    ) -> "Recorder":
        created = datetime.now(timezone.utc)
        session_id = f"{created.strftime('%Y%m%d-%H%M%S')}-{config.simulation.scene}"
        session_dir = Path(output_dir) / session_id
        manifest = {
            "schema_version": 1,
            "created_utc": created.isoformat(),
            "session_id": session_id,
            "config_path": config_path,
            "scene": config.simulation.scene,
            "config": config.model_dump(mode="json"),
            "streams": STREAM_FILES,
        }
        return cls(session_dir, manifest)

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()

    def record_motion_packets(self, packets: Iterable[MotionPacket]) -> None:
        self._write_many("motion_packets", packets)

    def record_detection_packets(self, packets: Iterable[DetectionPacket]) -> None:
        self._write_many("detection_packets", packets)

    def record_weavefield(self, volume: WeavefieldVolume) -> None:
        self._write_one("weavefields", volume)

    def record_measurement(self, measurement: Measurement3D) -> None:
        self._write_one("measurements", measurement)

    def record_track(self, track: Track) -> None:
        self._write_one("tracks", track)

    def record_summary(self, summary: RunSummary) -> None:
        _write_json(self.session_dir / "summary.json", summary.model_dump(mode="json"))

    def _write_many(self, stream: str, models: Iterable[SkyweaveModel]) -> None:
        for model in models:
            self._write_one(stream, model)

    def _write_one(self, stream: str, model: SkyweaveModel) -> None:
        self._files[stream].write(self._packer.pack(model.model_dump(mode="python")))
        self._files[stream].flush()


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

