"""Bounded evidence production for the C-001 detector campaign.

The campaign controller validates evidence, but it deliberately does not own
the machines which produce that evidence.  This module is the narrow producer
for the two authoritative environments used by C-001:

* a deterministic snapshot of the exact dirty v2 source tree plus the exact
  HEAD-derived v1 Python support, suitable for copying to the provisioned
  Linux gate; and
* identity-bound BUG A/B checks on the production RV1106 daemon plus the exact
  local E2/E5 pytest targets.

The approved ARM SHA-256 is an explicit operator trust anchor, not a claim that
this module rebuilt the daemon.  The bench operator must freshly build it with
the pinned SDK and retain that build log; this producer proves that the exact
approved bytes were pushed, hashed remotely, and executed with the current
source digest beside them.

Every retained file is created exclusively.  Re-running a command therefore
requires a new output prefix/directory; it can never turn old evidence into a
new claim by overwriting it.

Authoritative pytest children never execute from the live checkout.  Gate and
BUG E2/E5 runs receive a fresh temporary tree containing exactly the pinned
manifest members; ignored ``conftest.py``/bytecode and other live extras cannot
enter their import or collection paths.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import posixpath
import re
import secrets
import shlex
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from skyweave2.edge import campaign_c001 as c001
from skyweave2.edge import campaign_c001_run as c001_run
from skyweave2.edge import provision

SOURCE_MANIFEST_SCHEMA = "skyweave-c001-source-tree/1"
FIXTURE_MANIFEST_SCHEMA = "skyweave-c001-gate-fixtures/1"
SUBJECT_EVIDENCE_SCHEMA = "skyweave-c001-subject-evidence/1"
CHECK_TRANSCRIPT_SCHEMA = "skyweave-c001-check-transcript/1"

SOURCE_PREFIX = "v2"
CAMPAIGN_RUNTIME_PREFIX = "v2/docs/campaigns/C-001/"
CAMPAIGN_HISTORY_PREFIX = "v2/docs/campaigns/C-001-shifts/"
SOURCE_EXCLUDED_PREFIXES = (
    CAMPAIGN_RUNTIME_PREFIX,
    CAMPAIGN_HISTORY_PREFIX,
)
BUNDLE_MANIFEST_NAME = "v2/docs/campaigns/C-001/source-tree.json"
SUPPORT_BUNDLE_MANIFEST_NAME = "v2/docs/campaigns/C-001/gate-support.json"
MIN_GATE_RMEM_BYTES = 4 * 1024 * 1024
GATE_DATA_ROOTS = (
    "output/exp001_clips/gate",
    "output/exp001_renders/gate",
    "output/exp001_multiblob",
)
GATE_V1_ROOT = "v1/src"
GATE_IMAGE_ROOT = "v2/firmware/rv1106/image"
GATE_FIXTURE_ROOTS = (*GATE_DATA_ROOTS, GATE_V1_ROOT, GATE_IMAGE_ROOT)
FENCED_CRITICAL_SCAN_ROOTS = (
    "v2/src/skyweave2/contracts",
    "v2/tests/contracts",
    "v2/proto",
    "v2/tests/edge/fixtures/gate",
)

FENCED_PATHS = (
    "v1",
    ":(glob)**/golden/**",
    "v2/docs/DETECTION_CONTRACTS_D0.md",
    "v2/src/skyweave2/contracts",
    "v2/tests/contracts",
    "v2/proto",
    "v2/tests/edge/fixtures/gate",
)
FENCED_COMMAND = (
    "git",
    "status",
    "--porcelain",
    "--untracked-files=all",
    "--",
    *FENCED_PATHS,
)

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_DOCKER_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_PASS_COUNT = re.compile(r"\b[1-9][0-9]* passed\b", re.IGNORECASE)
_NON_AUTHORITATIVE_PYTEST = re.compile(
    r"\b(?:failed|error|errors|skipped|xfailed|xpassed|deselected|warning|warnings)\b",
    re.IGNORECASE,
)
_RMEM_FACTS = re.compile(
    r"(?:^|;)rmem_max=([0-9]+);rmem_default=([0-9]+)(?:;|$)"
)


class EvidenceError(c001.CampaignError):
    """An evidence command could not make the claim it was asked to retain."""


class RunProcess(Protocol):
    """The subprocess seam used by gate/fenced/host-check unit tests."""

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class SourceSnapshot:
    manifest_path: Path
    source_tree_sha256: str
    revision_sha: str
    bundle_path: Path | None = None


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _exclusive_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise EvidenceError(f"refusing symlink output {path}")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise EvidenceError(f"refusing to overwrite retained evidence {path}") from exc


def _read_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink():
        raise EvidenceError(f"refusing symlinked JSON input {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read JSON object {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"JSON input {source} must be an object")
    return payload


def _relative_path(raw: str | Path, *, label: str) -> Path:
    text = str(raw)
    path = Path(text)
    if (
        not text
        or path.is_absolute()
        or text != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise EvidenceError(f"{label} must be one canonical relative path")
    return path


def _campaign_member(campaign_root: str | Path, raw: str | Path, *, label: str) -> Path:
    root = Path(campaign_root).resolve(strict=True)
    relative = _relative_path(raw, label=label)
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise EvidenceError(f"{label} escapes the campaign root") from exc
    return candidate


def _git(
    repo_root: Path,
    args: Sequence[str],
    *,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=text,
        check=False,
        env=_clean_git_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EvidenceError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _clean_git_environment() -> dict[str, str]:
    """Remove repository/index/config redirection from evidence-producing Git."""

    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("GIT_"):
            environment.pop(name, None)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _fenced_git_environment() -> dict[str, str]:
    environment = _clean_git_environment()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "status.showUntrackedFiles",
            "GIT_CONFIG_VALUE_0": "all",
            "GIT_CONFIG_KEY_1": "core.fileMode",
            "GIT_CONFIG_VALUE_1": "true",
            "GIT_CONFIG_KEY_2": "core.excludesFile",
            "GIT_CONFIG_VALUE_2": os.devnull,
            "GIT_CONFIG_KEY_3": "core.fsmonitor",
            "GIT_CONFIG_VALUE_3": "false",
        }
    )
    return environment


def _git_config_values(repo_root: Path, key: str) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--get-all", key],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_git_environment(),
    )
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        raise EvidenceError(f"cannot inspect Git config {key}: {completed.stderr.strip()}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _guard_fenced_git_config(repo_root: Path) -> None:
    ignore_path = repo_root / ".gitignore"
    ignore_tree = _git(repo_root, ["ls-tree", "HEAD", "--", ".gitignore"])
    tree_line = ignore_tree.stdout.strip()
    if not tree_line:
        if os.path.lexists(ignore_path):
            raise EvidenceError("fenced proof requires root .gitignore to match HEAD")
    else:
        expected_mode = tree_line.split(None, 1)[0]
        if (
            ignore_path.is_symlink()
            or not ignore_path.is_file()
            or _git(repo_root, ["show", "HEAD:.gitignore"], text=False).stdout
            != ignore_path.read_bytes()
            or (expected_mode == "100755")
            != bool(ignore_path.stat().st_mode & stat.S_IXUSR)
        ):
            raise EvidenceError("fenced proof requires root .gitignore to match HEAD")

    git_exclude_raw = _git(
        repo_root, ["rev-parse", "--git-path", "info/exclude"]
    ).stdout.strip()
    git_exclude = Path(git_exclude_raw)
    if not git_exclude.is_absolute():
        git_exclude = repo_root / git_exclude
    if git_exclude.is_symlink():
        raise EvidenceError("fenced proof refuses symlinked .git/info/exclude")
    if git_exclude.exists():
        active_excludes = [
            line
            for line in git_exclude.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if active_excludes:
            raise EvidenceError("fenced proof refuses active .git/info/exclude rules")
    if _git_config_values(repo_root, "core.excludesFile"):
        raise EvidenceError("fenced proof refuses configured core.excludesFile")
    fsmonitor = [
        value.lower() for value in _git_config_values(repo_root, "core.fsmonitor")
    ]
    if any(value != "false" for value in fsmonitor):
        raise EvidenceError("fenced proof refuses enabled core.fsmonitor")

    untracked = [value.lower() for value in _git_config_values(
        repo_root, "status.showUntrackedFiles"
    )]
    if any(value != "all" for value in untracked):
        raise EvidenceError("fenced proof refuses status.showUntrackedFiles other than all")
    filemode = [value.lower() for value in _git_config_values(repo_root, "core.fileMode")]
    if filemode != ["true"]:
        raise EvidenceError("fenced proof requires core.fileMode=true")
    flagged = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-v", "--", *FENCED_PATHS],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_git_environment(),
    )
    if flagged.returncode != 0:
        raise EvidenceError(f"cannot inspect fenced index flags: {flagged.stderr.strip()}")
    offenders = [
        line
        for line in flagged.stdout.splitlines()
        if line and (line[0].islower() or line[0] == "S")
    ]
    if offenders:
        raise EvidenceError(
            "fenced proof refuses assume-unchanged/skip-worktree index entries: "
            + ", ".join(offenders)
        )
    fsmonitor_flagged = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-f", "--", *FENCED_PATHS],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_git_environment(),
    )
    if fsmonitor_flagged.returncode != 0:
        raise EvidenceError(
            "cannot inspect fenced fsmonitor-valid flags: "
            f"{fsmonitor_flagged.stderr.strip()}"
        )
    fsmonitor_offenders = [
        line
        for line in fsmonitor_flagged.stdout.splitlines()
        if line and line[0].islower()
    ]
    if fsmonitor_offenders:
        raise EvidenceError(
            "fenced proof refuses fsmonitor-valid index entries: "
            + ", ".join(fsmonitor_offenders)
        )
    _guard_ignored_critical_fence_members(repo_root)


def _guard_ignored_critical_fence_members(repo_root: Path) -> None:
    """Refuse ignored bytes in code/contract fixture roots where absence matters."""

    golden_paths = _git(
        repo_root, ["ls-files", "-z", "--", ":(glob)**/golden/**"]
    ).stdout.split("\0")
    scan_roots = {*FENCED_CRITICAL_SCAN_ROOTS, "v1"}
    for raw_path in golden_paths:
        if not raw_path:
            continue
        parts = Path(raw_path).parts
        if "golden" in parts:
            scan_roots.add(Path(*parts[: parts.index("golden") + 1]).as_posix())
    pruned_names = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
    }
    for current, directory_names, _file_names in os.walk(
        repo_root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        relative_current = current_path.relative_to(repo_root)
        if relative_current.parts[:2] == (".claude", "worktrees"):
            directory_names[:] = []
            continue
        discovered: list[str] = []
        retained: list[str] = []
        for name in directory_names:
            candidate = current_path / name
            if name == "golden":
                discovered.append(candidate.relative_to(repo_root).as_posix())
            if name not in pruned_names and not candidate.is_symlink():
                retained.append(name)
        scan_roots.update(discovered)
        directory_names[:] = retained

    candidates: list[str] = []
    for raw_root in sorted(scan_roots):
        root = repo_root / raw_root
        if not os.path.lexists(root):
            continue
        members = [root] if root.is_symlink() or root.is_file() else root.rglob("*")
        for member in members:
            if member.is_symlink() or member.is_file():
                relative = member.relative_to(repo_root).as_posix()
                if not _is_generated_output(relative):
                    candidates.append(relative)
    if not candidates:
        return
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "-z", "--stdin"],
        input="\0".join(sorted(set(candidates))) + "\0",
        capture_output=True,
        text=True,
        check=False,
        env=_fenced_git_environment(),
    )
    if completed.returncode not in {0, 1}:
        raise EvidenceError(
            f"cannot inspect ignored critical fence members: {completed.stderr.strip()}"
        )
    ignored = [path for path in completed.stdout.split("\0") if path]
    if ignored:
        preview = ", ".join(ignored[:20])
        if len(ignored) > 20:
            preview += f", ... ({len(ignored) - 20} more)"
        raise EvidenceError(
            "fenced proof refuses ignored members in critical fence roots: "
            + preview
        )


def _repo_root(path: str | Path) -> Path:
    requested = Path(path).resolve(strict=True)
    actual = Path(
        _git(requested, ["rev-parse", "--show-toplevel"]).stdout.strip()
    ).resolve(strict=True)
    if actual != requested:
        raise EvidenceError(f"repo root must be {actual}, not {requested}")
    _guard_git_object_rewrites(requested)
    return requested


def _guard_git_object_rewrites(repo_root: Path) -> None:
    replacements = _git(
        repo_root,
        ["for-each-ref", "--format=%(refname)", "refs/replace"],
    ).stdout.splitlines()
    if replacements:
        raise EvidenceError("evidence production refuses Git replace refs")
    grafts_raw = _git(
        repo_root,
        ["rev-parse", "--git-path", "info/grafts"],
    ).stdout.strip()
    grafts = Path(grafts_raw)
    if not grafts.is_absolute():
        grafts = repo_root / grafts
    if grafts.is_symlink():
        raise EvidenceError("evidence production refuses symlinked Git grafts")
    if grafts.exists():
        active = [
            line
            for line in grafts.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if active:
            raise EvidenceError("evidence production refuses active Git grafts")


def _path_has_prefix(path: str, prefix: str) -> bool:
    return path == prefix.rstrip("/") or path.startswith(prefix)


def _source_path_is_excluded(path: str) -> bool:
    return any(_path_has_prefix(path, prefix) for prefix in SOURCE_EXCLUDED_PREFIXES)


def _git_source_paths(repo_root: Path) -> list[str]:
    tracked = _git(repo_root, ["ls-files", "-z", "--", SOURCE_PREFIX]).stdout
    untracked = _git(
        repo_root,
        ["ls-files", "-z", "--others", "--exclude-standard", "--", SOURCE_PREFIX],
    ).stdout
    paths = {item for item in (tracked + untracked).split("\0") if item}
    kept: list[str] = []
    for raw in sorted(paths):
        relative = _relative_path(raw, label="source path")
        normalized = relative.as_posix()
        if _source_path_is_excluded(normalized):
            continue
        if normalized != SOURCE_PREFIX and not normalized.startswith(f"{SOURCE_PREFIX}/"):
            raise EvidenceError(f"git returned an out-of-scope source path {normalized!r}")
        kept.append(normalized)
    return kept


def _mode_and_payload(path: Path) -> tuple[str, str, bytes]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        payload = os.fsencode(os.readlink(path))
        return "120000", "symlink", payload
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError(f"source input must be a file or symlink: {path}")
    payload = path.read_bytes()
    mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
    return mode, "file", payload


def _source_entry(repo_root: Path, relative: str) -> dict[str, object]:
    source = repo_root / relative
    if not source.exists() and not source.is_symlink():
        raise EvidenceError(f"tracked source input is missing: {relative}")
    mode, kind, payload = _mode_and_payload(source)
    return {
        "path": relative,
        "type": kind,
        "mode": mode,
        "size": len(payload),
        "sha256": _sha256_bytes(payload),
    }


def _validate_symlink_closure(
    repo_root: Path, files: Sequence[Mapping[str, object]]
) -> None:
    """Require every link hop and its final regular target in the manifest."""

    by_path = {str(entry["path"]): entry for entry in files}
    for origin, entry in by_path.items():
        if entry["type"] != "symlink":
            continue
        current = origin
        seen: set[str] = set()
        while by_path[current]["type"] == "symlink":
            if current in seen:
                raise EvidenceError(f"source symlink cycle is not allowed: {origin}")
            seen.add(current)
            raw_target = os.readlink(repo_root / current)
            if Path(raw_target).is_absolute():
                raise EvidenceError(f"source symlink target escapes the tree: {origin}")
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(current), raw_target)
            )
            if target == ".." or target.startswith("../") or target not in by_path:
                raise EvidenceError(
                    f"source symlink target is not a manifested input: {origin} -> {target}"
                )
            current = target
        if by_path[current]["type"] != "file":  # pragma: no cover - closed type set
            raise EvidenceError(f"source symlink does not terminate at a file: {origin}")


def _tree_digest(
    revision_sha: str,
    files: Sequence[Mapping[str, object]],
    v1_head: Mapping[str, object] | None = None,
) -> str:
    material: dict[str, object] = {
        "revision_sha": revision_sha,
        "files": list(files),
    }
    if v1_head is not None:
        material["v1_head"] = dict(v1_head)
    return _sha256_bytes(
        _canonical_json(material)
    )


def _output_is_outside_source(repo_root: Path, output: Path, *, label: str) -> None:
    resolved = output.resolve(strict=False)
    try:
        relative = resolved.relative_to(repo_root).as_posix()
    except ValueError:
        return
    if relative == SOURCE_PREFIX or (
        relative.startswith(f"{SOURCE_PREFIX}/")
        and relative != CAMPAIGN_RUNTIME_PREFIX.rstrip("/")
        and not relative.startswith(CAMPAIGN_RUNTIME_PREFIX)
    ):
        raise EvidenceError(
            f"{label} must be outside v2 build inputs or under {CAMPAIGN_RUNTIME_PREFIX}"
        )


def _write_normalized_bundle(
    repo_root: Path,
    manifest: Mapping[str, object],
    destination: Path,
) -> None:
    _output_is_outside_source(repo_root, destination, label="source bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = destination.open("xb")
    except FileExistsError as exc:
        raise EvidenceError(f"refusing to overwrite retained evidence {destination}") from exc
    try:
        with raw, tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
            manifest_bytes = _canonical_json(manifest)
            manifest_info = tarfile.TarInfo(BUNDLE_MANIFEST_NAME)
            manifest_info.size = len(manifest_bytes)
            manifest_info.mode = 0o644
            manifest_info.mtime = 0
            manifest_info.uid = 0
            manifest_info.gid = 0
            manifest_info.uname = ""
            manifest_info.gname = ""
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
            bundle_entries = [*manifest["files"], *manifest["v1_head"]["files"]]
            for entry in bundle_entries:
                relative = str(entry["path"])
                source = repo_root / relative
                info = tarfile.TarInfo(relative)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                if entry["type"] == "symlink":
                    target = os.readlink(source)
                    if Path(target).is_absolute() or ".." in Path(target).parts:
                        raise EvidenceError(
                            f"unsafe symlink cannot enter source bundle: {relative}"
                        )
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    info.mode = 0o777
                    info.size = 0
                    archive.addfile(info)
                else:
                    info.mode = 0o755 if entry["mode"] == "100755" else 0o644
                    info.size = int(entry["size"])
                    with source.open("rb") as handle:
                        archive.addfile(info, handle)
    except Exception:
        # A partial archive is evidence of failure, not a reusable destination.
        # Keep it rather than overwriting/deleting an artifact the caller may inspect.
        raise


def create_source_snapshot(
    repo_root: str | Path,
    manifest_path: str | Path,
    *,
    bundle_path: str | Path | None = None,
) -> SourceSnapshot:
    """Hash dirty v2 inputs plus exact HEAD-derived v1 gate support."""

    root = _repo_root(repo_root)
    output = Path(manifest_path).resolve(strict=False)
    _output_is_outside_source(root, output, label="source manifest")
    revision = _git(root, ["rev-parse", "HEAD"]).stdout.strip()
    if _HEX40.fullmatch(revision) is None:
        raise EvidenceError("base HEAD revision is not 40 lowercase hexadecimal characters")
    files = [_source_entry(root, relative) for relative in _git_source_paths(root)]
    if not files:
        raise EvidenceError("source manifest cannot be empty")
    _validate_symlink_closure(root, files)
    v1_head = _v1_head_manifest(root, revision)
    digest = _tree_digest(revision, files, v1_head)
    payload = {
        "schema": SOURCE_MANIFEST_SCHEMA,
        "revision_sha": revision,
        "source_tree_sha256": digest,
        "files": files,
        "v1_head": v1_head,
    }
    _exclusive_write(output, _canonical_json(payload))
    bundle: Path | None = None
    if bundle_path is not None:
        bundle = Path(bundle_path).resolve(strict=False)
        _write_normalized_bundle(root, payload, bundle)
    verify_source_snapshot(root, output)
    return SourceSnapshot(output, digest, revision, bundle)


def _filesystem_source_paths(tree_root: Path) -> list[str]:
    source = tree_root / SOURCE_PREFIX
    if not source.is_dir() or source.is_symlink():
        raise EvidenceError("extracted tree lacks a real v2 directory")
    paths: list[str] = []
    for candidate in source.rglob("*"):
        relative = candidate.relative_to(tree_root).as_posix()
        if _source_path_is_excluded(relative):
            continue
        if candidate.is_symlink() or candidate.is_file():
            paths.append(relative)
    return sorted(paths)


def verify_source_snapshot(
    tree_root: str | Path,
    manifest_path: str | Path,
    *,
    allow_generated_outputs: bool = False,
    allowed_external_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Verify the manifest digest, every member, and the exact source member set."""

    root = Path(tree_root).resolve(strict=True)
    if (root / ".git").exists():
        _guard_git_object_rewrites(root)
    payload = _read_json(manifest_path)
    if set(payload) != {
        "schema",
        "revision_sha",
        "source_tree_sha256",
        "files",
        "v1_head",
    }:
        raise EvidenceError("source manifest fields are incomplete or unknown")
    if payload["schema"] != SOURCE_MANIFEST_SCHEMA:
        raise EvidenceError("source manifest schema is wrong")
    revision = payload["revision_sha"]
    digest = payload["source_tree_sha256"]
    files = payload["files"]
    v1_head = payload["v1_head"]
    if not isinstance(revision, str) or _HEX40.fullmatch(revision) is None:
        raise EvidenceError("source manifest revision is malformed")
    if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
        raise EvidenceError("source manifest tree digest is malformed")
    if not isinstance(files, list) or not files:
        raise EvidenceError("source manifest files must be a nonempty list")
    expected_paths: list[str] = []
    for entry in files:
        if not isinstance(entry, Mapping) or set(entry) != {
            "path",
            "type",
            "mode",
            "size",
            "sha256",
        }:
            raise EvidenceError("source manifest file entry is malformed")
        raw_path = entry["path"]
        if not isinstance(raw_path, str):
            raise EvidenceError("source manifest path must be a string")
        relative = _relative_path(raw_path, label="source manifest path").as_posix()
        if not relative.startswith(f"{SOURCE_PREFIX}/") or _source_path_is_excluded(
            relative
        ):
            raise EvidenceError(f"source manifest contains out-of-scope path {relative!r}")
        if entry["type"] not in {"file", "symlink"}:
            raise EvidenceError(f"source manifest type is invalid for {relative}")
        actual = _source_entry(root, relative)
        if dict(entry) != actual:
            raise EvidenceError(f"source input differs from manifest: {relative}")
        expected_paths.append(relative)
    if expected_paths != sorted(set(expected_paths)):
        raise EvidenceError("source manifest paths must be unique and sorted")
    _validate_symlink_closure(root, files)
    _validate_v1_head_manifest(root, revision, v1_head)
    if _tree_digest(revision, files, v1_head) != digest:
        raise EvidenceError("source_tree_sha256 does not match the canonical manifest")
    if (root / ".git").exists():
        checked_repo = _repo_root(root)
        actual_revision = _git(checked_repo, ["rev-parse", "HEAD"]).stdout.strip()
        if actual_revision != revision:
            raise EvidenceError("source manifest base HEAD differs from the checkout")
        actual_paths = _git_source_paths(checked_repo)
    else:
        actual_paths = _filesystem_source_paths(root)
    if actual_paths != expected_paths:
        missing = sorted(set(expected_paths) - set(actual_paths))
        unexpected = sorted(set(actual_paths) - set(expected_paths))
        if allow_generated_outputs:
            allowed = allowed_external_paths or set()
            unexpected = [
                path
                for path in unexpected
                if path not in allowed and not _is_generated_output(path)
            ]
        if not missing and not unexpected:
            return payload
        raise EvidenceError(
            f"source member set differs from manifest; missing={missing}, unexpected={unexpected}"
        )
    return payload


