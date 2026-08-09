"""Dataset identity: the determinism contract of design §9.

``dataset_id`` = SHA-256 over (canonical manifest, Blender version,
sensor-model version, dataset seed, git revision). The manifest is
canonicalized through parse -> sorted-key JSON so the ID tracks FIELD
changes, not whitespace or comment edits (test S8).

Also the host-side manifest tooling: the frozen scene YAML stays
authoritative, and ``to-json`` emits the JSON copy the Blender script
consumes (Blender's bundled Python has no PyYAML).

This working tree is not a git repository; when no revision is available
the ID records the literal string "no-git" rather than failing, and the
dataset.json carries that visibly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml


def load_manifest(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest {path} did not parse to a mapping")
    return manifest


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def git_revision(repo_root: str | Path | None = None) -> str:
    """Current git revision, or "no-git" when the tree is not a repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "no-git"
    return result.stdout.strip() if result.returncode == 0 else "no-git"


def dataset_id(
    manifest: dict[str, Any],
    blender_version: str,
    sensor_model_version: str,
    seed: int,
    git_rev: str,
    variant: str = "gate",
) -> str:
    """SHA-256 dataset identity (design §9).

    ``variant`` separates datasets sharing one manifest — the gate clip and
    the target-free negative clip must never collide.
    """
    digest = hashlib.sha256()
    digest.update(canonical_manifest_bytes(manifest))
    for part in (blender_version, sensor_model_version, str(seed), git_rev, variant):
        digest.update(b"\x00")
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Dataset manifest tooling")
    sub = parser.add_subparsers(dest="cmd", required=True)

    to_json = sub.add_parser("to-json", help="emit canonical JSON for the Blender script")
    to_json.add_argument("manifest_yaml")
    to_json.add_argument("out_json")

    ident = sub.add_parser("id", help="print the dataset id")
    ident.add_argument("manifest_yaml")
    ident.add_argument("--blender-version", default="unrendered")
    ident.add_argument("--sensor-model-version", default=None)
    ident.add_argument("--seed", type=int, default=None)

    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest_yaml)
    if args.cmd == "to-json":
        from skyweave2.sensor_model import SENSOR_MODEL_VERSION

        # The `_host` block carries host-side facts into Blender's Python
        # (which cannot import this package). It is NOT part of the manifest:
        # consumers must pop it before hashing (the bpy script does).
        payload = dict(manifest)
        payload["_host"] = {
            "sensor_model_version": SENSOR_MODEL_VERSION,
            "git_rev": git_revision(Path(args.manifest_yaml).resolve().parent),
            "source_yaml": str(Path(args.manifest_yaml).resolve()),
        }
        with open(args.out_json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"wrote {args.out_json}")
    else:
        from skyweave2.sensor_model import SENSOR_MODEL_VERSION

        seed = args.seed if args.seed is not None else int(
            manifest.get("determinism", {}).get("seed", 0)
        )
        sm_version = args.sensor_model_version or SENSOR_MODEL_VERSION
        print(
            dataset_id(manifest, args.blender_version, sm_version, seed, git_revision())
        )


if __name__ == "__main__":
    main()
