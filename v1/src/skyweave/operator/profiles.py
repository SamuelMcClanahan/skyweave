from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from skyweave.operator.state import OperatorState

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def list_profiles(profile_dir: Path) -> list[dict[str, Any]]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    profiles = []
    for path in sorted(profile_dir.glob("*.yaml")):
        profiles.append({"name": path.stem, "path": str(path), "mtime": path.stat().st_mtime})
    return profiles


def save_profile(state: OperatorState, name: str) -> dict[str, Any]:
    safe_name = normalize_profile_name(name)
    state.profile_dir.mkdir(parents=True, exist_ok=True)
    path = profile_path(state.profile_dir, safe_name)
    payload = state.settings_payload()
    payload["saved_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload["profile_name"] = safe_name
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with state.condition:
        state.profile_name = safe_name
        state.condition.notify_all()
    return {"name": safe_name, "path": str(path), "profile": payload}


def load_profile(state: OperatorState, name: str) -> dict[str, Any]:
    safe_name = normalize_profile_name(name)
    path = profile_path(state.profile_dir, safe_name)
    if not path.exists():
        raise FileNotFoundError(f"profile not found: {safe_name}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"profile must be a YAML object: {path}")
    snapshot = state.apply_payload(payload, profile_name=safe_name)
    return {"name": safe_name, "path": str(path), "profile": payload, "status": snapshot}


def profile_path(profile_dir: Path, name: str) -> Path:
    return profile_dir / f"{normalize_profile_name(name)}.yaml"


def normalize_profile_name(name: str) -> str:
    value = name.strip()
    if value.endswith(".yaml"):
        value = value[:-5]
    if not value or not PROFILE_NAME_RE.fullmatch(value):
        raise ValueError("profile name may only contain letters, numbers, underscores, dashes, and dots")
    if value in {".", ".."}:
        raise ValueError("profile name is not valid")
    return value