def _is_generated_output(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in parts)
        or any(part.endswith((".egg-info", ".dist-info")) for part in parts)
        or (
            relative.endswith((".pyc", ".pyo"))
            and "__pycache__" in parts
        )
        or parts[-1] in {".coverage", "coverage.xml"}
    )


def _fixture_tree_digest(files: Sequence[Mapping[str, object]]) -> str:
    return _sha256_bytes(
        _canonical_json({"roots": list(GATE_FIXTURE_ROOTS), "files": list(files)})
    )


def _git_paths_under(repo_root: Path, prefix: str) -> list[str]:
    tracked = _git(repo_root, ["ls-files", "-z", "--", prefix]).stdout
    untracked = _git(
        repo_root,
        ["ls-files", "-z", "--others", "--exclude-standard", "--", prefix],
    ).stdout
    return sorted({path for path in (tracked + untracked).split("\0") if path})


def _head_v1_paths(repo_root: Path) -> list[str]:
    raw_entries = _git(
        repo_root,
        ["ls-tree", "-r", "-z", "HEAD", "--", GATE_V1_ROOT],
    ).stdout.split("\0")
    paths: list[str] = []
    for raw_entry in raw_entries:
        if not raw_entry:
            continue
        metadata, relative = raw_entry.split("\t", 1)
        mode, kind, _object_id = metadata.split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise EvidenceError(f"v1 gate support must be a regular HEAD file: {relative}")
        paths.append(relative)
    if not paths:
        raise EvidenceError(f"gate support root is empty in HEAD: {GATE_V1_ROOT}")
    return sorted(paths)


