from __future__ import annotations

import glob
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoDeviceInfo:
    device: str
    id_path: str
    model: str
    serial: str
    bus_info: str
    is_uvc: bool
    is_capture: bool
    is_metadata: bool


def list_video_devices(paths: list[str] | None = None) -> list[VideoDeviceInfo]:
    devices = paths or sorted(glob.glob("/dev/video*"), key=_device_sort_key)
    return [inspect_video_device(device) for device in devices if Path(device).exists()]


def inspect_video_device(device: str) -> VideoDeviceInfo:
    properties = read_udev_properties(device)
    v4l2_all = _run_text(["v4l2-ctl", "-d", device, "--all"])
    return VideoDeviceInfo(
        device=device,
        id_path=properties.get("ID_PATH", ""),
        model=properties.get("ID_MODEL", ""),
        serial=properties.get("ID_SERIAL_SHORT", properties.get("ID_SERIAL", "")),
        bus_info=_extract_field(v4l2_all, "Bus info"),
        is_uvc="Driver name      : uvcvideo" in v4l2_all,
        is_capture=_device_caps_has(v4l2_all, "Video Capture"),
        is_metadata=_device_caps_has(v4l2_all, "Metadata Capture") and not _device_caps_has(v4l2_all, "Video Capture"),
    )


def read_udev_properties(device: str) -> dict[str, str]:
    output = _run_text(["udevadm", "info", "-q", "property", "-n", device])
    result: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def capture_nodes(devices: list[VideoDeviceInfo]) -> list[VideoDeviceInfo]:
    return [device for device in devices if device.is_uvc and device.is_capture]


def _device_caps_has(v4l2_all: str, capability: str) -> bool:
    lines = v4l2_all.splitlines()
    for index, line in enumerate(lines):
        if "Device Caps" not in line:
            continue
        block = "\n".join(lines[index : index + 8])
        return capability in block
    return False


def _extract_field(text: str, field: str) -> str:
    prefix = f"\t{field}"
    for line in text.splitlines():
        if line.startswith(prefix) and ":" in line:
            return line.split(":", 1)[1].strip()
    return ""


def _run_text(command: list[str]) -> str:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    return completed.stdout + completed.stderr


def _device_sort_key(device: str) -> tuple[str, int]:
    path = Path(device)
    stem = path.name.removeprefix("video")
    try:
        return path.parent.as_posix(), int(stem)
    except ValueError:
        return path.parent.as_posix(), 10_000
