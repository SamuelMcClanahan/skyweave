"""S8: dataset_id changes with any manifest field, stable otherwise."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from skyweave2.dataset import (
    canonical_manifest_bytes,
    dataset_id,
    load_manifest,
)
from skyweave2.dataset import (
    main as dataset_main,
)
from skyweave2.sensor_model import SENSOR_MODEL_VERSION

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "configs" / "exp001_scene.yaml"


def _base_id(manifest) -> str:
    return dataset_id(manifest, "Blender 5.2.0", SENSOR_MODEL_VERSION, 7, "no-git")


def test_s8_id_is_stable_for_identical_inputs():
    manifest = load_manifest(MANIFEST_PATH)
    assert _base_id(manifest) == _base_id(load_manifest(MANIFEST_PATH))


def test_s8_id_changes_when_any_manifest_field_changes():
    manifest = load_manifest(MANIFEST_PATH)
    base = _base_id(manifest)
    for path in (
        ("cameras", "baseline_m"),
        ("target", "speed_mps"),
        ("timing", "exposure_us"),
        ("acceptance", "range_p95_max_m"),
    ):
        changed = copy.deepcopy(manifest)
        node = changed
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = float(node[path[-1]]) + 1.0
        assert _base_id(changed) != base, f"field {'.'.join(path)} did not change the id"


def test_s8_id_tracks_versions_and_seed():
    manifest = load_manifest(MANIFEST_PATH)
    base = _base_id(manifest)
    assert dataset_id(manifest, "Blender 9.9.9", SENSOR_MODEL_VERSION, 7, "no-git") != base
    assert dataset_id(manifest, "Blender 5.2.0", "other/2", 7, "no-git") != base
    assert dataset_id(manifest, "Blender 5.2.0", SENSOR_MODEL_VERSION, 8, "no-git") != base
    assert dataset_id(manifest, "Blender 5.2.0", SENSOR_MODEL_VERSION, 7, "abc123") != base


def test_s8_id_ignores_formatting_only_changes():
    """Canonicalization: the ID is a function of fields, not of YAML bytes."""
    manifest = load_manifest(MANIFEST_PATH)
    reordered = dict(reversed(list(manifest.items())))  # same fields, new order
    assert canonical_manifest_bytes(manifest) == canonical_manifest_bytes(reordered)
    assert _base_id(manifest) == _base_id(reordered)


def test_to_json_emits_the_blender_copy(tmp_path, capsys):
    out = tmp_path / "scene.json"
    dataset_main(["to-json", str(MANIFEST_PATH), str(out)])
    scene = json.loads(out.read_text())
    assert scene["cameras"]["render_resolution"] == [2304, 1296]
    assert scene["determinism"]["seed"] == 7


def test_id_cli_prints_hex(tmp_path, capsys):
    dataset_main(["id", str(MANIFEST_PATH), "--blender-version", "Blender 5.2.0"])
    printed = capsys.readouterr().out.strip()
    assert len(printed) == 64
    int(printed, 16)  # valid hex
