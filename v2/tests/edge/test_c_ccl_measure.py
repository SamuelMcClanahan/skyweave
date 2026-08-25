"""Host execution gate for the SDK-independent C CCL measurements."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skyweave2.edge import daemon


def test_board_ccl_explicitly_selects_supported_8_connectivity():
    source = (
        Path(__file__).resolve().parents[2]
        / "firmware"
        / "rv1106"
        / "src"
        / "sw_detect_ive.c"
    ).read_text(encoding="utf-8")
    assert "state->ccl_ctrl.enMode = IVE_CCL_MODE_8C;" in source
    assert "state->ccl_ctrl.enMode = IVE_CCL_MODE_4C;" not in source


def test_ccl_measure_helper_executes(edge_build_dir):
    helper = edge_build_dir / "sw-ccl-measure-test"
    assert helper.is_file(), "the host build did not produce sw-ccl-measure-test"
    result = subprocess.run(
        [str(helper)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, (
        "sw-ccl-measure-test failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_production_daemon_exposes_structural_selftest(edge_build_dir):
    result = subprocess.run(
        [str(daemon.daemon_path(edge_build_dir)), "--self-test-ccl-measure"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "skyweave-ccl-selftest/1",
        "full_254_slot_region_scan": True,
        "mask_moment_centroid": True,
        "overlap_counter": True,
    }


def test_detector_failure_resets_c_persistence_chain(edge_build_dir):
    helper = edge_build_dir / "sw-pipeline-reset-test"
    assert helper.is_file(), "the host build did not produce sw-pipeline-reset-test"
    result = subprocess.run(
        [str(helper)], capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0, (
        "sw-pipeline-reset-test failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("source", "packet"),
        ("source", "stats"),
        ("source", "ccl"),
        ("source", "mask"),
        ("packet", "stats"),
        ("packet", "ccl"),
        ("packet", "mask"),
        ("stats", "ccl"),
        ("stats", "mask"),
        ("ccl", "mask"),
    ],
)
def test_daemon_refuses_every_input_and_artifact_path_collision(
    edge_build_dir, tmp_path, first, second
):
    shared = str(tmp_path / "same-artifact")
    paths = {
        "source": str(tmp_path / "source.swij"),
        "packet": str(tmp_path / "packets.hex"),
        "stats": str(tmp_path / "stats.json"),
        "ccl": str(tmp_path / "ccl.jsonl"),
        "mask": str(tmp_path / "masks.swfm"),
    }
    paths[first] = shared
    paths[second] = shared
    result = subprocess.run(
        [
            str(daemon.daemon_path(edge_build_dir)),
            "--inject-file",
            paths["source"],
            "--detector",
            "soft",
            "--packet-log",
            paths["packet"],
            "--stats",
            paths["stats"],
            "--ccl-log",
            paths["ccl"],
            "--fg-mask-log",
            paths["mask"],
            "--fg-mask-limit",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "must use distinct paths" in result.stderr


def test_soft_detector_refuses_ive_only_diagnostics(edge_build_dir, tmp_path):
    result = subprocess.run(
        [
            str(daemon.daemon_path(edge_build_dir)),
            "--inject-file",
            str(tmp_path / "source.swij"),
            "--detector",
            "soft",
            "--ccl-log",
            str(tmp_path / "ccl.jsonl"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "require --detector ive" in result.stderr