def _verify_v1_matches_head(repo_root: Path) -> list[str]:
    expected = _head_v1_paths(repo_root)
    actual = [
        path
        for path in _physical_regular_paths(repo_root, GATE_V1_ROOT)
        if not _is_generated_output(path)
    ]
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        raise EvidenceError(
            "v1 gate support must match HEAD exactly; "
            f"missing={missing}, unexpected={unexpected}"
        )
    for relative in expected:
        worktree = repo_root / relative
        head_bytes = _git(repo_root, ["show", f"HEAD:{relative}"], text=False).stdout
        head_mode = _git(repo_root, ["ls-tree", "HEAD", "--", relative]).stdout.split()[0]
        worktree_mode, _kind, worktree_bytes = _mode_and_payload(worktree)
        if worktree_bytes != head_bytes or worktree_mode != head_mode:
            raise EvidenceError(f"v1 gate support differs from HEAD: {relative}")
    return expected


def _v1_head_manifest(repo_root: Path, revision_sha: str) -> dict[str, object]:
    paths = _verify_v1_matches_head(repo_root)
    files = [_source_entry(repo_root, relative) for relative in paths]
    tree_oid = _git(repo_root, ["rev-parse", f"HEAD:{GATE_V1_ROOT}"]).stdout.strip()
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tree_oid) is None:
        raise EvidenceError("v1 HEAD tree oid is malformed")
    tree_sha256 = _sha256_bytes(
        _canonical_json(
            {
                "revision_sha": revision_sha,
                "git_tree_oid": tree_oid,
                "files": files,
            }
        )
    )
    return {
        "git_tree_oid": tree_oid,
        "tree_sha256": tree_sha256,
        "files": files,
    }


def _validate_v1_head_manifest(
    tree_root: Path,
    revision_sha: str,
    block: object,
) -> dict[str, object]:
    if not isinstance(block, Mapping) or set(block) != {
        "git_tree_oid",
        "tree_sha256",
        "files",
    }:
        raise EvidenceError("source v1 HEAD binding is malformed")
    tree_oid = block["git_tree_oid"]
    tree_sha256 = block["tree_sha256"]
    files = block["files"]
    if (
        not isinstance(tree_oid, str)
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tree_oid) is None
        or not isinstance(tree_sha256, str)
        or _HEX64.fullmatch(tree_sha256) is None
        or not isinstance(files, list)
        or not files
    ):
        raise EvidenceError("source v1 HEAD binding is malformed")
    expected_paths: list[str] = []
    for entry in files:
        if not isinstance(entry, Mapping) or set(entry) != {
            "path",
            "type",
            "mode",
            "size",
            "sha256",
        }:
            raise EvidenceError("source v1 HEAD file entry is malformed")
        raw_path = entry["path"]
        if not isinstance(raw_path, str):
            raise EvidenceError("source v1 HEAD path is malformed")
        relative = _relative_path(raw_path, label="source v1 HEAD path").as_posix()
        if (
            not relative.startswith(f"{GATE_V1_ROOT}/")
            or entry["type"] != "file"
            or entry["mode"] not in {"100644", "100755"}
            or not isinstance(entry["size"], int)
            or isinstance(entry["size"], bool)
            or int(entry["size"]) < 0
            or not isinstance(entry["sha256"], str)
            or _HEX64.fullmatch(str(entry["sha256"])) is None
        ):
            raise EvidenceError("source v1 HEAD file entry is malformed")
        if dict(entry) != _source_entry(tree_root, relative):
            raise EvidenceError(f"source v1 HEAD input differs: {relative}")
        expected_paths.append(relative)
    if expected_paths != sorted(set(expected_paths)):
        raise EvidenceError("source v1 HEAD paths must be unique and sorted")
    actual_paths = [
        path
        for path in _physical_regular_paths(tree_root, GATE_V1_ROOT)
        if not _is_generated_output(path)
    ]
    if actual_paths != expected_paths:
        raise EvidenceError("source v1 HEAD member set differs from its binding")
    expected_tree_sha256 = _sha256_bytes(
        _canonical_json(
            {
                "revision_sha": revision_sha,
                "git_tree_oid": tree_oid,
                "files": files,
            }
        )
    )
    if expected_tree_sha256 != tree_sha256:
        raise EvidenceError("source v1 HEAD tree digest is invalid")
    checked = {
        "git_tree_oid": tree_oid,
        "tree_sha256": tree_sha256,
        "files": files,
    }
    if (tree_root / ".git").exists() and checked != _v1_head_manifest(
        tree_root, revision_sha
    ):
        raise EvidenceError("source v1 HEAD binding differs from Git HEAD")
    return checked


def _physical_regular_paths(repo_root: Path, raw_root: str) -> list[str]:
    root = repo_root / raw_root
    if root.is_symlink() or not root.is_dir():
        raise EvidenceError(f"gate support root is missing or symlinked: {raw_root}")
    paths: list[str] = []
    for source in sorted(root.rglob("*")):
        relative = source.relative_to(repo_root).as_posix()
        if source.is_symlink():
            raise EvidenceError(f"gate support symlink is forbidden: {relative}")
        if source.is_dir():
            continue
        if not source.is_file():
            raise EvidenceError(f"gate support member is not a regular file: {relative}")
        paths.append(relative)
    return paths


