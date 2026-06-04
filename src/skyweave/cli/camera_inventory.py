from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from skyweave.camera.inventory import VideoDeviceInfo, capture_nodes, list_video_devices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List UVC camera nodes and stable ID_PATH values.")
    parser.add_argument("--labels", default=None, help="Comma-separated physical labels for capture nodes in listed order.")
    parser.add_argument("--output", default=None, help="Optional YAML config output path.")
    args = parser.parse_args(argv)

    devices = list_video_devices()
    captures = capture_nodes(devices)
    labels = _parse_labels(args.labels)
    if labels and len(labels) != len(captures):
        parser.error(f"--labels count ({len(labels)}) must match capture node count ({len(captures)})")

    for device in devices:
        role = "capture" if device.is_uvc and device.is_capture else "metadata" if device.is_metadata else "other"
        print(
            "camera_inventory "
            f"device={device.device} role={role} model={device.model or 'unknown'} "
            f"id_path={device.id_path or 'unknown'} bus_info={device.bus_info or 'unknown'}"
        )

    if args.output:
        output = Path(args.output)
        write_camera_label_config(captures, labels, output)
        print(f"camera_config={output}")
    return 0


def write_camera_label_config(devices: list[VideoDeviceInfo], labels: list[str], output: Path) -> None:
    payload = {
        "cameras": [
            {
                "label": labels[index] if index < len(labels) else f"camera_{index}",
                "device": device.device,
                "id_path": device.id_path,
                "model": device.model,
                "serial": device.serial,
                "bus_info": device.bus_info,
            }
            for index, device in enumerate(devices)
        ],
        "notes": [
            "Match cameras by id_path when /dev/videoN changes.",
            "The device field is the current node and may change after replugging.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _parse_labels(labels: str | None) -> list[str]:
    if labels is None:
        return []
    return [item.strip() for item in labels.split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
