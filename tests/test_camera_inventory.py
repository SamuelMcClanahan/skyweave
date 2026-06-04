from __future__ import annotations

from pathlib import Path

import yaml

from skyweave.camera.inventory import VideoDeviceInfo, capture_nodes
from skyweave.cli.camera_inventory import write_camera_label_config


def test_capture_nodes_filters_uvc_capture_devices() -> None:
    devices = [
        VideoDeviceInfo("/dev/video0", "path0", "cam", "s", "bus", True, True, False),
        VideoDeviceInfo("/dev/video1", "path0", "cam", "s", "bus", True, False, True),
        VideoDeviceInfo("/dev/video32", "codec", "codec", "", "bus", False, True, False),
    ]

    assert [device.device for device in capture_nodes(devices)] == ["/dev/video0"]


def test_camera_inventory_writes_label_config(tmp_path: Path) -> None:
    output = tmp_path / "cameras.yaml"
    devices = [
        VideoDeviceInfo("/dev/video0", "path0", "cam", "serial0", "bus0", True, True, False),
        VideoDeviceInfo("/dev/video2", "path1", "cam", "serial1", "bus1", True, True, False),
    ]

    write_camera_label_config(devices, ["north", "south"], output)

    data = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert data["cameras"][0]["label"] == "north"
    assert data["cameras"][0]["id_path"] == "path0"
    assert data["cameras"][1]["label"] == "south"