def _fixture_member_paths(
    repo_root: Path,
    source_manifest: Mapping[str, object] | None = None,
) -> list[str]:
    if source_manifest is None:
        raise EvidenceError("gate support verification requires the source manifest")
    paths: list[str] = []
    for data_root in GATE_DATA_ROOTS:
        members = _physical_regular_paths(repo_root, data_root)
        if not members:
            raise EvidenceError(f"gate support root is empty: {data_root}")
        paths.extend(members)
    v1_paths = [str(entry["path"]) for entry in source_manifest["v1_head"]["files"]]
    if not v1_paths:
        raise EvidenceError(f"gate support root is empty: {GATE_V1_ROOT}")
    paths.extend(v1_paths)
    image_paths = _physical_regular_paths(repo_root, GATE_IMAGE_ROOT)
    if (repo_root / ".git").exists():
        tracked_image = set(
            path
            for path in _git(repo_root, ["ls-files", "-z", "--", GATE_IMAGE_ROOT])
            .stdout.split("\0")
            if path
        )
    else:
        tracked_image = {
            str(entry["path"])
            for entry in source_manifest["files"]
            if str(entry["path"]).startswith(f"{GATE_IMAGE_ROOT}/")
        }
    payload_paths = [path for path in image_paths if path not in tracked_image]
    if not payload_paths:
        raise EvidenceError(f"gate support root has no payload files: {GATE_IMAGE_ROOT}")
    paths.extend(payload_paths)
    checked: list[str] = []
    for relative in sorted(set(paths)):
        if relative.startswith(f"{GATE_V1_ROOT}/"):
            checked.append(relative)
            continue
        source = repo_root / relative
        if source.is_symlink() or not source.is_file():
            raise EvidenceError(f"gate support member is missing or symlinked: {relative}")
        checked.append(relative)
    return checked


def _fixture_files(
    repo_root: Path,
    source_manifest: Mapping[str, object],
) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    authenticated_v1 = {
        str(entry["path"]): entry for entry in source_manifest["v1_head"]["files"]
    }
    for relative in _fixture_member_paths(repo_root, source_manifest):
        if relative in authenticated_v1:
            source_entry = authenticated_v1[relative]
            files.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": source_entry["size"],
                    "sha256": source_entry["sha256"],
                }
            )
        else:
            files.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": (repo_root / relative).stat().st_size,
                    "sha256": c001.sha256_file(repo_root / relative),
                }
            )
    return files


def create_fixture_manifest(
    repo_root: str | Path,
    source_manifest_path: str | Path,
    manifest_path: str | Path,
    *,
    bundle_path: str | Path | None = None,
) -> Path:
    """Bind external gate datasets/image payloads to authenticated v1 support."""

    root = _repo_root(repo_root)
    source_manifest = verify_source_snapshot(root, source_manifest_path)
    files = _fixture_files(root, source_manifest)
    v1_tree_oid = str(source_manifest["v1_head"]["git_tree_oid"])
    v1_files = [
        entry
        for entry in files
        if str(entry["path"]).startswith(f"{GATE_V1_ROOT}/")
    ]
    payload = {
        "schema": FIXTURE_MANIFEST_SCHEMA,
        "revision_sha": source_manifest["revision_sha"],
        "source_tree_sha256": source_manifest["source_tree_sha256"],
        "roots": list(GATE_FIXTURE_ROOTS),
        "fixture_tree_sha256": _fixture_tree_digest(files),
        "v1_head_tree_oid": v1_tree_oid,
        "v1_head_tree_sha256": _sha256_bytes(
            _canonical_json(
                {
                    "revision_sha": source_manifest["revision_sha"],
                    "git_tree_oid": v1_tree_oid,
                    "files": v1_files,
                }
            )
        ),
        "files": files,
    }
    destination = Path(manifest_path).resolve(strict=False)
    _exclusive_write(destination, _canonical_json(payload))
    verify_fixture_manifest(root, destination, source_manifest=source_manifest)
    if bundle_path is not None:
        _write_support_bundle(root, payload, Path(bundle_path).resolve(strict=False))
    return destination


def _write_support_bundle(
    repo_root: Path,
    manifest: Mapping[str, object],
    destination: Path,
) -> None:
    """Bundle only the small executable v1 support; large data stays external."""

    _output_is_outside_source(repo_root, destination, label="gate support bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = destination.open("xb")
    except FileExistsError as exc:
        raise EvidenceError(f"refusing to overwrite retained evidence {destination}") from exc
    with raw, tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        manifest_bytes = _canonical_json(manifest)
        manifest_info = tarfile.TarInfo(SUPPORT_BUNDLE_MANIFEST_NAME)
        manifest_info.size = len(manifest_bytes)
        manifest_info.mode = 0o644
        manifest_info.mtime = 0
        manifest_info.uid = manifest_info.gid = 0
        manifest_info.uname = manifest_info.gname = ""
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        for entry in manifest["files"]:
            relative = str(entry["path"])
            if not relative.startswith(f"{GATE_V1_ROOT}/"):
                continue
            source = repo_root / relative
            info = tarfile.TarInfo(relative)
            info.size = int(entry["size"])
            info.mode = 0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with source.open("rb") as handle:
                archive.addfile(info, handle)


def verify_fixture_manifest(
    repo_root: str | Path,
    manifest_path: str | Path,
    *,
    source_manifest: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Verify exact fixture membership and every retained content digest."""

    root = Path(repo_root).resolve(strict=True)
    if (root / ".git").exists():
        _guard_git_object_rewrites(root)
    if source_manifest is None:
        raise EvidenceError("gate support verification requires the source manifest")
    payload = _read_json(manifest_path)
    if set(payload) != {
        "schema",
        "revision_sha",
        "source_tree_sha256",
        "roots",
        "fixture_tree_sha256",
        "v1_head_tree_oid",
        "v1_head_tree_sha256",
        "files",
    }:
        raise EvidenceError("gate fixture manifest fields are incomplete or unknown")
    if payload["schema"] != FIXTURE_MANIFEST_SCHEMA:
        raise EvidenceError("gate fixture manifest schema is wrong")
    if (
        not isinstance(payload["revision_sha"], str)
        or _HEX40.fullmatch(payload["revision_sha"]) is None
        or not isinstance(payload["source_tree_sha256"], str)
        or _HEX64.fullmatch(payload["source_tree_sha256"]) is None
    ):
        raise EvidenceError("gate support source binding is malformed")
    if (
        payload["revision_sha"] != source_manifest.get("revision_sha")
        or payload["source_tree_sha256"] != source_manifest.get("source_tree_sha256")
    ):
        raise EvidenceError("gate support manifest differs from the source revision/tree")
    if payload["roots"] != list(GATE_FIXTURE_ROOTS):
        raise EvidenceError("gate fixture manifest roots are not the exact three datasets")
    tree_digest = payload["fixture_tree_sha256"]
    if not isinstance(tree_digest, str) or _HEX64.fullmatch(tree_digest) is None:
        raise EvidenceError("gate fixture tree digest is malformed")
    files = payload["files"]
    if not isinstance(files, list) or not files:
        raise EvidenceError("gate fixture manifest files must be nonempty")
    expected_paths: list[str] = []
    source_paths = {str(entry["path"]) for entry in source_manifest["files"]}
    authenticated_v1 = {
        str(entry["path"]): {
            "path": entry["path"],
            "type": "file",
            "size": entry["size"],
            "sha256": entry["sha256"],
        }
        for entry in source_manifest["v1_head"]["files"]
    }
    roots_seen = {fixture_root: False for fixture_root in GATE_FIXTURE_ROOTS}
    for entry in files:
        if not isinstance(entry, Mapping) or set(entry) != {
            "path",
            "type",
            "size",
            "sha256",
        }:
            raise EvidenceError("gate fixture manifest entry is malformed")
        raw_path = entry["path"]
        if not isinstance(raw_path, str):
            raise EvidenceError("gate fixture path must be a string")
        relative = _relative_path(raw_path, label="gate fixture path").as_posix()
        matching_roots = [
            fixture_root
            for fixture_root in GATE_FIXTURE_ROOTS
            if relative.startswith(f"{fixture_root}/")
        ]
        if len(matching_roots) != 1 or entry["type"] != "file":
            raise EvidenceError(f"gate fixture path/type is out of scope: {relative}")
        if relative in source_paths:
            raise EvidenceError(f"gate fixture overlaps the source manifest: {relative}")
        roots_seen[matching_roots[0]] = True
        size = entry["size"]
        digest = entry["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise EvidenceError(f"gate fixture size is invalid: {relative}")
        if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
            raise EvidenceError(f"gate fixture digest is invalid: {relative}")
        if relative.startswith(f"{GATE_V1_ROOT}/"):
            if authenticated_v1.get(relative) != dict(entry):
                raise EvidenceError(
                    "v1 fixture entries differ from authenticated source manifest"
                )
        else:
            source = root / relative
            if source.is_symlink() or not source.is_file():
                raise EvidenceError(
                    f"gate fixture is missing, non-file, or symlinked: {relative}"
                )
            if source.stat().st_size != size or c001.sha256_file(source) != digest:
                raise EvidenceError(f"gate fixture differs from manifest: {relative}")
        expected_paths.append(relative)
    if expected_paths != sorted(set(expected_paths)) or not all(roots_seen.values()):
        raise EvidenceError("gate fixture paths must be sorted, unique, and cover all roots")
    if _fixture_tree_digest(files) != tree_digest:
        raise EvidenceError("gate fixture tree digest does not match its manifest")
    v1_tree_oid = payload["v1_head_tree_oid"]
    v1_tree_sha256 = payload["v1_head_tree_sha256"]
    if (
        not isinstance(v1_tree_oid, str)
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", v1_tree_oid) is None
        or not isinstance(v1_tree_sha256, str)
        or _HEX64.fullmatch(v1_tree_sha256) is None
    ):
        raise EvidenceError("v1 HEAD tree identity is malformed")
    v1_files = [
        entry
        for entry in files
        if str(entry["path"]).startswith(f"{GATE_V1_ROOT}/")
    ]
    expected_v1_tree_sha256 = _sha256_bytes(
        _canonical_json(
            {
                "revision_sha": payload["revision_sha"],
                "git_tree_oid": v1_tree_oid,
                "files": v1_files,
            }
        )
    )
    if expected_v1_tree_sha256 != v1_tree_sha256:
        raise EvidenceError("v1 HEAD tree identity does not match its manifest")
    if (
        v1_tree_oid != source_manifest["v1_head"]["git_tree_oid"]
        or {str(entry["path"]): dict(entry) for entry in v1_files} != authenticated_v1
    ):
        raise EvidenceError("v1 fixture identity differs from authenticated source manifest")
    actual_paths = _fixture_member_paths(root, source_manifest)
    if actual_paths != expected_paths:
        missing = sorted(set(expected_paths) - set(actual_paths))
        unexpected = sorted(set(actual_paths) - set(expected_paths))
        raise EvidenceError(
            f"gate fixture member set differs; missing={missing}, unexpected={unexpected}"
        )
    if (root / ".git").exists():
        _guard_git_object_rewrites(root)
    return payload


def _materialize_manifest_tree(
    source_root: Path,
    staging_root: Path,
    manifest: Mapping[str, object],
) -> Path:
    """Copy exactly the pinned manifest members, never Git-filtered live extras."""

    pinned_manifest = staging_root / "c001-pinned-source-tree.json"
    _exclusive_write(pinned_manifest, _canonical_json(manifest))
    entries = [*manifest["files"], *manifest["v1_head"]["files"]]
    for entry in entries:
        if entry["type"] != "file":
            continue
        source = source_root / str(entry["path"])
        payload = source.read_bytes()
        if (
            len(payload) != entry["size"]
            or _sha256_bytes(payload) != entry["sha256"]
        ):
            raise EvidenceError(f"source changed while staging: {entry['path']}")
        destination = staging_root / str(entry["path"])
        _exclusive_write(destination, payload)
        destination.chmod(0o755 if entry["mode"] == "100755" else 0o644)
    for entry in entries:
        if entry["type"] != "symlink":
            continue
        source = source_root / str(entry["path"])
        target = os.readlink(source)
        target_bytes = os.fsencode(target)
        if (
            len(target_bytes) != entry["size"]
            or _sha256_bytes(target_bytes) != entry["sha256"]
        ):
            raise EvidenceError(f"source symlink changed while staging: {entry['path']}")
        destination = staging_root / str(entry["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, destination)
    # The runtime directory is deliberately outside the source digest, but
    # production CLI path guards require its canonical empty mount point.
    (staging_root / CAMPAIGN_RUNTIME_PREFIX.rstrip("/")).mkdir(parents=True)
    verify_source_snapshot(staging_root, pinned_manifest)
    return pinned_manifest


def _map_gate_fixtures(
    source_root: Path,
    staging_root: Path,
    fixture_manifest: Mapping[str, object],
) -> list[Path]:
    """Map only external data/image members; v1 comes from the source snapshot."""

    created_directories: set[Path] = set()
    for entry in fixture_manifest["files"]:
        relative = str(entry["path"])
        if relative.startswith(f"{GATE_V1_ROOT}/"):
            continue
        source = source_root / relative
        destination = staging_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(source, destination)
        created_directories.add(destination.parent)
    for directory in sorted(created_directories, key=lambda path: len(path.parts), reverse=True):
        directory.chmod(0o555)
    return sorted(created_directories, key=lambda path: len(path.parts))


def _verify_staged_gate_fixtures(
    staging_root: Path,
    fixture_root: Path,
    fixture_manifest: Mapping[str, object],
) -> None:
    expected_v1: list[str] = []
    expected_data: dict[str, list[str]] = {root: [] for root in GATE_DATA_ROOTS}
    for entry in fixture_manifest["files"]:
        relative = str(entry["path"])
        staged = staging_root / relative
        if relative.startswith(f"{GATE_V1_ROOT}/"):
            if (
                staged.is_symlink()
                or not staged.is_file()
                or staged.stat().st_size != entry["size"]
                or _sha256_bytes(staged.read_bytes()) != entry["sha256"]
            ):
                raise EvidenceError(f"staged v1 support differs from manifest: {relative}")
            expected_v1.append(relative)
            continue
        source = fixture_root / relative
        if not staged.is_symlink() or os.readlink(staged) != str(source):
            raise EvidenceError(f"staged gate support mapping differs: {relative}")
        for data_root in GATE_DATA_ROOTS:
            if relative.startswith(f"{data_root}/"):
                expected_data[data_root].append(relative)
                break
    actual_v1 = [
        path.relative_to(staging_root).as_posix()
        for path in (staging_root / GATE_V1_ROOT).rglob("*")
        if (path.is_file() or path.is_symlink())
        and not _is_generated_output(path.relative_to(staging_root).as_posix())
    ]
    if sorted(actual_v1) != sorted(expected_v1):
        raise EvidenceError("staged v1 support member set differs from manifest")
    for data_root, expected in expected_data.items():
        actual = [
            path.relative_to(staging_root).as_posix()
            for path in (staging_root / data_root).rglob("*")
            if path.is_file() or path.is_symlink()
        ]
        if sorted(actual) != sorted(expected):
            raise EvidenceError(
                f"staged gate data member set differs from manifest: {data_root}"
            )


@contextmanager
def _staged_source_tree(
    source_root: Path,
    manifest: Mapping[str, object],
    *,
    fixture_manifest: Mapping[str, object] | None = None,
    fixture_root: Path | None = None,
):
    """Yield an exact executable tree and rehash both copies before cleanup."""

    with tempfile.TemporaryDirectory(prefix="skyweave-c001-source-") as raw_stage:
        stage = Path(raw_stage)
        pinned_manifest = _materialize_manifest_tree(source_root, stage, manifest)
        pinned_fixtures: Path | None = None
        read_only_directories: list[Path] = []
        allowed_staged_extras: set[str] = set()
        if fixture_manifest is not None:
            if fixture_root is None:
                raise EvidenceError("fixture_root is required with a gate support manifest")
            pinned_fixtures = stage / "c001-pinned-gate-fixtures.json"
            _exclusive_write(pinned_fixtures, _canonical_json(fixture_manifest))
            verify_fixture_manifest(
                fixture_root,
                pinned_fixtures,
                source_manifest=manifest,
            )
            read_only_directories = _map_gate_fixtures(
                fixture_root,
                stage,
                fixture_manifest,
            )
            allowed_staged_extras = {
                str(entry["path"])
                for entry in fixture_manifest["files"]
                if str(entry["path"]).startswith(f"{GATE_IMAGE_ROOT}/")
            }
        try:
            yield stage
        finally:
            try:
                verify_source_snapshot(
                    stage,
                    pinned_manifest,
                    allow_generated_outputs=True,
                    allowed_external_paths=allowed_staged_extras,
                )
                if pinned_fixtures is not None:
                    _verify_staged_gate_fixtures(
                        stage,
                        fixture_root,
                        fixture_manifest,
                    )
                    verify_fixture_manifest(
                        fixture_root,
                        pinned_fixtures,
                        source_manifest=manifest,
                    )
                verify_source_snapshot(
                    source_root,
                    pinned_manifest,
                    allow_generated_outputs=True,
                )
            finally:
                for directory in read_only_directories:
                    directory.chmod(0o755)


def _default_gate_platform() -> dict[str, object]:
    try:
        rmem_max = int(Path("/proc/sys/net/core/rmem_max").read_text().strip())
        rmem_default = int(Path("/proc/sys/net/core/rmem_default").read_text().strip())
    except (OSError, ValueError) as exc:
        raise EvidenceError("gate cannot read Linux receive-buffer facts") from exc
    compiler = platform.python_compiler().replace(";", ",") or "unknown"
    return {
        "os": platform.system(),
        "arch": platform.machine().lower(),
        "python": f"{sys.implementation.name} {platform.python_version()}",
        "toolchain": (
            f"python_compiler={compiler};python_optimize={sys.flags.optimize};"
            f"rmem_max={rmem_max};"
            f"rmem_default={rmem_default}"
        ),
        "rmem_max_bytes": rmem_max,
        "rmem_default_bytes": rmem_default,
    }


def _validate_gate_platform(facts: Mapping[str, object]) -> dict[str, object]:
    if set(facts) != {
        "os",
        "arch",
        "python",
        "toolchain",
        "rmem_max_bytes",
        "rmem_default_bytes",
    }:
        raise EvidenceError("gate platform must contain the six exact platform fields")
    for key in ("os", "arch", "python", "toolchain"):
        if not isinstance(facts[key], str) or not str(facts[key]).strip():
            raise EvidenceError(f"gate platform {key} must be one nonempty string")
    checked: dict[str, object] = {
        key: (str(value).strip() if isinstance(value, str) else value)
        for key, value in facts.items()
    }
    if checked["os"] != "Linux":
        raise EvidenceError("authoritative C-001 gate must run on Linux")
    if checked["arch"].lower() not in {"x86_64", "amd64"}:
        raise EvidenceError("authoritative C-001 gate must run on x86_64/amd64")
    match = _RMEM_FACTS.search(str(checked["toolchain"]))
    if match is None:
        raise EvidenceError("gate toolchain must retain rmem_max and rmem_default")
    for key in ("rmem_max_bytes", "rmem_default_bytes"):
        value = checked[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise EvidenceError(f"gate platform {key} must be an integer")
        if value < MIN_GATE_RMEM_BYTES:
            raise EvidenceError("gate receive buffers must both be at least 4 MiB")
    parsed = (int(match.group(1)), int(match.group(2)))
    retained = (int(checked["rmem_max_bytes"]), int(checked["rmem_default_bytes"]))
    if parsed != retained:
        raise EvidenceError("gate toolchain receive-buffer facts disagree with integer facts")
    if min(parsed) < MIN_GATE_RMEM_BYTES:
        raise EvidenceError("gate receive buffers must both be at least 4 MiB")
    if re.search(
        r"(?:^|;)python_optimize=0(?:;|$)", str(checked["toolchain"])
    ) is None:
        raise EvidenceError("authoritative C-001 gate requires python optimize level zero")
    return checked


def _clean_gate_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith(("PYTHON", "PYTEST", "SKYWEAVE_")):
            environment.pop(name, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _pytest_passed(stdout: str, returncode: int) -> bool:
    return (
        returncode == 0
        and _PASS_COUNT.search(stdout) is not None
        and _NON_AUTHORITATIVE_PYTEST.search(stdout) is None
    )


def _evidence_process(argv: Sequence[str], **kwargs: Any) -> Any:
    """Private subprocess seam; production callers cannot inject claimed results."""

    return subprocess.run(argv, **kwargs)


def _subject_paths(campaign_root: str | Path, prefix: str | Path) -> tuple[Path, Path]:
    base = _campaign_member(campaign_root, prefix, label="stdout prefix")
    return Path(f"{base}.stdout"), Path(f"{base}.json")


def run_gate_evidence(
    tree_root: str | Path,
    manifest_path: str | Path,
    campaign_root: str | Path,
    stdout_prefix: str | Path,
    *,
    fixture_manifest_path: str | Path,
    fixture_root: str | Path,
    timeout_s: float = 1800.0,
) -> Path:
    """Run the unselected full suite and retain a strict subject transcript."""

    root = Path(tree_root).resolve(strict=True)
    manifest = verify_source_snapshot(root, manifest_path)
    campaign = Path(campaign_root).resolve(strict=True)
    fixture_path = Path(fixture_manifest_path)
    if fixture_path.is_symlink():
        raise EvidenceError("gate fixture manifest cannot be a symlink")
    fixture_path = fixture_path.resolve(strict=True)
    try:
        fixture_relative = fixture_path.relative_to(campaign)
    except ValueError as exc:
        raise EvidenceError("gate fixture manifest must be retained under campaign root") from exc
    support_root = Path(fixture_root).resolve(strict=True)
    fixtures = verify_fixture_manifest(
        support_root,
        fixture_path,
        source_manifest=manifest,
    )
    facts = _validate_gate_platform(_default_gate_platform())
    stdout_path, transcript_path = _subject_paths(campaign_root, stdout_prefix)
    if stdout_path.exists() or transcript_path.exists():
        raise EvidenceError("gate evidence destination already exists")
    argv = [sys.executable, "-m", "pytest", "-q"]
    gate_environment = _clean_gate_environment()
    # Import the v2 package from this exact staged tree, not from an editable
    # install or an unbound wheel in the gate interpreter.  The v1 support
    # entry remains the exact HEAD-derived tree mapped beside it.
    gate_environment["PYTHONPATH"] = "src:../v1/src"
    with _staged_source_tree(
        root,
        manifest,
        fixture_manifest=fixtures,
        fixture_root=support_root,
    ) as staged_root:
        completed = _evidence_process(
            argv,
            cwd=staged_root / SOURCE_PREFIX,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=gate_environment,
        )
    stdout = str(completed.stdout)
    stderr = str(completed.stderr)
    passed = _pytest_passed(stdout, int(completed.returncode)) and not stderr.strip()
    completion = (
        f"C001_EVIDENCE_PASS kind=gate_platform_suite "
        f"revision={manifest['revision_sha']}"
    )
    retained_stdout = stdout
    if stderr:
        if retained_stdout and not retained_stdout.endswith("\n"):
            retained_stdout += "\n"
        retained_stdout += "--- captured stderr ---\n" + stderr
    if passed:
        if retained_stdout and not retained_stdout.endswith("\n"):
            retained_stdout += "\n"
        retained_stdout += completion + "\n"
    _exclusive_write(stdout_path, retained_stdout.encode("utf-8"))
    transcript = {
        "schema": SUBJECT_EVIDENCE_SCHEMA,
        "kind": "gate_platform_suite",
        "revision_sha": manifest["revision_sha"],
        "source_tree_sha256": manifest["source_tree_sha256"],
        "exit_code": int(completed.returncode),
        "asserted_outcome": passed,
        "command": shlex.join(argv),
        "stdout_path": stdout_path.relative_to(campaign).as_posix(),
        "stdout_sha256": c001.sha256_file(stdout_path),
        "platform": facts,
        "checked_paths": [],
        "changed_paths": [],
        "fixture_manifest_path": fixture_relative.as_posix(),
        "fixture_manifest_sha256": c001.sha256_file(fixture_path),
        "fixture_tree_sha256": fixtures["fixture_tree_sha256"],
        "pythonpath": "src:../v1/src",
    }
    _exclusive_write(transcript_path, _canonical_json(transcript))
    if not passed:
        raise EvidenceError(
            "full gate suite did not prove a clean PASS (fail/error/skip/xfail/xpass/"
            "deselection are all refusals)"
        )
    return transcript_path


def run_fenced_evidence(
    repo_root: str | Path,
    manifest_path: str | Path,
    campaign_root: str | Path,
    stdout_prefix: str | Path,
    *,
    timeout_s: float = 60.0,
) -> Path:
    """Run exactly the seven-path scoped status check and retain its result."""

    root = _repo_root(repo_root)
    _guard_fenced_git_config(root)
    manifest = verify_source_snapshot(root, manifest_path)
    stdout_path, transcript_path = _subject_paths(campaign_root, stdout_prefix)
    if stdout_path.exists() or transcript_path.exists():
        raise EvidenceError("fenced evidence destination already exists")
    completed = _evidence_process(
        list(FENCED_COMMAND),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        env=_fenced_git_environment(),
    )
    _guard_fenced_git_config(root)
    verify_source_snapshot(root, manifest_path, allow_generated_outputs=True)
    raw_stdout = str(completed.stdout)
    changed = [line for line in raw_stdout.splitlines() if line.strip()]
    passed = int(completed.returncode) == 0 and not changed
    completion = (
        f"C001_EVIDENCE_PASS kind=fenced_paths_status "
        f"revision={manifest['revision_sha']} changed_paths=0"
    )
    retained_stdout = raw_stdout
    if passed:
        if retained_stdout and not retained_stdout.endswith("\n"):
            retained_stdout += "\n"
        retained_stdout += completion + "\n"
    _exclusive_write(stdout_path, retained_stdout.encode("utf-8"))
    campaign = Path(campaign_root).resolve(strict=True)
    transcript = {
        "schema": SUBJECT_EVIDENCE_SCHEMA,
        "kind": "fenced_paths_status",
        "revision_sha": manifest["revision_sha"],
        "source_tree_sha256": manifest["source_tree_sha256"],
        "exit_code": int(completed.returncode),
        "asserted_outcome": passed,
        "command": shlex.join(FENCED_COMMAND),
        "stdout_path": stdout_path.relative_to(campaign).as_posix(),
        "stdout_sha256": c001.sha256_file(stdout_path),
        "platform": None,
        "checked_paths": list(FENCED_PATHS),
        "changed_paths": changed,
        "fixture_manifest_path": None,
        "fixture_manifest_sha256": None,
        "fixture_tree_sha256": None,
        "pythonpath": None,
    }
    _exclusive_write(transcript_path, _canonical_json(transcript))
    if not passed:
        raise EvidenceError("fenced paths are changed or scoped git status failed")
    return transcript_path


def package_subject_evidence(
    campaign_root: str | Path,
    manifest_path: str | Path,
    gate_transcript: str | Path,
    fenced_transcript: str | Path,
    output_path: str | Path,
    *,
    phase: str,
    zero_failure_confirmation: bool = False,
) -> Path:
    """Package gate/fence attachments and validate through the campaign guard."""

    campaign = Path(campaign_root).resolve(strict=True)
    manifest = _read_json(manifest_path)
    gate_relative = _relative_path(gate_transcript, label="gate transcript")
    fence_relative = _relative_path(fenced_transcript, label="fenced transcript")
    gate = campaign / gate_relative
    fence = campaign / fence_relative
    evidence = c001.subject_to_template(phase)
    evidence.update(
        {
            "gate_platform_suite_green": True,
            "fenced_paths_untouched": True,
            "probe_input_only": True,
            "revision_sha": manifest.get("revision_sha"),
            "source_tree_sha256": manifest.get("source_tree_sha256"),
            "gate_evidence": {
                "path": gate_relative.as_posix(),
                "sha256": c001.sha256_file(gate),
            },
            "fenced_evidence": {
                "path": fence_relative.as_posix(),
                "sha256": c001.sha256_file(fence),
            },
        }
    )
    if phase in {"climb", "confirmation"} and not zero_failure_confirmation:
        evidence["host_board_parity_within_tolerance"] = True
        evidence["discriminator_allows_climb"] = True
    c001.validate_subject_to(
        evidence,
        phase,
        zero_failure_confirmation=zero_failure_confirmation,
        evidence_root=campaign / "subject-validation-root.json",
    )
    destination = _campaign_member(campaign, output_path, label="subject output")
    _exclusive_write(destination, _canonical_json(evidence))
    # Validate the serialized bytes too; callers never rely on an in-memory object.
    c001.validate_subject_to(
        _read_json(destination),
        phase,
        zero_failure_confirmation=zero_failure_confirmation,
        evidence_root=campaign / "subject-validation-root.json",
    )
    return destination


def _check_digest(label: str, value: str) -> str:
    if _HEX64.fullmatch(value) is None:
        raise EvidenceError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _write_check_transcript(
    output_dir: Path,
    *,
    key: str,
    check: str,
    command: str,
    result: Any,
    passed: bool,
    revision_sha: str,
    source_tree_sha256: str,
    toolchain: str,
    identity: Mapping[str, str],
    binary_sha256: str,
    check_binary_sha256: str | None,
    check_binary_remote_sha256: str | None,
    runtime_ive_library_sha256: str | None,
) -> tuple[Path, dict[str, str]]:
    stdout = output_dir / f"{key}.stdout"
    stderr = output_dir / f"{key}.stderr"
    transcript_path = output_dir / f"{key}.json"
    _exclusive_write(stdout, str(result.stdout).encode("utf-8"))
    _exclusive_write(stderr, str(result.stderr).encode("utf-8"))
    transcript = {
        "schema": CHECK_TRANSCRIPT_SCHEMA,
        "check": check,
        "exit_code": int(result.returncode),
        "asserted_outcome": passed,
        "git_sha": revision_sha,
        "toolchain": toolchain,
        "command": command,
        "board_identity": dict(identity),
        "binary_sha256": binary_sha256,
        "check_binary_sha256": check_binary_sha256,
        "check_binary_remote_sha256": check_binary_remote_sha256,
        "runtime_ive_library_sha256": runtime_ive_library_sha256,
        "source_tree_sha256": source_tree_sha256,
        "stdout_path": stdout.name,
        "stdout_sha256": c001.sha256_file(stdout),
        "stderr_path": stderr.name,
        "stderr_sha256": c001.sha256_file(stderr),
    }
    _exclusive_write(transcript_path, _canonical_json(transcript))
    return transcript_path, {
        "path": transcript_path.name,
        "sha256": c001.sha256_file(transcript_path),
    }


def _selftest_passed(result: provision.CommandResult) -> bool:
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return payload == {
        "schema": "skyweave-ccl-selftest/1",
        "full_254_slot_region_scan": True,
        "mask_moment_centroid": True,
        "overlap_counter": True,
    }


def _validate_arm_elf(path: Path) -> None:
    info = path.stat()
    if info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) == 0:
        raise EvidenceError("approved ARM binary must be executable")
    header = path.read_bytes()[:20]
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise EvidenceError("approved ARM binary must be an ELF file")
    if header[4] != 1:
        raise EvidenceError("approved ARM daemon must be ELFCLASS32")
    if header[5] == 1:
        elf_type = struct.unpack_from("<H", header, 16)[0]
        machine = struct.unpack_from("<H", header, 18)[0]
    elif header[5] == 2:
        elf_type = struct.unpack_from(">H", header, 16)[0]
        machine = struct.unpack_from(">H", header, 18)[0]
    else:
        raise EvidenceError("approved ARM ELF has an invalid byte order")
    if machine != 40:  # ELF EM_ARM; RV1106 firmware is 32-bit Arm.
        raise EvidenceError("approved daemon ELF is not ARM")
    if elf_type not in {2, 3}:  # ET_EXEC or PIE/ET_DYN.
        raise EvidenceError("approved ARM daemon ELF is not executable/PIE")


def _produce_bug_verification(
    tree_root: str | Path,
    manifest_path: str | Path,
    campaign_root: str | Path,
    output_dir: str | Path,
    *,
    binary: str | Path,
    approved_binary_sha256: str,
    build_log: str | Path,
    docker_image_digest: str,
    build_command: str,
    expected_identity: c001.BoardIdentity,
    expected_kernel: str,
    ssh_host: str,
    jump_host: str,
    toolchain: str,
    python: str = sys.executable,
    ssh_user: str = "root",
    ssh_port: int = 22,
    ssh_identity: str | None = None,
    interface: str = "eth0",
    remote_base: str = "/userdata/skyweave/c001-proof",
    ld_library_path: str = "/oem/usr/lib",
    timeout_s: float = 300.0,
    transport: provision.NodeTransport | None = None,
    run: RunProcess = subprocess.run,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16),
) -> Path:
    """Produce Phase-1.1 proof using the operator-approved binary SHA trust anchor."""

    if not jump_host.strip():
        raise EvidenceError("BUG proof requires an explicit SSH ProxyJump host")
    if not toolchain.strip():
        raise EvidenceError("BUG proof requires a nonempty pinned toolchain description")
    if ld_library_path != "/oem/usr/lib":
        raise EvidenceError("BUG proof loader path is frozen at /oem/usr/lib")
    root = Path(tree_root).resolve(strict=True)
    manifest = verify_source_snapshot(root, manifest_path)
    binary_input = Path(binary)
    if binary_input.is_symlink():
        raise EvidenceError("approved ARM binary must be one real regular file")
    binary_path = binary_input.resolve(strict=True)
    if not binary_path.is_file():
        raise EvidenceError("approved ARM binary must be one real regular file")
    _validate_arm_elf(binary_path)
    approved = _check_digest("approved binary digest", approved_binary_sha256)
    local_binary_sha256 = c001.sha256_file(binary_path)
    if local_binary_sha256 != approved:
        raise EvidenceError("ARM binary does not match the approved digest")
    build_log_input = Path(build_log)
    if build_log_input.is_symlink():
        raise EvidenceError("operator build log must be one real regular file")
    build_log_path = build_log_input.resolve(strict=True)
    if not build_log_path.is_file():
        raise EvidenceError("operator build log must be one real regular file")
    build_log_bytes = build_log_path.read_bytes()
    if not build_log_bytes:
        raise EvidenceError("operator build log must be nonempty")
    if _DOCKER_DIGEST.fullmatch(docker_image_digest) is None:
        raise EvidenceError("pinned Docker image digest must be sha256:<64 lowercase hex>")
    try:
        build_argv = shlex.split(build_command)
    except ValueError as exc:
        raise EvidenceError("exact build command is malformed") from exc
    if not build_argv:
        raise EvidenceError("exact build command must be nonempty")
    campaign = Path(campaign_root).resolve(strict=True)
    destination = _campaign_member(campaign, output_dir, label="BUG output directory")
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise EvidenceError(f"refusing to overwrite BUG evidence directory {destination}") from exc
    retained_build_log = destination / "build.log"
    _exclusive_write(retained_build_log, build_log_bytes)
    spec = provision.NodeSpec(
        name=expected_identity.board,
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ld_library_path=ld_library_path,
    )
    if transport is None:
        checked_transport: provision.NodeTransport = provision.SshTransport(
            spec=spec,
            strict_host_key_checking="yes",
            identity=ssh_identity,
            jump_host=jump_host,
        )
    else:
        checked_transport = transport
        if isinstance(checked_transport, provision.SshTransport):
            if (
                checked_transport.spec != spec
                or checked_transport.jump_host != jump_host
                or checked_transport.strict_host_key_checking != "yes"
            ):
                raise EvidenceError("injected SSH transport is not the declared ProxyJump route")
    identity = c001_run.preflight_identity(
        checked_transport,
        expected_identity,
        expected_kernel=expected_kernel,
        interface=interface,
    )
    bound_identity = {
        "board": identity.board,
        "mac": identity.mac,
        "image_marker": identity.image_marker,
    }
    runtime_ive_sha256_before = c001_run.runtime_ive_library_sha256(
        checked_transport, spec
    )
    proof_id = token_factory()
    if not isinstance(proof_id, str) or _RUN_ID.fullmatch(proof_id) is None:
        raise EvidenceError("proof id must be 32 lowercase hexadecimal characters")
    if not remote_base.startswith("/") or ".." in Path(remote_base).parts:
        raise EvidenceError("remote proof base must be one clean absolute path")
    remote_dir = f"{remote_base.rstrip('/')}/proof-{proof_id}"
    reserve = checked_transport.run(
        f"test ! -e {shlex.quote(remote_dir)} && mkdir -m 700 {shlex.quote(remote_dir)}",
        timeout_s=15.0,
    )
    if reserve.returncode != 0:
        raise EvidenceError("could not reserve a unique remote BUG proof directory")
    remote_binary = f"{remote_dir}/skyweave-edge"
    checked_transport.push(binary_path, remote_binary)
    remote_hash_result = checked_transport.run(
        f"sha256sum {shlex.quote(remote_binary)}", timeout_s=30.0
    )
    remote_words = remote_hash_result.stdout.strip().split()
    if (
        remote_hash_result.returncode != 0
        or not remote_words
        or remote_words[0] != local_binary_sha256
    ):
        raise EvidenceError("remote production daemon hash differs from approved binary")

    exact_toolchain = f"{toolchain.strip()};board_kernel={identity.kernel}"
    evidence: dict[str, dict[str, str]] = {}
    commands: dict[str, str] = {}
    board_checks = {
        "bug_a_board": "full_254_slot_region_scan",
        "bug_b_board": "mask_moment_centroid_and_overlap_counter",
    }
    plain_board_command = shlex.join([remote_binary, "--self-test-ccl-measure"])
    remote_command = plain_board_command
    if ld_library_path:
        remote_command = (
            f"LD_LIBRARY_PATH={shlex.quote(ld_library_path)} {plain_board_command}"
        )
    for key, check in board_checks.items():
        result = checked_transport.run(remote_command, timeout_s=timeout_s)
        passed = _selftest_passed(result)
        _, block = _write_check_transcript(
            destination,
            key=key,
            check=check,
            command=remote_command,
            result=result,
            passed=passed,
            revision_sha=str(manifest["revision_sha"]),
            source_tree_sha256=str(manifest["source_tree_sha256"]),
            toolchain=exact_toolchain,
            identity=bound_identity,
            binary_sha256=local_binary_sha256,
            check_binary_sha256=local_binary_sha256,
            check_binary_remote_sha256=remote_words[0],
            runtime_ive_library_sha256=runtime_ive_sha256_before,
        )
        if not passed:
            raise EvidenceError(f"{key} production daemon self-test did not prove PASS")
        evidence[key] = block
        commands[key] = remote_command

    runtime_ive_sha256_after = c001_run.runtime_ive_library_sha256(
        checked_transport, spec
    )
    if runtime_ive_sha256_after != runtime_ive_sha256_before:
        raise EvidenceError("board IVE runtime library changed during BUG self-tests")

    manifest_members = {str(entry["path"]) for entry in manifest["files"]}
    with _staged_source_tree(root, manifest) as staged_root:
        for key, check, target in (
            ("e2", "nanopb_byte_identity", "tests/edge/test_e2_nanopb_parity.py"),
            ("e5", "host_fixture_replay", "tests/edge/test_e5_fixture_replay.py"),
        ):
            source_target = f"v2/{target}"
            if source_target not in manifest_members or not (root / source_target).is_file():
                raise EvidenceError(
                    f"exact BUG pytest target is absent from source tree: {target}"
                )
            argv = [python, "-m", "pytest", "-q", target]
            result = run(
                argv,
                cwd=staged_root / SOURCE_PREFIX,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env=_clean_gate_environment(),
            )
            passed = (
                _pytest_passed(str(result.stdout), int(result.returncode))
                and not str(result.stderr).strip()
            )
            command = shlex.join(argv)
            _, block = _write_check_transcript(
                destination,
                key=key,
                check=check,
                command=command,
                result=result,
                passed=passed,
                revision_sha=str(manifest["revision_sha"]),
                source_tree_sha256=str(manifest["source_tree_sha256"]),
                toolchain=exact_toolchain,
                identity=bound_identity,
                binary_sha256=local_binary_sha256,
                check_binary_sha256=None,
                check_binary_remote_sha256=None,
                runtime_ive_library_sha256=None,
            )
            if not passed:
                raise EvidenceError(f"{key} exact pytest target did not prove PASS")
            evidence[key] = block
            commands[key] = command

    artifact = destination / "bug-verification.json"
    payload = {
        "schema": c001.BUG_VERIFICATION_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "summary": {
            "bug_a_verified": True,
            "bug_b_verified": True,
            "e2_green": True,
            "e5_green": True,
        },
        "provenance": {
            "git_sha": manifest["revision_sha"],
            "source_tree_sha256": manifest["source_tree_sha256"],
            "toolchain": exact_toolchain,
            "commands": commands,
            "build": {
                "path": retained_build_log.name,
                "sha256": c001.sha256_file(retained_build_log),
                "image_digest": docker_image_digest,
                "command": build_command,
                "binary_sha256": local_binary_sha256,
            },
        },
        "binding": {
            "identity": bound_identity,
            "binary_sha256": local_binary_sha256,
            "runtime_ive_library": {
                "path": "/oem/usr/lib/librve.so",
                "sha256_before": runtime_ive_sha256_before,
                "sha256_after": runtime_ive_sha256_after,
                "stable": True,
            },
            "git_sha": manifest["revision_sha"],
            "source_tree_sha256": manifest["source_tree_sha256"],
        },
        "evidence": evidence,
    }
    _exclusive_write(artifact, _canonical_json(payload))
    c001.validate_bug_verification_bundle(artifact, _read_json(artifact))
    return artifact


def produce_bug_verification(
    tree_root: str | Path,
    manifest_path: str | Path,
    campaign_root: str | Path,
    output_dir: str | Path,
    *,
    binary: str | Path,
    approved_binary_sha256: str,
    build_log: str | Path,
    docker_image_digest: str,
    build_command: str,
    expected_identity: c001.BoardIdentity,
    expected_kernel: str,
    ssh_host: str,
    jump_host: str,
    toolchain: str,
    ssh_user: str = "root",
    ssh_port: int = 22,
    ssh_identity: str | None = None,
    interface: str = "eth0",
    remote_base: str = "/userdata/skyweave/c001-proof",
    timeout_s: float = 300.0,
) -> Path:
    """Run real strict SSH/subprocess checks with no caller-injected result seams."""

    return _produce_bug_verification(
        tree_root,
        manifest_path,
        campaign_root,
        output_dir,
        binary=binary,
        approved_binary_sha256=approved_binary_sha256,
        build_log=build_log,
        docker_image_digest=docker_image_digest,
        build_command=build_command,
        expected_identity=expected_identity,
        expected_kernel=expected_kernel,
        ssh_host=ssh_host,
        jump_host=jump_host,
        toolchain=toolchain,
        python=sys.executable,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ssh_identity=ssh_identity,
        interface=interface,
        remote_base=remote_base,
        ld_library_path="/oem/usr/lib",
        timeout_s=timeout_s,
        transport=None,
        run=subprocess.run,
        token_factory=lambda: secrets.token_hex(16),
    )


def _module_repo_root() -> Path:
    root = Path(__file__).resolve().parents[4]
    expected = root / "v2/src/skyweave2/edge/campaign_c001_evidence.py"
    if expected.resolve(strict=True) != Path(__file__).resolve(strict=True):
        raise EvidenceError("cannot derive the producer's canonical repository root")
    return root


def _require_cli_path(raw: str | Path, expected: Path, *, label: str) -> None:
    if Path(raw).resolve(strict=False) != expected.resolve(strict=False):
        raise EvidenceError(f"CLI {label} must be the producer's canonical {expected}")


def _require_cli_campaign_member(
    raw: str | Path, campaign_root: Path, *, label: str
) -> None:
    candidate = Path(raw).resolve(strict=False)
    try:
        candidate.relative_to(campaign_root.resolve(strict=False))
    except ValueError as exc:
        raise EvidenceError(f"CLI {label} must be under canonical {campaign_root}") from exc


def _guard_cli_scope(args: argparse.Namespace) -> None:
    """Keep production CLI evidence in the checkout which contains this code."""

    root = _module_repo_root()
    campaign = root / "v2/docs/campaigns/C-001"
    if args.command in {"manifest", "fixtures", "verify-fixtures", "fenced"}:
        _require_cli_path(args.repo_root, root, label="repo root")
    if args.command in {"verify", "gate", "bug"}:
        _require_cli_path(args.tree_root, root, label="tree root")
    if hasattr(args, "campaign_root"):
        _require_cli_path(args.campaign_root, campaign, label="campaign root")
    if hasattr(args, "manifest"):
        _require_cli_campaign_member(args.manifest, campaign, label="source manifest")
    if hasattr(args, "fixture_manifest"):
        _require_cli_campaign_member(
            args.fixture_manifest, campaign, label="fixture manifest"
        )
    if args.command == "manifest":
        _require_cli_campaign_member(args.out, campaign, label="source manifest")
        if args.bundle is not None:
            _require_cli_campaign_member(args.bundle, campaign, label="source bundle")
    if args.command == "fixtures":
        _require_cli_campaign_member(args.out, campaign, label="fixture manifest")
        if args.bundle is not None:
            _require_cli_campaign_member(args.bundle, campaign, label="support bundle")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest", help="create a source-tree manifest")
    manifest.add_argument("--repo-root", type=Path, required=True)
    manifest.add_argument("--out", type=Path, required=True)
    manifest.add_argument("--bundle", type=Path)

    fixtures = subparsers.add_parser("fixtures", help="hash exact gate datasets")
    fixtures.add_argument("--repo-root", type=Path, required=True)
    fixtures.add_argument("--manifest", type=Path, required=True)
    fixtures.add_argument("--out", type=Path, required=True)
    fixtures.add_argument("--bundle", type=Path)

    verify_fixtures = subparsers.add_parser(
        "verify-fixtures", help="verify gate-support manifest"
    )
    verify_fixtures.add_argument("--repo-root", type=Path, required=True)
    verify_fixtures.add_argument("--manifest", type=Path, required=True)
    verify_fixtures.add_argument("--fixture-manifest", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="verify a source-tree manifest")
    verify.add_argument("--tree-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)

    gate = subparsers.add_parser("gate", help="run the authoritative Linux full suite")
    gate.add_argument("--tree-root", type=Path, required=True)
    gate.add_argument("--manifest", type=Path, required=True)
    gate.add_argument("--fixture-manifest", type=Path, required=True)
    gate.add_argument("--fixture-root", type=Path, required=True)
    gate.add_argument("--campaign-root", type=Path, required=True)
    gate.add_argument("--stdout-prefix", required=True)
    gate.add_argument("--timeout-s", type=float, default=1800.0)

    fenced = subparsers.add_parser("fenced", help="prove the seven fenced paths untouched")
    fenced.add_argument("--repo-root", type=Path, required=True)
    fenced.add_argument("--manifest", type=Path, required=True)
    fenced.add_argument("--campaign-root", type=Path, required=True)
    fenced.add_argument("--stdout-prefix", required=True)

    subject = subparsers.add_parser("subject", help="package validated subject-to evidence")
    subject.add_argument("--campaign-root", type=Path, required=True)
    subject.add_argument("--manifest", type=Path, required=True)
    subject.add_argument("--gate-transcript", required=True)
    subject.add_argument("--fenced-transcript", required=True)
    subject.add_argument("--out", required=True)
    subject.add_argument("--phase", choices=("phase1", "climb", "confirmation"), required=True)
    subject.add_argument("--zero-failure-confirmation", action="store_true")

    bug = subparsers.add_parser("bug", help="produce identity-bound BUG A/B/E2/E5 proof")
    bug.add_argument("--tree-root", type=Path, required=True)
    bug.add_argument("--manifest", type=Path, required=True)
    bug.add_argument("--campaign-root", type=Path, required=True)
    bug.add_argument("--out-dir", required=True)
    bug.add_argument("--binary", type=Path, required=True)
    bug.add_argument("--approved-binary-sha256", required=True)
    bug.add_argument("--build-log", type=Path, required=True)
    bug.add_argument("--docker-image-digest", required=True)
    bug.add_argument("--build-command", required=True)
    bug.add_argument("--board", required=True)
    bug.add_argument("--host", required=True)
    bug.add_argument("--expected-mac", required=True)
    bug.add_argument("--expected-image-marker", required=True)
    bug.add_argument("--expected-kernel", required=True)
    bug.add_argument("--jump-host", required=True)
    bug.add_argument("--toolchain", required=True)
    bug.add_argument("--ssh-user", default="root")
    bug.add_argument("--ssh-port", type=int, default=22)
    bug.add_argument("--ssh-identity")
    bug.add_argument("--interface", default="eth0")
    bug.add_argument("--remote-base", default="/userdata/skyweave/c001-proof")
    bug.add_argument("--timeout-s", type=float, default=300.0)
    return parser


def _run_cli_operation(args: argparse.Namespace) -> None:
    if args.command == "manifest":
        snapshot = create_source_snapshot(args.repo_root, args.out, bundle_path=args.bundle)
        print(snapshot.source_tree_sha256)
    elif args.command == "fixtures":
        fixture_path = create_fixture_manifest(
            args.repo_root,
            args.manifest,
            args.out,
            bundle_path=args.bundle,
        )
        print(_read_json(fixture_path)["fixture_tree_sha256"])
    elif args.command == "verify-fixtures":
        source_manifest = verify_source_snapshot(args.repo_root, args.manifest)
        fixture_manifest = verify_fixture_manifest(
            args.repo_root,
            args.fixture_manifest,
            source_manifest=source_manifest,
        )
        print(fixture_manifest["fixture_tree_sha256"])
    elif args.command == "verify":
        print(verify_source_snapshot(args.tree_root, args.manifest)["source_tree_sha256"])
    elif args.command == "gate":
        print(
            run_gate_evidence(
                args.tree_root,
                args.manifest,
                args.campaign_root,
                args.stdout_prefix,
                fixture_manifest_path=args.fixture_manifest,
                fixture_root=args.fixture_root,
                timeout_s=args.timeout_s,
            )
        )
    elif args.command == "fenced":
        print(
            run_fenced_evidence(
                args.repo_root,
                args.manifest,
                args.campaign_root,
                args.stdout_prefix,
            )
        )
    elif args.command == "subject":
        print(
            package_subject_evidence(
                args.campaign_root,
                args.manifest,
                args.gate_transcript,
                args.fenced_transcript,
                args.out,
                phase=args.phase,
                zero_failure_confirmation=args.zero_failure_confirmation,
            )
        )
    elif args.command == "bug":
        print(
            produce_bug_verification(
                args.tree_root,
                args.manifest,
                args.campaign_root,
                args.out_dir,
                binary=args.binary,
                approved_binary_sha256=args.approved_binary_sha256,
                build_log=args.build_log,
                docker_image_digest=args.docker_image_digest,
                build_command=args.build_command,
                expected_identity=c001.BoardIdentity(
                    args.board, args.expected_mac, args.expected_image_marker
                ),
                expected_kernel=args.expected_kernel,
                ssh_host=args.host,
                jump_host=args.jump_host,
                toolchain=args.toolchain,
                ssh_user=args.ssh_user,
                ssh_port=args.ssh_port,
                ssh_identity=args.ssh_identity,
                interface=args.interface,
                remote_base=args.remote_base,
                timeout_s=args.timeout_s,
            )
        )
    else:  # pragma: no cover - argparse owns the closed command set
        raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    _guard_cli_scope(args)
    campaign = _module_repo_root() / "v2/docs/campaigns/C-001"
    with c001.campaign_shift_lock(campaign, exclusive=False):
        c001.validate_current_shift(campaign, verify_artifacts=True)
        _run_cli_operation(args)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "BUNDLE_MANIFEST_NAME",
    "CAMPAIGN_HISTORY_PREFIX",
    "CAMPAIGN_RUNTIME_PREFIX",
    "EvidenceError",
    "FENCED_COMMAND",
    "FENCED_PATHS",
    "MIN_GATE_RMEM_BYTES",
    "SOURCE_MANIFEST_SCHEMA",
    "SourceSnapshot",
    "create_source_snapshot",
    "main",
    "package_subject_evidence",
    "produce_bug_verification",
    "run_fenced_evidence",
    "run_gate_evidence",
    "verify_source_snapshot",
]
