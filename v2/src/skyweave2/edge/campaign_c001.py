"""Guarded campaign infrastructure for C-001.

This module is deliberately a *host-side* campaign controller.  It prepares
the exact short clip consumed by the existing RAM-loop source, records the
loop-slot truth, scores explicit board CCL diagnostics, and maintains an
append-only ledger.  It does not contain switch credentials, make network
calls, change detector contracts, or ratify a winning configuration.

The safety boundary is executable rather than conventional:

* only the four declared detector knobs (and their documented aliases) vary;
* resolution, warm-up, noise model, observation cap, and seed are checked;
* gate/acceptance inputs and path aliases are refused;
* a score needs at least 630 total / 600 post-warm-up frames and an explicit
  ``ccl_attempts`` denominator;
* ledger rows are hash chained, sequential, and appended with ``O_APPEND``
  only after their retained artifact has been opened and hashed; and
* confirmation can reach ``pending_ratification`` but never edits D0.

The CLI is intentionally small::

    python -m skyweave2.edge.campaign_c001 prepare --out ... --kind sparse --seed 101
    python -m skyweave2.edge.campaign_c001 host --manifest ... --out ...
    python -m skyweave2.edge.campaign_c001 record --ledger ... --artifact ... [...]
    python -m skyweave2.edge.campaign_c001 status --ledger ...
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import stat
import struct
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from skyweave2.contracts import proc_to_full
from skyweave2.detector.backends import make_backend
from skyweave2.detector.cap import apply_component_cap, component_confidence
from skyweave2.detector.components import (
    MaskComponent,
    _require_cv2,
    find_components,
    open_mask,
)
from skyweave2.detector.config import Backend, DetectorConfig
from skyweave2.detector.persistence import PersistenceFilter
from skyweave2.edge import benchmark
from skyweave2.edge.injection import iter_injection_frames, read_injection_session
from skyweave2.transport import codec
from skyweave2.transport.wire import PayloadType, unframe

CAMPAIGN_ID = "C-001"
PROBE_SCHEMA = "skyweave-c001-probe/1"
TRUTH_SCHEMA = "skyweave-c001-loop-truth/1"
HOST_SCHEMA = "skyweave-c001-host-discriminator/1"
SCORE_SCHEMA = "skyweave-c001-board-score/1"
RUN_BINDING_SCHEMA = "skyweave-c001-board-run-binding/1"
LEGACY_BUG_VERIFICATION_SCHEMA = "skyweave-c001-bug-verification/1"
BUG_VERIFICATION_SCHEMA = "skyweave-c001-bug-verification/2"
STOP_SCHEMA = "skyweave-c001-stop/1"
SHIFT_LINEAGE_SCHEMA = "skyweave-c001-shift-lineage/1"
SUCCESSOR_SCHEMA = "skyweave-c001-successor/1"
ROLLOVER_JOURNAL_SCHEMA = "skyweave-c001-rollover-journal/1"
SHIFT_HISTORY_DIRECTORY = "C-001-shifts"
SHIFT_LINEAGE_NAME = "lineage.jsonl"
SUCCESSOR_NAME = "SUCCESSOR.json"
GATE_FIXTURE_MANIFEST_SCHEMA = "skyweave-c001-gate-fixtures/1"
GATE_FIXTURE_ROOTS = (
    "output/exp001_clips/gate",
    "output/exp001_renders/gate",
    "output/exp001_multiblob",
    "v1/src",
    "v2/firmware/rv1106/image",
)

PROC_WIDTH = 1152
PROC_HEIGHT = 648
WARMUP_FRAMES = 30
MIN_POSTWARM_FRAMES = 600
MIN_TOTAL_FRAMES = WARMUP_FRAMES + MIN_POSTWARM_FRAMES
SCENE_NOISE_DN = 2.0
COMPONENT_CAP = 7
PERSISTENCE_FRAMES = 2
PERSISTENCE_GATE_PX = 12.0
REGION_TABLE_CAPACITY = 254
STANDARD_PROBE_MOVERS = 6
SPARSE_PROBE_MOVERS = 3
WIN_FAIL_RATE = 0.02
GATE_RMEM_MIN_BYTES = 4_194_304
GATE_ARCHES = frozenset({"x86_64", "amd64"})

MAX_EXPERIMENTS = 40
MAX_EXPERIMENT_MINUTES = 20.0
MAX_POWER_CYCLES = 6
MAX_CONSECUTIVE_REGRESSIONS = 8
MAX_WEDGES_PER_EXPERIMENT = 2
MAX_RECOVERY_CYCLES_PER_BOARD = 2
MAX_FAILED_MASKS = 16
RECOVERY_LEDGER_SCHEMA = "skyweave-c001-recovery-ledger/1"
ATTEMPT_LEDGER_SCHEMA = "skyweave-c001-attempt-ledger/1"

FG_MASK_MAGIC = b"SWFM"
FG_MASK_VERSION = 1
_FG_MASK_HEADER = ">4sB3sQIII"
_FG_MASK_HEADER_BYTES = struct.calcsize(_FG_MASK_HEADER)

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HEX_BINARY64 = re.compile(r"[0-9a-f]{16}\Z")
_MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z")
_SHIFT_ARCHIVE = re.compile(r"shift-[0-9]{4}-[0-9a-f]{12}\Z")
_FORBIDDEN_INPUT_TOKENS = {"accept", "acceptance", "d9", "gate", "golden"}

SUBJECT_TO_KEYS = frozenset(
    {
        "gate_platform_suite_green",
        "fenced_paths_untouched",
        "probe_input_only",
        "host_board_parity_within_tolerance",
        "discriminator_allows_climb",
    }
)
SUBJECT_TO_EVIDENCE_KEYS = frozenset(
    {"revision_sha", "source_tree_sha256", "gate_evidence", "fenced_evidence"}
)

_KNOB_ALIASES = {
    "min_area_px": "min_area_px",
    # The campaign name and DetectorConfig name are explicit aliases.  Giving
    # both in one experiment is refused rather than resolved by ordering.
    "morph_open": "open_radius_px",
    "open_radius_px": "open_radius_px",
    # The campaign document uses the hardware block's name; DetectorConfig
    # calls its host approximation ``ive_approx``.  Both spellings are listed
    # here so either side can serialize its native name without becoming a new
    # knob.
    "gmm2.match_sigmas": "ive_approx.match_sigmas",
    "ive_approx.match_sigmas": "ive_approx.match_sigmas",
    "gmm2.var_min": "ive_approx.var_min",
    "ive_approx.var_min": "ive_approx.var_min",
}

_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "campaign_id",
        "role",
        "probe_kind",
        "generator",
        "source_mode",
        "seed",
        "movers",
        "proc_width",
        "proc_height",
        "warmup_frames",
        "total_frames",
        "postwarm_frames",
        "noise_dn",
        "cap",
        "persistence_frames",
        "persistence_gate_px",
        "ram_clip_frames",
        "ram_loop_total_frames",
        "ram_loop_pts_stride_ns",
        "ram_budget_mb",
        "clip_path",
        "clip_sha256",
        "truth_path",
        "truth_sha256",
        "truth_schema",
        "truth_coordinate_space",
        "truth_loop_rule",
    }
)


class CampaignError(ValueError):
    """A C-001 input or operation crossed a declared boundary."""


class GuardrailViolation(CampaignError):
    """A subject-to, budget, or stop rule was violated."""


class SubjectToViolation(GuardrailViolation):
    """Retained Subject-to evidence failed structural validation."""


class LedgerIntegrityError(CampaignError):
    """The append-only ledger or one of its retained artifacts is invalid."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(name: str, value: object, low: int, high: int | None = None) -> int:
    if not _is_int(value):
        raise CampaignError(f"{name} must be an integer, got {value!r}")
    number = int(value)
    if number < low or (high is not None and number > high):
        ceiling = "" if high is None else f"..{high}"
        raise CampaignError(f"{name} must be in {low}{ceiling}, got {number}")
    return number


def _require_finite(name: str, value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignError(f"{name} must be a finite number, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise CampaignError(f"{name} must be in {low:g}..{high:g}, got {value!r}")
    return number


def validate_seed(seed: object) -> int:
    """Validate the explicit uint32 seed carried by every experiment."""

    return _require_int("seed", seed, 0, 2**32 - 1)


def normalize_knobs(knobs: Mapping[str, object]) -> dict[str, int | float]:
    """Return DetectorConfig-shaped knob names after strict whitelist checks."""

    if not isinstance(knobs, Mapping):
        raise CampaignError("knobs must be a JSON object")
    normalized: dict[str, int | float] = {}
    origins: dict[str, str] = {}
    for supplied, value in knobs.items():
        if supplied not in _KNOB_ALIASES:
            raise GuardrailViolation(
                f"{supplied!r} is not a C-001 knob; allowed names are "
                f"{sorted(_KNOB_ALIASES)}"
            )
        canonical = _KNOB_ALIASES[supplied]
        if canonical in normalized:
            raise GuardrailViolation(
                f"{supplied!r} and {origins[canonical]!r} alias the same knob; "
                "declare it once"
            )
        if canonical == "min_area_px":
            checked: int | float = _require_int(supplied, value, 2, 64)
        elif canonical == "open_radius_px":
            checked = _require_int(supplied, value, 0, 1)
        elif canonical == "ive_approx.match_sigmas":
            checked = _require_finite(supplied, value, 2.5, 4.0)
        elif canonical == "ive_approx.var_min":
            checked = _require_finite(supplied, value, 25.0, 100.0)
        else:  # pragma: no cover - the alias table and cases are one closed set
            raise AssertionError(canonical)
        normalized[canonical] = checked
        origins[canonical] = supplied
    return dict(sorted(normalized.items()))


def validate_frozen_settings(settings: Mapping[str, object], *, seed: int) -> None:
    """Reject drift in the five frozen run axes and require the declared seed."""

    expected = {
        "proc_width": PROC_WIDTH,
        "proc_height": PROC_HEIGHT,
        "warmup_frames": WARMUP_FRAMES,
        "noise_dn": SCENE_NOISE_DN,
        "cap": COMPONENT_CAP,
        "seed": validate_seed(seed),
    }
    missing = set(expected) - set(settings)
    unknown = set(settings) - set(expected)
    if missing or unknown:
        raise GuardrailViolation(
            f"frozen settings must contain exactly {sorted(expected)}; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    for name, frozen in expected.items():
        actual = settings[name]
        if isinstance(frozen, float):
            matches = (
                not isinstance(actual, bool)
                and isinstance(actual, (int, float))
                and float(actual) == frozen
            )
        else:
            matches = _is_int(actual) and int(actual) == frozen
        if not matches:
            raise GuardrailViolation(f"{name} is frozen at {frozen!r}, got {actual!r}")


def subject_to_template(phase: str) -> dict[str, bool | None]:
    """A serialization template; callers must replace evidence, not defaults."""

    if phase not in {"phase1", "climb", "confirmation"}:
        raise CampaignError(f"unknown campaign phase {phase!r}")
    return {
        "gate_platform_suite_green": False,
        "fenced_paths_untouched": False,
        "probe_input_only": False,
        "host_board_parity_within_tolerance": None,
        "discriminator_allows_climb": None,
    }


def validate_subject_to(
    evidence: Mapping[str, object],
    phase: str,
    *,
    zero_failure_confirmation: bool = False,
    evidence_root: str | Path | None = None,
) -> dict[str, bool | None]:
    """Validate explicit subject-to evidence for a phase-1 or climbing run."""

    if phase not in {"phase1", "climb", "confirmation"}:
        raise CampaignError(f"unknown campaign phase {phase!r}")
    expected_keys = set(SUBJECT_TO_KEYS)
    if evidence_root is not None:
        expected_keys |= set(SUBJECT_TO_EVIDENCE_KEYS)
    if set(evidence) != expected_keys:
        raise GuardrailViolation(
            f"subject-to evidence must contain exactly {sorted(expected_keys)}"
        )
    checked: dict[str, bool | None] = {}
    for key in SUBJECT_TO_KEYS:
        value = evidence[key]
        if value is not None and not isinstance(value, bool):
            raise GuardrailViolation(f"subject-to {key} must be true, false, or null")
        checked[key] = value
    if evidence_root is not None:
        revision = evidence["revision_sha"]
        if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise GuardrailViolation("subject-to revision_sha must be 40 lowercase hex")
        source_tree_sha256 = _validate_digest(
            "subject-to source_tree_sha256", evidence["source_tree_sha256"]
        )
        expected_kinds = {
            "gate_evidence": "gate_platform_suite",
            "fenced_evidence": "fenced_paths_status",
        }
        for key, kind in expected_kinds.items():
            block = evidence[key]
            if not isinstance(block, Mapping) or set(block) != {"path", "sha256"}:
                raise GuardrailViolation(f"subject-to {key} attachment is malformed")
            _validate_retained_members(evidence_root, [(block, "path", "sha256")])
            path = Path(evidence_root).parent / str(block["path"])
            transcript = _read_json_object(path)
            if (
                set(transcript)
                != {
                    "schema",
                    "kind",
                    "revision_sha",
                    "source_tree_sha256",
                    "exit_code",
                    "asserted_outcome",
                    "command",
                    "stdout_path",
                    "stdout_sha256",
                    "platform",
                    "checked_paths",
                    "changed_paths",
                    "fixture_manifest_path",
                    "fixture_manifest_sha256",
                    "fixture_tree_sha256",
                    "pythonpath",
                }
                or transcript.get("schema") != "skyweave-c001-subject-evidence/1"
                or transcript.get("kind") != kind
                or transcript.get("revision_sha") != revision
                or transcript.get("source_tree_sha256") != source_tree_sha256
                or transcript.get("exit_code") != 0
                or transcript.get("asserted_outcome") is not True
                or not isinstance(transcript.get("command"), str)
                or not transcript["command"].strip()
            ):
                raise GuardrailViolation(f"subject-to {key} does not prove PASS")
            _validate_retained_members(
                evidence_root, [(transcript, "stdout_path", "stdout_sha256")]
            )
            stdout_path = Path(evidence_root).parent / str(transcript["stdout_path"])
            _assert_no_symlinks(stdout_path)
            stdout = stdout_path.read_text(encoding="utf-8")
            completion = f"C001_EVIDENCE_PASS kind={kind} revision={revision}"
            if not stdout.strip() or completion not in stdout:
                raise GuardrailViolation(
                    f"subject-to {key} stdout lacks its structured completion summary"
                )
            try:
                command_argv = shlex.split(str(transcript["command"]))
            except ValueError as exc:
                raise GuardrailViolation(f"subject-to {key} command is malformed") from exc
            if kind == "gate_platform_suite":
                platform = transcript["platform"]
                if not isinstance(platform, Mapping) or set(platform) != {
                    "os",
                    "arch",
                    "python",
                    "toolchain",
                    "rmem_max_bytes",
                    "rmem_default_bytes",
                }:
                    raise GuardrailViolation("gate evidence lacks exact platform facts")
                string_facts = ("os", "arch", "python", "toolchain")
                if any(
                    not isinstance(platform[key], str) or not platform[key].strip()
                    for key in string_facts
                ):
                    raise GuardrailViolation("gate evidence lacks exact platform facts")
                if platform["os"] != "Linux" or platform["arch"].lower() not in GATE_ARCHES:
                    raise GuardrailViolation(
                        "gate evidence must come from Linux x86_64/amd64"
                    )
                for key in ("rmem_max_bytes", "rmem_default_bytes"):
                    value = platform[key]
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value < GATE_RMEM_MIN_BYTES
                    ):
                        raise GuardrailViolation(
                            f"gate evidence {key} must be at least "
                            f"{GATE_RMEM_MIN_BYTES}"
                        )
                rmem_facts = re.search(
                    r"(?:^|;)rmem_max=([0-9]+);rmem_default=([0-9]+)(?:;|$)",
                    str(platform["toolchain"]),
                )
                if rmem_facts is None or (
                    int(rmem_facts.group(1)),
                    int(rmem_facts.group(2)),
                ) != (
                    int(platform["rmem_max_bytes"]),
                    int(platform["rmem_default_bytes"]),
                ):
                    raise GuardrailViolation(
                        "gate toolchain receive-buffer facts disagree with platform facts"
                    )
                if re.search(
                    r"(?:^|;)python_optimize=0(?:;|$)",
                    str(platform["toolchain"]),
                ) is None:
                    raise GuardrailViolation(
                        "gate evidence requires python optimize level zero"
                    )
                if transcript["pythonpath"] != "src:../v1/src":
                    raise GuardrailViolation(
                        "gate evidence must pin PYTHONPATH=src:../v1/src"
                    )
                _validate_retained_members(
                    evidence_root,
                    [(transcript, "fixture_manifest_path", "fixture_manifest_sha256")],
                )
                fixture_path = Path(evidence_root).parent / str(
                    transcript["fixture_manifest_path"]
                )
                fixture_manifest = _read_json_object(fixture_path)
                if set(fixture_manifest) != {
                    "schema",
                    "revision_sha",
                    "source_tree_sha256",
                    "roots",
                    "fixture_tree_sha256",
                    "v1_head_tree_oid",
                    "v1_head_tree_sha256",
                    "files",
                } or fixture_manifest.get("schema") != GATE_FIXTURE_MANIFEST_SCHEMA:
                    raise GuardrailViolation("gate fixture manifest schema is invalid")
                if (
                    fixture_manifest.get("revision_sha") != revision
                    or fixture_manifest.get("source_tree_sha256") != source_tree_sha256
                    or fixture_manifest.get("roots") != list(GATE_FIXTURE_ROOTS)
                ):
                    raise GuardrailViolation("gate fixture source/root binding is invalid")
                fixture_files = fixture_manifest.get("files")
                if not isinstance(fixture_files, list) or not fixture_files:
                    raise GuardrailViolation("gate fixture manifest is empty")
                fixture_paths: list[str] = []
                for fixture_entry in fixture_files:
                    if not isinstance(fixture_entry, Mapping) or set(fixture_entry) != {
                        "path",
                        "type",
                        "size",
                        "sha256",
                    }:
                        raise GuardrailViolation("gate fixture manifest entry is malformed")
                    member = fixture_entry["path"]
                    member_size = fixture_entry["size"]
                    member_path = Path(member) if isinstance(member, str) else None
                    matching_roots = (
                        [
                            root
                            for root in GATE_FIXTURE_ROOTS
                            if member.startswith(f"{root}/")
                        ]
                        if isinstance(member, str)
                        else []
                    )
                    if (
                        not isinstance(member, str)
                        or member_path is None
                        or member_path.is_absolute()
                        or member != member_path.as_posix()
                        or "\\" in member
                        or any(part in {"", ".", ".."} for part in member_path.parts)
                        or len(matching_roots) != 1
                        or fixture_entry["type"] != "file"
                        or not isinstance(member_size, int)
                        or isinstance(member_size, bool)
                        or member_size < 0
                    ):
                        raise GuardrailViolation("gate fixture manifest entry is out of scope")
                    _validate_digest("gate fixture member", fixture_entry["sha256"])
                    fixture_paths.append(member)
                if fixture_paths != sorted(set(fixture_paths)) or any(
                    not any(path.startswith(f"{root}/") for path in fixture_paths)
                    for root in GATE_FIXTURE_ROOTS
                ):
                    raise GuardrailViolation("gate fixture manifest membership is invalid")
                fixture_tree = hashlib.sha256(
                    _canonical_json(
                        {"roots": list(GATE_FIXTURE_ROOTS), "files": fixture_files}
                    )
                ).hexdigest()
                if (
                    fixture_manifest.get("fixture_tree_sha256") != fixture_tree
                    or transcript.get("fixture_tree_sha256") != fixture_tree
                ):
                    raise GuardrailViolation("gate fixture tree digest is invalid")
                v1_tree_oid = fixture_manifest.get("v1_head_tree_oid")
                v1_tree_sha256 = _validate_digest(
                    "gate v1 HEAD tree", fixture_manifest.get("v1_head_tree_sha256")
                )
                if (
                    not isinstance(v1_tree_oid, str)
                    or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", v1_tree_oid)
                    is None
                ):
                    raise GuardrailViolation("gate v1 HEAD tree oid is invalid")
                v1_files = [
                    entry
                    for entry in fixture_files
                    if str(entry["path"]).startswith("v1/src/")
                ]
                expected_v1_tree_sha256 = hashlib.sha256(
                    _canonical_json(
                        {
                            "revision_sha": revision,
                            "git_tree_oid": v1_tree_oid,
                            "files": v1_files,
                        }
                    )
                ).hexdigest()
                if v1_tree_sha256 != expected_v1_tree_sha256:
                    raise GuardrailViolation("gate v1 HEAD tree identity is invalid")
                lowered = stdout.lower()
                if (
                    len(command_argv) != 4
                    or Path(command_argv[0]).name not in {"python", "python3"}
                    or command_argv[1:] != ["-m", "pytest", "-q"]
                    or re.search(r"\b[1-9][0-9]* passed\b", lowered) is None
                    or any(
                        token in lowered
                        for token in (" failed", " error", " warning")
                    )
                    or re.search(
                        r"\b(?:skipped|xfailed|xpassed|deselected)\b", lowered
                    )
                    is not None
                ):
                    raise GuardrailViolation(
                        "gate evidence must run the unselected full pytest suite to PASS"
                    )
            elif (
                transcript["platform"] is not None
                or transcript["fixture_manifest_path"] is not None
                or transcript["fixture_manifest_sha256"] is not None
                or transcript["fixture_tree_sha256"] is not None
                or transcript["pythonpath"] is not None
                or transcript["changed_paths"] != []
                or not isinstance(transcript["checked_paths"], list)
                or set(transcript["checked_paths"])
                != {
                    "v1",
                    ":(glob)**/golden/**",
                    "v2/docs/DETECTION_CONTRACTS_D0.md",
                    "v2/src/skyweave2/contracts",
                    "v2/tests/contracts",
                    "v2/proto",
                    "v2/tests/edge/fixtures/gate",
                }
            ):
                raise GuardrailViolation("fenced evidence does not prove an empty fenced diff")
            elif command_argv != [
                "git",
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                "v1",
                ":(glob)**/golden/**",
                "v2/docs/DETECTION_CONTRACTS_D0.md",
                "v2/src/skyweave2/contracts",
                "v2/tests/contracts",
                "v2/proto",
                "v2/tests/edge/fixtures/gate",
            ] or f"{completion} changed_paths=0" not in stdout:
                raise GuardrailViolation(
                    "fenced evidence must run the exact scoped git status and "
                    "enumerate zero changes"
                )
    for key in (
        "gate_platform_suite_green",
        "fenced_paths_untouched",
        "probe_input_only",
    ):
        if checked[key] is not True:
            raise GuardrailViolation(f"subject-to violation: {key} is not proven true")
    if zero_failure_confirmation:
        if phase != "confirmation":
            raise GuardrailViolation("zero-failure path is confirmation-only, never a climb")
        for key in ("host_board_parity_within_tolerance", "discriminator_allows_climb"):
            if checked[key] is not None:
                raise GuardrailViolation(
                    f"zero-failure confirmation requires {key}=null, not a parity claim"
                )
    elif phase in {"climb", "confirmation"}:
        for key in (
            "host_board_parity_within_tolerance",
            "discriminator_allows_climb",
        ):
            if checked[key] is not True:
                raise GuardrailViolation(
                    f"subject-to violation: {key} must be true before {phase}"
                )
    return checked


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}


def _assert_no_symlinks(path: Path, *, missing_leaf_ok: bool = False) -> None:
    """Refuse symlinks in every existing component, including broken links."""

    absolute = Path(os.path.abspath(path))
    chain = [absolute, *absolute.parents]
    for index, candidate in enumerate(reversed(chain)):
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            if missing_leaf_ok or index < len(chain) - 1:
                continue
            raise
        if stat.S_ISLNK(mode):
            raise GuardrailViolation(f"symlink/path aliases are forbidden: {candidate}")


def _read_json_object(path: Path) -> dict[str, Any]:
    _assert_no_symlinks(path)
    if not path.is_file():
        raise CampaignError(f"required JSON artifact does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CampaignError(f"{path} must contain one JSON object")
    return payload


def sha256_file(path: str | Path) -> str:
    """Hash one retained regular file without following a symlink leaf."""

    artifact = Path(path)
    if not artifact.exists() and not artifact.is_symlink():
        raise CampaignError(f"retained artifact does not exist: {artifact}")
    _assert_no_symlinks(artifact)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(artifact, flags)
    except OSError as exc:
        raise CampaignError(f"cannot open retained artifact {artifact}: {exc}") from exc
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CampaignError(f"retained artifact is not a regular file: {artifact}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _validate_digest(name: str, digest: object) -> str:
    if not isinstance(digest, str) or _HEX_SHA256.fullmatch(digest) is None:
        raise CampaignError(f"{name} must be a lowercase SHA256 hex digest")
    return digest


def _canonical_remote_path(name: str, value: object) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise GuardrailViolation(f"{name} must be an absolute POSIX path")
    path = Path(value)
    if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise GuardrailViolation(f"{name} contains a path alias")
    return path


def _write_new_bytes(path: Path, data: bytes) -> None:
    """Install a new artifact atomically, never replacing retained evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlinks(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise CampaignError(f"refusing to replace retained artifact {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _install_generated_file(temporary: Path, target: Path) -> None:
    """Atomically link a fully written generator output into its final name."""

    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    try:
        os.link(temporary, target)
    except FileExistsError as exc:
        raise CampaignError(f"refusing to replace retained artifact {target}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _probe_plan(
    *, seed: int, movers: int, total_frames: int, ram_clip_frames: int | None = None
) -> benchmark.BenchmarkPlan:
    if benchmark.SCENE_NOISE_DN != SCENE_NOISE_DN:
        raise GuardrailViolation(
            f"benchmark noise drifted to {benchmark.SCENE_NOISE_DN}; C-001 freezes 2.0 DN"
        )
    return benchmark.BenchmarkPlan(
        frames=total_frames,
        warmup_frames=WARMUP_FRAMES,
        seed=validate_seed(seed),
        movers=movers,
        cap=COMPONENT_CAP,
        source_mode=benchmark.SOURCE_MODE_INJECT_RAM,
        ram_clip_frames=ram_clip_frames,
    )


def _truth_rows(plan: benchmark.BenchmarkPlan, clip_frames: int) -> list[dict[str, Any]]:
    """Truth for the *stored* clip slots (the daemon repeats these slots)."""

    clip_plan = replace(plan, frames=clip_frames, warmup_frames=0)
    tracks = benchmark._tracks(clip_plan)
    scale_x = benchmark.FULL_WIDTH / PROC_WIDTH
    scale_y = benchmark.FULL_HEIGHT / PROC_HEIGHT
    rows: list[dict[str, Any]] = []
    for slot in range(clip_frames):
        movers: list[dict[str, Any]] = []
        for mover_id, (start, velocity) in enumerate(tracks):
            full_u, full_v = start + velocity * slot
            u = float(full_u / scale_x)
            v = float(full_v / scale_y)
            visible = 0.0 <= u < PROC_WIDTH and 0.0 <= v < PROC_HEIGHT
            movers.append(
                {
                    "mover_id": mover_id,
                    "visible": visible,
                    "u": u if visible else None,
                    "v": v if visible else None,
                    "full_u": float(full_u) if visible else None,
                    "full_v": float(full_v) if visible else None,
                }
            )
        rows.append({"clip_slot": slot, "movers": movers})
    return rows


@dataclass(frozen=True)
class PreparedProbe:
    manifest_path: Path
    clip_path: Path
    truth_path: Path
    checksums_path: Path
    manifest_sha256: str
    clip_sha256: str
    truth_sha256: str


def prepare_probe(
    output_dir: str | Path,
    *,
    kind: str,
    seed: int,
    total_frames: int = MIN_TOTAL_FRAMES,
) -> PreparedProbe:
    """Generate one immutable C-001 RAM-loop clip, manifest, and slot truth."""

    seed = validate_seed(seed)
    total_frames = _require_int("total_frames", total_frames, MIN_TOTAL_FRAMES)
    if kind not in {"benchmark", "sparse"}:
        raise CampaignError("probe kind must be 'benchmark' or 'sparse'")
    movers = STANDARD_PROBE_MOVERS if kind == "benchmark" else SPARSE_PROBE_MOVERS
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    _assert_no_symlinks(root)
    if _tokens(str(root)) & _FORBIDDEN_INPUT_TOKENS:
        raise GuardrailViolation(f"a probe may not be prepared in a gate/acceptance path: {root}")

    plan = _probe_plan(seed=seed, movers=movers, total_frames=total_frames)
    declaration = benchmark.ram_loop_declaration(
        plan, PROC_WIDTH, PROC_HEIGHT, total_frames, detector="ive"
    )
    clip_path = root / "probe.swij"
    truth_path = root / "truth_slots.jsonl"
    manifest_path = root / "probe_manifest.json"
    checksums_path = root / "sha256.json"
    for path in (clip_path, truth_path, manifest_path, checksums_path):
        if path.exists() or path.is_symlink():
            raise CampaignError(f"refusing to replace retained artifact {path}")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".probe.", dir=root)
    os.close(descriptor)
    temporary_clip = Path(temporary_name)
    try:
        benchmark.write_benchmark_stream(
            temporary_clip,
            plan,
            PROC_WIDTH,
            PROC_HEIGHT,
            session_uuid=f"c001-{kind}-{seed:08x}",
            frame_count=declaration.clip_frames,
        )
        _install_generated_file(temporary_clip, clip_path)
    finally:
        temporary_clip.unlink(missing_ok=True)
    clip_sha = sha256_file(clip_path)

    truth_bytes = b"".join(
        _canonical_json(row) for row in _truth_rows(plan, declaration.clip_frames)
    )
    _write_new_bytes(truth_path, truth_bytes)
    truth_sha = sha256_file(truth_path)
    manifest = {
        "schema": PROBE_SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "role": "probe",
        "probe_kind": kind,
        "generator": "skyweave2.edge.benchmark",
        "source_mode": benchmark.SOURCE_MODE_INJECT_RAM,
        "seed": seed,
        "movers": movers,
        "proc_width": PROC_WIDTH,
        "proc_height": PROC_HEIGHT,
        "warmup_frames": WARMUP_FRAMES,
        "total_frames": total_frames,
        "postwarm_frames": total_frames - WARMUP_FRAMES,
        "noise_dn": SCENE_NOISE_DN,
        "cap": COMPONENT_CAP,
        "persistence_frames": PERSISTENCE_FRAMES,
        "persistence_gate_px": PERSISTENCE_GATE_PX,
        "ram_clip_frames": declaration.clip_frames,
        "ram_loop_total_frames": declaration.total_frames,
        "ram_loop_pts_stride_ns": declaration.pts_stride_ns,
        "ram_budget_mb": declaration.budget_mb,
        "clip_path": clip_path.name,
        "clip_sha256": clip_sha,
        "truth_path": truth_path.name,
        "truth_sha256": truth_sha,
        "truth_schema": TRUTH_SCHEMA,
        "truth_coordinate_space": "processing pixels, half-open image bounds",
        "truth_loop_rule": "clip_slot = run_frame_seq % ram_clip_frames",
    }
    _write_new_bytes(manifest_path, _canonical_json(manifest))
    manifest_sha = sha256_file(manifest_path)
    _write_new_bytes(
        checksums_path,
        _canonical_json(
            {
                manifest_path.name: manifest_sha,
                clip_path.name: clip_sha,
                truth_path.name: truth_sha,
            }
        ),
    )
    # Re-open through the same hostile-input checks used by host and board runs.
    load_probe_manifest(manifest_path)
    return PreparedProbe(
        manifest_path=manifest_path,
        clip_path=clip_path,
        truth_path=truth_path,
        checksums_path=checksums_path,
        manifest_sha256=manifest_sha,
        clip_sha256=clip_sha,
        truth_sha256=truth_sha,
    )


def _resolve_manifest_member(manifest_path: Path, value: object, field_name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CampaignError(f"manifest {field_name} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or value != relative.as_posix() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise GuardrailViolation(f"manifest {field_name} uses a path alias: {value!r}")
    candidate = manifest_path.parent / relative
    _assert_no_symlinks(candidate)
    resolved_root = manifest_path.parent.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise GuardrailViolation(
            f"manifest {field_name} escapes its probe directory: {value!r}"
        ) from exc
    return resolved


def _forbid_gate_or_acceptance(path: Path, payload: Mapping[str, object]) -> None:
    path_tokens: set[str] = set()
    for part in (*path.parts, *path.resolve(strict=True).parts):
        path_tokens.update(_tokens(part))
    if path_tokens & _FORBIDDEN_INPUT_TOKENS:
        raise GuardrailViolation(f"gate/acceptance manifest is forbidden: {path}")
    for key in ("role", "probe_kind", "generator", "source_mode"):
        value = payload.get(key)
        if isinstance(value, str) and _tokens(value) & _FORBIDDEN_INPUT_TOKENS:
            raise GuardrailViolation(f"gate/acceptance marker in manifest {key}: {value!r}")


def _read_truth_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CampaignError(f"invalid truth JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise CampaignError(f"truth row {line_number} is not an object")
        rows.append(row)
    return rows


def load_truth_slots(
    manifest: Mapping[str, Any], manifest_path: str | Path
) -> list[dict[str, Any]]:
    """Load and re-derive truth so a matching digest alone cannot bless drift."""

    source = Path(manifest_path)
    truth_path = _resolve_manifest_member(source, manifest["truth_path"], "truth_path")
    if sha256_file(truth_path) != manifest["truth_sha256"]:
        raise GuardrailViolation("probe truth SHA256 does not match its manifest")
    rows = _read_truth_rows(truth_path)
    plan = _probe_plan(
        seed=manifest["seed"],
        movers=manifest["movers"],
        total_frames=manifest["total_frames"],
        ram_clip_frames=manifest["ram_clip_frames"],
    )
    expected = _truth_rows(plan, manifest["ram_clip_frames"])
    if rows != expected:
        raise GuardrailViolation(
            "truth does not match the benchmark generator's exact RAM-loop slots"
        )
    return rows


def _validate_probe_clip_generator(
    manifest_path: Path, manifest: Mapping[str, Any]
) -> None:
    """Re-derive every SWIJ slot and declaration from the frozen generator."""

    clip_path = _resolve_manifest_member(manifest_path, manifest["clip_path"], "clip_path")
    with clip_path.open("rb") as handle:
        session = read_injection_session(handle)
        stored = list(iter_injection_frames(handle))
    plan = _probe_plan(
        seed=int(manifest["seed"]),
        movers=int(manifest["movers"]),
        total_frames=int(manifest["total_frames"]),
        ram_clip_frames=int(manifest["ram_clip_frames"]),
    )
    clip_plan = replace(
        plan, frames=int(manifest["ram_clip_frames"]), warmup_frames=0
    )
    expected_session = benchmark.benchmark_session(
        clip_plan,
        PROC_WIDTH,
        PROC_HEIGHT,
        f"c001-{manifest['probe_kind']}-{int(manifest['seed']):08x}",
        clip_plan.frames,
    )
    if session != expected_session or len(stored) != clip_plan.frames:
        raise GuardrailViolation("probe SWIJ session differs from edge.benchmark")
    profile = benchmark.PtsProfile()
    generated = benchmark.iter_scene_frames(clip_plan, PROC_WIDTH, PROC_HEIGHT)
    for slot, (stored_frame, expected_luma) in enumerate(
        zip(stored, generated, strict=True)
    ):
        scene_ts_ns = int(round(slot / clip_plan.fps * 1e9))
        expected_ts, expected_sync_ms = benchmark.fabricate_pts(
            scene_ts_ns, profile, clip_plan.camera_id, slot
        )
        if (
            stored_frame.frame_seq != slot
            or stored_frame.capture_ts_ns != expected_ts
            or stored_frame.time_sync_error_ms != float(np.float32(expected_sync_ms))
            or stored_frame.luma.shape != (PROC_HEIGHT, PROC_WIDTH)
            or not np.array_equal(stored_frame.luma, expected_luma)
        ):
            raise GuardrailViolation(
                f"retained clip slot {slot} differs from edge.benchmark"
            )


def load_probe_manifest(
    path: str | Path, *, verify_artifacts: bool = True
) -> dict[str, Any]:
    """Load a C-001 probe and reject gate scenes, aliases, drift, and truncation."""

    manifest_path = Path(path)
    _assert_no_symlinks(manifest_path)
    payload = _read_json_object(manifest_path)
    _forbid_gate_or_acceptance(manifest_path, payload)
    if set(payload) != set(_MANIFEST_FIELDS):
        raise CampaignError(
            f"probe manifest fields drifted: missing={sorted(_MANIFEST_FIELDS - set(payload))}, "
            f"unknown={sorted(set(payload) - _MANIFEST_FIELDS)}"
        )
    if payload["schema"] != PROBE_SCHEMA or payload["campaign_id"] != CAMPAIGN_ID:
        raise GuardrailViolation("manifest is not a C-001 probe schema")
    if payload["role"] != "probe":
        raise GuardrailViolation("gate and acceptance roles are forbidden; role must be probe")
    if payload["probe_kind"] not in {"benchmark", "sparse"}:
        raise CampaignError("probe_kind must be benchmark or sparse")
    if payload["generator"] != "skyweave2.edge.benchmark":
        raise GuardrailViolation("C-001 probe generator is frozen to edge.benchmark")
    if payload["source_mode"] != benchmark.SOURCE_MODE_INJECT_RAM:
        raise GuardrailViolation("C-001 source mode is frozen to inject-ram")
    seed = validate_seed(payload["seed"])
    validate_frozen_settings(
        {
            "proc_width": payload["proc_width"],
            "proc_height": payload["proc_height"],
            "warmup_frames": payload["warmup_frames"],
            "noise_dn": payload["noise_dn"],
            "cap": payload["cap"],
            "seed": payload["seed"],
        },
        seed=seed,
    )
    if payload["persistence_frames"] != PERSISTENCE_FRAMES:
        raise GuardrailViolation("persistence_frames is frozen at 2")
    if (
        isinstance(payload["persistence_gate_px"], bool)
        or not isinstance(payload["persistence_gate_px"], (int, float))
        or float(payload["persistence_gate_px"]) != PERSISTENCE_GATE_PX
    ):
        raise GuardrailViolation("persistence_gate_px is frozen at 12.0")
    total = _require_int("total_frames", payload["total_frames"], 0)
    postwarm = _require_int("postwarm_frames", payload["postwarm_frames"], 0)
    if total < MIN_TOTAL_FRAMES or postwarm < MIN_POSTWARM_FRAMES:
        raise GuardrailViolation(
            "C-001 requires at least 630 total and 600 post-warm-up frames"
        )
    if postwarm != total - WARMUP_FRAMES:
        raise GuardrailViolation("postwarm_frames must equal total_frames - 30")
    movers = _require_int("movers", payload["movers"], 1)
    expected_movers = (
        SPARSE_PROBE_MOVERS if payload["probe_kind"] == "sparse" else STANDARD_PROBE_MOVERS
    )
    if movers != expected_movers:
        raise GuardrailViolation(
            f"{payload['probe_kind']} probe mover count is frozen at {expected_movers}"
        )
    if payload["truth_schema"] != TRUTH_SCHEMA:
        raise GuardrailViolation("truth schema drifted")
    if payload["truth_coordinate_space"] != "processing pixels, half-open image bounds":
        raise GuardrailViolation("truth coordinate space drifted")
    if payload["truth_loop_rule"] != "clip_slot = run_frame_seq % ram_clip_frames":
        raise GuardrailViolation("truth must repeat the exact RAM-loop slot")

    plan = _probe_plan(seed=seed, movers=movers, total_frames=total)
    expected_loop = benchmark.ram_loop_declaration(
        plan, PROC_WIDTH, PROC_HEIGHT, total, detector="ive"
    )
    loop_values = {
        "ram_clip_frames": expected_loop.clip_frames,
        "ram_loop_total_frames": expected_loop.total_frames,
        "ram_loop_pts_stride_ns": expected_loop.pts_stride_ns,
        "ram_budget_mb": expected_loop.budget_mb,
    }
    for name, expected in loop_values.items():
        if not _is_int(payload[name]) or payload[name] != expected:
            raise GuardrailViolation(f"{name} must be the exact RAM-loop value {expected}")
    _validate_digest("clip_sha256", payload["clip_sha256"])
    _validate_digest("truth_sha256", payload["truth_sha256"])
    if verify_artifacts:
        clip_path = _resolve_manifest_member(manifest_path, payload["clip_path"], "clip_path")
        if sha256_file(clip_path) != payload["clip_sha256"]:
            raise GuardrailViolation("probe clip SHA256 does not match its manifest")
        _validate_probe_clip_generator(manifest_path, payload)
        load_truth_slots(payload, manifest_path)
    return payload


def iter_looped_probe_frames(
    manifest_path: str | Path,
) -> Iterator[tuple[int, int, np.ndarray]]:
    """Yield the exact 630+ RAM-loop frames without writing a 630-frame clip."""

    source = Path(manifest_path)
    manifest = load_probe_manifest(source)
    clip_path = _resolve_manifest_member(source, manifest["clip_path"], "clip_path")
    with clip_path.open("rb") as handle:
        session = read_injection_session(handle)
        stored = list(iter_injection_frames(handle))
    if (
        session.proc_width,
        session.proc_height,
        session.frame_count,
    ) != (PROC_WIDTH, PROC_HEIGHT, manifest["ram_clip_frames"]):
        raise GuardrailViolation("clip session header disagrees with the probe manifest")
    if len(stored) != manifest["ram_clip_frames"]:
        raise GuardrailViolation("clip frame count disagrees with its RAM-loop declaration")
    for slot, frame in enumerate(stored):
        if frame.frame_seq != slot or frame.luma.shape != (PROC_HEIGHT, PROC_WIDTH):
            raise GuardrailViolation(f"RAM-loop slot {slot} is malformed or out of sequence")

    for frame_seq in range(manifest["total_frames"]):
        slot = frame_seq % len(stored)
        yield frame_seq, slot, stored[slot].luma


def detector_config_for(knobs: Mapping[str, object]) -> DetectorConfig:
    """Build the host discriminator config with every non-whitelisted field frozen."""

    normalized = normalize_knobs(knobs)
    base = DetectorConfig(
        backend=Backend.IVE_APPROX,
        proc_width=PROC_WIDTH,
        proc_height=PROC_HEIGHT,
        warmup_frames=WARMUP_FRAMES,
        max_components_per_frame=COMPONENT_CAP,
        persistence_frames=PERSISTENCE_FRAMES,
        persistence_gate_px=PERSISTENCE_GATE_PX,
        detector_rev="c001-host-discriminator/1",
        calibration_rev="c001-synthetic",
    )
    top_updates: dict[str, object] = {}
    ive_updates: dict[str, object] = {}
    for name, value in normalized.items():
        if name.startswith("ive_approx."):
            ive_updates[name.split(".", 1)[1]] = value
        else:
            top_updates[name] = value
    if ive_updates:
        top_updates["ive_approx"] = base.ive_approx.model_copy(update=ive_updates)
    return base.model_copy(update=top_updates)


def _bbox_overlaps(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    lx = int(left["bbox_x"])
    ly = int(left["bbox_y"])
    rx = int(right["bbox_x"])
    ry = int(right["bbox_y"])
    return (
        lx < rx + int(right["bbox_w"])
        and rx < lx + int(left["bbox_w"])
        and ly < ry + int(right["bbox_h"])
        and ry < ly + int(left["bbox_h"])
    )


def overlapping_bbox_pairs(components: Sequence[Mapping[str, object]]) -> int:
    return sum(
        _bbox_overlaps(components[left], components[right])
        for left in range(len(components))
        for right in range(left + 1, len(components))
    )


def _component_dict(component: MaskComponent) -> dict[str, int | float]:
    return {
        "centroid_u": component.centroid_u,
        "centroid_v": component.centroid_v,
        "area_px": component.area_px,
        "bbox_x": component.bbox_x,
        "bbox_y": component.bbox_y,
        "bbox_w": component.bbox_w,
        "bbox_h": component.bbox_h,
    }


def components_with_connectivity(
    mask: np.ndarray, *, connectivity: int, min_area_px: int, max_area_px: int
) -> list[dict[str, int | float]]:
    """Diagnostic CCL view; the authoritative host oracle remains 4-connected."""

    if connectivity not in {4, 8}:
        raise CampaignError("diagnostic connectivity must be 4 or 8")
    cv2 = _require_cv2()
    count, _, stats, centroids = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=connectivity
    )
    components: list[dict[str, int | float]] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if not min_area_px <= area <= max_area_px:
            continue
        components.append(
            {
                "centroid_u": float(centroids[label, 0]),
                "centroid_v": float(centroids[label, 1]),
                "area_px": area,
                "bbox_x": int(stats[label, cv2.CC_STAT_LEFT]),
                "bbox_y": int(stats[label, cv2.CC_STAT_TOP]),
                "bbox_w": int(stats[label, cv2.CC_STAT_WIDTH]),
                "bbox_h": int(stats[label, cv2.CC_STAT_HEIGHT]),
            }
        )
    return components


def summarize_host_rows(
    rows: Sequence[Mapping[str, object]], *, total_frames: int
) -> dict[str, object]:
    expected_sequences = list(range(WARMUP_FRAMES, total_frames))
    sequences = [row.get("frame_seq") for row in rows]
    if sequences != expected_sequences:
        raise CampaignError("host rows must cover every post-warm-up frame exactly once")
    counts: list[int] = []
    overlap_total = 0
    overlap_frames = 0
    for row in rows:
        count = _require_int("raw_components", row.get("raw_components"), 0)
        components = row.get("components")
        if not isinstance(components, list) or len(components) != count:
            raise CampaignError("host raw_components must equal len(components)")
        pairs = _require_int("overlap_pairs", row.get("overlap_pairs"), 0)
        if pairs != overlapping_bbox_pairs(components):
            raise CampaignError("host overlap_pairs does not match its component bboxes")
        counts.append(count)
        overlap_total += pairs
        overlap_frames += pairs > 0
    return {
        "postwarm_frames": len(rows),
        "max_raw_components": max(counts, default=0),
        "frames_at_or_over_region_capacity": sum(
            count >= REGION_TABLE_CAPACITY for count in counts
        ),
        # Clean/parallel evidence is a paired decision on actual BOARD failure
        # frame_seq values.  A host-only threshold would misclassify the named
        # sub-cap mode, so no climb authorization is inferred here.
        "clean_host": None,
        "overlapping_bbox_pairs": overlap_total,
        "frames_with_overlapping_bboxes": overlap_frames,
        "raw_component_counts": counts,
    }


def evaluate_host_discriminator(
    host_rows: Sequence[Mapping[str, object]],
    board_ccl_rows: Sequence[Mapping[str, object]],
    *,
    manifest_path: str | Path,
    host_8_components: Mapping[int, Sequence[Mapping[str, object]]] | None = None,
    mask_diff_within_tolerance: bool = False,
) -> dict[str, Any]:
    """Apply the predeclared zero-extra-component rule on board failure frames.

    A host frame is clean only when one-to-one bbox matching recalls every
    visible truth mover and the raw host component count equals the visible
    truth count.  Therefore there are exactly zero unexplained host components;
    no arbitrary speckle-count tolerance is introduced.  Any extra component
    is symmetric-speckle evidence and keeps the climb question open.
    """

    manifest = load_probe_manifest(manifest_path)
    truth = load_truth_slots(manifest, manifest_path)
    host_by_frame = {int(row["frame_seq"]): row for row in host_rows}
    if host_8_components is None:
        raise GuardrailViolation(
            "paired discriminator requires the diagnostic 8-connected host mask view"
        )
    failures = [
        row
        for row in board_ccl_rows
        if row.get("api_failure") is False and row.get("s8_label_status") != 0
    ]
    if any(row.get("api_failure") is True for row in board_ccl_rows):
        raise GuardrailViolation("API failures invalidate the host-board discriminator")
    failure_sequences = [int(row["frame_seq"]) for row in failures]
    if not failures:
        return {
            "clean_host": None,
            "discriminator_allows_climb": False,
            "zero_failure_candidate_path": True,
            "decision": "not_applicable_no_board_label_failures",
            "board_ccl_attempts": len(board_ccl_rows),
            "board_label_failures": 0,
            "board_region_count_mismatch_frames": sum(
                row.get("region_count_mismatch") is True for row in board_ccl_rows
            ),
            "board_failure_frame_sequences": [],
            "failure_frames_compared": 0,
            "frames": [],
            "rule": "zero unexplained host components on every board label-failure frame",
        }
    comparisons: list[dict[str, Any]] = []
    for failure in failures:
        frame_seq = int(failure["frame_seq"])
        if frame_seq not in host_by_frame:
            raise GuardrailViolation(f"host discriminator lacks board failure frame {frame_seq}")
        components_4 = host_by_frame[frame_seq].get("components")
        components_8 = host_8_components.get(frame_seq)
        if not isinstance(components_4, list) or components_8 is None:
            raise CampaignError("host discriminator row lacks component bboxes")
        slot = frame_seq % int(manifest["ram_clip_frames"])
        visible = [mover for mover in truth[slot]["movers"] if mover["visible"]]
        matches_4 = _maximum_bbox_matching(visible, components_4)
        matches_8 = _maximum_bbox_matching(visible, components_8)
        missing_4 = len(visible) - len(matches_4)
        unexplained_4 = len(components_4) - len(matches_4)
        missing_8 = len(visible) - len(matches_8)
        unexplained_8 = len(components_8) - len(matches_8)
        clean = missing_4 == 0 and unexplained_4 == 0 and missing_8 == 0 and unexplained_8 == 0
        comparisons.append(
            {
                "frame_seq": frame_seq,
                "truth_movers": len(visible),
                "truth_movers_matched_4c": len(matches_4),
                "truth_movers_matched_8c": len(matches_8),
                "host_raw_components_4c": len(components_4),
                "host_raw_components_8c": len(components_8),
                "missing_truth_movers_4c": missing_4,
                "missing_truth_movers_8c": missing_8,
                "unexplained_host_components_4c": unexplained_4,
                "unexplained_host_components_8c": unexplained_8,
                "clean": clean,
            }
        )
    clean_host = all(row["clean"] for row in comparisons)
    any_clean_frame = any(row["clean"] for row in comparisons)
    any_extra_8 = any(row["unexplained_host_components_8c"] > 0 for row in comparisons)
    all_extra_8 = all(row["unexplained_host_components_8c"] > 0 for row in comparisons)
    any_missing_8 = any(row["missing_truth_movers_8c"] > 0 for row in comparisons)
    connectivity_divergence = any(
        row["unexplained_host_components_4c"]
        > row["unexplained_host_components_8c"]
        for row in comparisons
    )
    if clean_host:
        decision = "clean_host_stop"
        allows_climb = False
        rendered_clean: bool | None = True
    elif any_missing_8:
        decision = "host_8c_missing_truth_re_scope"
        allows_climb = False
        rendered_clean = None
    elif connectivity_divergence:
        decision = "connectivity_divergence_re_scope"
        allows_climb = False
        rendered_clean = None
    elif any_clean_frame:
        decision = "mixed_clean_host_re_scope"
        allows_climb = False
        rendered_clean = None
    elif any_extra_8 and all_extra_8:
        decision = (
            "symmetric_host_evidence"
            if mask_diff_within_tolerance
            else "symmetric_candidate_pending_exact_mask_parity"
        )
        allows_climb = mask_diff_within_tolerance
        rendered_clean = False
    else:
        decision = "ambiguous_host_parity_re_scope"
        allows_climb = False
        rendered_clean = None
    return {
        "clean_host": rendered_clean,
        "discriminator_allows_climb": allows_climb,
        "zero_failure_candidate_path": False,
        "decision": decision,
        "board_ccl_attempts": len(board_ccl_rows),
        "board_label_failures": len(failures),
        "board_region_count_mismatch_frames": sum(
            row.get("region_count_mismatch") is True for row in board_ccl_rows
        ),
        "board_failure_frame_sequences": failure_sequences,
        "failure_frames_compared": len(comparisons),
        "frames": comparisons,
        "rule": (
            "zero unexplained components under both 4c and diagnostic 8c for clean; "
            "only unexplained 8c components plus paired mask diff justify symmetry"
        ),
    }


def load_failed_fg_masks(path: str | Path) -> dict[int, np.ndarray]:
    """Parse the bounded board post-morph mask stream used by phase-1 diffing.

    The board format is deliberately tiny and credential-free: a repeated
    28-byte big-endian header followed by exactly ``width * height`` row-major
    bytes.  Only failed post-warm-up frames belong in the file and the firmware
    hard bound of sixteen is enforced here again.
    """

    source = Path(path)
    _assert_no_symlinks(source)
    if not source.is_file():
        raise CampaignError(f"failed-mask artifact does not exist: {source}")
    masks: dict[int, np.ndarray] = {}
    with source.open("rb") as handle:
        while True:
            header = handle.read(_FG_MASK_HEADER_BYTES)
            if not header:
                break
            if len(header) != _FG_MASK_HEADER_BYTES:
                raise CampaignError("failed-mask artifact has a truncated 28-byte header")
            magic, version, reserved, frame_seq, width, height, payload_len = struct.unpack(
                _FG_MASK_HEADER, header
            )
            if magic != FG_MASK_MAGIC or version != FG_MASK_VERSION or reserved != b"\0\0\0":
                raise CampaignError("failed-mask header magic/version/reserved bytes are invalid")
            if (width, height) != (PROC_WIDTH, PROC_HEIGHT):
                raise GuardrailViolation(
                    f"failed mask is {width}x{height}; C-001 is frozen at 1152x648"
                )
            if payload_len != width * height:
                raise CampaignError("failed-mask payload length is not width * height")
            if frame_seq < WARMUP_FRAMES:
                raise CampaignError("failed-mask artifact contains a warm-up frame")
            if frame_seq in masks:
                raise CampaignError(f"failed-mask frame_seq {frame_seq} is duplicated")
            payload = handle.read(payload_len)
            if len(payload) != payload_len:
                raise CampaignError(f"failed-mask frame {frame_seq} payload is truncated")
            # IVE masks may serialize foreground as 1 or 255.  The declared
            # semantic is binary occupancy, so normalize nonzero to True.
            masks[int(frame_seq)] = np.frombuffer(payload, dtype=np.uint8).reshape(
                PROC_HEIGHT, PROC_WIDTH
            ) != 0
            if len(masks) > MAX_FAILED_MASKS:
                raise GuardrailViolation("failed-mask artifact exceeds its hard 16-frame bound")
    if list(masks) != sorted(masks):
        raise CampaignError("failed masks must be strictly ordered by frame_seq")
    return masks


def compare_fg_masks(
    board_masks: Mapping[int, np.ndarray], host_masks: Mapping[int, np.ndarray]
) -> dict[str, Any]:
    """Diff identical frame_seq masks; missing or extra host frames are errors."""

    if set(board_masks) != set(host_masks):
        raise GuardrailViolation(
            "board and host masks must name identical frame_seq values; "
            f"board_only={sorted(set(board_masks) - set(host_masks))}, "
            f"host_only={sorted(set(host_masks) - set(board_masks))}"
        )
    frames: list[dict[str, int | float]] = []
    total_different = 0
    total_pixels = 0
    for frame_seq in sorted(board_masks):
        board = np.asarray(board_masks[frame_seq], dtype=bool)
        host = np.asarray(host_masks[frame_seq], dtype=bool)
        if board.shape != (PROC_HEIGHT, PROC_WIDTH) or host.shape != board.shape:
            raise GuardrailViolation(f"mask shape drift on frame {frame_seq}")
        different = int(np.count_nonzero(board != host))
        intersection = int(np.count_nonzero(board & host))
        union = int(np.count_nonzero(board | host))
        pixels = int(board.size)
        total_different += different
        total_pixels += pixels
        frames.append(
            {
                "frame_seq": frame_seq,
                "board_foreground_pixels": int(np.count_nonzero(board)),
                "host_foreground_pixels": int(np.count_nonzero(host)),
                "differing_pixels": different,
                "intersection_pixels": intersection,
                "union_pixels": union,
                "iou": 1.0 if union == 0 else intersection / union,
                "agreement_rate": (pixels - different) / pixels,
            }
        )
    return {
        "frames_compared": len(frames),
        "differing_pixels": total_different,
        "pixels_compared": total_pixels,
        "agreement_rate": (
            None if total_pixels == 0 else (total_pixels - total_different) / total_pixels
        ),
        "frames": frames,
    }


@dataclass(frozen=True)
class WrittenArtifact:
    path: Path
    sha256: str
    payload: dict[str, Any]


def run_host_discriminator(
    manifest_path: str | Path,
    output_path: str | Path,
    *,
    knobs: Mapping[str, object] | None = None,
    board: str | None = None,
    board_fg_masks_path: str | Path | None = None,
    board_ccl_log_path: str | Path | None = None,
) -> WrittenArtifact:
    """Run ``ive_approx`` on exact looped frames and retain raw host counts."""

    knobs = knobs or {}
    normalized = normalize_knobs(knobs)
    if board_ccl_log_path is not None and (not isinstance(board, str) or not board.strip()):
        raise CampaignError("paired host discrimination requires a board identity label")
    if board_fg_masks_path is not None and board_ccl_log_path is None:
        raise GuardrailViolation("SWFM comparison requires its paired board CCL log")
    manifest_source = Path(manifest_path)
    manifest = load_probe_manifest(manifest_source)
    config = detector_config_for(normalized)
    backend = make_backend(config)
    board_masks = (
        load_failed_fg_masks(board_fg_masks_path) if board_fg_masks_path is not None else {}
    )
    board_rows = (
        load_ccl_log(board_ccl_log_path, manifest_path=manifest_source)
        if board_ccl_log_path is not None
        else []
    )
    board_failure_sequences = [
        int(row["frame_seq"])
        for row in board_rows
        if row["api_failure"] is False and row["s8_label_status"] != 0
    ]
    expected_mask_sequences = board_failure_sequences[: min(10, len(board_failure_sequences))]
    if board_fg_masks_path is not None and list(board_masks) != expected_mask_sequences:
        raise GuardrailViolation(
            "SWFM masks must be the first min(10, label_failures) board failure frames"
        )
    if board_masks and max(board_masks) >= manifest["total_frames"]:
        raise GuardrailViolation("failed-mask frame lies beyond the probe run")
    host_masks: dict[int, np.ndarray] = {}
    host_8_components: dict[int, list[dict[str, int | float]]] = {}
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for frame_seq, clip_slot, luma in iter_looped_probe_frames(manifest_source):
        warming_up = frame_seq < WARMUP_FRAMES
        mask = backend.apply(luma, warming_up=warming_up)
        if warming_up:
            continue
        mask = open_mask(mask, config.open_radius_px)
        if frame_seq in board_failure_sequences:
            host_8_components[frame_seq] = components_with_connectivity(
                mask,
                connectivity=8,
                min_area_px=config.min_area_px,
                max_area_px=config.max_area_px,
            )
        if frame_seq in board_masks:
            host_masks[frame_seq] = mask.copy()
        components = find_components(mask, config.min_area_px, config.max_area_px)
        serialized = [_component_dict(component) for component in components]
        rows.append(
            {
                "frame_seq": frame_seq,
                "clip_slot": clip_slot,
                "raw_components": len(serialized),
                "overlap_pairs": overlapping_bbox_pairs(serialized),
                "components": serialized,
            }
        )
        if time.monotonic() - started > MAX_EXPERIMENT_MINUTES * 60.0:
            raise GuardrailViolation("host discriminator exceeded the 20-minute run budget")
    wall_s = time.monotonic() - started
    summary = summarize_host_rows(rows, total_frames=manifest["total_frames"])
    mask_diff = (
        compare_fg_masks(board_masks, host_masks)
        if board_fg_masks_path is not None
        else None
    )
    if board_ccl_log_path is not None:
        summary["paired_discriminator"] = evaluate_host_discriminator(
            rows,
            board_rows,
            manifest_path=manifest_source,
            host_8_components=host_8_components,
            mask_diff_within_tolerance=(
                isinstance(mask_diff, Mapping)
                and int(mask_diff["frames_compared"]) > 0
                and int(mask_diff["differing_pixels"]) == 0
            ),
        )
        summary["clean_host"] = summary["paired_discriminator"]["clean_host"]
    payload = {
        "schema": HOST_SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "board": board.strip() if isinstance(board, str) else None,
        "manifest_path": str(manifest_source.resolve(strict=True)),
        "manifest_sha256": sha256_file(manifest_source),
        "clip_sha256": manifest["clip_sha256"],
        "truth_sha256": manifest["truth_sha256"],
        "seed": manifest["seed"],
        "knobs": normalized,
        "frozen": {
            "proc_width": PROC_WIDTH,
            "proc_height": PROC_HEIGHT,
            "warmup_frames": WARMUP_FRAMES,
            "noise_dn": SCENE_NOISE_DN,
            "cap": COMPONENT_CAP,
        },
        "wall_s": wall_s,
        "summary": summary,
        "frames": rows,
    }
    if board_fg_masks_path is not None:
        payload["mask_diff"] = mask_diff
        payload["board_fg_masks"] = {
            "path": str(Path(board_fg_masks_path).resolve(strict=True)),
            "sha256": sha256_file(board_fg_masks_path),
            "format": "SWFM/1 repeated 28-byte header plus row-major binary mask",
        }
    if board_ccl_log_path is not None:
        payload["board_ccl_log"] = {
            "path": str(Path(board_ccl_log_path).resolve(strict=True)),
            "sha256": sha256_file(board_ccl_log_path),
        }
    destination = Path(output_path)
    if destination.exists() or destination.is_symlink():
        raise CampaignError(f"refusing to replace retained artifact {destination}")
    retained_dir = destination.parent / f"{destination.name}.inputs"
    if retained_dir.exists() or retained_dir.is_symlink():
        raise CampaignError(f"refusing to replace retained input bundle {retained_dir}")
    retained_dir.mkdir(parents=True)
    _assert_no_symlinks(retained_dir)
    sources: dict[str, Path] = {
        "manifest": manifest_source,
        "clip": _resolve_manifest_member(manifest_source, manifest["clip_path"], "clip_path"),
        "truth": _resolve_manifest_member(
            manifest_source, manifest["truth_path"], "truth_path"
        ),
    }
    if board_ccl_log_path is not None:
        sources["board_ccl_log"] = Path(board_ccl_log_path)
    if board_fg_masks_path is not None:
        sources["board_fg_masks"] = Path(board_fg_masks_path)
    retained_names = {
        "manifest": "probe_manifest.json",
        "clip": "probe.swij",
        "truth": "truth_slots.jsonl",
        "board_ccl_log": "board_ccl.jsonl",
        "board_fg_masks": "board_failed_masks.swfm",
    }
    retained: dict[str, Path] = {}
    for name, source_path in sources.items():
        target = retained_dir / retained_names[name]
        _write_new_bytes(target, source_path.read_bytes())
        retained[name] = target

    def relative(path: Path) -> str:
        return path.relative_to(destination.parent).as_posix()

    payload["manifest_path"] = relative(retained["manifest"])
    payload["inputs"] = {
        "manifest_path": relative(retained["manifest"]),
        "manifest_sha256": payload["manifest_sha256"],
        "clip_path": relative(retained["clip"]),
        "clip_sha256": payload["clip_sha256"],
        "truth_path": relative(retained["truth"]),
        "truth_sha256": payload["truth_sha256"],
    }
    if "board_ccl_log" in retained:
        payload["board_ccl_log"]["path"] = relative(retained["board_ccl_log"])
        payload["inputs"].update(
            {
                "board_ccl_log_path": relative(retained["board_ccl_log"]),
                "board_ccl_log_sha256": payload["board_ccl_log"]["sha256"],
            }
        )
    if "board_fg_masks" in retained:
        payload["board_fg_masks"]["path"] = relative(retained["board_fg_masks"])
        payload["inputs"].update(
            {
                "board_fg_masks_path": relative(retained["board_fg_masks"]),
                "board_fg_masks_sha256": payload["board_fg_masks"]["sha256"],
            }
        )
    _write_new_bytes(destination, _canonical_json(payload))
    return WrittenArtifact(destination, sha256_file(destination), payload)


def classify_ccl_failure(region_num: object) -> str:
    """Classify an already-failed CCL attempt by the D0-prescribed rule."""

    region = _require_int("u8_region_num", region_num, 0, 255)
    if region == 0:
        return "threshold_runaway"
    if 0 < region < REGION_TABLE_CAPACITY:
        return "sub_cap"
    return "other"


def _validate_bbox(
    component: object,
    *,
    context: str,
    width: int = PROC_WIDTH,
    height: int = PROC_HEIGHT,
) -> dict[str, int]:
    if not isinstance(component, Mapping):
        raise CampaignError(f"{context} component is not an object")
    required = {"bbox_x", "bbox_y", "bbox_w", "bbox_h", "area_px"}
    allowed = required | {"persistence_count", "centroid_u", "centroid_v"}
    if not required <= set(component) or set(component) - allowed:
        raise CampaignError(
            f"{context} component fields must be {sorted(required)} plus optional diagnostics"
        )
    if ("centroid_u" in component) != ("centroid_v" in component):
        raise CampaignError(f"{context} component must carry both centroid coordinates or neither")
    checked = {
        "bbox_x": _require_int(f"{context}.bbox_x", component["bbox_x"], 0, width - 1),
        "bbox_y": _require_int(f"{context}.bbox_y", component["bbox_y"], 0, height - 1),
        "bbox_w": _require_int(f"{context}.bbox_w", component["bbox_w"], 1, width),
        "bbox_h": _require_int(f"{context}.bbox_h", component["bbox_h"], 1, height),
        "area_px": _require_int(f"{context}.area_px", component["area_px"], 1),
    }
    if checked["bbox_x"] + checked["bbox_w"] > width:
        raise CampaignError(f"{context} bbox crosses the processing-grid right edge")
    if checked["bbox_y"] + checked["bbox_h"] > height:
        raise CampaignError(f"{context} bbox crosses the processing-grid bottom edge")
    if "persistence_count" in component:
        checked["persistence_count"] = _require_int(
            f"{context}.persistence_count", component["persistence_count"], PERSISTENCE_FRAMES
        )
    if "centroid_u" in component:
        centroid_u = _require_finite(
            f"{context}.centroid_u", component["centroid_u"], 0.0, float(width)
        )
        centroid_v = _require_finite(
            f"{context}.centroid_v", component["centroid_v"], 0.0, float(height)
        )
        if not _bbox_contains(checked, centroid_u, centroid_v):
            raise CampaignError(f"{context} centroid lies outside its bbox")
    return checked


def _path_is_shift_archive_member(path: Path) -> bool:
    """Recognize immutable predecessor evidence without weakening current logs."""

    parts = Path(os.path.abspath(path)).parts
    return any(
        part == SHIFT_HISTORY_DIRECTORY
        and index + 1 < len(parts)
        and _SHIFT_ARCHIVE.fullmatch(parts[index + 1]) is not None
        for index, part in enumerate(parts)
    )


def _binary64_from_hex(name: str, value: object) -> float:
    if not isinstance(value, str) or _HEX_BINARY64.fullmatch(value) is None:
        raise CampaignError(f"{name} must be exactly 16 lowercase hexadecimal digits")
    number = struct.unpack(">d", bytes.fromhex(value))[0]
    if not math.isfinite(number):
        raise CampaignError(f"{name} must encode a finite IEEE-754 binary64 value")
    return number


def _within_one_binary64_ulp(advisory: float, authoritative: float) -> bool:
    return advisory == authoritative or advisory in {
        math.nextafter(authoritative, -math.inf),
        math.nextafter(authoritative, math.inf),
    }


def _validate_ccl_component(
    component: object, *, context: str, require_centroid_bits: bool
) -> dict[str, int | float]:
    decimal_fields = {
        "centroid_u",
        "centroid_v",
        "bbox_x",
        "bbox_y",
        "bbox_w",
        "bbox_h",
        "area_px",
    }
    bit_fields = {"centroid_u_bits", "centroid_v_bits"}
    fields = frozenset(component) if isinstance(component, Mapping) else frozenset()
    if (
        not isinstance(component, Mapping)
        or fields not in {frozenset(decimal_fields), frozenset(decimal_fields | bit_fields)}
        or (require_centroid_bits and not bit_fields <= fields)
    ):
        raise CampaignError(f"{context} CCL component fields are incomplete or unknown")
    bbox = _validate_bbox(
        {key: component[key] for key in ("bbox_x", "bbox_y", "bbox_w", "bbox_h", "area_px")},
        context=context,
    )
    advisory_u = _require_finite(
        f"{context}.centroid_u", component["centroid_u"], 0.0, float(PROC_WIDTH)
    )
    advisory_v = _require_finite(
        f"{context}.centroid_v", component["centroid_v"], 0.0, float(PROC_HEIGHT)
    )
    if not _bbox_contains(bbox, advisory_u, advisory_v):
        raise CampaignError(f"{context} advisory centroid lies outside its bbox")
    if not bit_fields <= fields:
        return {"centroid_u": advisory_u, "centroid_v": advisory_v, **bbox}

    centroid_u = _binary64_from_hex(
        f"{context}.centroid_u_bits", component["centroid_u_bits"]
    )
    centroid_v = _binary64_from_hex(
        f"{context}.centroid_v_bits", component["centroid_v_bits"]
    )
    if not (0.0 <= centroid_u <= float(PROC_WIDTH)) or not (
        0.0 <= centroid_v <= float(PROC_HEIGHT)
    ):
        raise CampaignError(f"{context} centroid bits encode an out-of-range coordinate")
    if not _bbox_contains(bbox, centroid_u, centroid_v):
        raise CampaignError(f"{context} authoritative centroid lies outside its bbox")
    for name, advisory, authoritative in (
        ("centroid_u", advisory_u, centroid_u),
        ("centroid_v", advisory_v, centroid_v),
    ):
        if not _within_one_binary64_ulp(advisory, authoritative):
            raise CampaignError(
                f"{context}.{name} differs from its authoritative bits by more than "
                "1 binary64 ULP"
            )
    return {"centroid_u": centroid_u, "centroid_v": centroid_v, **bbox}


def load_ccl_log(
    path: str | Path, *, manifest_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Read strict per-attempt board CCL diagnostics and recompute classes."""

    source = Path(path)
    _assert_no_symlinks(source)
    if not source.is_file():
        raise CampaignError(f"CCL log does not exist: {source}")
    # Pre-bit-field shifts remain replayable only from their immutable archive
    # namespace. Any copied/current form must satisfy the live schema.
    require_centroid_bits = not _path_is_shift_archive_member(source)
    rows: list[dict[str, Any]] = []
    required = {
        "frame_seq",
        "api_failure",
        "s8_label_status",
        "u8_region_num",
        "u32_cur_area_thr",
        "nonzero_region_slots",
        "region_count_mismatch",
        "accepted_components",
        "overlap_pairs",
    }
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        try:
            original = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CampaignError(f"invalid CCL JSONL at {source}:{line_number}: {exc}") from exc
        if not isinstance(original, dict) or not required <= set(original):
            raise CampaignError(f"CCL row {line_number} lacks explicit diagnostic fields")
        if set(original) - (required | {"components"}):
            raise CampaignError(f"CCL row {line_number} has unknown fields")
        frame_seq = _require_int("frame_seq", original["frame_seq"], WARMUP_FRAMES)
        api_failure = original["api_failure"]
        if not isinstance(api_failure, bool):
            raise CampaignError("api_failure must be a JSON boolean")
        status_value = original["s8_label_status"]
        if status_value is not None and (
            not _is_int(status_value) or not -128 <= int(status_value) <= 127
        ):
            raise CampaignError("s8_label_status must be a signed byte or null on API failure")
        status_value = None if status_value is None else int(status_value)
        region = _require_int("u8_region_num", original["u8_region_num"], 0, 255)
        area_threshold = _require_int(
            "u32_cur_area_thr", original["u32_cur_area_thr"], 0, 2**32 - 1
        )
        nonzero = _require_int("nonzero_region_slots", original["nonzero_region_slots"], 0, 254)
        region_count_mismatch = original["region_count_mismatch"]
        if not isinstance(region_count_mismatch, bool):
            raise CampaignError("region_count_mismatch must be a JSON boolean")
        accepted = _require_int("accepted_components", original["accepted_components"], 0, 254)
        pairs = _require_int("overlap_pairs", original["overlap_pairs"], 0)
        components_value = original.get("components", [])
        if not isinstance(components_value, list):
            raise CampaignError("CCL components must be a list")
        components = [
            _validate_ccl_component(
                component,
                context=f"CCL row {line_number}",
                require_centroid_bits=require_centroid_bits,
            )
            for component in components_value
        ]
        if api_failure:
            if (
                status_value is not None
                or region != 0
                or area_threshold != 0
                or nonzero != 0
                or region_count_mismatch
                or accepted != 0
                or components
                or pairs != 0
            ):
                raise CampaignError("API failure row must carry only its null/zero sentinels")
            failure_class = "api_failure"
        elif status_value == 0:
            if region_count_mismatch != (region != nonzero):
                raise CampaignError(
                    "successful CCL row region_count_mismatch disagrees with its raw fields"
                )
            if len(components) != accepted:
                raise CampaignError("successful CCL row must retain every returned component bbox")
            if accepted > nonzero:
                raise CampaignError("accepted_components exceeds nonzero_region_slots")
            if pairs != overlapping_bbox_pairs(components):
                raise CampaignError("CCL overlap_pairs does not match retained component bboxes")
            failure_class = None
        else:
            if status_value is None:
                raise CampaignError("completed CCL row requires s8_label_status")
            if region_count_mismatch or nonzero != 0 or accepted != 0 or components or pairs != 0:
                raise CampaignError("failed CCL row must return zero components and overlap pairs")
            failure_class = classify_ccl_failure(region)
        rows.append(
            {
                "frame_seq": frame_seq,
                "api_failure": api_failure,
                "s8_label_status": status_value,
                "u8_region_num": region,
                "u32_cur_area_thr": area_threshold,
                "nonzero_region_slots": nonzero,
                "region_count_mismatch": region_count_mismatch,
                "accepted_components": accepted,
                "overlap_pairs": pairs,
                "components": components,
                "failure_class": failure_class,
            }
        )
    sequences = [row["frame_seq"] for row in rows]
    if len(sequences) != len(set(sequences)) or sequences != sorted(sequences):
        raise CampaignError("CCL rows must be unique and strictly ordered by frame_seq")
    if manifest_path is not None:
        manifest = load_probe_manifest(manifest_path)
        expected = list(range(WARMUP_FRAMES, manifest["total_frames"]))
        if sequences != expected:
            raise GuardrailViolation("CCL log must cover every post-warm-up probe frame")
    return rows


def aggregate_ccl_rows(rows: Sequence[Mapping[str, object]]) -> dict[str, int]:
    failures = [
        row
        for row in rows
        if row.get("api_failure") is False and row.get("s8_label_status") != 0
    ]
    return {
        "ccl_attempts": len(rows),
        "ccl_api_failures": sum(row.get("api_failure") is True for row in rows),
        "ccl_label_failures": len(failures),
        "ccl_threshold_runaway_failures": sum(
            row.get("failure_class") == "threshold_runaway" for row in failures
        ),
        "ccl_sub_cap_failures": sum(row.get("failure_class") == "sub_cap" for row in failures),
        "ccl_other_failures": sum(row.get("failure_class") == "other" for row in failures),
        "ccl_region_count_mismatch_frames": sum(
            row.get("region_count_mismatch") is True for row in rows
        ),
        "overlapping_bbox_pairs": sum(int(row.get("overlap_pairs", 0)) for row in rows),
        "frames_with_overlapping_bboxes": sum(
            int(row.get("overlap_pairs", 0)) > 0 for row in rows
        ),
    }


_CCL_COUNTERS = (
    "ccl_attempts",
    "ccl_api_failures",
    "ccl_label_failures",
    "ccl_threshold_runaway_failures",
    "ccl_sub_cap_failures",
    "ccl_other_failures",
    "ccl_region_count_mismatch_frames",
    "overlapping_bbox_pairs",
    "frames_with_overlapping_bboxes",
)


def compute_objective(
    stats: Mapping[str, object], *, derived_from_rows: Mapping[str, int] | None = None
) -> dict[str, Any]:
    """Compute fail rate from explicit counters; never infer a denominator."""

    counters: dict[str, int] = {}
    for name in _CCL_COUNTERS:
        if name not in stats:
            raise CampaignError(f"board stats lacks explicit {name}")
        counters[name] = _require_int(name, stats[name], 0)
    attempts = counters["ccl_attempts"]
    if counters["ccl_api_failures"] != 0:
        raise GuardrailViolation(
            "ccl_api_failures is nonzero; the run is invalid rather than part of the objective"
        )
    if attempts < MIN_POSTWARM_FRAMES:
        raise GuardrailViolation(
            f"objective needs at least {MIN_POSTWARM_FRAMES} CCL attempts, got {attempts}"
        )
    frames_in = _require_int("frames_in", stats.get("frames_in"), MIN_TOTAL_FRAMES)
    if attempts > frames_in - WARMUP_FRAMES:
        raise CampaignError("ccl_attempts exceeds the available post-warm-up frames")
    failures = counters["ccl_label_failures"]
    classified = (
        counters["ccl_threshold_runaway_failures"]
        + counters["ccl_sub_cap_failures"]
        + counters["ccl_other_failures"]
    )
    if classified != failures:
        raise CampaignError("CCL failure classifier counters must sum to ccl_label_failures")
    if failures > attempts:
        raise CampaignError("ccl_label_failures exceeds ccl_attempts")
    if counters["ccl_region_count_mismatch_frames"] > attempts - failures:
        raise CampaignError("CCL region-count mismatches exceed successful attempts")
    if counters["frames_with_overlapping_bboxes"] > attempts:
        raise CampaignError("frames_with_overlapping_bboxes exceeds ccl_attempts")
    if derived_from_rows is not None:
        mismatches = {
            name: (counters[name], derived_from_rows.get(name))
            for name in _CCL_COUNTERS
            if counters[name] != derived_from_rows.get(name)
        }
        if mismatches:
            raise CampaignError(f"aggregate CCL stats disagree with the retained log: {mismatches}")
    return {
        "detector_fail_rate": failures / attempts,
        "denominator": "ccl_attempts",
        "ccl_attempts": attempts,
        "ccl_label_failures": failures,
        "ccl_region_count_mismatch_frames": counters[
            "ccl_region_count_mismatch_frames"
        ],
        # C-001 predeclares only threshold-runaway and sub-cap as the causal
        # model.  A 254/255 region value is retained as ``other`` for truth,
        # but it is a contract-change stop rather than a third tuneable mode.
        "contract_change_required": counters["ccl_other_failures"] > 0,
        "failure_classes": {
            "threshold_runaway": counters["ccl_threshold_runaway_failures"],
            "sub_cap": counters["ccl_sub_cap_failures"],
            "other": counters["ccl_other_failures"],
        },
    }


def validate_board_run_binding(
    stats: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    knobs: Mapping[str, object],
) -> None:
    """Bind board echoes/counters to the selected probe and knob declaration."""

    normalized = normalize_knobs(knobs)
    expected_config: dict[str, object] = {
        "detector": "ive-gmm2",
        "source_mode": benchmark.SOURCE_MODE_INJECT_RAM,
        "proc_width": PROC_WIDTH,
        "proc_height": PROC_HEIGHT,
        "warmup_frames": WARMUP_FRAMES,
        "max_components_per_frame": COMPONENT_CAP,
        "max_area_px": 10000,
        "persistence_frames": PERSISTENCE_FRAMES,
        "persistence_gate_px": PERSISTENCE_GATE_PX,
        "min_area_px": normalized.get("min_area_px", 2),
        "morph_open": normalized.get("open_radius_px", 1),
        "gmm2_match_sigmas": normalized.get("ive_approx.match_sigmas", 2.5),
        "gmm2_var_min": normalized.get("ive_approx.var_min", 25.0),
        "source_frames_planned": manifest["total_frames"],
        "source_frames_served": manifest["total_frames"],
        "frames_in": manifest["total_frames"],
        "ram_clip_frames": manifest["ram_clip_frames"],
        "ram_clip_bytes": PROC_WIDTH * PROC_HEIGHT * int(manifest["ram_clip_frames"]),
        "ram_loop_pts_stride_ns": manifest["ram_loop_pts_stride_ns"],
        "ram_loop_period_ns": 0,
        "ram_budget_mb": manifest["ram_budget_mb"],
        "ccl_attempts": manifest["postwarm_frames"],
    }
    for name, expected in expected_config.items():
        if name not in stats:
            raise GuardrailViolation(f"board stats lacks run-binding echo {name}")
        actual = stats[name]
        if name in {"gmm2_match_sigmas", "gmm2_var_min"}:
            matches = (
                not isinstance(actual, bool)
                and isinstance(actual, (int, float))
                and float(np.float32(actual)) == float(np.float32(expected))
            )
        elif isinstance(expected, float):
            matches = (
                not isinstance(actual, bool)
                and isinstance(actual, (int, float))
                and float(actual) == expected
            )
        elif isinstance(expected, int):
            matches = _is_int(actual) and int(actual) == expected
        else:
            matches = actual == expected
        if not matches:
            raise GuardrailViolation(
                f"board run binding mismatch: {name}={actual!r}, expected {expected!r}"
            )
    _require_int("fg_mask_limit", stats.get("fg_mask_limit"), 0, MAX_FAILED_MASKS)
    expected_failures = int(stats["ccl_api_failures"]) + int(stats["ccl_label_failures"])
    if stats.get("frames_detector_failed") != expected_failures:
        raise GuardrailViolation(
            "frames_detector_failed must equal API plus label failures for this CCL run"
        )
    expected_scored = int(stats["ccl_attempts"]) - expected_failures
    if stats.get("frames_scored") != expected_scored:
        raise GuardrailViolation(
            "frames_scored must equal completed successful post-warm-up CCL attempts"
        )
    mismatch_frames = _require_int(
        "ccl_region_count_mismatch_frames",
        stats.get("ccl_region_count_mismatch_frames"),
        0,
        int(stats["ccl_attempts"]),
    )
    if mismatch_frames > expected_scored:
        raise GuardrailViolation(
            "CCL region-count mismatches exceed successful post-warm-up attempts"
        )
    optional_digest_echoes = {
        "probe_manifest_sha256": manifest.get("manifest_sha256"),
        "ram_clip_sha256": manifest.get("clip_sha256"),
    }
    for name, expected in optional_digest_echoes.items():
        if name in stats and expected is not None and stats[name] != expected:
            raise GuardrailViolation(f"board {name} echo disagrees with the selected probe")


def validate_external_run_binding(
    binding_path: str | Path,
    *,
    board: str,
    manifest_path: str | Path,
    manifest: Mapping[str, object],
    stats_path: str | Path,
    ccl_log_path: str | Path,
    packet_log_path: str | Path,
) -> dict[str, Any]:
    """Validate the rig adapter's identity + remote-clip digest evidence.

    The campaign runner deliberately performs no SSH/switch calls.  The
    external adapter that did perform them must retain this small proof object;
    without it, a same-shaped clip from another seed could be relabelled.
    """

    payload = _read_json_object(Path(binding_path))
    required = {
        "schema",
        "campaign_id",
        "board",
        "seed",
        "identity",
        "source_mode",
        "proc_width",
        "proc_height",
        "total_frames",
        "ram_clip_frames",
        "probe_manifest_sha256",
        "remote_clip_sha256",
        "stats_sha256",
        "ccl_log_sha256",
        "packet_log_sha256",
        "run_id",
        "remote_run_dir",
    }
    if set(payload) != required:
        raise GuardrailViolation("external run-binding fields are incomplete or unknown")
    expected = {
        "schema": RUN_BINDING_SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "board": board,
        "seed": manifest["seed"],
        "source_mode": benchmark.SOURCE_MODE_INJECT_RAM,
        "proc_width": PROC_WIDTH,
        "proc_height": PROC_HEIGHT,
        "total_frames": manifest["total_frames"],
        "ram_clip_frames": manifest["ram_clip_frames"],
        "probe_manifest_sha256": sha256_file(manifest_path),
        "remote_clip_sha256": manifest["clip_sha256"],
        "stats_sha256": sha256_file(stats_path),
        "ccl_log_sha256": sha256_file(ccl_log_path),
        "packet_log_sha256": sha256_file(packet_log_path),
    }
    for name, value in expected.items():
        if payload[name] != value:
            raise GuardrailViolation(
                f"external run binding mismatch: {name}={payload[name]!r}, expected {value!r}"
            )
    run_id = payload["run_id"]
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise GuardrailViolation("external run binding run_id must be 32 lowercase hex")
    remote_run_dir = payload["remote_run_dir"]
    remote_run_path = _canonical_remote_path(
        "external run binding remote_run_dir", remote_run_dir
    )
    if run_id not in remote_run_path.as_posix():
        raise GuardrailViolation("external run binding remote_run_dir is not unique/canonical")
    identity = payload["identity"]
    if not isinstance(identity, Mapping) or set(identity) != {"board", "mac", "image_marker"}:
        raise GuardrailViolation("run binding requires board/MAC/image identity")
    checked_identity = BoardIdentity(
        str(identity["board"]), str(identity["mac"]), str(identity["image_marker"])
    )
    if checked_identity.board != board:
        raise GuardrailViolation("run-binding identity board label does not match the run")
    return payload


def _validate_recovery_ledger_snapshot(path: Path) -> list[dict[str, Any]]:
    """Replay the independent PoE reservation hash chain retained by a run."""

    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise GuardrailViolation("recovery ledger snapshot has a torn final row")
    rows: list[dict[str, Any]] = []
    previous = "0" * 64
    board_counts: dict[str, int] = {}
    run_ids: set[str] = set()
    required = {
        "schema",
        "campaign_id",
        "event",
        "run_id",
        "board",
        "mac",
        "shift_cycle_n",
        "board_cycle_n",
        "recorded_at",
        "previous_sha256",
        "row_sha256",
    }
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GuardrailViolation(
                f"recovery ledger line {line_number} is not canonical JSON"
            ) from exc
        if not isinstance(row, dict) or set(row) != required:
            raise GuardrailViolation("recovery ledger row fields are incomplete or unknown")
        if (
            row["schema"] != RECOVERY_LEDGER_SCHEMA
            or row["campaign_id"] != CAMPAIGN_ID
            or row["event"] != "poe_cycle_reserved"
        ):
            raise GuardrailViolation("recovery ledger schema/event differs from C-001")
        row_run_id = row["run_id"]
        if (
            not isinstance(row_run_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", row_run_id) is None
            or row_run_id in run_ids
        ):
            raise GuardrailViolation("recovery ledger run_id is malformed or reused")
        run_ids.add(row_run_id)
        identity = BoardIdentity(str(row["board"]), str(row["mac"]), "recovery-ledger")
        mac = identity.mac.lower()
        board_counts[mac] = board_counts.get(mac, 0) + 1
        if (
            _require_int("recovery shift_cycle_n", row["shift_cycle_n"], 1) != len(rows) + 1
            or _require_int("recovery board_cycle_n", row["board_cycle_n"], 1)
            != board_counts[mac]
            or int(row["shift_cycle_n"]) > MAX_POWER_CYCLES
            or int(row["board_cycle_n"]) > MAX_RECOVERY_CYCLES_PER_BOARD
            or row["previous_sha256"] != previous
            or not isinstance(row["recorded_at"], str)
            or not row["recorded_at"].strip()
        ):
            raise GuardrailViolation("recovery ledger counters/hash link are invalid")
        digest = _validate_digest("recovery row_sha256", row["row_sha256"])
        material = {name: value for name, value in row.items() if name != "row_sha256"}
        if hashlib.sha256(_canonical_json(material)).hexdigest() != digest:
            raise GuardrailViolation("recovery ledger row digest fails semantic replay")
        previous = digest
        rows.append(row)
    return rows


def _validate_attempt_ledger_snapshot(path: Path) -> list[dict[str, Any]]:
    """Replay the physical-attempt reservation/outcome hash chain."""

    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise GuardrailViolation("attempt ledger snapshot is empty or torn")
    rows: list[dict[str, Any]] = []
    reservations: dict[str, dict[str, Any]] = {}
    outcome_counts: dict[str, int] = {}
    previous = "0" * 64
    common = {
        "schema",
        "campaign_id",
        "event",
        "run_id",
        "attempt_n",
        "recorded_at",
        "previous_sha256",
        "row_sha256",
    }
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GuardrailViolation(
                f"attempt ledger line {line_number} is not canonical JSON"
            ) from exc
        if not isinstance(row, dict) or not common <= set(row):
            raise GuardrailViolation("attempt ledger row fields are incomplete")
        if (
            row["schema"] != ATTEMPT_LEDGER_SCHEMA
            or row["campaign_id"] != CAMPAIGN_ID
        ):
            raise GuardrailViolation("attempt ledger schema/campaign differs from C-001")
        run_id = row["run_id"]
        if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise GuardrailViolation("attempt ledger run_id is malformed")
        if row["event"] == "attempt_reserved":
            required = common | {"board", "mac", "seed", "manifest_sha256"}
            if set(row) != required or run_id in reservations:
                raise GuardrailViolation("attempt reservation fields/run_id are invalid")
            BoardIdentity(str(row["board"]), str(row["mac"]), "attempt-ledger")
            validate_seed(row["seed"])
            _validate_digest("attempt manifest_sha256", row["manifest_sha256"])
            if _require_int("attempt_n", row["attempt_n"], 1, MAX_EXPERIMENTS) != (
                len(reservations) + 1
            ):
                raise GuardrailViolation("attempt reservation number is not sequential")
            reservations[run_id] = row
            outcome_counts[run_id] = 0
        elif row["event"] == "attempt_outcome":
            required = common | {"outcome_n", "outcome", "wall_s", "wedge", "error"}
            reservation = reservations.get(run_id)
            if set(row) != required or reservation is None:
                raise GuardrailViolation("attempt outcome has no valid reservation")
            outcome_n = _require_int("attempt outcome_n", row["outcome_n"], 1)
            if (
                row["attempt_n"] != reservation["attempt_n"]
                or outcome_n != outcome_counts[run_id] + 1
                or not isinstance(row["outcome"], str)
                or re.fullmatch(r"[a-z][a-z0-9_]*", row["outcome"]) is None
                or not isinstance(row["wedge"], bool)
                or (row["error"] is not None and not isinstance(row["error"], str))
            ):
                raise GuardrailViolation("attempt outcome fields are invalid")
            _require_finite(
                "attempt outcome wall_s",
                row["wall_s"],
                0.0,
                MAX_EXPERIMENT_MINUTES * 60,
            )
            outcome_counts[run_id] = outcome_n
        else:
            raise GuardrailViolation("attempt ledger event is unknown")
        if (
            row["previous_sha256"] != previous
            or not isinstance(row["recorded_at"], str)
            or not row["recorded_at"].strip()
        ):
            raise GuardrailViolation("attempt ledger timestamp/hash link is invalid")
        digest = _validate_digest("attempt row_sha256", row["row_sha256"])
        material = {name: value for name, value in row.items() if name != "row_sha256"}
        if hashlib.sha256(_canonical_json(material)).hexdigest() != digest:
            raise GuardrailViolation("attempt ledger row digest fails semantic replay")
        previous = digest
        rows.append(row)
    return rows


def validate_provision_artifact(
    provision_path: str | Path,
    *,
    board: str,
    stats: Mapping[str, object],
    manifest: Mapping[str, object],
    stats_path: str | Path | None = None,
    ccl_log_path: str | Path | None = None,
    packet_log_path: str | Path | None = None,
    board_fg_masks_path: str | Path | None = None,
    exit_status_path: str | Path | None = None,
    run_log_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate the collected deployment proof without performing provisioning."""

    payload = _read_json_object(Path(provision_path))
    if payload.get("schema") != "d8-provision/1" or payload.get("binary_verified") is not True:
        raise GuardrailViolation("provision artifact must prove the deployed binary hash")
    _require_finite("provision.wall_s", payload.get("wall_s"), 0.0, MAX_EXPERIMENT_MINUTES * 60)
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise GuardrailViolation("provision run_id must be 32 lowercase hex")
    remote_run_dir = payload.get("remote_run_dir")
    remote_run_path = _canonical_remote_path("provision remote_run_dir", remote_run_dir)
    if run_id not in remote_run_path.as_posix():
        raise GuardrailViolation("provision remote_run_dir is not unique/canonical")
    if (
        payload.get("daemon_exit_code") != 0
        or payload.get("exit_status") != 0
        or payload.get("daemon_stopped") is not True
        or payload.get("completed_before_deadline") is not True
        or payload.get("stop_succeeded") is not None
    ):
        raise GuardrailViolation(
            "provision must prove a natural exit=0 before deadline without forced stop"
        )
    identity_preflight = payload.get("identity_preflight")
    if not isinstance(identity_preflight, Mapping) or set(identity_preflight) != {
        "board",
        "mac",
        "image_marker",
        "kernel",
        "interface",
    }:
        raise GuardrailViolation("provision lacks transport-read identity_preflight")
    preflight_identity = BoardIdentity(
        str(identity_preflight["board"]),
        str(identity_preflight["mac"]),
        str(identity_preflight["image_marker"]),
    )
    if preflight_identity.board != board or any(
        not isinstance(identity_preflight[key], str) or not identity_preflight[key].strip()
        for key in ("kernel", "interface")
    ):
        raise GuardrailViolation("provision identity_preflight is incomplete or wrong board")
    power_cycles = _require_int(
        "provision.power_cycles", payload.get("power_cycles"), 0, MAX_RECOVERY_CYCLES_PER_BOARD
    )
    recovery_attempts = payload.get("recovery_attempts")
    if not isinstance(recovery_attempts, list) or len(recovery_attempts) != power_cycles:
        raise GuardrailViolation("provision recovery_attempts disagrees with power_cycles")
    for attempt in recovery_attempts:
        if not isinstance(attempt, Mapping) or set(attempt) != {
            "run_id",
            "board",
            "mac",
            "shift_cycle_n",
            "board_cycle_n",
            "reservation_sha256",
            "outcome",
            "identity_revalidated",
        }:
            raise GuardrailViolation("provision recovery attempt schema is invalid")
        if (
            attempt["run_id"] != run_id
            or attempt["board"] != preflight_identity.board
            or str(attempt["mac"]).lower() != preflight_identity.mac.lower()
            or _require_int("shift_cycle_n", attempt["shift_cycle_n"], 1, MAX_POWER_CYCLES)
            < 1
            or _require_int(
                "board_cycle_n",
                attempt["board_cycle_n"],
                1,
                MAX_RECOVERY_CYCLES_PER_BOARD,
            )
            < 1
            or attempt["outcome"] != "ready"
            or attempt["identity_revalidated"] is not True
        ):
            raise GuardrailViolation("provision recovery attempt did not end identity-ready")
        _validate_digest("recovery reservation_sha256", attempt["reservation_sha256"])
    recovery_ledger = payload.get("recovery_ledger")
    if not isinstance(recovery_ledger, Mapping) or set(recovery_ledger) != {
        "path",
        "sha256",
        "tip_sha256",
    }:
        raise GuardrailViolation("provision lacks the shared recovery-ledger snapshot")
    recovery_path_value = recovery_ledger["path"]
    if not isinstance(recovery_path_value, str) or Path(recovery_path_value).name != (
        recovery_path_value
    ):
        raise GuardrailViolation("recovery ledger snapshot path must be a local filename")
    recovery_path = Path(provision_path).parent / recovery_path_value
    if sha256_file(recovery_path) != _validate_digest(
        "recovery ledger sha256", recovery_ledger["sha256"]
    ):
        raise GuardrailViolation("recovery ledger snapshot digest mismatch")
    tip = _validate_digest("recovery ledger tip_sha256", recovery_ledger["tip_sha256"])
    recovery_rows = _validate_recovery_ledger_snapshot(recovery_path)
    expected_recovery_tip = (
        str(recovery_rows[-1]["row_sha256"]) if recovery_rows else "0" * 64
    )
    if expected_recovery_tip != tip:
        raise GuardrailViolation("recovery ledger tip does not match its final row")
    by_digest = {str(row["row_sha256"]): row for row in recovery_rows}
    for attempt in recovery_attempts:
        row = by_digest.get(str(attempt["reservation_sha256"]))
        if row is None or any(
            row[name] != attempt[name]
            for name in (
                "run_id",
                "board",
                "mac",
                "shift_cycle_n",
                "board_cycle_n",
            )
        ):
            raise GuardrailViolation(
                "recovery attempt is not bound to its durable reservation row"
            )
    attempt_reservation = payload.get("attempt_reservation")
    if not isinstance(attempt_reservation, Mapping) or set(attempt_reservation) != {
        "run_id",
        "attempt_n",
        "reservation_sha256",
    }:
        raise GuardrailViolation("provision lacks its physical-attempt reservation")
    if attempt_reservation["run_id"] != run_id:
        raise GuardrailViolation("attempt reservation run_id differs from provision")
    attempt_n = _require_int(
        "attempt reservation attempt_n",
        attempt_reservation["attempt_n"],
        1,
        MAX_EXPERIMENTS,
    )
    reservation_sha = _validate_digest(
        "attempt reservation_sha256", attempt_reservation["reservation_sha256"]
    )
    attempt_ledger = payload.get("attempt_ledger")
    if not isinstance(attempt_ledger, Mapping) or set(attempt_ledger) != {
        "path",
        "sha256",
        "tip_sha256",
    }:
        raise GuardrailViolation("provision lacks the shared attempt-ledger snapshot")
    attempt_path_value = attempt_ledger["path"]
    if not isinstance(attempt_path_value, str) or Path(attempt_path_value).name != (
        attempt_path_value
    ):
        raise GuardrailViolation("attempt ledger snapshot path must be a local filename")
    attempt_path = Path(provision_path).parent / attempt_path_value
    if sha256_file(attempt_path) != _validate_digest(
        "attempt ledger sha256", attempt_ledger["sha256"]
    ):
        raise GuardrailViolation("attempt ledger snapshot digest mismatch")
    attempt_tip = _validate_digest(
        "attempt ledger tip_sha256", attempt_ledger["tip_sha256"]
    )
    attempt_rows = _validate_attempt_ledger_snapshot(attempt_path)
    if str(attempt_rows[-1]["row_sha256"]) != attempt_tip:
        raise GuardrailViolation("attempt ledger tip does not match its final row")
    reservation_row = next(
        (row for row in attempt_rows if row["row_sha256"] == reservation_sha), None
    )
    manifest_digest = _validate_digest(
        "provision probe_manifest_sha256", payload.get("probe_manifest_sha256")
    )
    if (
        reservation_row is None
        or reservation_row["event"] != "attempt_reserved"
        or reservation_row["run_id"] != run_id
        or reservation_row["attempt_n"] != attempt_n
        or reservation_row["board"] != preflight_identity.board
        or str(reservation_row["mac"]).lower() != preflight_identity.mac.lower()
        or reservation_row["seed"] != manifest["seed"]
        or reservation_row["manifest_sha256"] != manifest_digest
    ):
        raise GuardrailViolation("provision is not bound to its attempt reservation")
    outcomes = [
        row
        for row in attempt_rows
        if row["event"] == "attempt_outcome" and row["run_id"] == run_id
    ]
    if (
        len(outcomes) != 1
        or outcomes[0]["outcome"] != "run_complete"
        or outcomes[0]["wedge"] is not False
        or outcomes[0]["error"] is not None
        or not math.isclose(
            float(outcomes[0]["wall_s"]), float(payload["wall_s"]), abs_tol=1e-9
        )
    ):
        raise GuardrailViolation("attempt ledger does not prove one clean completed run")
    collected_sha256 = payload.get("collected_sha256")
    if not isinstance(collected_sha256, Mapping):
        raise GuardrailViolation("provision lacks collected output SHA256s")
    for name, digest in collected_sha256.items():
        if not isinstance(name, str) or Path(name).name != name:
            raise GuardrailViolation("provision collected_sha256 contains a path alias")
        _validate_digest(f"provision collected {name}", digest)
    local_sha = _validate_digest("provision.local_sha256", payload.get("local_sha256"))
    remote_sha = _validate_digest("provision.remote_sha256", payload.get("remote_sha256"))
    if local_sha != remote_sha:
        raise GuardrailViolation("local and deployed ARM binary SHA256 differ")
    if payload.get("source_verified") is not True or not payload.get("source_remote_path"):
        raise GuardrailViolation("provision artifact must prove the pushed RAM clip hash")
    source_remote_path = _canonical_remote_path(
        "provision source_remote_path", payload["source_remote_path"]
    )
    try:
        relative_source = source_remote_path.relative_to(remote_run_path)
    except ValueError as exc:
        raise GuardrailViolation("verified RAM source is outside remote_run_dir") from exc
    if not relative_source.parts:
        raise GuardrailViolation("verified RAM source must be a child of remote_run_dir")
    source_local = _validate_digest(
        "provision.source_local_sha256", payload.get("source_local_sha256")
    )
    source_remote = _validate_digest(
        "provision.source_remote_sha256", payload.get("source_remote_sha256")
    )
    if source_local != source_remote or source_remote != manifest["clip_sha256"]:
        raise GuardrailViolation("provisioned RAM clip SHA256 differs from the probe manifest")
    node = payload.get("node")
    if not isinstance(node, Mapping) or node.get("name") != board:
        raise GuardrailViolation("provision node identity does not match the scored board")
    ld_library_path = node.get("ld_library_path")
    if not isinstance(ld_library_path, str):
        raise GuardrailViolation("provision node lacks its declared ld_library_path")
    runtime_library = payload.get("runtime_ive_library")
    if not isinstance(runtime_library, Mapping) or set(runtime_library) != {
        "path",
        "sha256_before",
        "sha256_after",
        "stable",
    }:
        raise GuardrailViolation("provision lacks exact IVE runtime library evidence")
    runtime_path = _canonical_remote_path(
        "provision runtime_ive_library.path", runtime_library["path"]
    )
    if ld_library_path != "/oem/usr/lib" or runtime_path.as_posix() != (
        "/oem/usr/lib/librve.so"
    ):
        raise GuardrailViolation(
            "provision must bind the exact /oem/usr/lib/librve.so runtime"
        )
    runtime_before = _validate_digest(
        "provision runtime IVE SHA256 before", runtime_library["sha256_before"]
    )
    runtime_after = _validate_digest(
        "provision runtime IVE SHA256 after", runtime_library["sha256_after"]
    )
    if runtime_library["stable"] is not True or runtime_before != runtime_after:
        raise GuardrailViolation(
            "provision does not prove a stable IVE runtime library across the run"
        )
    remote_binary = _canonical_remote_path(
        "provision remote_binary", payload.get("remote_binary")
    )
    expected_binary = remote_run_path / "skyweave-edge"
    if remote_binary != expected_binary:
        raise GuardrailViolation("provision remote_binary is not the fresh run executable")
    if payload.get("stats") != stats:
        raise GuardrailViolation("provision artifact stats differ from the retained stats.json")
    collected = payload.get("collected")
    if not isinstance(collected, list) or not {
        "stats.json",
        "ccl.jsonl",
        "packets.hex",
        "exit.status",
        "run.log",
    } <= set(collected):
        raise GuardrailViolation("provision artifact did not collect all scored raw outputs")
    required_collected = {
        "stats.json",
        "ccl.jsonl",
        "packets.hex",
        "exit.status",
        "run.log",
    }
    if board_fg_masks_path is not None:
        required_collected.add("fg-masks.swfm")
    if not required_collected <= set(collected_sha256):
        raise GuardrailViolation("provision collected_sha256 omits a scored raw output")
    local_outputs = {
        "stats.json": stats_path,
        "ccl.jsonl": ccl_log_path,
        "packets.hex": packet_log_path,
        "fg-masks.swfm": board_fg_masks_path,
        "exit.status": exit_status_path,
        "run.log": run_log_path,
    }
    for name in required_collected:
        local_path = local_outputs[name]
        if local_path is not None and sha256_file(local_path) != collected_sha256[name]:
            raise GuardrailViolation(f"provision collected SHA256 mismatch for {name}")
    if exit_status_path is not None and Path(exit_status_path).read_bytes() not in {
        b"0",
        b"0\n",
    }:
        raise GuardrailViolation("retained exit.status does not prove exact daemon exit 0")
    if run_log_path is not None and Path(run_log_path).stat().st_size == 0:
        raise GuardrailViolation("retained run.log is empty")
    argv_value = payload.get("argv")
    if not isinstance(argv_value, str):
        raise GuardrailViolation("provision artifact lacks the launched argv")
    try:
        argv = shlex.split(argv_value)
    except ValueError as exc:
        raise GuardrailViolation("provision argv is not valid shell quoting") from exc
    expected_prefix = (
        ["env", f"LD_LIBRARY_PATH={ld_library_path}", str(remote_binary)]
        if ld_library_path
        else [str(remote_binary)]
    )
    if argv[: len(expected_prefix)] != expected_prefix:
        raise GuardrailViolation(
            "provision argv must launch the verified fresh binary with only the declared "
            "LD_LIBRARY_PATH prefix"
        )
    argv = argv[len(expected_prefix) :]
    if not argv or any(
        token == "env" or token.startswith(("LD_PRELOAD=", "LD_LIBRARY_PATH="))
        for token in argv
    ):
        raise GuardrailViolation("provision argv contains an extra wrapper/environment prefix")

    def flag_value(flag: str) -> str:
        if argv.count(flag) != 1:
            raise GuardrailViolation(f"provision argv must carry {flag} exactly once")
        index = argv.index(flag)
        if index + 1 >= len(argv):
            raise GuardrailViolation(f"provision argv {flag} has no value")
        return argv[index + 1]

    required_flags = {
        "--ram-loop-frames": str(manifest["total_frames"]),
        "--ram-loop-pts-stride-ns": str(manifest["ram_loop_pts_stride_ns"]),
        "--ram-budget-mb": str(manifest["ram_budget_mb"]),
        "--ram-loop-period-ns": "0",
        "--detector": "ive",
        "--proc": f"{PROC_WIDTH}x{PROC_HEIGHT}",
        "--warmup": str(WARMUP_FRAMES),
        "--cap": str(COMPONENT_CAP),
        "--min-area-px": str(stats["min_area_px"]),
        "--morph-open": str(stats["morph_open"]),
    }
    for flag, expected in required_flags.items():
        actual = flag_value(flag)
        if actual != expected:
            raise GuardrailViolation(
                f"provision argv binding mismatch: {flag}={actual!r}, expected {expected!r}"
            )
    for flag, stats_key in (
        ("--gmm2-match-sigmas", "gmm2_match_sigmas"),
        ("--gmm2-var-min", "gmm2_var_min"),
    ):
        actual = float(flag_value(flag))
        if float(np.float32(actual)) != float(np.float32(stats[stats_key])):
            raise GuardrailViolation(f"provision argv {flag} differs from the stats echo")
    if argv.count("--inject-ram") != 1:
        raise GuardrailViolation("provision argv must select exactly one RAM-loop source")
    if flag_value("--inject-ram") != payload["source_remote_path"]:
        raise GuardrailViolation(
            "provision argv RAM source differs from the verified remote clip path"
        )
    if "--inject-file" in argv or "--inject-listen" in argv:
        raise GuardrailViolation("provision argv contains a second injection source")
    output_flags = {
        "--stats": "stats.json",
        "--ccl-log": "ccl.jsonl",
        "--packet-log": "packets.hex",
    }
    if board_fg_masks_path is not None:
        output_flags["--fg-mask-log"] = "fg-masks.swfm"
    for flag, filename in output_flags.items():
        expected_path = f"{remote_run_dir.rstrip('/')}/{filename}"
        if flag_value(flag) != expected_path:
            raise GuardrailViolation(f"provision argv {flag} is outside remote_run_dir")
    payload["_c001_recovery_rows"] = recovery_rows
    payload["_c001_attempt_rows"] = attempt_rows
    return payload


def _bbox_contains(component: Mapping[str, object], u: float, v: float) -> bool:
    """Half-open bbox containment; no centroid radius or distance gate exists."""

    x = float(component["bbox_x"])
    y = float(component["bbox_y"])
    return x <= u < x + float(component["bbox_w"]) and y <= v < y + float(
        component["bbox_h"]
    )


def _maximum_bbox_matching(
    movers: Sequence[Mapping[str, object]], components: Sequence[Mapping[str, object]]
) -> dict[int, int]:
    """Maximum one-to-one matching from truth movers to distinct bboxes."""

    edges = {
        mover_index: [
            component_index
            for component_index, component in enumerate(components)
            if _bbox_contains(component, float(mover["u"]), float(mover["v"]))
        ]
        for mover_index, mover in enumerate(movers)
    }
    component_owner: dict[int, int] = {}

    def augment(mover_index: int, visited: set[int]) -> bool:
        for component_index in edges[mover_index]:
            if component_index in visited:
                continue
            visited.add(component_index)
            owner = component_owner.get(component_index)
            if owner is None or augment(owner, visited):
                component_owner[component_index] = mover_index
                return True
        return False

    for mover_index in range(len(movers)):
        augment(mover_index, set())
    return {mover_index: component for component, mover_index in component_owner.items()}


def persistence_eligibility(
    *, manifest_path: str | Path
) -> dict[str, Any]:
    """Derive truth points persistence can structurally emit, before detection.

    Eligibility is intentionally a property of truth and the frozen persistence
    schedule only: the same mover must be visible for two consecutive scored
    frames and each consecutive processing-grid displacement must be at most
    12 px.  Warm-up does not seed persistence, and a RAM wrap is just another
    consecutive pair whose (usually large) displacement is tested exactly.
    """

    source = Path(manifest_path)
    manifest = load_probe_manifest(source)
    truth = load_truth_slots(manifest, source)
    eligible: set[tuple[int, int]] = set()
    exclusions: list[dict[str, int | str]] = []
    for frame_seq in range(WARMUP_FRAMES, manifest["total_frames"]):
        slot = frame_seq % manifest["ram_clip_frames"]
        for mover_id in range(manifest["movers"]):
            current = truth[slot]["movers"][mover_id]
            if not current["visible"]:
                continue  # not a truth denominator at this frame
            reason: str | None = None
            chain: list[Mapping[str, object]] = []
            for offset in reversed(range(PERSISTENCE_FRAMES)):
                historical_seq = frame_seq - offset
                if historical_seq < WARMUP_FRAMES:
                    reason = "warmup_did_not_seed_persistence"
                    break
                historical_slot = historical_seq % manifest["ram_clip_frames"]
                point = truth[historical_slot]["movers"][mover_id]
                if not point["visible"]:
                    reason = "mover_not_visible_through_required_chain"
                    break
                chain.append(point)
            if reason is None:
                for previous, following in zip(chain, chain[1:], strict=False):
                    displacement = math.hypot(
                        float(following["u"]) - float(previous["u"]),
                        float(following["v"]) - float(previous["v"]),
                    )
                    if displacement > PERSISTENCE_GATE_PX:
                        reason = "truth_displacement_exceeds_persistence_gate"
                        break
            if reason is None:
                eligible.add((frame_seq, mover_id))
            else:
                exclusions.append(
                    {"frame_seq": frame_seq, "mover_id": mover_id, "reason": reason}
                )
    by_reason: dict[str, int] = {}
    for item in exclusions:
        reason = str(item["reason"])
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "eligible": eligible,
        "structural_exclusions": exclusions,
        "exclusions_by_reason": dict(sorted(by_reason.items())),
        "definition": (
            "frozen persistence_frames=2 and persistence_gate_px=12.0: same mover "
            "visible through the scored-frame chain and every consecutive truth "
            "displacement within the gate; warm-up does not seed the chain"
        ),
    }


def score_mover_recall(
    frame_rows: Sequence[Mapping[str, object]],
    *,
    manifest_path: str | Path,
    coordinate_space: str = "proc",
    eligibility: str = "all",
) -> dict[str, Any]:
    """Score exact truth points against distinct component/observation bboxes."""

    source = Path(manifest_path)
    manifest = load_probe_manifest(source)
    if coordinate_space not in {"proc", "full"}:
        raise CampaignError("coordinate_space must be proc or full")
    if eligibility not in {"all", "persistence"}:
        raise CampaignError("eligibility must be all or persistence")
    truth = load_truth_slots(manifest, source)
    eligibility_info = (
        persistence_eligibility(manifest_path=source)
        if eligibility == "persistence"
        else {
            "eligible": None,
            "structural_exclusions": [],
            "exclusions_by_reason": {},
            "definition": "all visible post-warm-up truth points",
        }
    )
    expected_sequences = list(range(WARMUP_FRAMES, manifest["total_frames"]))
    sequences = [row.get("frame_seq") for row in frame_rows]
    if sequences != expected_sequences:
        raise GuardrailViolation("recall input must cover every post-warm-up frame exactly once")
    per_mover = {
        mover_id: {"truth_points": 0, "matched": 0}
        for mover_id in range(manifest["movers"])
    }
    frame_matches: list[dict[str, int]] = []
    for row in frame_rows:
        frame_seq = int(row["frame_seq"])
        slot = frame_seq % manifest["ram_clip_frames"]
        visible = [dict(mover) for mover in truth[slot]["movers"] if mover["visible"]]
        if eligibility == "persistence":
            eligible_keys = eligibility_info["eligible"]
            visible = [
                mover
                for mover in visible
                if (frame_seq, int(mover["mover_id"])) in eligible_keys
            ]
        if coordinate_space == "full":
            for mover in visible:
                mover["u"] = mover["full_u"]
                mover["v"] = mover["full_v"]
        components_value = row.get("components")
        if not isinstance(components_value, list):
            raise CampaignError("recall rows require an explicit components list")
        width = PROC_WIDTH if coordinate_space == "proc" else benchmark.FULL_WIDTH
        height = PROC_HEIGHT if coordinate_space == "proc" else benchmark.FULL_HEIGHT
        components = [
            _validate_bbox(
                component,
                context=f"recall frame {frame_seq}",
                width=width,
                height=height,
            )
            for component in components_value
        ]
        matches = _maximum_bbox_matching(visible, components)
        for index, mover in enumerate(visible):
            record = per_mover[int(mover["mover_id"])]
            record["truth_points"] += 1
            record["matched"] += index in matches
        frame_matches.append(
            {
                "frame_seq": frame_seq,
                "truth_points": len(visible),
                "matched": len(matches),
            }
        )
    total_truth = sum(item["truth_points"] for item in per_mover.values())
    total_matched = sum(item["matched"] for item in per_mover.values())
    rendered_per_mover: dict[str, dict[str, int | float]] = {}
    for mover_id, values in per_mover.items():
        denominator = values["truth_points"]
        if denominator == 0:
            raise CampaignError(f"mover {mover_id} is never visible in the loop truth")
        rendered_per_mover[str(mover_id)] = {
            **values,
            "recall": values["matched"] / denominator,
        }
    return {
        "definition": (
            "maximum one-to-one matching: each visible truth point and each distinct "
            "component/observation bbox are used at most once; bbox containment is "
            f"half-open in {coordinate_space} coordinates and no radius is applied; "
            f"denominator={eligibility_info['definition']}"
        ),
        "eligibility": eligibility,
        "structural_exclusions": {
            "count": len(eligibility_info["structural_exclusions"]),
            "by_reason": eligibility_info["exclusions_by_reason"],
            "points": eligibility_info["structural_exclusions"],
        },
        "truth_points": total_truth,
        "matched": total_matched,
        "recall": total_matched / total_truth,
        "all_truth_recalled": total_matched == total_truth,
        "per_mover": rendered_per_mover,
        "frames": frame_matches,
    }


def load_observation_rows(
    packet_log_path: str | Path, *, manifest_path: str | Path
) -> list[dict[str, Any]]:
    """Decode packets, bind their envelopes, and expose absent frames as empties."""

    packet_source = Path(packet_log_path)
    _assert_no_symlinks(packet_source)
    if not packet_source.is_file():
        raise CampaignError(f"packet log does not exist: {packet_source}")
    manifest_source = Path(manifest_path)
    manifest = load_probe_manifest(manifest_source)
    clip_path = _resolve_manifest_member(manifest_source, manifest["clip_path"], "clip_path")
    with clip_path.open("rb") as handle:
        session = read_injection_session(handle)
        stored = list(iter_injection_frames(handle))
    by_frame: dict[int, dict[str, list[dict[str, object]]]] = {}
    for line_number, line in enumerate(packet_source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            datagram = bytes.fromhex(line.strip())
        except ValueError as exc:
            raise CampaignError(f"invalid packet hex at line {line_number}") from exc
        payload_type, body = unframe(datagram)
        if payload_type is not PayloadType.OBSERVATION:
            raise CampaignError(f"packet log line {line_number} is not an observation packet")
        envelope, observations = codec.decode_observation_packet(body)
        if not observations:
            raise GuardrailViolation(
                "packet log contains an empty observation event the daemon never emits"
            )
        frame_seq = envelope.frame_seq
        if not WARMUP_FRAMES <= frame_seq < manifest["total_frames"]:
            raise GuardrailViolation(
                f"packet frame_seq {frame_seq} lies outside the scored horizon"
            )
        if frame_seq in by_frame:
            raise CampaignError(f"more than one observation packet for frame {frame_seq}")
        if (
            envelope.proc_width,
            envelope.proc_height,
            envelope.full_width,
            envelope.full_height,
        ) != (PROC_WIDTH, PROC_HEIGHT, benchmark.FULL_WIDTH, benchmark.FULL_HEIGHT):
            raise GuardrailViolation("observation packet resolution disagrees with C-001")
        slot = frame_seq % int(manifest["ram_clip_frames"])
        loop_pass = frame_seq // int(manifest["ram_clip_frames"])
        expected_frame = stored[slot]
        expected_envelope = {
            "camera_id": session.camera_id,
            "session_uuid": session.session_uuid,
            "frame_seq": frame_seq,
            "capture_ts_ns": expected_frame.capture_ts_ns
            + loop_pass * int(manifest["ram_loop_pts_stride_ns"]),
            "clock_domain": session.clock_domain,
            "time_sync_error_ms": expected_frame.time_sync_error_ms,
            "exposure_us": session.exposure_us,
            "gain_db": session.gain_db,
            "full_width": session.full_width,
            "full_height": session.full_height,
            "proc_width": session.proc_width,
            "proc_height": session.proc_height,
            "calibration_rev": session.calibration_rev,
            "detector_rev": session.detector_rev,
            "line_readout_us": session.line_readout_us,
        }
        actual_envelope = {
            name: getattr(envelope, name) for name in expected_envelope
        }
        if actual_envelope != expected_envelope:
            raise GuardrailViolation(
                f"observation packet envelope differs from RAM-loop slot at frame {frame_seq}"
            )
        bboxes = [
            {
                "bbox_x": observation.bbox_x,
                "bbox_y": observation.bbox_y,
                "bbox_w": observation.bbox_w,
                "bbox_h": observation.bbox_h,
                "area_px": observation.area_px,
                "persistence_count": observation.persistence_count,
            }
            for observation in observations
        ]
        wire = [
            {
                "obs_id": observation.obs_id,
                "u": observation.u,
                "v": observation.v,
                "cov_uu": observation.cov_uu,
                "cov_uv": observation.cov_uv,
                "cov_vv": observation.cov_vv,
                **bbox,
                "confidence": observation.confidence,
                "local_blob_id": observation.local_blob_id,
                "evidence_ref": observation.evidence_ref,
            }
            for observation, bbox in zip(observations, bboxes, strict=True)
        ]
        by_frame[frame_seq] = {"components": bboxes, "wire_observations": wire}
    return [
        {
            "frame_seq": frame_seq,
            "components": by_frame.get(frame_seq, {}).get("components", []),
            "wire_observations": by_frame.get(frame_seq, {}).get(
                "wire_observations", []
            ),
        }
        for frame_seq in range(WARMUP_FRAMES, manifest["total_frames"])
    ]


def validate_observation_lineage(
    ccl_rows: Sequence[Mapping[str, object]],
    observation_rows: Sequence[Mapping[str, object]],
) -> None:
    """Require every wire bbox to be a distinct scaled same-frame CCL bbox."""

    if [row.get("frame_seq") for row in ccl_rows] != [
        row.get("frame_seq") for row in observation_rows
    ]:
        raise GuardrailViolation("CCL and packet lineage rows cover different frames")
    scale_x = benchmark.FULL_WIDTH / PROC_WIDTH
    scale_y = benchmark.FULL_HEIGHT / PROC_HEIGHT
    persistence = PersistenceFilter(detector_config_for({}))
    for ccl_row, observation_row in zip(ccl_rows, observation_rows, strict=True):
        raw = ccl_row.get("components")
        emitted = observation_row.get("components")
        wire_observations = observation_row.get("wire_observations")
        if not isinstance(raw, list) or not isinstance(emitted, list):
            raise CampaignError("CCL/packet lineage requires explicit component lists")
        if len(emitted) > COMPONENT_CAP:
            raise GuardrailViolation("packet frame exceeds the frozen seven-observation cap")
        raw_components = [
            MaskComponent(
                centroid_u=float(component["centroid_u"]),
                centroid_v=float(component["centroid_v"]),
                area_px=int(component["area_px"]),
                bbox_x=int(component["bbox_x"]),
                bbox_y=int(component["bbox_y"]),
                bbox_w=int(component["bbox_w"]),
                bbox_h=int(component["bbox_h"]),
            )
            for component in raw
        ]
        expected_items = apply_component_cap(
            persistence.update(raw_components), COMPONENT_CAP
        ).kept
        expected = [
            {
                "bbox_x": int(math.floor(component.bbox_x * scale_x)),
                "bbox_y": int(math.floor(component.bbox_y * scale_y)),
                "bbox_w": max(1, int(math.ceil(component.bbox_w * scale_x))),
                "bbox_h": max(1, int(math.ceil(component.bbox_h * scale_y))),
                "area_px": component.area_px,
                "persistence_count": persistence_count,
            }
            for component, persistence_count, _blob_id in expected_items
        ]
        if emitted != expected:
            raise GuardrailViolation(
                "emitted observations do not match frozen nearest-pair persistence/cap replay "
                f"at frame {ccl_row.get('frame_seq')}"
            )
        if wire_observations is not None:
            if not isinstance(wire_observations, list):
                raise CampaignError("packet lineage wire observations must be a list")
            expected_wire = [
                {
                    "obs_id": obs_id,
                    "u": proc_to_full(component.centroid_u, scale_x),
                    "v": proc_to_full(component.centroid_v, scale_y),
                    "cov_uu": persistence.config.centroid_cov_floor_px2,
                    "cov_uv": 0.0,
                    "cov_vv": persistence.config.centroid_cov_floor_px2,
                    **bbox,
                    "confidence": float(
                        np.float32(component_confidence(component, persistence_count))
                    ),
                    "local_blob_id": blob_id,
                    "evidence_ref": None,
                }
                for obs_id, ((component, persistence_count, blob_id), bbox) in enumerate(
                    zip(expected_items, expected, strict=True)
                )
            ]
            if wire_observations != expected_wire:
                raise GuardrailViolation(
                    "wire observation fields do not match frozen component projection "
                    f"at frame {ccl_row.get('frame_seq')}"
                )


def score_board_run(
    stats_path: str | Path,
    ccl_log_path: str | Path,
    packet_log_path: str | Path,
    manifest_path: str | Path,
    run_binding_path: str | Path,
    *,
    board: str,
    knobs: Mapping[str, object],
    wall_s: float | None = None,
    output_path: str | Path | None = None,
    board_fg_masks_path: str | Path | None = None,
    provision_path: str | Path | None = None,
    exit_status_path: str | Path | None = None,
    run_log_path: str | Path | None = None,
) -> dict[str, Any] | WrittenArtifact:
    """Build a scored board artifact from retained explicit counters and bboxes."""

    if not isinstance(board, str) or not board.strip():
        raise CampaignError("board identity label must be non-empty")
    if wall_s is not None:
        if isinstance(wall_s, bool) or not isinstance(wall_s, (int, float)):
            raise CampaignError("wall_s must be a finite number")
        wall_s = float(wall_s)
        if not math.isfinite(wall_s) or wall_s < 0 or wall_s > MAX_EXPERIMENT_MINUTES * 60:
            raise GuardrailViolation("experiment wall time must be within 0..20 minutes")
    manifest_source = Path(manifest_path)
    manifest = load_probe_manifest(manifest_source)
    stats_source = Path(stats_path)
    stats = _read_json_object(stats_source)
    validate_board_run_binding(
        stats,
        manifest={**manifest, "manifest_sha256": sha256_file(manifest_source)},
        knobs=knobs,
    )
    run_binding = validate_external_run_binding(
        run_binding_path,
        board=board.strip(),
        manifest_path=manifest_source,
        manifest=manifest,
        stats_path=stats_source,
        ccl_log_path=ccl_log_path,
        packet_log_path=packet_log_path,
    )
    if provision_path is not None and (exit_status_path is None or run_log_path is None):
        raise GuardrailViolation("provisioned score requires retained exit.status and run.log")
    provision = (
        validate_provision_artifact(
            provision_path,
            board=board.strip(),
            stats=stats,
            manifest=manifest,
            stats_path=stats_source,
            ccl_log_path=ccl_log_path,
            packet_log_path=packet_log_path,
            board_fg_masks_path=board_fg_masks_path,
            exit_status_path=exit_status_path,
            run_log_path=run_log_path,
        )
        if provision_path is not None
        else None
    )
    if provision is not None:
        if (
            run_binding["run_id"] != provision["run_id"]
            or run_binding["remote_run_dir"] != provision["remote_run_dir"]
        ):
            raise GuardrailViolation("run binding freshness fields differ from provision")
        preflight_subset = {
            key: provision["identity_preflight"][key]
            for key in ("board", "mac", "image_marker")
        }
        if {
            **preflight_subset,
            "mac": str(preflight_subset["mac"]).lower(),
        } != {
            **run_binding["identity"],
            "mac": str(run_binding["identity"]["mac"]).lower(),
        }:
            raise GuardrailViolation("run binding identity differs from transport preflight")
        if provision["probe_manifest_sha256"] != sha256_file(manifest_source):
            raise GuardrailViolation(
                "provision attempt reservation differs from the selected probe manifest"
            )
        measured_wall_s = float(provision["wall_s"])
        if wall_s is not None and not math.isclose(wall_s, measured_wall_s, abs_tol=1e-9):
            raise GuardrailViolation("caller wall_s differs from provision's measured wall_s")
        wall_s = measured_wall_s
    elif wall_s is None:
        raise GuardrailViolation("an unprovisioned diagnostic score needs explicit wall_s")
    rows = load_ccl_log(ccl_log_path, manifest_path=manifest_source)
    derived = aggregate_ccl_rows(rows)
    objective = compute_objective(stats, derived_from_rows=derived)
    component_recall = score_mover_recall(rows, manifest_path=manifest_source)
    observation_rows = load_observation_rows(
        packet_log_path, manifest_path=manifest_source
    )
    validate_observation_lineage(rows, observation_rows)
    packets_with_observations = sum(bool(row["components"]) for row in observation_rows)
    observations_in_packets = sum(len(row["components"]) for row in observation_rows)
    if stats.get("capture_events") != packets_with_observations:
        raise GuardrailViolation("packet log count disagrees with stats.capture_events")
    if stats.get("observations_sent") != observations_in_packets:
        raise GuardrailViolation("packet observations disagree with stats.observations_sent")
    unfiltered_observation_recall = score_mover_recall(
        observation_rows,
        manifest_path=manifest_source,
        coordinate_space="full",
    )
    eligible_observation_recall = score_mover_recall(
        observation_rows,
        manifest_path=manifest_source,
        coordinate_space="full",
        eligibility="persistence",
    )
    normalized = normalize_knobs(knobs)
    probe_semantics = {
        key: manifest[key]
        for key in (
            "probe_kind",
            "generator",
            "source_mode",
            "movers",
            "proc_width",
            "proc_height",
            "warmup_frames",
            "total_frames",
            "postwarm_frames",
            "noise_dn",
            "cap",
            "persistence_frames",
            "persistence_gate_px",
            "ram_clip_frames",
            "ram_loop_total_frames",
            "ram_loop_pts_stride_ns",
            "ram_budget_mb",
            "truth_schema",
            "truth_coordinate_space",
            "truth_loop_rule",
        )
    }
    payload = {
        "schema": SCORE_SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "board": board.strip(),
        "seed": manifest["seed"],
        "knobs": normalized,
        "probe_semantics": probe_semantics,
        "effective_board_knobs": {
            "min_area_px": int(stats["min_area_px"]),
            "open_radius_px": int(stats["morph_open"]),
            "ive_approx.match_sigmas": float(stats["gmm2_match_sigmas"]),
            "ive_approx.var_min": float(stats["gmm2_var_min"]),
        },
        "wall_s": wall_s,
        "manifest": {
            "path": str(manifest_source.resolve(strict=True)),
            "sha256": sha256_file(manifest_source),
        },
        "run_binding": {
            "path": str(Path(run_binding_path).resolve(strict=True)),
            "sha256": sha256_file(run_binding_path),
            "identity": run_binding["identity"],
            "run_id": run_binding["run_id"],
            "remote_run_dir": run_binding["remote_run_dir"],
        },
        "inputs": {
            "stats_path": str(stats_source.resolve(strict=True)),
            "stats_sha256": sha256_file(stats_source),
            "ccl_log_path": str(Path(ccl_log_path).resolve(strict=True)),
            "ccl_log_sha256": sha256_file(ccl_log_path),
            "packet_log_path": str(Path(packet_log_path).resolve(strict=True)),
            "packet_log_sha256": sha256_file(packet_log_path),
            "clip_path": str(
                _resolve_manifest_member(manifest_source, manifest["clip_path"], "clip_path")
            ),
            "clip_sha256": manifest["clip_sha256"],
            "truth_path": str(
                _resolve_manifest_member(manifest_source, manifest["truth_path"], "truth_path")
            ),
            "truth_sha256": manifest["truth_sha256"],
        },
        "explicit_ccl_counters": {name: int(stats[name]) for name in _CCL_COUNTERS},
        "result": {
            **objective,
            "raw_component_mover_recall": component_recall,
            "unfiltered_emitted_mover_recall": unfiltered_observation_recall,
            "mover_recall": eligible_observation_recall,
        },
    }
    if provision_path is not None:
        recovery_rows = provision["_c001_recovery_rows"]
        attempt_rows = provision["_c001_attempt_rows"]
        run_mac = str(provision["identity_preflight"]["mac"]).lower()
        payload["provision"] = {
            "path": str(Path(provision_path).resolve(strict=True)),
            "sha256": sha256_file(provision_path),
            "binary_sha256": provision["remote_sha256"],
            "node": provision["node"],
            "run_id": provision["run_id"],
            "remote_run_dir": provision["remote_run_dir"],
            "power_cycles": provision["power_cycles"],
            "identity_preflight": provision["identity_preflight"],
            "runtime_ive_library": provision["runtime_ive_library"],
            "recovery_ledger_tip_sha256": provision["recovery_ledger"]["tip_sha256"],
            "recovery_ledger_row_sha256s": [
                row["row_sha256"] for row in recovery_rows
            ],
            "recovery_shift_cycles": len(recovery_rows),
            "recovery_board_cycles": sum(
                str(row["mac"]).lower() == run_mac for row in recovery_rows
            ),
            "attempt_n": provision["attempt_reservation"]["attempt_n"],
            "attempt_reservation_sha256": provision["attempt_reservation"][
                "reservation_sha256"
            ],
            "attempt_ledger_tip_sha256": provision["attempt_ledger"]["tip_sha256"],
            "attempt_ledger_row_sha256s": [row["row_sha256"] for row in attempt_rows],
            "attempts_reserved": sum(
                row["event"] == "attempt_reserved" for row in attempt_rows
            ),
        }
    if board_fg_masks_path is not None:
        # Parse before retention so a malformed optional artifact cannot hide
        # behind a valid digest.
        parsed_masks = load_failed_fg_masks(board_fg_masks_path)
        expected_masks = min(10, int(stats["ccl_label_failures"]))
        if len(parsed_masks) != expected_masks:
            raise GuardrailViolation(
                "failed-mask artifact must contain first min(10, label_failures) records"
            )
        masks_written = _require_int(
            "fg_masks_written", stats.get("fg_masks_written"), 0, MAX_FAILED_MASKS
        )
        mask_write_failures = _require_int(
            "fg_mask_write_failures", stats.get("fg_mask_write_failures"), 0
        )
        if masks_written != len(parsed_masks):
            raise GuardrailViolation(
                "stats.fg_masks_written disagrees with parsed SWFM record count"
            )
        if mask_write_failures != 0:
            raise GuardrailViolation("any failed SWFM write invalidates mask-diff evidence")
        if int(stats["fg_mask_limit"]) < len(parsed_masks):
            raise GuardrailViolation("fg_mask_limit is below the retained SWFM count")
        payload["inputs"]["board_fg_masks_path"] = str(
            Path(board_fg_masks_path).resolve(strict=True)
        )
        payload["inputs"]["board_fg_masks_sha256"] = sha256_file(board_fg_masks_path)
    if exit_status_path is not None and run_log_path is not None:
        payload["inputs"].update(
            {
                "exit_status_path": str(Path(exit_status_path).resolve(strict=True)),
                "exit_status_sha256": sha256_file(exit_status_path),
                "run_log_path": str(Path(run_log_path).resolve(strict=True)),
                "run_log_sha256": sha256_file(run_log_path),
            }
        )
    if output_path is None:
        return payload
    destination = Path(output_path)
    if destination.exists() or destination.is_symlink():
        raise CampaignError(f"refusing to replace retained artifact {destination}")
    retained_dir = destination.parent / f"{destination.name}.inputs"
    if retained_dir.exists() or retained_dir.is_symlink():
        raise CampaignError(f"refusing to replace retained input bundle {retained_dir}")
    retained_dir.mkdir(parents=True)
    _assert_no_symlinks(retained_dir)
    sources: dict[str, Path] = {
        "manifest": manifest_source,
        "stats": stats_source,
        "ccl_log": Path(ccl_log_path),
        "packet_log": Path(packet_log_path),
        "run_binding": Path(run_binding_path),
        "clip": _resolve_manifest_member(manifest_source, manifest["clip_path"], "clip_path"),
        "truth": _resolve_manifest_member(
            manifest_source, manifest["truth_path"], "truth_path"
        ),
    }
    if board_fg_masks_path is not None:
        sources["board_fg_masks"] = Path(board_fg_masks_path)
    if provision_path is not None:
        sources["provision"] = Path(provision_path)
        provision_recovery = provision.get("recovery_ledger") if provision is not None else None
        if isinstance(provision_recovery, Mapping):
            sources["recovery_ledger"] = Path(provision_path).parent / str(
                provision_recovery["path"]
            )
        provision_attempts = provision.get("attempt_ledger") if provision is not None else None
        if isinstance(provision_attempts, Mapping):
            sources["attempt_ledger"] = Path(provision_path).parent / str(
                provision_attempts["path"]
            )
    if exit_status_path is not None and run_log_path is not None:
        sources["exit_status"] = Path(exit_status_path)
        sources["run_log"] = Path(run_log_path)
    retained_names = {
        "manifest": "probe_manifest.json",
        "stats": "stats.json",
        "ccl_log": "ccl.jsonl",
        "packet_log": "packets.hex",
        "run_binding": "run_binding.json",
        "clip": "probe.swij",
        "truth": "truth_slots.jsonl",
        "board_fg_masks": "failed_masks.swfm",
        "provision": "provision.json",
        "exit_status": "exit.status",
        "run_log": "run.log",
        "recovery_ledger": "recovery-ledger-snapshot.jsonl",
        "attempt_ledger": "attempt-ledger-snapshot.jsonl",
    }
    retained: dict[str, Path] = {}
    for name, source_path in sources.items():
        target = retained_dir / retained_names[name]
        _write_new_bytes(target, source_path.read_bytes())
        retained[name] = target

    def relative(path: Path) -> str:
        return path.relative_to(destination.parent).as_posix()

    payload["manifest"]["path"] = relative(retained["manifest"])
    payload["run_binding"]["path"] = relative(retained["run_binding"])
    if "provision" in retained:
        payload["provision"]["path"] = relative(retained["provision"])
    payload["inputs"].update(
        {
            "stats_path": relative(retained["stats"]),
            "ccl_log_path": relative(retained["ccl_log"]),
            "packet_log_path": relative(retained["packet_log"]),
            "clip_path": relative(retained["clip"]),
            "truth_path": relative(retained["truth"]),
        }
    )
    if "board_fg_masks" in retained:
        payload["inputs"]["board_fg_masks_path"] = relative(
            retained["board_fg_masks"]
        )
    if "exit_status" in retained:
        payload["inputs"]["exit_status_path"] = relative(retained["exit_status"])
        payload["inputs"]["run_log_path"] = relative(retained["run_log"])
    _write_new_bytes(destination, _canonical_json(payload))
    return WrittenArtifact(destination, sha256_file(destination), payload)


def result_is_win(result: Mapping[str, object]) -> bool:
    """The declared scalar threshold plus exact one-to-one mover coverage."""

    rate = result.get("detector_fail_rate")
    recall = result.get("mover_recall")
    raw_recall = result.get("raw_component_mover_recall")
    return (
        isinstance(rate, (int, float))
        and not isinstance(rate, bool)
        and math.isfinite(float(rate))
        and float(rate) <= WIN_FAIL_RATE
        and result.get("contract_change_required") is not True
        and isinstance(recall, Mapping)
        and recall.get("all_truth_recalled") is True
        and isinstance(raw_recall, Mapping)
        and raw_recall.get("all_truth_recalled") is True
    )


def _validate_retained_members(
    artifact_path: str | Path,
    pairs: Sequence[tuple[Mapping[str, object], str, str]],
) -> None:
    """Verify relative, non-symlinked, digest-bound members beside an artifact."""

    artifact = Path(artifact_path)
    root = artifact.parent.resolve(strict=True)
    for block, path_key, digest_key in pairs:
        raw_path = block.get(path_key)
        expected_digest = _validate_digest(digest_key, block.get(digest_key))
        if not isinstance(raw_path, str):
            raise LedgerIntegrityError(f"retained {path_key} must be a relative path")
        relative = Path(raw_path)
        if relative.is_absolute() or raw_path != relative.as_posix() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise LedgerIntegrityError(f"retained {path_key} uses a path alias")
        retained = artifact.parent / relative
        _assert_no_symlinks(retained)
        try:
            retained.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise LedgerIntegrityError(
                f"retained {path_key} escapes its artifact directory"
            ) from exc
        if sha256_file(retained) != expected_digest:
            raise LedgerIntegrityError(f"retained input digest fails for {path_key}")


def validate_retained_host_bundle(
    host_path: str | Path, payload: Mapping[str, object]
) -> None:
    """Verify host Phase-1 evidence retains every raw dependency durably."""

    if payload.get("schema") != HOST_SCHEMA:
        raise LedgerIntegrityError("retained host artifact has the wrong C-001 schema")
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        raise LedgerIntegrityError("host discriminator lacks a retained input bundle")
    required = {
        "manifest_path",
        "manifest_sha256",
        "clip_path",
        "clip_sha256",
        "truth_path",
        "truth_sha256",
    }
    if "board_ccl_log" in payload:
        required |= {"board_ccl_log_path", "board_ccl_log_sha256"}
    if "board_fg_masks" in payload:
        required |= {"board_fg_masks_path", "board_fg_masks_sha256"}
    if set(inputs) != required:
        raise LedgerIntegrityError("host retained-input fields are incomplete or unknown")
    pairs = [
        (inputs, "manifest_path", "manifest_sha256"),
        (inputs, "clip_path", "clip_sha256"),
        (inputs, "truth_path", "truth_sha256"),
    ]
    if "board_ccl_log_path" in inputs:
        pairs.append((inputs, "board_ccl_log_path", "board_ccl_log_sha256"))
    if "board_fg_masks_path" in inputs:
        pairs.append((inputs, "board_fg_masks_path", "board_fg_masks_sha256"))
    _validate_retained_members(host_path, pairs)
    for name in ("manifest", "clip", "truth"):
        if payload.get(f"{name}_sha256") != inputs[f"{name}_sha256"]:
            raise LedgerIntegrityError(f"host {name} digest disagrees with retained input")
    for name in ("board_ccl_log", "board_fg_masks"):
        block = payload.get(name)
        if isinstance(block, Mapping) and block.get("sha256") != inputs.get(
            f"{name}_sha256"
        ):
            raise LedgerIntegrityError(f"host {name} digest disagrees with retained input")

    # Hashes alone do not make a hand-authored summary evidence.  Replay the
    # exact retained RAM loop through the same host backend and recompute every
    # claimed row, paired discriminator decision, and optional mask diff.
    artifact = Path(host_path)
    manifest_path = artifact.parent / str(inputs["manifest_path"])
    manifest = load_probe_manifest(manifest_path)
    if (
        payload.get("manifest_sha256") != sha256_file(manifest_path)
        or payload.get("seed") != manifest["seed"]
        or payload.get("clip_sha256") != manifest["clip_sha256"]
        or payload.get("truth_sha256") != manifest["truth_sha256"]
    ):
        raise LedgerIntegrityError("host metadata disagrees with retained probe manifest")
    frozen = payload.get("frozen")
    if not isinstance(frozen, Mapping):
        raise LedgerIntegrityError("host artifact lacks frozen settings")
    validate_frozen_settings({**frozen, "seed": payload.get("seed")}, seed=manifest["seed"])
    knobs = payload.get("knobs")
    if not isinstance(knobs, Mapping):
        raise LedgerIntegrityError("host artifact lacks its knob declaration")
    config = detector_config_for(knobs)
    backend = make_backend(config)
    board_rows: list[dict[str, Any]] = []
    if "board_ccl_log_path" in inputs:
        board_rows = load_ccl_log(
            artifact.parent / str(inputs["board_ccl_log_path"]),
            manifest_path=manifest_path,
        )
    failures = [
        int(row["frame_seq"])
        for row in board_rows
        if row["api_failure"] is False and row["s8_label_status"] != 0
    ]
    board_masks = (
        load_failed_fg_masks(artifact.parent / str(inputs["board_fg_masks_path"]))
        if "board_fg_masks_path" in inputs
        else {}
    )
    expected_masks = failures[: min(10, len(failures))]
    if list(board_masks) != expected_masks:
        raise LedgerIntegrityError("host retained masks are not the first declared failures")
    replay_rows: list[dict[str, Any]] = []
    host_8_components: dict[int, list[dict[str, int | float]]] = {}
    host_masks: dict[int, np.ndarray] = {}
    for frame_seq, clip_slot, luma in iter_looped_probe_frames(manifest_path):
        warming_up = frame_seq < WARMUP_FRAMES
        mask = backend.apply(luma, warming_up=warming_up)
        if warming_up:
            continue
        mask = open_mask(mask, config.open_radius_px)
        if frame_seq in failures:
            host_8_components[frame_seq] = components_with_connectivity(
                mask,
                connectivity=8,
                min_area_px=config.min_area_px,
                max_area_px=config.max_area_px,
            )
        if frame_seq in board_masks:
            host_masks[frame_seq] = mask.copy()
        serialized = [
            _component_dict(component)
            for component in find_components(mask, config.min_area_px, config.max_area_px)
        ]
        replay_rows.append(
            {
                "frame_seq": frame_seq,
                "clip_slot": clip_slot,
                "raw_components": len(serialized),
                "overlap_pairs": overlapping_bbox_pairs(serialized),
                "components": serialized,
            }
        )
    if payload.get("frames") != replay_rows:
        raise LedgerIntegrityError("host frame rows do not replay from retained probe bytes")
    replay_mask_diff = (
        compare_fg_masks(board_masks, host_masks)
        if "board_fg_masks" in payload
        else None
    )
    replay_summary = summarize_host_rows(replay_rows, total_frames=manifest["total_frames"])
    if board_rows:
        replay_summary["paired_discriminator"] = evaluate_host_discriminator(
            replay_rows,
            board_rows,
            manifest_path=manifest_path,
            host_8_components=host_8_components,
            mask_diff_within_tolerance=(
                isinstance(replay_mask_diff, Mapping)
                and int(replay_mask_diff["frames_compared"]) > 0
                and int(replay_mask_diff["differing_pixels"]) == 0
            ),
        )
        replay_summary["clean_host"] = replay_summary["paired_discriminator"]["clean_host"]
    if payload.get("summary") != replay_summary or payload.get("mask_diff") != replay_mask_diff:
        raise LedgerIntegrityError("host summary/mask decision does not match semantic replay")


def validate_bug_verification_bundle(
    artifact_path: str | Path, payload: Mapping[str, object]
) -> None:
    """Require durable transcripts and provenance for Phase 1.1 claims."""

    schema = payload.get("schema")
    legacy = schema == LEGACY_BUG_VERIFICATION_SCHEMA
    if schema not in {LEGACY_BUG_VERIFICATION_SCHEMA, BUG_VERIFICATION_SCHEMA}:
        raise LedgerIntegrityError("BUG verification artifact has the wrong schema")
    if legacy and SHIFT_HISTORY_DIRECTORY not in Path(artifact_path).parts:
        raise LedgerIntegrityError(
            "legacy BUG verification evidence is accepted only from immutable shift history"
        )
    if payload.get("campaign_id") != CAMPAIGN_ID:
        raise LedgerIntegrityError("BUG verification artifact has the wrong campaign id")
    provenance = payload.get("provenance")
    evidence = payload.get("evidence")
    binding = payload.get("binding")
    keys = {"bug_a_board", "bug_b_board", "e2", "e5"}
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "git_sha",
        "source_tree_sha256",
        "toolchain",
        "commands",
        "build",
    }:
        raise LedgerIntegrityError("BUG verification requires exact provenance fields")
    git_sha = provenance["git_sha"]
    if not isinstance(git_sha, str) or re.fullmatch(r"[0-9a-f]{40}", git_sha) is None:
        raise LedgerIntegrityError("BUG verification git_sha must be 40 lowercase hex")
    if not isinstance(provenance["toolchain"], str) or not provenance["toolchain"].strip():
        raise LedgerIntegrityError("BUG verification toolchain must be non-empty")
    source_tree_sha256 = _validate_digest(
        "BUG verification source tree", provenance["source_tree_sha256"]
    )
    commands = provenance["commands"]
    if not isinstance(commands, Mapping) or set(commands) != keys or any(
        not isinstance(commands[key], str) or not commands[key].strip() for key in keys
    ):
        raise LedgerIntegrityError("BUG verification requires one exact command per check")
    build = provenance["build"]
    if not isinstance(build, Mapping) or set(build) != {
        "path",
        "sha256",
        "image_digest",
        "command",
        "binary_sha256",
    }:
        raise LedgerIntegrityError("BUG verification requires exact build provenance")
    if (
        not isinstance(build["image_digest"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", build["image_digest"]) is None
    ):
        raise LedgerIntegrityError("BUG verification Docker image digest is not pinned")
    if not isinstance(build["command"], str) or not build["command"].strip():
        raise LedgerIntegrityError("BUG verification build command must be non-empty")
    try:
        if not shlex.split(build["command"]):
            raise LedgerIntegrityError("BUG verification build command must be non-empty")
    except ValueError as exc:
        raise LedgerIntegrityError("BUG verification build command is malformed") from exc
    if not isinstance(evidence, Mapping) or set(evidence) != keys:
        raise LedgerIntegrityError("BUG verification requires four retained transcripts")
    required_binding = {
        "identity",
        "binary_sha256",
        "git_sha",
        "source_tree_sha256",
    }
    if not legacy:
        required_binding.add("runtime_ive_library")
    if not isinstance(binding, Mapping) or set(binding) != required_binding:
        raise LedgerIntegrityError("BUG verification requires board/binary/git binding")
    bound_identity = binding["identity"]
    if not isinstance(bound_identity, Mapping) or set(bound_identity) != {
        "board",
        "mac",
        "image_marker",
    }:
        raise LedgerIntegrityError("BUG verification binding lacks board identity")
    checked_bound_identity = BoardIdentity(
        str(bound_identity["board"]),
        str(bound_identity["mac"]),
        str(bound_identity["image_marker"]),
    )
    bound_binary = _validate_digest(
        "BUG verification binding binary", binding["binary_sha256"]
    )
    runtime_before: str | None = None
    if not legacy:
        bound_runtime = binding["runtime_ive_library"]
        if not isinstance(bound_runtime, Mapping) or set(bound_runtime) != {
            "path",
            "sha256_before",
            "sha256_after",
            "stable",
        }:
            raise LedgerIntegrityError("BUG verification lacks exact IVE runtime binding")
        if bound_runtime["path"] != "/oem/usr/lib/librve.so":
            raise LedgerIntegrityError("BUG verification IVE runtime path is not frozen")
        runtime_before = _validate_digest(
            "BUG verification IVE runtime before", bound_runtime["sha256_before"]
        )
        runtime_after = _validate_digest(
            "BUG verification IVE runtime after", bound_runtime["sha256_after"]
        )
        if bound_runtime["stable"] is not True or runtime_before != runtime_after:
            raise LedgerIntegrityError("BUG verification IVE runtime was not stable")
    if _validate_digest("BUG verification build binary", build["binary_sha256"]) != bound_binary:
        raise LedgerIntegrityError("BUG verification build output differs from bound binary")
    if binding["git_sha"] != provenance["git_sha"]:
        raise LedgerIntegrityError("BUG verification binding git SHA differs from provenance")
    if binding["source_tree_sha256"] != source_tree_sha256:
        raise LedgerIntegrityError("BUG verification binding source tree differs from provenance")
    pairs: list[tuple[Mapping[str, object], str, str]] = [
        (build, "path", "sha256")
    ]
    for key in sorted(keys):
        block = evidence[key]
        if not isinstance(block, Mapping) or set(block) != {"path", "sha256"}:
            raise LedgerIntegrityError(f"BUG verification {key} attachment is malformed")
        pairs.append((block, "path", "sha256"))
    _validate_retained_members(artifact_path, pairs)
    artifact = Path(artifact_path)
    build_log = artifact.parent / str(build["path"])
    if not build_log.read_bytes():
        raise LedgerIntegrityError("BUG verification retained build log is empty")
    expected_checks = {
        "bug_a_board": "full_254_slot_region_scan",
        "bug_b_board": "mask_moment_centroid_and_overlap_counter",
        "e2": "nanopb_byte_identity",
        "e5": "host_fixture_replay",
    }
    identities: set[tuple[str, str, str]] = set()
    binaries: set[str] = set()
    for key in keys:
        attachment = artifact.parent / str(evidence[key]["path"])
        transcript = _read_json_object(attachment)
        required = {
            "schema",
            "check",
            "exit_code",
            "asserted_outcome",
            "git_sha",
            "toolchain",
            "command",
            "board_identity",
            "binary_sha256",
            "check_binary_sha256",
            "check_binary_remote_sha256",
            "source_tree_sha256",
            "stdout_path",
            "stdout_sha256",
            "stderr_path",
            "stderr_sha256",
        }
        if not legacy:
            required.add("runtime_ive_library_sha256")
        if set(transcript) != required or transcript.get("schema") != (
            "skyweave-c001-check-transcript/1"
        ):
            raise LedgerIntegrityError(f"BUG verification {key} transcript schema is invalid")
        if (
            transcript.get("check") != expected_checks[key]
            or transcript.get("exit_code") != 0
            or transcript.get("asserted_outcome") is not True
            or transcript.get("git_sha") != provenance["git_sha"]
            or transcript.get("source_tree_sha256") != source_tree_sha256
            or transcript.get("toolchain") != provenance["toolchain"]
            or transcript.get("command") != commands[key]
        ):
            raise LedgerIntegrityError(f"BUG verification {key} did not prove PASS")
        check_binary = transcript["check_binary_sha256"]
        check_binary_remote = transcript["check_binary_remote_sha256"]
        _validate_retained_members(
            artifact_path,
            [
                (transcript, "stdout_path", "stdout_sha256"),
                (transcript, "stderr_path", "stderr_sha256"),
            ],
        )
        stdout_path = artifact.parent / str(transcript["stdout_path"])
        stderr_path = artifact.parent / str(transcript["stderr_path"])
        stdout = stdout_path.read_text(encoding="utf-8")
        stderr = stderr_path.read_text(encoding="utf-8")
        if key in {"e2", "e5"} and stderr.strip():
            raise LedgerIntegrityError(
                f"BUG verification {key} pytest emitted stderr"
            )
        try:
            command_argv = shlex.split(str(transcript["command"]))
        except ValueError as exc:
            raise LedgerIntegrityError(f"BUG verification {key} command is malformed") from exc
        if key in {"bug_a_board", "bug_b_board"}:
            local_check = _validate_digest(
                f"BUG verification {key} check binary", check_binary
            )
            remote_check = _validate_digest(
                f"BUG verification {key} remote check binary", check_binary_remote
            )
            if local_check != remote_check or local_check != bound_binary:
                raise LedgerIntegrityError(f"BUG verification {key} check binary was not verified")
            if not legacy and _validate_digest(
                f"BUG verification {key} IVE runtime",
                transcript["runtime_ive_library_sha256"],
            ) != runtime_before:
                raise LedgerIntegrityError(
                    f"BUG verification {key} IVE runtime differs from binding"
                )
            if (
                len(command_argv) != 3
                or command_argv[0] != "LD_LIBRARY_PATH=/oem/usr/lib"
                or Path(command_argv[1]).name != "skyweave-edge"
                or command_argv[2] != "--self-test-ccl-measure"
            ):
                raise LedgerIntegrityError(
                    f"BUG verification {key} did not execute the production daemon self-test"
                )
            try:
                selftest = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise LedgerIntegrityError(
                    f"BUG verification {key} self-test stdout is not JSON"
                ) from exc
            expected_selftest = {
                "schema": "skyweave-ccl-selftest/1",
                "full_254_slot_region_scan": True,
                "mask_moment_centroid": True,
                "overlap_counter": True,
            }
            if selftest != expected_selftest:
                raise LedgerIntegrityError(f"BUG verification {key} self-test claims mismatch")
        elif (
            check_binary is not None
            or check_binary_remote is not None
            or (not legacy and transcript["runtime_ive_library_sha256"] is not None)
        ):
            raise LedgerIntegrityError(f"BUG verification {key} check binary must be null")
        else:
            expected_test = {
                "e2": "tests/edge/test_e2_nanopb_parity.py",
                "e5": "tests/edge/test_e5_fixture_replay.py",
            }[key]
            if (
                len(command_argv) != 5
                or Path(command_argv[0]).name not in {"python", "python3"}
                or command_argv[1:4] != ["-m", "pytest", "-q"]
                or command_argv[4] != expected_test
            ):
                raise LedgerIntegrityError(f"BUG verification {key} command target is wrong")
            lowered = stdout.lower()
            if (
                re.search(r"\b[1-9][0-9]* passed\b", lowered) is None
                or any(
                    token in lowered for token in (" failed", " error", " warning")
                )
                or re.search(
                    r"\b(?:skipped|xfailed|xpassed|deselected)\b", lowered
                )
                is not None
            ):
                raise LedgerIntegrityError(f"BUG verification {key} pytest transcript did not pass")
        identity = transcript.get("board_identity")
        if not isinstance(identity, Mapping) or set(identity) != {
            "board",
            "mac",
            "image_marker",
        }:
            raise LedgerIntegrityError(f"BUG verification {key} lacks board identity")
        checked_identity = BoardIdentity(
            str(identity["board"]), str(identity["mac"]), str(identity["image_marker"])
        )
        identities.add(checked_identity.normalized())
        binaries.add(
            _validate_digest(f"BUG verification {key} binary", transcript["binary_sha256"])
        )
    if len(identities) != 1 or len(binaries) != 1:
        raise LedgerIntegrityError(
            "BUG A/B/E2/E5 transcripts must bind one board identity and deployed binary"
        )
    if identities != {checked_bound_identity.normalized()} or binaries != {bound_binary}:
        raise LedgerIntegrityError("BUG transcript identity/binary differs from artifact binding")


def validate_retained_score_bundle(score_path: str | Path, payload: Mapping[str, object]) -> None:
    """Verify every raw input named by a retained score is local and immutable."""

    if payload.get("schema") != SCORE_SCHEMA:
        raise LedgerIntegrityError("retained board score has the wrong C-001 schema")
    manifest_block = payload.get("manifest")
    binding_block = payload.get("run_binding")
    provision_block = payload.get("provision")
    inputs = payload.get("inputs")
    if not all(
        isinstance(block, Mapping)
        for block in (manifest_block, binding_block, provision_block, inputs)
    ):
        raise LedgerIntegrityError(
            "ledgerable score lacks manifest/run-binding/provision/input blocks"
        )
    pairs = [
        (manifest_block, "path", "sha256"),
        (binding_block, "path", "sha256"),
        (provision_block, "path", "sha256"),
        (inputs, "stats_path", "stats_sha256"),
        (inputs, "ccl_log_path", "ccl_log_sha256"),
        (inputs, "packet_log_path", "packet_log_sha256"),
        (inputs, "clip_path", "clip_sha256"),
        (inputs, "truth_path", "truth_sha256"),
        (inputs, "exit_status_path", "exit_status_sha256"),
        (inputs, "run_log_path", "run_log_sha256"),
    ]
    if "board_fg_masks_path" in inputs or "board_fg_masks_sha256" in inputs:
        pairs.append((inputs, "board_fg_masks_path", "board_fg_masks_sha256"))
    _validate_retained_members(score_path, pairs)
    score = Path(score_path)
    recomputed = score_board_run(
        score.parent / str(inputs["stats_path"]),
        score.parent / str(inputs["ccl_log_path"]),
        score.parent / str(inputs["packet_log_path"]),
        score.parent / str(manifest_block["path"]),
        score.parent / str(binding_block["path"]),
        board=str(payload.get("board", "")),
        knobs=payload.get("knobs", {}),
        wall_s=None,
        board_fg_masks_path=(
            score.parent / str(inputs["board_fg_masks_path"])
            if "board_fg_masks_path" in inputs
            else None
        ),
        provision_path=score.parent / str(provision_block["path"]),
        exit_status_path=score.parent / str(inputs["exit_status_path"]),
        run_log_path=score.parent / str(inputs["run_log_path"]),
    )
    if not isinstance(recomputed, Mapping):  # pragma: no cover - output_path is absent
        raise AssertionError("score semantic replay unexpectedly wrote an artifact")
    for key in (
        "schema",
        "campaign_id",
        "board",
        "seed",
        "knobs",
        "effective_board_knobs",
        "probe_semantics",
        "wall_s",
        "explicit_ccl_counters",
        "result",
    ):
        if payload.get(key) != recomputed.get(key):
            raise LedgerIntegrityError(f"retained score semantic replay mismatch: {key}")
    for key in ("identity", "run_id", "remote_run_dir"):
        if payload["run_binding"].get(key) != recomputed["run_binding"].get(key):
            raise LedgerIntegrityError("retained score run identity was hand-authored")
    for key in (
        "binary_sha256",
        "node",
        "run_id",
        "remote_run_dir",
        "power_cycles",
        "identity_preflight",
        "runtime_ive_library",
        "recovery_ledger_tip_sha256",
        "recovery_ledger_row_sha256s",
        "recovery_shift_cycles",
        "recovery_board_cycles",
        "attempt_n",
        "attempt_reservation_sha256",
        "attempt_ledger_tip_sha256",
        "attempt_ledger_row_sha256s",
        "attempts_reserved",
    ):
        if payload["provision"].get(key) != recomputed["provision"].get(key):
            raise LedgerIntegrityError(f"retained score provision mismatch: {key}")


def _entry_hash(row_without_hash: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(row_without_hash)).hexdigest()


def validate_phase1_artifact(
    step: int,
    payload: Mapping[str, object],
    *,
    artifact_path: str | Path | None = None,
) -> None:
    """Enforce the four predeclared discriminator measurements by content."""

    if step == 1:
        if payload.get("schema") not in {
            LEGACY_BUG_VERIFICATION_SCHEMA,
            BUG_VERIFICATION_SCHEMA,
        }:
            raise GuardrailViolation("Phase 1.1 requires the BUG A/B verification schema")
        result = payload.get("result", payload.get("summary"))
        required = ("bug_a_verified", "bug_b_verified", "e2_green", "e5_green")
        if not isinstance(result, Mapping) or any(result.get(key) is not True for key in required):
            raise GuardrailViolation("Phase 1.1 requires BUG A/B plus E2/E5 true evidence")
        if artifact_path is None:
            raise LedgerIntegrityError("Phase 1.1 validation requires its retained artifact path")
        validate_bug_verification_bundle(artifact_path, payload)
        return
    if step == 2:
        summary = payload.get("summary")
        paired = summary.get("paired_discriminator") if isinstance(summary, Mapping) else None
        if (
            payload.get("schema") != HOST_SCHEMA
            or not isinstance(paired, Mapping)
            or paired.get("board_ccl_attempts", 0) < MIN_POSTWARM_FRAMES
            or paired.get("board_label_failures")
            != paired.get("failure_frames_compared")
            or paired.get("clean_host") not in {True, False, None}
            or "board_ccl_log" not in payload
        ):
            raise GuardrailViolation(
                "Phase 1.2 requires a paired exact-loop host/board discriminator artifact"
            )
        if paired.get("failure_frames_compared") == 0 and (
            paired.get("decision") != "not_applicable_no_board_label_failures"
            or paired.get("zero_failure_candidate_path") is not True
            or paired.get("discriminator_allows_climb") is not False
        ):
            raise GuardrailViolation("Phase 1.2 zero-failure branch is malformed")
        if artifact_path is None:
            raise LedgerIntegrityError("Phase 1.2 validation requires its retained artifact path")
        validate_retained_host_bundle(artifact_path, payload)
        return
    if step == 3:
        mask_diff = payload.get("mask_diff")
        summary = payload.get("summary")
        paired = summary.get("paired_discriminator") if isinstance(summary, Mapping) else None
        expected_count = (
            min(10, int(paired.get("board_label_failures", 0)))
            if isinstance(paired, Mapping)
            else -1
        )
        if (
            payload.get("schema") != HOST_SCHEMA
            or not isinstance(mask_diff, Mapping)
            or mask_diff.get("frames_compared") != expected_count
            or "board_fg_masks" not in payload
        ):
            raise GuardrailViolation(
                "Phase 1.3 requires first min(10, failures) aligned SWFM mask diffs"
            )
        if mask_diff.get("differing_pixels") != 0 and paired.get(
            "discriminator_allows_climb"
        ) is True:
            raise GuardrailViolation("non-identical masks cannot authorize a C-001 climb")
        expected_sequences = list(paired.get("board_failure_frame_sequences", []))[
            :expected_count
        ]
        mask_frames = mask_diff.get("frames")
        if not isinstance(mask_frames, list) or [
            row.get("frame_seq") for row in mask_frames if isinstance(row, Mapping)
        ] != expected_sequences:
            raise GuardrailViolation("Phase 1.3 masks are not the declared first failures")
        if artifact_path is None:
            raise LedgerIntegrityError("Phase 1.3 validation requires its retained artifact path")
        validate_retained_host_bundle(artifact_path, payload)
        return
    if step == 4:
        if payload.get("schema") != SCORE_SCHEMA:
            raise GuardrailViolation("Phase 1.4 requires a retained C-001 board score")
        result = payload.get("result")
        counters = payload.get("explicit_ccl_counters")
        if (
            not isinstance(result, Mapping)
            or not isinstance(counters, Mapping)
            or result.get("ccl_attempts", 0) < MIN_POSTWARM_FRAMES
            or counters.get("ccl_api_failures") != 0
            or counters.get("ccl_other_failures") != 0
            or result.get("contract_change_required") is not False
        ):
            raise GuardrailViolation(
                "Phase 1.4 requires a complete API-clean classified board run"
            )
        if artifact_path is None:
            raise LedgerIntegrityError("Phase 1.4 validation requires its retained artifact path")
        validate_retained_score_bundle(artifact_path, payload)
        return
    raise GuardrailViolation("Phase 1 step must be one of 1..4")


def _validate_phase1_chain(payloads: Sequence[Mapping[str, object]]) -> None:
    """Cross-bind Phase 1 host/mask/score evidence to one board probe run."""

    if len(payloads) >= 3:
        host, masks = payloads[1], payloads[2]
        for key in ("board", "seed", "manifest_sha256", "clip_sha256", "truth_sha256", "knobs"):
            if host.get(key) != masks.get(key):
                raise LedgerIntegrityError(f"Phase 1.2/1.3 cross-binding mismatch: {key}")
        host_inputs = host.get("inputs")
        mask_inputs = masks.get("inputs")
        if not isinstance(host_inputs, Mapping) or not isinstance(mask_inputs, Mapping):
            raise LedgerIntegrityError("Phase 1 host evidence lacks retained inputs")
        if host_inputs.get("board_ccl_log_sha256") != mask_inputs.get(
            "board_ccl_log_sha256"
        ):
            raise LedgerIntegrityError("Phase 1.2/1.3 use different board CCL logs")
        host_paired = host.get("summary", {}).get("paired_discriminator")
        mask_paired = masks.get("summary", {}).get("paired_discriminator")
        if not isinstance(host_paired, Mapping) or not isinstance(mask_paired, Mapping):
            raise LedgerIntegrityError("Phase 1 paired discriminator result is missing")
        for key in (
            "board_ccl_attempts",
            "board_label_failures",
            "board_region_count_mismatch_frames",
            "board_failure_frame_sequences",
            "failure_frames_compared",
        ):
            if host_paired.get(key) != mask_paired.get(key):
                raise LedgerIntegrityError(f"Phase 1 discriminator mismatch: {key}")
    if len(payloads) >= 4:
        bug, host, masks, score = payloads[0], payloads[1], payloads[2], payloads[3]
        manifest = score.get("manifest")
        inputs = score.get("inputs")
        result = score.get("result")
        if not all(isinstance(item, Mapping) for item in (manifest, inputs, result)):
            raise LedgerIntegrityError("Phase 1.4 lacks bound manifest/input/result blocks")
        validate_campaign_runtime_binding(bug, score, phase1=True)
        expected = {
            "board": score.get("board"),
            "seed": score.get("seed"),
            "manifest_sha256": manifest.get("sha256"),
            "clip_sha256": inputs.get("clip_sha256"),
            "truth_sha256": inputs.get("truth_sha256"),
            "knobs": score.get("knobs"),
        }
        for payload in (host, masks):
            for key, value in expected.items():
                if payload.get(key) != value:
                    raise LedgerIntegrityError(f"Phase 1 host/score mismatch: {key}")
            host_inputs = payload.get("inputs")
            if not isinstance(host_inputs, Mapping) or host_inputs.get(
                "board_ccl_log_sha256"
            ) != inputs.get("ccl_log_sha256"):
                raise LedgerIntegrityError("Phase 1 host/score CCL log digest mismatch")
        mask_inputs = masks.get("inputs")
        if not isinstance(mask_inputs, Mapping) or mask_inputs.get(
            "board_fg_masks_sha256"
        ) != inputs.get("board_fg_masks_sha256"):
            raise LedgerIntegrityError(
                "Phase 1.3/1.4 SWFM digest mismatch or score omitted masks"
            )
        paired = masks.get("summary", {}).get("paired_discriminator")
        if not isinstance(paired, Mapping) or paired.get("board_label_failures") != result.get(
            "ccl_label_failures"
        ):
            raise LedgerIntegrityError("Phase 1 score failure count differs from discriminator")
        counters = score.get("explicit_ccl_counters")
        if not isinstance(counters, Mapping) or paired.get(
            "board_region_count_mismatch_frames"
        ) != counters.get("ccl_region_count_mismatch_frames"):
            raise LedgerIntegrityError(
                "Phase 1 score region-count diagnostics differ from discriminator"
            )


def validate_campaign_runtime_binding(
    bug_payload: Mapping[str, object],
    score_payload: Mapping[str, object],
    *,
    phase1: bool = False,
    phase1_score: Mapping[str, object] | None = None,
) -> None:
    """Pin the approved daemon binary, image, and kernel across campaign scores."""

    bug_binding = bug_payload.get("binding")
    run_binding = score_payload.get("run_binding")
    provision = score_payload.get("provision")
    if not all(isinstance(item, Mapping) for item in (bug_binding, run_binding, provision)):
        raise LedgerIntegrityError("campaign runtime identity or binary binding is missing")
    approved_identity = bug_binding.get("identity")
    score_identity = run_binding.get("identity")
    if not isinstance(approved_identity, Mapping) or not isinstance(score_identity, Mapping):
        raise LedgerIntegrityError("campaign runtime image identity is missing")
    if phase1 and approved_identity != score_identity:
        raise LedgerIntegrityError("Phase 1.1/1.4 board identity mismatch")
    if approved_identity.get("image_marker") != score_identity.get("image_marker"):
        raise LedgerIntegrityError("campaign image marker changed outside the knob whitelist")
    if bug_binding.get("binary_sha256") != provision.get("binary_sha256"):
        label = "Phase 1.1/1.4" if phase1 else "campaign"
        raise LedgerIntegrityError(f"{label} deployed binary mismatch")
    approved_runtime = bug_binding.get("runtime_ive_library")
    score_runtime = provision.get("runtime_ive_library")
    if not isinstance(approved_runtime, Mapping) or not isinstance(
        score_runtime, Mapping
    ):
        raise LedgerIntegrityError("campaign IVE runtime binding is missing")
    if (
        approved_runtime.get("path") != score_runtime.get("path")
        or approved_runtime.get("sha256_after") != score_runtime.get("sha256_after")
    ):
        label = "Phase 1.1/1.4" if phase1 else "campaign"
        raise LedgerIntegrityError(f"{label} IVE runtime mismatch")
    score_preflight = provision.get("identity_preflight")
    if not isinstance(score_preflight, Mapping) or not isinstance(
        score_preflight.get("kernel"), str
    ) or not str(score_preflight["kernel"]).strip():
        raise LedgerIntegrityError("campaign score lacks a transport-read kernel identity")
    if any(score_preflight.get(key) != score_identity.get(key) for key in score_identity):
        raise LedgerIntegrityError("score preflight identity differs from its run binding")
    if phase1_score is not None:
        phase1_provision = phase1_score.get("provision")
        phase1_binding = phase1_score.get("run_binding")
        if not isinstance(phase1_provision, Mapping) or not isinstance(
            phase1_binding, Mapping
        ):
            raise LedgerIntegrityError("approved Phase 1 runtime binding is missing")
        phase1_identity = phase1_binding.get("identity")
        phase1_preflight = phase1_provision.get("identity_preflight")
        if not isinstance(phase1_identity, Mapping) or not isinstance(
            phase1_preflight, Mapping
        ):
            raise LedgerIntegrityError("approved Phase 1 image/kernel proof is missing")
        if (
            phase1_identity.get("image_marker") != score_identity.get("image_marker")
            or phase1_preflight.get("kernel") != score_preflight.get("kernel")
        ):
            raise LedgerIntegrityError(
                "campaign image/kernel changed outside the knob whitelist"
            )
        phase1_runtime = phase1_provision.get("runtime_ive_library")
        score_runtime = provision.get("runtime_ive_library")
        if not isinstance(phase1_runtime, Mapping) or not isinstance(
            score_runtime, Mapping
        ):
            raise LedgerIntegrityError("campaign IVE runtime binding is missing")
        if (
            phase1_runtime.get("path") != score_runtime.get("path")
            or phase1_runtime.get("sha256_after") != score_runtime.get("sha256_after")
        ):
            raise LedgerIntegrityError(
                "campaign IVE runtime changed outside the knob whitelist"
            )


def _phase1_authorization(
    payloads: Sequence[Mapping[str, object]],
) -> tuple[bool, bool]:
    """Return (climb_authorized, zero-failure-confirmation-authorized)."""

    if len(payloads) < 4:
        return False, False
    paired = payloads[2].get("summary", {}).get("paired_discriminator")
    phase1_result = payloads[3].get("result")
    if not isinstance(paired, Mapping) or not isinstance(phase1_result, Mapping):
        return False, False
    climb = paired.get("discriminator_allows_climb") is True
    zero_failure = (
        paired.get("zero_failure_candidate_path") is True
        and phase1_result.get("ccl_label_failures") == 0
        and result_is_win(phase1_result)
    )
    return climb, zero_failure


def _derived_rate_verdict(
    prior_rows: Sequence[Mapping[str, object]], result: Mapping[str, object]
) -> str:
    """Derive climb outcome against the best retained scalar seen so far."""

    rate = result.get("detector_fail_rate")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not math.isfinite(rate):
        raise LedgerIntegrityError("scored result lacks a finite detector_fail_rate")
    prior_rates = [
        float(prior["result"]["detector_fail_rate"])
        for prior in prior_rows
        if isinstance(prior.get("result"), Mapping)
        and isinstance(prior["result"].get("detector_fail_rate"), (int, float))
        and not isinstance(prior["result"].get("detector_fail_rate"), bool)
    ]
    best = min(prior_rates) if prior_rates else None
    if best is not None and float(rate) > best and not math.isclose(
        float(rate), best, abs_tol=1e-15
    ):
        # Objective regression is authoritative even when recall also fails;
        # otherwise eight blind, worsening runs could evade the shift stop.
        return "regressed"
    if not (
        isinstance(result.get("raw_component_mover_recall"), Mapping)
        and result["raw_component_mover_recall"].get("all_truth_recalled") is True
        and isinstance(result.get("mover_recall"), Mapping)
        and result["mover_recall"].get("all_truth_recalled") is True
    ):
        return "failed"
    if best is None or float(rate) < best:
        return "improved"
    return "unchanged"


def _artifact_for_row(ledger_path: Path, row: Mapping[str, object]) -> Path:
    artifact = row.get("artifact")
    if not isinstance(artifact, Mapping) or set(artifact) != {"path", "sha256"}:
        raise LedgerIntegrityError("ledger artifact must contain exactly path and sha256")
    relative_value = artifact["path"]
    if not isinstance(relative_value, str):
        raise LedgerIntegrityError("ledger artifact path must be relative text")
    relative = Path(relative_value)
    if relative.is_absolute() or relative_value != relative.as_posix() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise LedgerIntegrityError("ledger artifact path uses an alias or escape")
    path = ledger_path.parent / relative
    _assert_no_symlinks(path)
    try:
        path.resolve(strict=True).relative_to(ledger_path.parent.resolve(strict=True))
    except ValueError as exc:
        raise LedgerIntegrityError("ledger artifact escapes the campaign directory") from exc
    return path


def validate_power_cycle_budget(rows: Sequence[Mapping[str, object]]) -> None:
    """Refuse a prospective or retained chain whose aggregate exceeds six."""

    total = 0
    by_mac: dict[str, int] = {}
    for row in rows:
        cycles = _require_int(
            "power_cycles", row.get("power_cycles", 0), 0, MAX_POWER_CYCLES
        )
        total += cycles
        identity = row.get("identity")
        if cycles and isinstance(identity, Mapping):
            checked = BoardIdentity(
                str(identity.get("board", "")),
                str(identity.get("mac", "")),
                str(identity.get("image_marker", "")),
            )
            mac = checked.mac.lower()
            by_mac[mac] = by_mac.get(mac, 0) + cycles
            if by_mac[mac] > MAX_RECOVERY_CYCLES_PER_BOARD:
                raise LedgerIntegrityError("ledger exceeds the two-cycle per-MAC PoE budget")
    if total > MAX_POWER_CYCLES:
        raise LedgerIntegrityError("ledger exceeds the aggregate six-cycle PoE budget")


def _validate_cumulative_chain_extension(
    label: str,
    prior: Sequence[str] | None,
    current: Sequence[str],
    *,
    strict: bool,
) -> None:
    """Require one canonical cumulative chain across all retained SCORE rows."""

    if prior is None:
        return
    if len(current) < len(prior) or list(current[: len(prior)]) != list(prior):
        raise LedgerIntegrityError(
            f"{label} ledger is not a prefix extension of the prior score"
        )
    if strict and len(current) == len(prior):
        raise LedgerIntegrityError(
            f"{label} ledger is not a strict extension of the prior score"
        )


def _artifact_wall_minutes(payload: Mapping[str, object]) -> float | None:
    """Return a retained SCORE/HOST duration; other evidence has no timer."""

    schema = payload.get("schema")
    if schema not in {SCORE_SCHEMA, HOST_SCHEMA}:
        return None
    wall_s = _require_finite(
        f"{schema} wall_s",
        payload.get("wall_s"),
        0.0,
        MAX_EXPERIMENT_MINUTES * 60.0,
    )
    return wall_s / 60.0


def _bind_artifact_wall_minutes(
    payload: Mapping[str, object], supplied: float, *, retained_chain: bool
) -> float:
    expected = _artifact_wall_minutes(payload)
    if expected is None:
        return supplied
    if not math.isclose(supplied, expected, abs_tol=1e-12):
        message = "wall_minutes must equal retained artifact wall_s / 60"
        if retained_chain:
            raise LedgerIntegrityError(message)
        raise GuardrailViolation(message)
    return expected


def validate_ledger_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    ledger_path: str | Path,
    verify_artifacts: bool = True,
) -> list[dict[str, Any]]:
    """Verify sequence, hash chain, budgets, and optionally retained bytes."""

    source = Path(ledger_path)
    checked: list[dict[str, Any]] = []
    artifact_payloads: list[dict[str, Any]] = []
    previous: str | None = None
    seen_artifacts: set[str] = set()
    cycles_by_mac: dict[str, int] = {}
    prior_attempt_chain: list[str] | None = None
    prior_recovery_chain: list[str] | None = None
    for expected_n, original in enumerate(rows, 1):
        if not isinstance(original, Mapping):
            raise LedgerIntegrityError(f"ledger row {expected_n} is not an object")
        row = dict(original)
        if row.get("n") != expected_n:
            raise LedgerIntegrityError(
                f"ledger n must be sequential; expected {expected_n}, got {row.get('n')!r}"
            )
        if row.get("previous_entry_sha256") != previous:
            raise LedgerIntegrityError(f"ledger hash chain breaks at n={expected_n}")
        digest = row.pop("entry_sha256", None)
        if digest != _entry_hash(row):
            raise LedgerIntegrityError(f"ledger entry digest fails at n={expected_n}")
        row["entry_sha256"] = digest
        artifact = _artifact_for_row(source, row)
        relative = str(row["artifact"]["path"])
        if relative in seen_artifacts:
            raise LedgerIntegrityError(f"artifact {relative} is referenced more than once")
        seen_artifacts.add(relative)
        expected_digest = _validate_digest("artifact.sha256", row["artifact"]["sha256"])
        if verify_artifacts and sha256_file(artifact) != expected_digest:
            raise LedgerIntegrityError(f"retained artifact digest fails for n={expected_n}")
        artifact_payload = _read_json_object(artifact)
        if verify_artifacts:
            if artifact_payload.get("schema") == SCORE_SCHEMA:
                validate_retained_score_bundle(artifact, artifact_payload)
            elif artifact_payload.get("schema") == HOST_SCHEMA:
                validate_retained_host_bundle(artifact, artifact_payload)
            elif artifact_payload.get("schema") in {
                LEGACY_BUG_VERIFICATION_SCHEMA,
                BUG_VERIFICATION_SCHEMA,
            }:
                validate_bug_verification_bundle(artifact, artifact_payload)
        artifact_result = artifact_payload.get("result", artifact_payload.get("summary"))
        if not isinstance(artifact_result, Mapping) or row.get("result") != artifact_result:
            raise LedgerIntegrityError("ledger result differs from its retained artifact")
        wall_minutes = _require_finite(
            "wall_minutes", row.get("wall_minutes"), 0.0, MAX_EXPERIMENT_MINUTES
        )
        if wall_minutes > MAX_EXPERIMENT_MINUTES:  # explicit despite helper's bound
            raise LedgerIntegrityError("experiment exceeded the 20-minute budget")
        row_cycles = _require_int(
            "power_cycles", row.get("power_cycles"), 0, MAX_POWER_CYCLES
        )
        if artifact_payload.get("schema") == SCORE_SCHEMA:
            provision_block = artifact_payload.get("provision")
            if not isinstance(provision_block, Mapping):
                raise LedgerIntegrityError("score row lacks retained provision evidence")
            expected_cycles = _require_int(
                "score provision power_cycles",
                provision_block.get("power_cycles"),
                0,
                MAX_RECOVERY_CYCLES_PER_BOARD,
            )
            if row_cycles != expected_cycles:
                raise LedgerIntegrityError(
                    "ledger power_cycles differs from retained recovery evidence"
                )
            score_identity = artifact_payload.get("run_binding", {}).get("identity")
            if not isinstance(score_identity, Mapping):
                raise LedgerIntegrityError("score lacks a run-binding identity")
            normalized_identity = BoardIdentity(
                str(score_identity.get("board", "")),
                str(score_identity.get("mac", "")),
                str(score_identity.get("image_marker", "")),
            )
            mac = normalized_identity.mac.lower()
            cycles_by_mac[mac] = cycles_by_mac.get(mac, 0) + row_cycles
            if cycles_by_mac[mac] > MAX_RECOVERY_CYCLES_PER_BOARD:
                raise LedgerIntegrityError("ledger exceeds the two-cycle per-MAC PoE budget")
            attempt_chain = provision_block.get("attempt_ledger_row_sha256s")
            recovery_chain = provision_block.get("recovery_ledger_row_sha256s")
            if not isinstance(attempt_chain, list) or not isinstance(recovery_chain, list):
                raise LedgerIntegrityError("score omits cumulative attempt/recovery chains")
            for label, chain in (
                ("attempt", attempt_chain),
                ("recovery", recovery_chain),
            ):
                if any(
                    not isinstance(digest, str) or _HEX_SHA256.fullmatch(digest) is None
                    for digest in chain
                ):
                    raise LedgerIntegrityError(f"score {label} chain has a malformed digest")
            attempts_reserved = _require_int(
                "score attempts_reserved",
                provision_block.get("attempts_reserved"),
                1,
                MAX_EXPERIMENTS,
            )
            attempt_n = _require_int(
                "score attempt_n", provision_block.get("attempt_n"), 1, MAX_EXPERIMENTS
            )
            if attempt_n != attempts_reserved:
                raise LedgerIntegrityError(
                    "current physical attempt is not the latest shared reservation"
                )
            recovery_shift_cycles = _require_int(
                "score recovery_shift_cycles",
                provision_block.get("recovery_shift_cycles"),
                0,
                MAX_POWER_CYCLES,
            )
            recovery_board_cycles = _require_int(
                "score recovery_board_cycles",
                provision_block.get("recovery_board_cycles"),
                0,
                MAX_RECOVERY_CYCLES_PER_BOARD,
            )
            if recovery_shift_cycles != len(recovery_chain):
                raise LedgerIntegrityError(
                    "recovery shift-cycle count differs from the retained chain"
                )
            _validate_cumulative_chain_extension(
                "physical-attempt",
                prior_attempt_chain,
                attempt_chain,
                strict=True,
            )
            _validate_cumulative_chain_extension(
                "recovery",
                prior_recovery_chain,
                recovery_chain,
                strict=False,
            )
            prior_attempt_chain = list(attempt_chain)
            prior_recovery_chain = list(recovery_chain)
            expected_attempt_budget = {
                "attempt_n": attempt_n,
                "attempts_reserved": attempts_reserved,
                "tip_sha256": provision_block.get("attempt_ledger_tip_sha256"),
            }
            expected_recovery_budget = {
                "shift_cycles": recovery_shift_cycles,
                "board_cycles": recovery_board_cycles,
                "tip_sha256": provision_block.get("recovery_ledger_tip_sha256"),
            }
            if row.get("attempt_budget") != expected_attempt_budget:
                raise LedgerIntegrityError("ledger attempt budget differs from retained score")
            if row.get("recovery_budget") != expected_recovery_budget:
                raise LedgerIntegrityError("ledger recovery budget differs from retained score")
        elif row_cycles != 0:
            raise LedgerIntegrityError(
                "non-score Phase-1 evidence cannot self-assert power cycles"
            )
        elif row.get("attempt_budget") is not None or row.get("recovery_budget") is not None:
            raise LedgerIntegrityError("non-score evidence cannot assert hardware budgets")
        _require_int(
            "wedges", row.get("wedges"), 0, MAX_WEDGES_PER_EXPERIMENT
        )
        seed = validate_seed(row.get("seed"))
        normalized = normalize_knobs(row.get("knobs", {}))
        if "seed" in artifact_payload and artifact_payload["seed"] != seed:
            raise LedgerIntegrityError("ledger seed differs from retained artifact")
        if "knobs" in artifact_payload and normalize_knobs(
            artifact_payload["knobs"]
        ) != normalized:
            raise LedgerIntegrityError("ledger knobs differ from retained artifact")
        if "board" in artifact_payload and artifact_payload["board"] != row.get("board"):
            raise LedgerIntegrityError("ledger board differs from retained artifact")
        if artifact_payload.get("schema") == SCORE_SCHEMA and row.get(
            "probe_semantics"
        ) != artifact_payload.get("probe_semantics"):
            raise LedgerIntegrityError("ledger probe semantics differ from retained score")
        wall_minutes = _bind_artifact_wall_minutes(
            artifact_payload, wall_minutes, retained_chain=True
        )
        phase = row.get("phase")
        if phase == "phase1":
            if row.get("phase1_step") != expected_n or not 1 <= expected_n <= 4:
                raise LedgerIntegrityError("Phase 1 rows must be ordered steps n=1..4")
            if row.get("knobs") != {}:
                raise LedgerIntegrityError("Phase 1 measurements use frozen default knobs")
            if row.get("verdict") != "measurement":
                raise LedgerIntegrityError("Phase 1 n=1..4 verdict must be measurement")
            validate_phase1_artifact(expected_n, artifact_payload, artifact_path=artifact)
        elif expected_n <= 4:
            raise LedgerIntegrityError("ledger n=1..4 are reserved for ordered Phase 1")
        else:
            if phase not in {"climb", "confirmation"}:
                raise LedgerIntegrityError("post-Phase-1 row has an unknown phase")
            identity = row.get("identity")
            if not isinstance(identity, Mapping):
                raise LedgerIntegrityError("climb/confirmation row lacks bound board identity")
            BoardIdentity(
                str(identity.get("board", "")),
                str(identity.get("mac", "")),
                str(identity.get("image_marker", "")),
            )
            if artifact_payload.get("schema") != SCORE_SCHEMA:
                raise LedgerIntegrityError("climb/confirmation rows require a C-001 score")
            bound_identity = artifact_payload.get("run_binding", {}).get("identity")
            if identity != bound_identity:
                raise LedgerIntegrityError(
                    "ledger identity differs from retained score run binding"
                )
        artifact_payloads.append(artifact_payload)
        try:
            if len(artifact_payloads) >= 3:
                _validate_phase1_chain(artifact_payloads[:4])
            if phase != "phase1" and artifact_payloads:
                validate_campaign_runtime_binding(
                    artifact_payloads[0],
                    artifact_payload,
                    phase1_score=(
                        artifact_payloads[3] if len(artifact_payloads) >= 4 else None
                    ),
                )
        except CampaignError as exc:
            raise SubjectToViolation(str(exc)) from exc
        if not isinstance(row.get("subject_to"), Mapping):
            raise SubjectToViolation("ledger row lacks subject-to evidence")
        climb_authorized, zero_failure = _phase1_authorization(artifact_payloads[:4])
        if phase == "climb" and not climb_authorized:
            raise LedgerIntegrityError("Phase 1 evidence did not authorize a climb")
        if phase == "confirmation" and not (climb_authorized or zero_failure):
            raise LedgerIntegrityError("Phase 1 evidence did not authorize confirmation")
        if phase == "confirmation" and zero_failure and not climb_authorized and normalized:
            raise LedgerIntegrityError("zero-failure confirmation must keep default knobs")
        try:
            validate_subject_to(
                row["subject_to"],
                str(phase),
                zero_failure_confirmation=(
                    phase == "confirmation" and zero_failure and not climb_authorized
                ),
                evidence_root=source,
            )
        except CampaignError as exc:
            raise SubjectToViolation(str(exc)) from exc
        if phase == "phase1" and expected_n == 1:
            bug_binding = artifact_payload.get("binding")
            if not isinstance(bug_binding, Mapping) or (
                bug_binding.get("git_sha") != row["subject_to"].get("revision_sha")
                or bug_binding.get("source_tree_sha256")
                != row["subject_to"].get("source_tree_sha256")
            ):
                raise SubjectToViolation(
                    "Phase 1.1 BUG proof differs from gate/fenced source revision"
                )
        if checked:
            approved_subject = checked[0].get("subject_to")
            if not isinstance(approved_subject, Mapping) or any(
                row["subject_to"].get(key) != approved_subject.get(key)
                for key in ("revision_sha", "source_tree_sha256")
            ):
                raise SubjectToViolation(
                    "subject-to source revision/tree changed during the campaign"
                )
        checked.append(dict(original))
        if phase != "phase1":
            verdict = row.get("verdict")
            prior_candidate = next(
                (prior for prior in checked[:-1] if prior.get("verdict") == "candidate"),
                None,
            )
            if prior_candidate is not None and phase != "confirmation":
                raise LedgerIntegrityError("after a candidate, only confirmations may run")
            if (
                result_is_win(artifact_result)
                and prior_candidate is None
                and verdict != "candidate"
            ):
                raise LedgerIntegrityError("the first winning score must immediately be candidate")
            if verdict == "candidate":
                if not result_is_win(artifact_result) or any(
                    prior.get("verdict") == "candidate" for prior in checked[:-1]
                ):
                    raise LedgerIntegrityError("candidate verdict requires the first actual win")
            elif verdict == "confirmed":
                confirmation = _confirmation_from_rows(checked)
                if (
                    not result_is_win(artifact_result)
                    or confirmation is None
                    or confirmation.get("confirmed") is not True
                ):
                    raise LedgerIntegrityError(
                        "confirmed verdict requires completed fresh-seed/distinct-MAC evidence"
                    )
            else:
                expected_verdict = _derived_rate_verdict(checked[:-1], artifact_result)
                if verdict != expected_verdict:
                    raise LedgerIntegrityError(
                        f"score verdict must be derived as {expected_verdict!r}"
                    )
        previous = str(digest)
    if len(checked) > MAX_EXPERIMENTS:
        raise LedgerIntegrityError("ledger exceeds the 40-experiment shift budget")
    if evaluate_shift(checked).experiments > MAX_EXPERIMENTS:
        raise LedgerIntegrityError(
            "ledger plus reserved physical attempts exceeds the 40-experiment budget"
        )
    validate_power_cycle_budget(checked)
    confirmation = _confirmation_from_rows(checked)
    if confirmation is not None and confirmation.get("confirmed") is True:
        confirmed_rows = [row for row in checked if row.get("verdict") == "confirmed"]
        if not confirmed_rows or confirmed_rows[-1].get("n") != checked[-1].get("n"):
            raise LedgerIntegrityError("the confirmation-completing final row must be confirmed")
    return checked


def _parse_ledger_bytes(data: bytes, path: Path) -> list[dict[str, Any]]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise LedgerIntegrityError(f"ledger has a torn final append: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(data.splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerIntegrityError(f"invalid ledger JSON at line {line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise LedgerIntegrityError(f"ledger line {line_number} is not an object")
        rows.append(row)
    return rows


def read_ledger(
    path: str | Path, *, verify_artifacts: bool = True
) -> list[dict[str, Any]]:
    ledger = Path(path)
    if not ledger.exists():
        return []
    _assert_no_symlinks(ledger)
    rows = _parse_ledger_bytes(ledger.read_bytes(), ledger)
    return validate_ledger_rows(rows, ledger_path=ledger, verify_artifacts=verify_artifacts)


def _stop_path(ledger_path: str | Path) -> Path:
    return Path(ledger_path).with_name("STOP.json")


def read_campaign_stop(ledger_path: str | Path) -> dict[str, Any] | None:
    """Read and validate the immutable shift stop marker, if present."""

    path = _stop_path(ledger_path)
    if not path.exists() and not path.is_symlink():
        return None
    payload = _read_json_object(path)
    required = {
        "schema",
        "campaign_id",
        "ledger",
        "category",
        "reason",
        "ts",
        "source_artifact_sha256",
    }
    if set(payload) != required or payload.get("schema") != STOP_SCHEMA:
        raise LedgerIntegrityError("campaign STOP.json has an invalid schema")
    if payload.get("campaign_id") != CAMPAIGN_ID or payload.get("ledger") != Path(
        ledger_path
    ).name:
        raise LedgerIntegrityError("campaign STOP.json is bound to a different ledger")
    if payload.get("category") not in {
        "subject_to_violation",
        "knob_violation",
        "contract_change_required",
        "board_unreachable_after_two_cycles",
        "wedge_limit",
        "operator_stop",
    }:
        raise LedgerIntegrityError("campaign STOP.json has an unknown category")
    if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
        raise LedgerIntegrityError("campaign STOP.json reason is empty")
    digest = payload.get("source_artifact_sha256")
    if digest is not None:
        _validate_digest("STOP source_artifact_sha256", digest)
    if payload["category"] == "board_unreachable_after_two_cycles":
        _validate_recovery_stop_bundle(Path(ledger_path), payload)
    elif payload["category"] == "subject_to_violation":
        source = Path(ledger_path).with_name("STOP.source.json")
        if source.exists() or source.is_symlink():
            _validate_identity_stop_bundle(Path(ledger_path), payload)
    return payload


def _validate_recovery_stop_bundle(
    ledger_path: Path, stop_payload: Mapping[str, object]
) -> None:
    """Bind a terminal unreachable-board STOP to both durable budget chains."""

    source = ledger_path.with_name("STOP.source.json")
    expected_source_sha = stop_payload.get("source_artifact_sha256")
    if expected_source_sha is None or sha256_file(source) != expected_source_sha:
        raise LedgerIntegrityError("unreachable-board STOP lacks its immutable source proof")
    evidence = _read_json_object(source)
    required = {
        "schema",
        "campaign_id",
        "category",
        "reason",
        "recorded_at",
        "run_id",
        "identity",
        "recovery_attempt",
        "recovery_ledger",
        "attempt_reservation",
        "attempt_ledger",
    }
    if (
        set(evidence) != required
        or evidence["schema"] != "skyweave-c001-recovery-stop-evidence/1"
        or evidence["campaign_id"] != CAMPAIGN_ID
        or evidence["category"] != "board_unreachable_after_two_cycles"
        or evidence["reason"] != stop_payload.get("reason")
        or not isinstance(evidence["recorded_at"], str)
        or not evidence["recorded_at"].strip()
    ):
        raise LedgerIntegrityError("unreachable-board STOP source schema is invalid")
    run_id = evidence["run_id"]
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise LedgerIntegrityError("unreachable-board STOP run_id is malformed")
    identity = evidence["identity"]
    if not isinstance(identity, Mapping) or set(identity) != {
        "board",
        "mac",
        "image_marker",
    }:
        raise LedgerIntegrityError("unreachable-board STOP identity is malformed")
    checked_identity = BoardIdentity(
        str(identity["board"]), str(identity["mac"]), str(identity["image_marker"])
    )
    recovery_attempt = evidence["recovery_attempt"]
    if not isinstance(recovery_attempt, Mapping) or set(recovery_attempt) != {
        "run_id",
        "board",
        "mac",
        "shift_cycle_n",
        "board_cycle_n",
        "reservation_sha256",
        "outcome",
        "identity_revalidated",
    }:
        raise LedgerIntegrityError("terminal recovery-attempt evidence is malformed")
    if (
        recovery_attempt["run_id"] != run_id
        or recovery_attempt["board"] != checked_identity.board
        or str(recovery_attempt["mac"]).lower() != checked_identity.mac.lower()
        or _require_int(
            "terminal recovery shift_cycle_n",
            recovery_attempt["shift_cycle_n"],
            1,
            MAX_POWER_CYCLES,
        )
        < 1
        or _require_int(
            "terminal recovery board_cycle_n",
            recovery_attempt["board_cycle_n"],
            MAX_RECOVERY_CYCLES_PER_BOARD,
            MAX_RECOVERY_CYCLES_PER_BOARD,
        )
        != MAX_RECOVERY_CYCLES_PER_BOARD
        or recovery_attempt["outcome"] != "unreachable"
        or recovery_attempt["identity_revalidated"] is not False
    ):
        raise LedgerIntegrityError("terminal recovery evidence did not exhaust this board")
    recovery_sha = _validate_digest(
        "terminal recovery reservation", recovery_attempt["reservation_sha256"]
    )
    recovery_block = evidence["recovery_ledger"]
    if not isinstance(recovery_block, Mapping) or recovery_block.get("path") != (
        "STOP.recovery-ledger.jsonl"
    ) or set(recovery_block) != {"path", "sha256", "tip_sha256"}:
        raise LedgerIntegrityError("terminal recovery-ledger attachment is malformed")
    recovery_path = ledger_path.with_name("STOP.recovery-ledger.jsonl")
    if sha256_file(recovery_path) != _validate_digest(
        "terminal recovery ledger", recovery_block["sha256"]
    ):
        raise LedgerIntegrityError("terminal recovery-ledger digest mismatch")
    recovery_rows = _validate_recovery_ledger_snapshot(recovery_path)
    if (
        not recovery_rows
        or recovery_rows[-1]["row_sha256"] != recovery_block["tip_sha256"]
    ):
        raise LedgerIntegrityError("terminal recovery-ledger tip mismatch")
    recovery_row = next(
        (row for row in recovery_rows if row["row_sha256"] == recovery_sha), None
    )
    if recovery_row is None or any(
        recovery_row[name] != recovery_attempt[name]
        for name in ("run_id", "board", "mac", "shift_cycle_n", "board_cycle_n")
    ):
        raise LedgerIntegrityError("terminal recovery attempt lacks its reservation row")
    attempt_reservation = evidence["attempt_reservation"]
    if not isinstance(attempt_reservation, Mapping) or set(attempt_reservation) != {
        "run_id",
        "attempt_n",
        "reservation_sha256",
    } or attempt_reservation["run_id"] != run_id:
        raise LedgerIntegrityError("terminal physical-attempt reservation is malformed")
    attempt_sha = _validate_digest(
        "terminal attempt reservation", attempt_reservation["reservation_sha256"]
    )
    attempt_block = evidence["attempt_ledger"]
    if not isinstance(attempt_block, Mapping) or attempt_block.get("path") != (
        "STOP.attempt-ledger.jsonl"
    ) or set(attempt_block) != {"path", "sha256", "tip_sha256"}:
        raise LedgerIntegrityError("terminal attempt-ledger attachment is malformed")
    attempt_path = ledger_path.with_name("STOP.attempt-ledger.jsonl")
    if sha256_file(attempt_path) != _validate_digest(
        "terminal attempt ledger", attempt_block["sha256"]
    ):
        raise LedgerIntegrityError("terminal attempt-ledger digest mismatch")
    attempt_rows = _validate_attempt_ledger_snapshot(attempt_path)
    if attempt_rows[-1]["row_sha256"] != attempt_block["tip_sha256"]:
        raise LedgerIntegrityError("terminal attempt-ledger tip mismatch")
    attempt_row = next(
        (row for row in attempt_rows if row["row_sha256"] == attempt_sha), None
    )
    if (
        attempt_row is None
        or attempt_row["event"] != "attempt_reserved"
        or attempt_row["run_id"] != run_id
        or attempt_row["attempt_n"] != attempt_reservation["attempt_n"]
        or attempt_row["board"] != checked_identity.board
        or str(attempt_row["mac"]).lower() != checked_identity.mac.lower()
    ):
        raise LedgerIntegrityError("terminal attempt proof lacks its reservation row")
    outcome = attempt_rows[-1]
    allowed_recovery_failures = {
        "preflight_failure",
        "run_failed",
        "timeout",
        "wedge",
    }
    if (
        outcome["event"] != "attempt_outcome"
        or outcome["run_id"] != run_id
        or outcome["attempt_n"] != attempt_reservation["attempt_n"]
        or outcome["outcome_n"] != 1
        or outcome["outcome"] not in allowed_recovery_failures
        or outcome["wedge"] is not (outcome["outcome"] == "wedge")
        or not isinstance(outcome["error"], str)
        or not evidence["reason"].endswith(outcome["error"])
    ):
        raise LedgerIntegrityError(
            "terminal recovery STOP lacks its durable failed attempt outcome"
        )


def _validate_identity_stop_bundle(
    ledger_path: Path, stop_payload: Mapping[str, object]
) -> None:
    """Replay an identity Subject-to STOP and its durable failed attempt."""

    source = ledger_path.with_name("STOP.source.json")
    expected_source_sha = stop_payload.get("source_artifact_sha256")
    if expected_source_sha is None or sha256_file(source) != expected_source_sha:
        raise LedgerIntegrityError("identity STOP lacks its immutable source proof")
    evidence = _read_json_object(source)
    required = {
        "schema",
        "campaign_id",
        "category",
        "reason",
        "recorded_at",
        "run_id",
        "expected",
        "observed",
        "mismatched_fields",
        "attempt_reservation",
        "attempt_ledger",
    }
    if (
        set(evidence) != required
        or evidence["schema"] != "skyweave-c001-identity-stop-evidence/1"
        or evidence["campaign_id"] != CAMPAIGN_ID
        or evidence["category"] != "subject_to_violation"
        or evidence["reason"] != stop_payload.get("reason")
        or not isinstance(evidence["recorded_at"], str)
        or not evidence["recorded_at"].strip()
    ):
        raise LedgerIntegrityError("identity STOP source schema is invalid")
    run_id = evidence["run_id"]
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise LedgerIntegrityError("identity STOP run_id is malformed")
    identity_fields = {"board", "mac", "image_marker", "kernel", "interface"}
    expected = evidence["expected"]
    observed = evidence["observed"]
    if (
        not isinstance(expected, Mapping)
        or set(expected) != identity_fields
        or not isinstance(observed, Mapping)
        or set(observed) != identity_fields
    ):
        raise LedgerIntegrityError("identity STOP expected/observed values are malformed")
    expected_identity = BoardIdentity(
        str(expected["board"]), str(expected["mac"]), str(expected["image_marker"])
    )
    for name in identity_fields:
        expected_value = expected[name]
        observed_value = observed[name]
        if not isinstance(expected_value, str) or not expected_value.strip():
            raise LedgerIntegrityError("identity STOP expected values must be non-empty")
        if name == "kernel" and observed_value is None:
            continue
        if not isinstance(observed_value, str) or not observed_value.strip():
            raise LedgerIntegrityError("identity STOP observed values must be non-empty")
    if observed["interface"] != expected["interface"]:
        raise LedgerIntegrityError("identity STOP changed the identity interface")
    ordered_fields = ("board", "mac", "image_marker", "kernel")
    derived_mismatches = [
        name
        for name in ordered_fields
        if observed[name] is not None and observed[name] != expected[name]
    ]
    if not derived_mismatches or evidence["mismatched_fields"] != derived_mismatches:
        raise LedgerIntegrityError("identity STOP mismatch list is not evidence-derived")
    attempt_reservation = evidence["attempt_reservation"]
    if (
        not isinstance(attempt_reservation, Mapping)
        or set(attempt_reservation) != {"run_id", "attempt_n", "reservation_sha256"}
        or attempt_reservation["run_id"] != run_id
    ):
        raise LedgerIntegrityError("identity STOP attempt reservation is malformed")
    attempt_sha = _validate_digest(
        "identity STOP attempt reservation", attempt_reservation["reservation_sha256"]
    )
    attempt_block = evidence["attempt_ledger"]
    if (
        not isinstance(attempt_block, Mapping)
        or set(attempt_block) != {"path", "sha256", "tip_sha256"}
        or attempt_block.get("path") != "STOP.attempt-ledger.jsonl"
    ):
        raise LedgerIntegrityError("identity STOP attempt-ledger attachment is malformed")
    attempt_path = ledger_path.with_name("STOP.attempt-ledger.jsonl")
    if sha256_file(attempt_path) != _validate_digest(
        "identity STOP attempt ledger", attempt_block["sha256"]
    ):
        raise LedgerIntegrityError("identity STOP attempt-ledger digest mismatch")
    attempt_rows = _validate_attempt_ledger_snapshot(attempt_path)
    if attempt_rows[-1]["row_sha256"] != attempt_block["tip_sha256"]:
        raise LedgerIntegrityError("identity STOP attempt-ledger tip mismatch")
    reservation = next(
        (row for row in attempt_rows if row["row_sha256"] == attempt_sha), None
    )
    if (
        reservation is None
        or reservation["event"] != "attempt_reserved"
        or reservation["run_id"] != run_id
        or reservation["attempt_n"] != attempt_reservation["attempt_n"]
        or reservation["board"] != expected_identity.board
        or str(reservation["mac"]).lower() != expected_identity.mac.lower()
    ):
        raise LedgerIntegrityError("identity STOP lacks its attempt reservation")
    outcome = attempt_rows[-1]
    if (
        outcome["event"] != "attempt_outcome"
        or outcome["run_id"] != run_id
        or outcome["attempt_n"] != attempt_reservation["attempt_n"]
        or outcome["outcome"] != "identity_failure"
        or outcome["wedge"] is not False
        or outcome["error"] != evidence["reason"]
    ):
        raise LedgerIntegrityError("identity STOP lacks its durable failure outcome")


def record_campaign_stop(
    ledger_path: str | Path,
    *,
    category: str,
    reason: str,
    source_artifact_sha256: str | None = None,
    _lock_held: bool = False,
) -> dict[str, Any]:
    """Persist one typed, append-only terminal incident beside the ledger."""

    ledger = Path(ledger_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlinks(ledger.parent)
    if not _lock_held:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(ledger, flags, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return record_campaign_stop(
                ledger,
                category=category,
                reason=reason,
                source_artifact_sha256=source_artifact_sha256,
                _lock_held=True,
            )
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
    if read_campaign_stop(ledger) is not None:
        raise GuardrailViolation("campaign already has an immutable STOP.json")
    if category not in {
        "subject_to_violation",
        "knob_violation",
        "contract_change_required",
        "board_unreachable_after_two_cycles",
        "wedge_limit",
        "operator_stop",
    }:
        raise CampaignError(f"unknown campaign stop category {category!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise CampaignError("campaign stop reason must be non-empty")
    if source_artifact_sha256 is not None:
        source_artifact_sha256 = _validate_digest(
            "source_artifact_sha256", source_artifact_sha256
        )
    payload = {
        "schema": STOP_SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "ledger": ledger.name,
        "category": category,
        "reason": reason.strip(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "source_artifact_sha256": source_artifact_sha256,
    }
    if category == "board_unreachable_after_two_cycles":
        _validate_recovery_stop_bundle(ledger, payload)
    elif category == "subject_to_violation":
        source = ledger.with_name("STOP.source.json")
        if source.exists() or source.is_symlink():
            _validate_identity_stop_bundle(ledger, payload)
    # Validate the category/reason through the same reader rules after the
    # no-replace atomic install.
    _write_new_bytes(_stop_path(ledger), _canonical_json(payload))
    return read_campaign_stop(ledger) or payload


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _campaign_history_directory(campaign_dir: Path) -> Path:
    if campaign_dir.name != CAMPAIGN_ID:
        raise GuardrailViolation(
            f"campaign directory must be named {CAMPAIGN_ID!r}, not {campaign_dir.name!r}"
        )
    return campaign_dir.with_name(SHIFT_HISTORY_DIRECTORY)


@contextmanager
def campaign_shift_lock(
    campaign_dir: str | Path, *, exclusive: bool = False
) -> Iterator[None]:
    """Hold the cross-shift lock shared by every production writer.

    The lock lives beside the canonical runtime so renaming that runtime during
    a rollover cannot rename the lock out from under another process.
    """

    campaign = Path(os.path.abspath(campaign_dir))
    history = _campaign_history_directory(campaign)
    _assert_no_symlinks(history.parent)
    if history.exists() and (history.is_symlink() or not history.is_dir()):
        raise GuardrailViolation("C-001 shift history must be a real directory")
    history.mkdir(mode=0o755, exist_ok=True)
    _assert_no_symlinks(history)
    lock_path = history / ".lock"
    _assert_no_symlinks(lock_path, missing_leaf_ok=True)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_regular_bytes(path: Path, *, allow_missing: bool = False) -> tuple[bytes, bool]:
    if not path.exists() and not path.is_symlink():
        if allow_missing:
            return b"", False
        raise GuardrailViolation(f"required shift artifact is missing: {path}")
    _assert_no_symlinks(path)
    if not path.is_file():
        raise GuardrailViolation(f"shift artifact is not a regular file: {path}")
    return path.read_bytes(), True


def _attempt_chain_snapshot(path: Path) -> tuple[bytes, dict[str, object]]:
    data, present = _read_regular_bytes(path, allow_missing=True)
    rows = _validate_attempt_ledger_snapshot(path) if data else []
    return data, {
        "path": path.name,
        "present": present,
        "bytes": len(data),
        "sha256": _digest_bytes(data),
        "entries": len(rows),
        "reservations": sum(row["event"] == "attempt_reserved" for row in rows),
        "tip_sha256": rows[-1]["row_sha256"] if rows else None,
    }


def _recovery_chain_snapshot(path: Path) -> tuple[bytes, dict[str, object]]:
    data, present = _read_regular_bytes(path, allow_missing=True)
    rows = _validate_recovery_ledger_snapshot(path) if present else []
    return data, {
        "path": path.name,
        "present": present,
        "bytes": len(data),
        "sha256": _digest_bytes(data),
        "entries": len(rows),
        "cycles": len(rows),
        "tip_sha256": rows[-1]["row_sha256"] if rows else None,
    }


def _archive_tree_inventory(root: Path) -> dict[str, object]:
    """Digest every directory and byte without following aliases or hardlinks."""

    _assert_no_symlinks(root)
    if not root.is_dir():
        raise GuardrailViolation(f"shift root is not a real directory: {root}")
    entries: list[dict[str, object]] = []
    seen_files: set[tuple[int, int]] = set()
    total_bytes = 0
    candidates = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for candidate in candidates:
        relative = candidate.relative_to(root).as_posix()
        info = candidate.lstat()
        mode = f"{stat.S_IMODE(info.st_mode):04o}"
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "type": "directory", "mode": mode})
            continue
        if not stat.S_ISREG(info.st_mode):
            raise GuardrailViolation(
                f"shift archive refuses symlink or special member: {relative}"
            )
        inode = (info.st_dev, info.st_ino)
        if info.st_nlink != 1 or inode in seen_files:
            raise GuardrailViolation(f"shift archive refuses hardlinked member: {relative}")
        seen_files.add(inode)
        digest = sha256_file(candidate)
        entries.append(
            {
                "path": relative,
                "type": "file",
                "mode": mode,
                "bytes": info.st_size,
                "sha256": digest,
            }
        )
        total_bytes += info.st_size
    return {
        "sha256": _digest_bytes(_canonical_json({"entries": entries})),
        "members": len(entries),
        "bytes": total_bytes,
    }


def _stopped_shift_snapshot(campaign_dir: Path) -> dict[str, object]:
    ledger = campaign_dir / "ledger.jsonl"
    ledger_data, _ = _read_regular_bytes(ledger)
    rows = read_ledger(ledger, verify_artifacts=True)
    stop = read_campaign_stop(ledger)
    if stop is None:
        raise GuardrailViolation("a successor shift requires an immutable predecessor STOP")
    stop_path = campaign_dir / "STOP.json"
    _, _ = _read_regular_bytes(stop_path)
    _, attempt = _attempt_chain_snapshot(campaign_dir / "attempt-ledger.jsonl")
    _, recovery = _recovery_chain_snapshot(campaign_dir / "recovery-ledger.jsonl")
    source: dict[str, object] = {
        "revision_sha": None,
        "source_tree_sha256": None,
    }
    if rows:
        subject = rows[0].get("subject_to")
        if not isinstance(subject, Mapping):  # read_ledger normally closes this case
            raise LedgerIntegrityError("predecessor ledger lacks source identity")
        source = {
            "revision_sha": subject.get("revision_sha"),
            "source_tree_sha256": subject.get("source_tree_sha256"),
        }
    return {
        "ledger": {
            "path": ledger.name,
            "bytes": len(ledger_data),
            "sha256": _digest_bytes(ledger_data),
            "rows": len(rows),
            "tip_sha256": rows[-1]["entry_sha256"] if rows else None,
        },
        "stop": {
            "path": stop_path.name,
            "sha256": sha256_file(stop_path),
            "category": stop["category"],
        },
        "source": source,
        "attempt_ledger": attempt,
        "recovery_ledger": recovery,
    }


def _shift_lineage_path(campaign_dir: Path) -> Path:
    return _campaign_history_directory(campaign_dir) / SHIFT_LINEAGE_NAME


def _lineage_row_hash(row_without_hash: Mapping[str, object]) -> str:
    return _digest_bytes(_canonical_json(dict(row_without_hash)))


def _parse_shift_lineage_bytes(data: bytes) -> list[dict[str, Any]]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise LedgerIntegrityError("C-001 shift lineage has a torn final append")
    required = {
        "schema",
        "campaign_id",
        "event",
        "archived_shift_n",
        "successor_shift_n",
        "archive_id",
        "archive_tree",
        "ledger",
        "stop",
        "source",
        "attempt_ledger",
        "recovery_ledger",
        "source_policy",
        "note",
        "recorded_at",
        "previous_entry_sha256",
        "entry_sha256",
    }
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    for line_number, line in enumerate(data.splitlines(), 1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerIntegrityError(
                f"shift lineage line {line_number} is not canonical JSON"
            ) from exc
        if not isinstance(row, dict) or set(row) != required:
            raise LedgerIntegrityError("shift lineage fields are incomplete or unknown")
        archived_n = _require_int(
            "archived_shift_n", row["archived_shift_n"], 1
        )
        successor_n = _require_int(
            "successor_shift_n", row["successor_shift_n"], 2
        )
        if (
            row["schema"] != SHIFT_LINEAGE_SCHEMA
            or row["campaign_id"] != CAMPAIGN_ID
            or row["event"] != "successor_shift_opened"
            or archived_n != len(rows) + 1
            or successor_n != archived_n + 1
            or row["previous_entry_sha256"] != previous
            or not isinstance(row["note"], str)
            or not row["note"].strip()
            or not isinstance(row["recorded_at"], str)
            or not row["recorded_at"].strip()
        ):
            raise LedgerIntegrityError("shift lineage sequence or metadata is invalid")
        archive_id = row["archive_id"]
        if not isinstance(archive_id, str) or _SHIFT_ARCHIVE.fullmatch(archive_id) is None:
            raise LedgerIntegrityError("shift lineage archive id is malformed")
        archive_tree = row["archive_tree"]
        if (
            not isinstance(archive_tree, Mapping)
            or set(archive_tree) != {"sha256", "members", "bytes"}
        ):
            raise LedgerIntegrityError("shift lineage archive tree is malformed")
        _validate_digest("shift lineage archive tree SHA256", archive_tree["sha256"])
        _require_int("shift lineage archive members", archive_tree["members"], 1)
        _require_int("shift lineage archive bytes", archive_tree["bytes"], 0)

        ledger = row["ledger"]
        if (
            not isinstance(ledger, Mapping)
            or set(ledger) != {"path", "bytes", "sha256", "rows", "tip_sha256"}
            or ledger.get("path") != "ledger.jsonl"
        ):
            raise LedgerIntegrityError("shift lineage ledger summary is malformed")
        ledger_rows = _require_int("shift lineage ledger rows", ledger["rows"], 0)
        _require_int("shift lineage ledger bytes", ledger["bytes"], 0)
        _validate_digest("shift lineage ledger SHA256", ledger["sha256"])
        if ledger_rows:
            _validate_digest("shift lineage ledger tip", ledger["tip_sha256"])
        elif ledger["tip_sha256"] is not None:
            raise LedgerIntegrityError("empty shift ledger cannot have a tip")

        stop = row["stop"]
        if (
            not isinstance(stop, Mapping)
            or set(stop) != {"path", "sha256", "category"}
            or stop.get("path") != "STOP.json"
            or not isinstance(stop.get("category"), str)
        ):
            raise LedgerIntegrityError("shift lineage STOP summary is malformed")
        stop_sha = _validate_digest("shift lineage STOP SHA256", stop["sha256"])
        if archive_id != f"shift-{archived_n:04d}-{stop_sha[:12]}":
            raise LedgerIntegrityError("shift lineage archive id is not deterministic")

        source = row["source"]
        if (
            not isinstance(source, Mapping)
            or set(source) != {"revision_sha", "source_tree_sha256"}
        ):
            raise LedgerIntegrityError("shift lineage source summary is malformed")
        revision = source["revision_sha"]
        if revision is not None and (
            not isinstance(revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        ):
            raise LedgerIntegrityError("shift lineage source revision is malformed")
        source_tree = source["source_tree_sha256"]
        if source_tree is not None:
            _validate_digest("shift lineage source tree SHA256", source_tree)

        for name, filename, counter in (
            ("attempt_ledger", "attempt-ledger.jsonl", "reservations"),
            ("recovery_ledger", "recovery-ledger.jsonl", "cycles"),
        ):
            summary = row[name]
            if (
                not isinstance(summary, Mapping)
                or set(summary)
                != {
                    "path",
                    "present",
                    "bytes",
                    "sha256",
                    "entries",
                    counter,
                    "tip_sha256",
                }
                or summary.get("path") != filename
                or not isinstance(summary.get("present"), bool)
            ):
                raise LedgerIntegrityError(f"shift lineage {name} summary is malformed")
            entries = _require_int(f"shift lineage {name} entries", summary["entries"], 0)
            count = _require_int(f"shift lineage {name} {counter}", summary[counter], 0)
            if count > entries:
                raise LedgerIntegrityError(f"shift lineage {name} count exceeds entries")
            _require_int(f"shift lineage {name} bytes", summary["bytes"], 0)
            _validate_digest(f"shift lineage {name} SHA256", summary["sha256"])
            if entries:
                _validate_digest(f"shift lineage {name} tip", summary["tip_sha256"])
            elif summary["tip_sha256"] is not None:
                raise LedgerIntegrityError(f"empty shift lineage {name} cannot have a tip")

        policy = row["source_policy"]
        if (
            not isinstance(policy, Mapping)
            or set(policy) != {"first_row", "forbidden_source_tree_sha256"}
            or policy.get("first_row") not in {"must_differ", "may_match"}
        ):
            raise LedgerIntegrityError("shift lineage source policy is malformed")
        forbidden = policy["forbidden_source_tree_sha256"]
        if forbidden is not None:
            _validate_digest("shift lineage forbidden source tree", forbidden)
        if dict(policy) != _source_policy_for_row(row):
            raise LedgerIntegrityError("shift lineage source policy is not STOP-derived")
        digest = _validate_digest("shift lineage entry_sha256", row["entry_sha256"])
        material = {name: value for name, value in row.items() if name != "entry_sha256"}
        if _lineage_row_hash(material) != digest:
            raise LedgerIntegrityError("shift lineage entry digest fails semantic replay")
        previous = digest
        rows.append(row)
    return rows


def _read_shift_lineage(campaign_dir: Path) -> list[dict[str, Any]]:
    path = _shift_lineage_path(campaign_dir)
    data, present = _read_regular_bytes(path, allow_missing=True)
    return _parse_shift_lineage_bytes(data) if present else []


def _successor_pointer(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema": SUCCESSOR_SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "shift_n": row["successor_shift_n"],
        "lineage_entry_sha256": row["entry_sha256"],
    }


def _validate_successor_pointer(path: Path, row: Mapping[str, object]) -> None:
    expected = _successor_pointer(row)
    payload = _read_json_object(path)
    if payload != expected or path.read_bytes() != _canonical_json(expected):
        raise LedgerIntegrityError("SUCCESSOR.json does not match its lineage entry")


def _validate_budget_prefix(
    predecessor: Path,
    successor: Path,
    *,
    attempt: bool,
) -> None:
    if attempt:
        predecessor_data, _ = _attempt_chain_snapshot(predecessor)
        successor_data, successor_present = _read_regular_bytes(successor)
        if not successor_present or not successor_data.startswith(predecessor_data):
            raise LedgerIntegrityError("successor attempt ledger reset its inherited prefix")
        if successor_data:
            _validate_attempt_ledger_snapshot(successor)
    else:
        predecessor_data, _ = _recovery_chain_snapshot(predecessor)
        successor_data, successor_present = _read_regular_bytes(successor)
        if not successor_present or not successor_data.startswith(predecessor_data):
            raise LedgerIntegrityError("successor recovery ledger reset its inherited prefix")
        _validate_recovery_ledger_snapshot(successor)


def _source_policy_for_row(row: Mapping[str, object]) -> dict[str, object]:
    stop = row["stop"]
    source = row["source"]
    if not isinstance(stop, Mapping) or not isinstance(source, Mapping):
        raise LedgerIntegrityError("shift lineage source policy inputs are malformed")
    forbidden = source.get("source_tree_sha256")
    must_differ = stop.get("category") == "contract_change_required"
    return {
        "first_row": "must_differ" if must_differ else "may_match",
        "forbidden_source_tree_sha256": forbidden if must_differ else None,
    }


def validate_current_shift(
    campaign_dir: str | Path,
    *,
    verify_artifacts: bool = True,
    _allowed_history_members: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Replay the complete rollover lineage and return the live shift identity."""

    campaign = Path(os.path.abspath(campaign_dir))
    if campaign.name != CAMPAIGN_ID:
        raise GuardrailViolation(f"campaign directory must be named {CAMPAIGN_ID!r}")
    _assert_no_symlinks(campaign)
    if not campaign.is_dir():
        raise GuardrailViolation("canonical C-001 campaign directory is missing")
    history = _campaign_history_directory(campaign)
    if history.exists() or history.is_symlink():
        _assert_no_symlinks(history)
        if not history.is_dir():
            raise LedgerIntegrityError("C-001 shift history directory is invalid")
    rows = _read_shift_lineage(campaign)
    if not rows:
        if (campaign / SUCCESSOR_NAME).exists() or (campaign / SUCCESSOR_NAME).is_symlink():
            raise LedgerIntegrityError("SUCCESSOR.json exists without a lineage entry")
        if history.exists():
            leftovers = [
                member.name
                for member in history.iterdir()
                if member.name not in {".lock", SHIFT_LINEAGE_NAME}
            ]
            if leftovers:
                raise LedgerIntegrityError("uncommitted C-001 rollover state exists")
        return {
            "campaign_dir": str(campaign),
            "shift_n": 1,
            "lineage_entry_sha256": None,
            "predecessor_archive": None,
        }

    expected_archives = {str(row["archive_id"]) for row in rows}
    actual_archives: set[str] = set()
    allowed_files = {".lock", SHIFT_LINEAGE_NAME}
    for member in history.iterdir():
        mode = member.lstat().st_mode
        if member.name in expected_archives:
            if not stat.S_ISDIR(mode):
                raise LedgerIntegrityError("C-001 shift archive is not a real directory")
            actual_archives.add(member.name)
        elif member.name in allowed_files:
            if not stat.S_ISREG(mode):
                raise LedgerIntegrityError("C-001 shift history metadata is not regular")
        elif member.name not in _allowed_history_members:
            raise LedgerIntegrityError(
                f"unknown C-001 shift history member: {member.name}"
            )
    if actual_archives != expected_archives:
        raise LedgerIntegrityError("C-001 shift archive set differs from lineage")

    for index, row in enumerate(rows):
        archive = history / str(row["archive_id"])
        inventory = _archive_tree_inventory(archive)
        if inventory != row["archive_tree"]:
            raise LedgerIntegrityError("archived shift tree differs from its lineage digest")
        snapshot = _stopped_shift_snapshot(archive)
        for name in (
            "ledger",
            "stop",
            "source",
            "attempt_ledger",
            "recovery_ledger",
        ):
            if snapshot[name] != row[name]:
                raise LedgerIntegrityError(
                    f"archived shift {name} differs from its lineage summary"
                )
        if row["source_policy"] != _source_policy_for_row(row):
            raise LedgerIntegrityError("shift lineage source policy is not STOP-derived")

        successor_root = (
            campaign if index == len(rows) - 1 else history / str(rows[index + 1]["archive_id"])
        )
        _validate_successor_pointer(successor_root / SUCCESSOR_NAME, row)
        _validate_budget_prefix(
            archive / "attempt-ledger.jsonl",
            successor_root / "attempt-ledger.jsonl",
            attempt=True,
        )
        _validate_budget_prefix(
            archive / "recovery-ledger.jsonl",
            successor_root / "recovery-ledger.jsonl",
            attempt=False,
        )
        successor_ledger = successor_root / "ledger.jsonl"
        if successor_ledger.exists() or successor_ledger.is_symlink():
            successor_rows = read_ledger(
                successor_ledger, verify_artifacts=verify_artifacts
            )
            policy = row["source_policy"]
            if successor_rows and policy["first_row"] == "must_differ":
                source = successor_rows[0]["subject_to"]["source_tree_sha256"]
                if source == policy["forbidden_source_tree_sha256"]:
                    raise SubjectToViolation(
                        "successor Phase 1.1 reused the stopped source tree"
                    )

    latest = rows[-1]
    return {
        "campaign_dir": str(campaign),
        "shift_n": latest["successor_shift_n"],
        "lineage_entry_sha256": latest["entry_sha256"],
        "predecessor_archive": str(
            history / str(latest["archive_id"])
        ),
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_descriptor_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _append_shift_lineage_row(descriptor: int, row: Mapping[str, object]) -> None:
    existing = _parse_shift_lineage_bytes(_read_descriptor_bytes(descriptor))
    previous = existing[-1]["entry_sha256"] if existing else None
    if row.get("previous_entry_sha256") != previous:
        raise LedgerIntegrityError("shift lineage changed before successor commit")
    expected_n = len(existing) + 2
    if row.get("successor_shift_n") != expected_n:
        raise LedgerIntegrityError("successor shift number changed before commit")
    encoded = _canonical_json(dict(row))
    os.lseek(descriptor, 0, os.SEEK_END)
    offset = 0
    while offset < len(encoded):
        offset += os.write(descriptor, encoded[offset:])
    os.fsync(descriptor)


def _prepare_successor_stage(
    stage: Path,
    predecessor: Path,
    row: Mapping[str, object],
) -> None:
    pointer = _successor_pointer(row)
    attempt_data, attempt = _attempt_chain_snapshot(
        predecessor / "attempt-ledger.jsonl"
    )
    recovery_data, recovery = _recovery_chain_snapshot(
        predecessor / "recovery-ledger.jsonl"
    )
    if attempt != row["attempt_ledger"] or recovery != row["recovery_ledger"]:
        raise LedgerIntegrityError("predecessor budget changed during rollover")
    _assert_no_symlinks(stage, missing_leaf_ok=True)
    stage.mkdir(mode=0o755, parents=False, exist_ok=True)
    _assert_no_symlinks(stage)
    if not stage.is_dir():
        raise GuardrailViolation("successor staging path is not a real directory")
    expected = {
        "attempt-ledger.jsonl": attempt_data,
        "recovery-ledger.jsonl": recovery_data,
        SUCCESSOR_NAME: _canonical_json(pointer),
    }
    extras = {member.name for member in stage.iterdir()} - set(expected)
    if extras:
        raise GuardrailViolation("successor staging directory contains unknown members")
    for name, payload in expected.items():
        target = stage / name
        if target.exists() or target.is_symlink():
            existing, _ = _read_regular_bytes(target)
            if target.stat().st_nlink != 1:
                raise GuardrailViolation(f"staged successor member is hardlinked: {name}")
            if existing != payload:
                raise LedgerIntegrityError(f"staged successor member differs: {name}")
        else:
            _write_new_bytes(target, payload)
        target.chmod(0o600 if name.endswith("ledger.jsonl") else 0o644)
    _fsync_directory(stage)


def _rollover_journal_path(history: Path, stop_sha256: str) -> Path:
    return history / f".rollover-{stop_sha256[:12]}.json"


def _validate_rollover_journal(
    journal: Mapping[str, object],
    *,
    expected_stop_sha256: str,
    note: str,
    lineage_rows: Sequence[Mapping[str, object]],
) -> dict[str, Any]:
    required = {
        "schema",
        "campaign_id",
        "expected_stop_sha256",
        "note",
        "stage_name",
        "lineage_row",
    }
    if (
        set(journal) != required
        or journal.get("schema") != ROLLOVER_JOURNAL_SCHEMA
        or journal.get("campaign_id") != CAMPAIGN_ID
        or journal.get("expected_stop_sha256") != expected_stop_sha256
        or journal.get("note") != note
    ):
        raise LedgerIntegrityError("rollover journal does not match this authorization")
    stage_name = journal.get("stage_name")
    if (
        not isinstance(stage_name, str)
        or not stage_name.startswith(".successor-")
        or "/" in stage_name
    ):
        raise LedgerIntegrityError("rollover journal staging name is malformed")
    row = journal.get("lineage_row")
    if not isinstance(row, dict):
        raise LedgerIntegrityError("rollover journal lacks its lineage row")
    row_digest = row.get("entry_sha256")
    committed = bool(
        lineage_rows and lineage_rows[-1].get("entry_sha256") == row_digest
    )
    if committed:
        if dict(lineage_rows[-1]) != row:
            raise LedgerIntegrityError("committed rollover journal differs from lineage")
    else:
        if any(existing.get("entry_sha256") == row_digest for existing in lineage_rows):
            raise LedgerIntegrityError("rollover journal points behind the lineage tip")
        combined = b"".join(_canonical_json(dict(existing)) for existing in lineage_rows)
        parsed = _parse_shift_lineage_bytes(combined + _canonical_json(row))
        if parsed[:-1] != list(lineage_rows) or parsed[-1] != row:
            raise LedgerIntegrityError("rollover journal lineage row is malformed")
    return dict(journal)


def _sync_rollover_rename(history: Path) -> None:
    """Persist a rename crossing the history/canonical directory boundary."""

    _fsync_directory(history)
    _fsync_directory(history.parent)


def _validate_archived_predecessor(
    archive: Path, row: Mapping[str, object]
) -> None:
    if _archive_tree_inventory(archive) != row["archive_tree"]:
        raise LedgerIntegrityError("archived shift changed during rollover")
    snapshot = _stopped_shift_snapshot(archive)
    for name in (
        "ledger",
        "stop",
        "source",
        "attempt_ledger",
        "recovery_ledger",
    ):
        if snapshot[name] != row[name]:
            raise LedgerIntegrityError(
                f"archived predecessor {name} changed during rollover"
            )


def start_successor_shift(
    campaign_dir: str | Path,
    *,
    expected_stop_sha256: str,
    note: str,
) -> dict[str, object]:
    """Archive one stopped shift and durably open its canonical successor."""

    expected_stop_sha256 = _validate_digest(
        "expected_stop_sha256", expected_stop_sha256
    )
    if not isinstance(note, str) or not note.strip():
        raise CampaignError("successor authorization note must be non-empty")
    note = note.strip()
    campaign = Path(os.path.abspath(campaign_dir))
    history = _campaign_history_directory(campaign)
    with campaign_shift_lock(campaign, exclusive=True):
        lineage_path = history / SHIFT_LINEAGE_NAME
        descriptor = os.open(
            lineage_path,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            lineage_rows = _parse_shift_lineage_bytes(
                _read_descriptor_bytes(descriptor)
            )
            journal_path = _rollover_journal_path(history, expected_stop_sha256)
            if journal_path.exists() or journal_path.is_symlink():
                journal_payload = _read_json_object(journal_path)
                if journal_path.read_bytes() != _canonical_json(journal_payload):
                    raise LedgerIntegrityError("rollover journal is not canonical JSON")
                journal = _validate_rollover_journal(
                    journal_payload,
                    expected_stop_sha256=expected_stop_sha256,
                    note=note,
                    lineage_rows=lineage_rows,
                )
                row = dict(journal["lineage_row"])
                stage = history / str(journal["stage_name"])
            else:
                if (
                    lineage_rows
                    and lineage_rows[-1]["stop"]["sha256"]
                    == expected_stop_sha256
                ):
                    state = validate_current_shift(campaign, verify_artifacts=True)
                    return {
                        **state,
                        "archive": state["predecessor_archive"],
                        "already_open": True,
                    }
                state = validate_current_shift(campaign, verify_artifacts=True)
                if int(state["shift_n"]) != len(lineage_rows) + 1:
                    raise LedgerIntegrityError("live shift number differs from lineage")
                snapshot = _stopped_shift_snapshot(campaign)
                if snapshot["stop"]["sha256"] != expected_stop_sha256:
                    raise GuardrailViolation(
                        "canonical STOP SHA256 differs from --expected-stop-sha256"
                    )
                archived_n = len(lineage_rows) + 1
                successor_n = archived_n + 1
                archive_id = f"shift-{archived_n:04d}-{expected_stop_sha256[:12]}"
                inventory = _archive_tree_inventory(campaign)
                policy = {
                    "first_row": (
                        "must_differ"
                        if snapshot["stop"]["category"] == "contract_change_required"
                        else "may_match"
                    ),
                    "forbidden_source_tree_sha256": (
                        snapshot["source"]["source_tree_sha256"]
                        if snapshot["stop"]["category"] == "contract_change_required"
                        else None
                    ),
                }
                material: dict[str, object] = {
                    "schema": SHIFT_LINEAGE_SCHEMA,
                    "campaign_id": CAMPAIGN_ID,
                    "event": "successor_shift_opened",
                    "archived_shift_n": archived_n,
                    "successor_shift_n": successor_n,
                    "archive_id": archive_id,
                    "archive_tree": inventory,
                    "ledger": snapshot["ledger"],
                    "stop": snapshot["stop"],
                    "source": snapshot["source"],
                    "attempt_ledger": snapshot["attempt_ledger"],
                    "recovery_ledger": snapshot["recovery_ledger"],
                    "source_policy": policy,
                    "note": note,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "previous_entry_sha256": (
                        lineage_rows[-1]["entry_sha256"] if lineage_rows else None
                    ),
                }
                row = {**material, "entry_sha256": _lineage_row_hash(material)}
                stage = history / (
                    f".successor-{successor_n:04d}-{row['entry_sha256'][:12]}"
                )
                journal = {
                    "schema": ROLLOVER_JOURNAL_SCHEMA,
                    "campaign_id": CAMPAIGN_ID,
                    "expected_stop_sha256": expected_stop_sha256,
                    "note": note,
                    "stage_name": stage.name,
                    "lineage_row": row,
                }
                _write_new_bytes(journal_path, _canonical_json(journal))
                journal_path.chmod(0o600)
                _fsync_directory(history)

            archive = history / str(row["archive_id"])
            committed = bool(
                lineage_rows
                and lineage_rows[-1]["entry_sha256"] == row["entry_sha256"]
            )
            if not committed and (
                row["previous_entry_sha256"]
                != (lineage_rows[-1]["entry_sha256"] if lineage_rows else None)
            ):
                raise LedgerIntegrityError("rollover journal is not next in lineage")
            successor_installed = False
            if campaign.exists() or campaign.is_symlink():
                _assert_no_symlinks(campaign)
                if not campaign.is_dir():
                    raise LedgerIntegrityError("canonical C-001 path is not a directory")
                successor_path = campaign / SUCCESSOR_NAME
                if committed and (
                    successor_path.exists() or successor_path.is_symlink()
                ):
                    _validate_successor_pointer(successor_path, row)
                    successor_installed = True
                elif archive.exists():
                    raise LedgerIntegrityError(
                        "predecessor and archive both exist during rollover"
                    )
                elif committed:
                    raise LedgerIntegrityError(
                        "committed rollover left an unexpected canonical directory"
                    )

            if not successor_installed:
                predecessor = archive if archive.exists() else campaign
                if not predecessor.exists():
                    raise LedgerIntegrityError("rollover predecessor disappeared")
                _prepare_successor_stage(stage, predecessor, row)
                _fsync_directory(history)

            if not committed:
                try:
                    if not archive.exists():
                        if successor_installed or not campaign.exists():
                            raise LedgerIntegrityError("rollover predecessor disappeared")
                        if _archive_tree_inventory(campaign) != row["archive_tree"]:
                            raise LedgerIntegrityError(
                                "predecessor changed after rollover approval"
                            )
                        os.rename(campaign, archive)
                        _sync_rollover_rename(history)
                    _validate_archived_predecessor(archive, row)
                    _append_shift_lineage_row(descriptor, row)
                except Exception:
                    replayed = _parse_shift_lineage_bytes(
                        _read_descriptor_bytes(descriptor)
                    )
                    committed = bool(
                        replayed
                        and replayed[-1]["entry_sha256"] == row["entry_sha256"]
                    )
                    if not committed and not campaign.exists() and archive.exists():
                        os.rename(archive, campaign)
                        _sync_rollover_rename(history)
                    raise
                committed = True
            else:
                if not archive.exists():
                    raise LedgerIntegrityError("committed rollover archive is missing")
                _validate_archived_predecessor(archive, row)

            if not successor_installed:
                if campaign.exists():
                    raise LedgerIntegrityError("canonical successor path is occupied")
                if not stage.is_dir() or stage.is_symlink():
                    raise LedgerIntegrityError("staged successor is missing after commit")
                os.rename(stage, campaign)
                _sync_rollover_rename(history)
            state = validate_current_shift(
                campaign,
                verify_artifacts=True,
                _allowed_history_members=frozenset({journal_path.name}),
            )
            journal_path.unlink()
            _fsync_directory(history)
            return {
                **state,
                "archive": str(archive),
                "already_open": False,
            }
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _enforce_successor_source_policy(
    ledger: Path, subject_to: Mapping[str, object]
) -> None:
    pointer_path = ledger.parent / SUCCESSOR_NAME
    if not pointer_path.exists() and not pointer_path.is_symlink():
        return
    pointer = _read_json_object(pointer_path)
    if set(pointer) != {
        "schema",
        "campaign_id",
        "shift_n",
        "lineage_entry_sha256",
    } or pointer.get("schema") != SUCCESSOR_SCHEMA:
        raise LedgerIntegrityError("successor pointer is malformed")
    lineage = _read_shift_lineage(ledger.parent)
    if not lineage or lineage[-1]["entry_sha256"] != pointer["lineage_entry_sha256"]:
        raise LedgerIntegrityError("successor pointer is not the live lineage tip")
    policy = lineage[-1]["source_policy"]
    if (
        policy["first_row"] == "must_differ"
        and subject_to.get("source_tree_sha256")
        == policy["forbidden_source_tree_sha256"]
    ):
        raise SubjectToViolation("successor Phase 1.1 must use a new source tree")


def append_ledger(
    ledger_path: str | Path,
    artifact_path: str | Path,
    artifact_sha256: str,
    *,
    hypothesis: str,
    knobs: Mapping[str, object],
    seed: int,
    board: str,
    result: Mapping[str, object] | None = None,
    verdict: str,
    note: str,
    wall_minutes: float,
    subject_to: Mapping[str, object],
    phase: str,
    phase1_step: int | None = None,
    power_cycles: int | None = None,
    wedges: int = 0,
    n: int | None = None,
) -> dict[str, Any]:
    """Append one hash-chained row after validating its retained artifact."""

    ledger = Path(ledger_path)
    stopped = read_campaign_stop(ledger)
    if stopped is not None:
        raise GuardrailViolation(
            f"shift is stopped by {stopped['category']}: {stopped['reason']}"
        )
    artifact = Path(artifact_path)
    expected_artifact_digest = _validate_digest("artifact_sha256", artifact_sha256)
    actual_artifact_digest = sha256_file(artifact)
    if actual_artifact_digest != expected_artifact_digest:
        raise LedgerIntegrityError("artifact does not match the supplied SHA256")
    artifact_payload = _read_json_object(artifact)
    attempt_budget: dict[str, object] | None = None
    recovery_budget: dict[str, object] | None = None
    if artifact_payload.get("schema") == SCORE_SCHEMA:
        validate_retained_score_bundle(artifact, artifact_payload)
    elif artifact_payload.get("schema") == HOST_SCHEMA:
        validate_retained_host_bundle(artifact, artifact_payload)
    elif artifact_payload.get("schema") in {
        LEGACY_BUG_VERIFICATION_SCHEMA,
        BUG_VERIFICATION_SCHEMA,
    }:
        validate_bug_verification_bundle(artifact, artifact_payload)
    artifact_result = artifact_payload.get("result", artifact_payload.get("summary"))
    if not isinstance(artifact_result, Mapping):
        raise LedgerIntegrityError("ledger artifact contains neither result nor summary")
    if result is not None and dict(artifact_result) != dict(result):
        raise LedgerIntegrityError("ledger result must equal the retained artifact result")
    result = dict(artifact_result)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlinks(ledger.parent)
    _assert_no_symlinks(ledger, missing_leaf_ok=True)
    resolved_parent = ledger.parent.resolve(strict=True)
    try:
        relative_artifact = artifact.resolve(strict=True).relative_to(resolved_parent)
    except ValueError as exc:
        raise LedgerIntegrityError(
            "artifact must be retained beneath the ledger directory"
        ) from exc
    if artifact.resolve(strict=True) == ledger.resolve(strict=False):
        raise LedgerIntegrityError("the ledger cannot be its own scored artifact")
    if not hypothesis.strip() or not board.strip():
        raise CampaignError("hypothesis and board identity must be non-empty")
    try:
        normalized = normalize_knobs(knobs)
    except CampaignError as exc:
        record_campaign_stop(
            ledger,
            category="knob_violation",
            reason=str(exc),
            source_artifact_sha256=expected_artifact_digest,
        )
        raise
    try:
        seed = validate_seed(seed)
    except CampaignError as exc:
        record_campaign_stop(
            ledger,
            category="subject_to_violation",
            reason=str(exc),
            source_artifact_sha256=expected_artifact_digest,
        )
        raise SubjectToViolation(str(exc)) from exc
    if "seed" in artifact_payload and artifact_payload["seed"] != seed:
        reason = "ledger seed disagrees with the retained artifact"
        record_campaign_stop(
            ledger,
            category="subject_to_violation",
            reason=reason,
            source_artifact_sha256=expected_artifact_digest,
        )
        raise SubjectToViolation(reason)
    if "knobs" in artifact_payload and normalize_knobs(artifact_payload["knobs"]) != normalized:
        reason = "ledger knobs disagree with the retained artifact"
        record_campaign_stop(
            ledger,
            category="subject_to_violation",
            reason=reason,
            source_artifact_sha256=expected_artifact_digest,
        )
        raise SubjectToViolation(reason)
    if "board" in artifact_payload and artifact_payload["board"] != board.strip():
        reason = "ledger board disagrees with the retained artifact"
        record_campaign_stop(
            ledger,
            category="subject_to_violation",
            reason=reason,
            source_artifact_sha256=expected_artifact_digest,
        )
        raise SubjectToViolation(reason)
    if not isinstance(subject_to, Mapping):
        reason = "subject-to evidence must be an object"
        record_campaign_stop(
            ledger,
            category="subject_to_violation",
            reason=reason,
            source_artifact_sha256=expected_artifact_digest,
        )
        raise SubjectToViolation(reason)
    if verdict not in {
        "measurement",
        "improved",
        "unchanged",
        "regressed",
        "candidate",
        "confirmed",
        "failed",
    }:
        raise CampaignError(f"unknown experiment verdict {verdict!r}")
    wall_minutes = _require_finite(
        "wall_minutes", wall_minutes, 0.0, MAX_EXPERIMENT_MINUTES
    )
    wall_minutes = _bind_artifact_wall_minutes(
        artifact_payload, wall_minutes, retained_chain=False
    )
    if artifact_payload.get("schema") == SCORE_SCHEMA:
        provision_block = artifact_payload.get("provision")
        if not isinstance(provision_block, Mapping):
            raise LedgerIntegrityError("score lacks retained provision recovery evidence")
        derived_power_cycles = _require_int(
            "score provision power_cycles",
            provision_block.get("power_cycles"),
            0,
            MAX_RECOVERY_CYCLES_PER_BOARD,
        )
        if power_cycles is not None and power_cycles != derived_power_cycles:
            raise LedgerIntegrityError(
                "caller power_cycles differs from retained recovery evidence"
            )
        power_cycles = derived_power_cycles
        attempt_budget = {
            "attempt_n": provision_block.get("attempt_n"),
            "attempts_reserved": provision_block.get("attempts_reserved"),
            "tip_sha256": provision_block.get("attempt_ledger_tip_sha256"),
        }
        recovery_budget = {
            "shift_cycles": provision_block.get("recovery_shift_cycles"),
            "board_cycles": provision_block.get("recovery_board_cycles"),
            "tip_sha256": provision_block.get("recovery_ledger_tip_sha256"),
        }
    else:
        if power_cycles not in {None, 0}:
            raise LedgerIntegrityError(
                "non-score Phase-1 evidence cannot self-assert power cycles"
            )
        power_cycles = 0
    wedges = _require_int("wedges", wedges, 0, MAX_WEDGES_PER_EXPERIMENT)
    if wedges >= MAX_WEDGES_PER_EXPERIMENT:
        record_campaign_stop(
            ledger,
            category="wedge_limit",
            reason="one experiment wedged a board twice",
            source_artifact_sha256=expected_artifact_digest,
        )
        raise GuardrailViolation("one experiment wedged a board twice; shift stopped")
    if result.get("contract_change_required") is True:
        record_campaign_stop(
            ledger,
            category="contract_change_required",
            reason="retained score contains an unmodelled CCL failure class",
            source_artifact_sha256=expected_artifact_digest,
        )
        raise GuardrailViolation("surprise requires a contract change; shift stopped")
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger, flags, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        stopped = read_campaign_stop(ledger)
        if stopped is not None:
            raise GuardrailViolation(
                f"shift is stopped by {stopped['category']}: {stopped['reason']}"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        existing = _parse_ledger_bytes(b"".join(chunks), ledger)
        existing = validate_ledger_rows(
            existing, ledger_path=ledger, verify_artifacts=True
        )
        status = evaluate_shift(existing)
        if not status.can_continue:
            raise GuardrailViolation(f"shift is stopped: {status.stop_reasons}")
        expected_n = len(existing) + 1
        if expected_n > MAX_EXPERIMENTS:
            raise GuardrailViolation("40-experiment shift budget is exhausted")
        if n is not None and n != expected_n:
            raise LedgerIntegrityError(f"next ledger n is {expected_n}, not {n}")
        if expected_n == 1:
            _enforce_successor_source_policy(ledger, subject_to)
        if phase == "phase1":
            if phase1_step != expected_n or not 1 <= expected_n <= 4:
                raise GuardrailViolation("Phase 1 must ledger ordered steps n=1..4")
            if normalized:
                raise GuardrailViolation("Phase 1 measurements must use frozen default knobs")
            validate_phase1_artifact(
                expected_n, artifact_payload, artifact_path=artifact
            )
        if any(row["artifact"]["path"] == relative_artifact.as_posix() for row in existing):
            raise LedgerIntegrityError("a retained artifact may be ledgered only once")

        # Re-hash while holding the ledger lock.  The no-symlink path and
        # second digest check close the ordinary rename/edit race before the
        # O_APPEND write.
        if sha256_file(artifact) != expected_artifact_digest:
            raise LedgerIntegrityError("artifact changed before ledger append")
        previous = existing[-1]["entry_sha256"] if existing else None
        row: dict[str, Any] = {
            "n": expected_n,
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "phase1_step": phase1_step if phase == "phase1" else None,
            "hypothesis": hypothesis.strip(),
            "knobs": normalized,
            "seed": seed,
            "board": board.strip(),
            "identity": (
                artifact_payload.get("run_binding", {}).get("identity")
                if artifact_payload.get("schema") == SCORE_SCHEMA
                else None
            ),
            "probe_semantics": (
                artifact_payload.get("probe_semantics")
                if artifact_payload.get("schema") == SCORE_SCHEMA
                else None
            ),
            "subject_to": dict(subject_to),
            "artifact": {
                "path": relative_artifact.as_posix(),
                "sha256": expected_artifact_digest,
            },
            "result": dict(result),
            "verdict": verdict,
            "note": note,
            "wall_minutes": wall_minutes,
            "power_cycles": power_cycles,
            "attempt_budget": attempt_budget,
            "recovery_budget": recovery_budget,
            "wedges": wedges,
            "previous_entry_sha256": previous,
        }
        row["entry_sha256"] = _entry_hash(row)
        # Re-run the complete validator on the prospective chain while the
        # append lock is held.  This is the single authority for Phase-1
        # cross-binding, derived verdicts, zero-failure confirmation, and
        # confirmation termination.
        try:
            validate_ledger_rows(
                [*existing, row], ledger_path=ledger, verify_artifacts=True
            )
        except SubjectToViolation as exc:
            record_campaign_stop(
                ledger,
                category="subject_to_violation",
                reason=str(exc),
                source_artifact_sha256=expected_artifact_digest,
                _lock_held=True,
            )
            raise
        line = _canonical_json(row)
        written = os.write(descriptor, line)
        if written != len(line):  # one short O_APPEND write is a torn ledger
            raise LedgerIntegrityError("short atomic ledger append")
        os.fsync(descriptor)
        return row
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class ShiftStatus:
    experiments: int
    power_cycles: int
    consecutive_regressions: int
    stop_reasons: tuple[str, ...]

    @property
    def can_continue(self) -> bool:
        return not self.stop_reasons

    def as_dict(self) -> dict[str, object]:
        return {
            "experiments": self.experiments,
            "power_cycles": self.power_cycles,
            "consecutive_regressions": self.consecutive_regressions,
            "can_continue": self.can_continue,
            "stop_reasons": list(self.stop_reasons),
        }


def evaluate_shift(
    rows: Sequence[Mapping[str, object]],
    *,
    extra_power_cycles: int = 0,
    wedge_events: Mapping[int, int] | None = None,
    clean_host: bool = False,
    subject_to_violation: bool = False,
    contract_change_required: bool = False,
    board_unreachable_after_two_cycles: bool = False,
) -> ShiftStatus:
    """Evaluate every C-001 hard stop without mutating a board or ledger."""

    physical_attempts = max(
        (
            int(row.get("attempt_budget", {}).get("attempts_reserved", 0))
            for row in rows
            if isinstance(row.get("attempt_budget"), Mapping)
        ),
        default=0,
    )
    non_physical_rows = sum(
        not isinstance(row.get("attempt_budget"), Mapping) for row in rows
    )
    experiments = physical_attempts + non_physical_rows
    scored_cycles = extra_power_cycles + sum(
        int(row.get("power_cycles", 0)) for row in rows
    )
    reserved_cycles = max(
        (
            int(row.get("recovery_budget", {}).get("shift_cycles", 0))
            for row in rows
            if isinstance(row.get("recovery_budget"), Mapping)
        ),
        default=0,
    )
    cycles = max(scored_cycles, reserved_cycles)
    consecutive = 0
    best_rate: float | None = None
    for row in rows:
        result = row.get("result")
        rate = result.get("detector_fail_rate") if isinstance(result, Mapping) else None
        if (
            isinstance(rate, (int, float))
            and not isinstance(rate, bool)
            and math.isfinite(float(rate))
        ):
            numeric_rate = float(rate)
            if best_rate is not None and numeric_rate > best_rate and not math.isclose(
                numeric_rate, best_rate, abs_tol=1e-15
            ):
                consecutive += 1
            else:
                consecutive = 0
            best_rate = numeric_rate if best_rate is None else min(best_rate, numeric_rate)
        elif row.get("verdict") == "regressed":
            # Retain utility for synthetic/manual status checks without scored
            # results; ledgered score verdicts are independently validated.
            consecutive += 1
        elif row.get("phase") != "phase1":
            consecutive = 0
    wedges_by_n = {int(key): int(value) for key, value in (wedge_events or {}).items()}
    derived_clean_host = False
    derived_subject_violation = False
    derived_contract_change = False
    derived_unreachable = False
    for row in rows:
        wedges_by_n[int(row.get("n", 0))] = max(
            wedges_by_n.get(int(row.get("n", 0)), 0), int(row.get("wedges", 0))
        )
        result = row.get("result")
        if isinstance(result, Mapping):
            derived_clean_host |= result.get("clean_host") is True
            derived_subject_violation |= result.get("subject_to_violation") is True
            derived_contract_change |= result.get("contract_change_required") is True
            derived_unreachable |= result.get("board_unreachable_after_two_cycles") is True
    reasons: list[str] = []
    if experiments >= MAX_EXPERIMENTS:
        reasons.append("40 experiments exhausted")
    if cycles >= MAX_POWER_CYCLES:
        reasons.append("6 PoE cycles exhausted")
    if consecutive >= MAX_CONSECUTIVE_REGRESSIONS:
        reasons.append("8 consecutive regressions")
    if any(count >= MAX_WEDGES_PER_EXPERIMENT for count in wedges_by_n.values()):
        reasons.append("one experiment wedged a board twice")
    if clean_host or derived_clean_host:
        reasons.append("clean host discriminator: climb whitelist is void pending re-scope")
    if subject_to_violation or derived_subject_violation:
        reasons.append("subject-to violation")
    if contract_change_required or derived_contract_change:
        reasons.append("surprise requires a contract change")
    if board_unreachable_after_two_cycles or derived_unreachable:
        reasons.append("board unreachable after two recovery cycles")
    if len(rows) >= 4 and all(
        row.get("phase") == "phase1" and row.get("phase1_step") == index
        for index, row in enumerate(rows[:4], 1)
    ):
        phase3_summary = rows[2].get("result")
        paired = (
            phase3_summary.get("paired_discriminator")
            if isinstance(phase3_summary, Mapping)
            else None
        )
        phase4_result = rows[3].get("result")
        climb_authorized = isinstance(paired, Mapping) and paired.get(
            "discriminator_allows_climb"
        ) is True
        zero_failure_confirmation = (
            isinstance(paired, Mapping)
            and paired.get("zero_failure_candidate_path") is True
            and isinstance(phase4_result, Mapping)
            and phase4_result.get("ccl_label_failures") == 0
            and result_is_win(phase4_result)
        )
        if not climb_authorized and not zero_failure_confirmation:
            reasons.append("completed Phase 1 did not authorize climb; re-scope required")
    confirmation = _confirmation_from_rows(rows)
    if confirmation is not None and confirmation.get("confirmed") is True:
        reasons.append("campaign win confirmed pending planning-session ratification")
    return ShiftStatus(experiments, cycles, consecutive, tuple(reasons))


@dataclass(frozen=True)
class ConfirmationRun:
    n: int
    seed: int
    board: str
    knobs: Mapping[str, object]
    detector_fail_rate: float
    truth_points: int
    matched_truth_points: int
    raw_truth_points: int
    raw_matched_truth_points: int
    mac: str
    image_marker: str

    def __post_init__(self) -> None:
        validate_seed(self.seed)
        normalize_knobs(self.knobs)
        if not self.board.strip():
            raise CampaignError("confirmation board must be non-empty")
        BoardIdentity(self.board, self.mac, self.image_marker)
        if not math.isfinite(self.detector_fail_rate) or self.detector_fail_rate < 0:
            raise CampaignError("confirmation fail rate must be finite and non-negative")
        _require_int("truth_points", self.truth_points, 1)
        _require_int("matched_truth_points", self.matched_truth_points, 0)
        if self.matched_truth_points > self.truth_points:
            raise CampaignError("matched truth points exceeds truth points")
        _require_int("raw_truth_points", self.raw_truth_points, 1)
        _require_int("raw_matched_truth_points", self.raw_matched_truth_points, 0)
        if self.raw_matched_truth_points > self.raw_truth_points:
            raise CampaignError("raw matched truth points exceeds raw truth points")

    @property
    def passes(self) -> bool:
        return (
            self.detector_fail_rate <= WIN_FAIL_RATE
            and self.matched_truth_points == self.truth_points
            and self.raw_matched_truth_points == self.raw_truth_points
        )


@dataclass
class ConfirmationTracker:
    """Track anti-overfit confirmations; D0 always remains pending."""

    candidate: ConfirmationRun
    fresh_seed_runs: list[int] = field(default_factory=list)
    second_board_runs: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.candidate.passes:
            raise CampaignError("the provisional candidate does not meet the win condition")

    def observe(self, run: ConfirmationRun) -> None:
        if run.n == self.candidate.n:
            return
        if normalize_knobs(run.knobs) != normalize_knobs(self.candidate.knobs) or not run.passes:
            return
        if run.seed != self.candidate.seed:
            self.fresh_seed_runs.append(run.n)
        if run.mac.lower() != self.candidate.mac.lower():
            self.second_board_runs.append(run.n)

    @property
    def confirmed(self) -> bool:
        return bool(self.fresh_seed_runs and self.second_board_runs)

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_n": self.candidate.n,
            "fresh_seed_confirmed": bool(self.fresh_seed_runs),
            "fresh_seed_runs": sorted(set(self.fresh_seed_runs)),
            "second_board_confirmed": bool(self.second_board_runs),
            "second_board_runs": sorted(set(self.second_board_runs)),
            "confirmed": self.confirmed,
            "status": (
                "confirmed_pending_planning_session_ratification"
                if self.confirmed
                else "provisional_pending_confirmations"
            ),
            "d0_ratified": False,
        }


@dataclass(frozen=True)
class BoardIdentity:
    board: str
    mac: str
    image_marker: str

    def __post_init__(self) -> None:
        if not self.board.strip() or not self.image_marker.strip():
            raise CampaignError("board label and image marker must be non-empty")
        if _MAC.fullmatch(self.mac) is None:
            raise CampaignError("board MAC must use six colon-separated octets")

    def normalized(self) -> tuple[str, str, str]:
        return self.board.strip(), self.mac.lower(), self.image_marker.strip()


@runtime_checkable
class PoERecovery(Protocol):
    """External rig adapter.  Implementations own credentials and I/O."""

    def cycle_port(self, expected: BoardIdentity) -> None: ...

    def wait_for_boot(self, expected: BoardIdentity, timeout_s: float) -> bool: ...

    def read_identity(self, expected: BoardIdentity) -> BoardIdentity: ...


class RecoveryState(str, Enum):
    IDLE = "idle"
    CYCLING = "cycling"
    WAITING_FOR_BOOT = "waiting_for_boot"
    REVALIDATING_IDENTITY = "revalidating_identity"
    READY = "ready"
    EXCLUDED = "excluded"


@dataclass
class RecoveryStateMachine:
    """Pure transition model for external PoE recovery and identity checks."""

    expected: BoardIdentity
    shift_cycles_used: int = 0
    board_cycles_used: int = 0
    state: RecoveryState = RecoveryState.IDLE
    reason: str | None = None

    def begin_cycle(self) -> None:
        if self.state not in {RecoveryState.IDLE}:
            raise GuardrailViolation(f"cannot begin a cycle from {self.state.value}")
        if self.shift_cycles_used >= MAX_POWER_CYCLES:
            self.state = RecoveryState.EXCLUDED
            self.reason = "six-cycle shift budget exhausted"
            raise GuardrailViolation(self.reason)
        if self.board_cycles_used >= MAX_RECOVERY_CYCLES_PER_BOARD:
            self.state = RecoveryState.EXCLUDED
            self.reason = "board unreachable after two recovery cycles"
            raise GuardrailViolation(self.reason)
        self.shift_cycles_used += 1
        self.board_cycles_used += 1
        self.state = RecoveryState.CYCLING

    def cycle_dispatched(self) -> None:
        if self.state is not RecoveryState.CYCLING:
            raise GuardrailViolation("cycle completion arrived out of order")
        self.state = RecoveryState.WAITING_FOR_BOOT

    def boot_result(self, reachable: bool) -> None:
        if self.state is not RecoveryState.WAITING_FOR_BOOT:
            raise GuardrailViolation("boot result arrived out of order")
        if reachable:
            self.state = RecoveryState.REVALIDATING_IDENTITY
        elif self.board_cycles_used >= MAX_RECOVERY_CYCLES_PER_BOARD:
            self.state = RecoveryState.EXCLUDED
            self.reason = "board unreachable after two recovery cycles"
        else:
            self.state = RecoveryState.IDLE
            self.reason = "board still unreachable; one recovery attempt remains"

    def identity_result(self, actual: BoardIdentity) -> None:
        if self.state is not RecoveryState.REVALIDATING_IDENTITY:
            raise GuardrailViolation("identity result arrived out of order")
        if actual.normalized() != self.expected.normalized():
            self.state = RecoveryState.EXCLUDED
            self.reason = "MAC/image identity mismatch after recovery; board excluded"
        else:
            self.state = RecoveryState.READY
            self.reason = None

    def reset_ready(self) -> None:
        if self.state is not RecoveryState.READY:
            raise GuardrailViolation("only a revalidated ready board may return to idle")
        self.state = RecoveryState.IDLE


def _json_argument(value: str) -> dict[str, Any]:
    source = value[1:] if value.startswith("@") else None
    try:
        payload = json.loads(Path(source).read_text(encoding="utf-8") if source else value)
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"invalid JSON argument {value!r}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CampaignError("JSON argument must be an object")
    return payload


def _confirmation_from_rows(rows: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    candidate_row = next((row for row in rows if row.get("verdict") == "candidate"), None)
    if candidate_row is None:
        return None

    def convert(row: Mapping[str, object]) -> ConfirmationRun | None:
        result = row.get("result")
        if not isinstance(result, Mapping):
            return None
        recall = result.get("mover_recall")
        raw_recall = result.get("raw_component_mover_recall")
        identity = row.get("identity")
        if (
            not isinstance(recall, Mapping)
            or not isinstance(raw_recall, Mapping)
            or not isinstance(identity, Mapping)
        ):
            return None
        try:
            return ConfirmationRun(
                n=int(row["n"]),
                seed=int(row["seed"]),
                board=str(row["board"]),
                knobs=row.get("knobs", {}),
                detector_fail_rate=float(result["detector_fail_rate"]),
                truth_points=int(recall["truth_points"]),
                matched_truth_points=int(recall["matched"]),
                raw_truth_points=int(raw_recall["truth_points"]),
                raw_matched_truth_points=int(raw_recall["matched"]),
                mac=str(identity["mac"]),
                image_marker=str(identity["image_marker"]),
            )
        except (KeyError, TypeError, ValueError, CampaignError):
            return None

    candidate = convert(candidate_row)
    candidate_semantics = candidate_row.get("probe_semantics")
    if candidate is None or not candidate.passes or not isinstance(
        candidate_semantics, Mapping
    ):
        return None
    tracker = ConfirmationTracker(candidate)
    for row in rows:
        if row is not candidate_row and row.get("phase") != "confirmation":
            continue
        if row.get("probe_semantics") != candidate_semantics:
            continue
        run = convert(row)
        if run is not None:
            tracker.observe(run)
    return tracker.as_dict()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="C-001 guarded campaign runner")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="prepare an immutable RAM-loop probe")
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--kind", choices=("benchmark", "sparse"), required=True)
    prepare.add_argument("--seed", required=True, type=int)
    prepare.add_argument("--total-frames", type=int, default=MIN_TOTAL_FRAMES)

    host = commands.add_parser("host", help="run the exact-loop host discriminator")
    host.add_argument("--manifest", required=True)
    host.add_argument("--out", required=True)
    host.add_argument("--board", help="required label when --board-ccl-log is supplied")
    host.add_argument("--knobs", default="{}", help="JSON object or @file")
    host.add_argument(
        "--board-fg-masks",
        help="optional SWFM/1 failed-frame masks to align/diff against the host",
    )
    host.add_argument(
        "--board-ccl-log",
        help="paired board CCL JSONL required to derive a clean-host decision",
    )

    score = commands.add_parser("score", help="score and retain a bound board run")
    score.add_argument("--stats", required=True)
    score.add_argument("--ccl-log", required=True)
    score.add_argument("--packets", required=True)
    score.add_argument("--manifest", required=True)
    score.add_argument("--run-binding", required=True)
    score.add_argument("--provision", required=True)
    score.add_argument("--exit-status", required=True)
    score.add_argument("--run-log", required=True)
    score.add_argument("--board", required=True)
    score.add_argument("--knobs", required=True, help="JSON object or @file")
    score.add_argument("--out", required=True)
    score.add_argument("--board-fg-masks")

    record = commands.add_parser("record", help="append a retained artifact to the ledger")
    record.add_argument("--ledger", required=True)
    record.add_argument("--artifact", required=True)
    record.add_argument("--artifact-sha256", required=True)
    record.add_argument("--hypothesis", required=True)
    record.add_argument("--knobs", required=True, help="JSON object or @file")
    record.add_argument("--seed", required=True, type=int)
    record.add_argument("--board", required=True)
    record.add_argument("--verdict", required=True)
    record.add_argument("--note", default="")
    record.add_argument("--wall-minutes", required=True, type=float)
    record.add_argument("--subject-to", required=True, help="JSON object or @file")
    record.add_argument("--phase", choices=("phase1", "climb", "confirmation"), required=True)
    record.add_argument("--phase1-step", type=int)
    record.add_argument(
        "--power-cycles",
        type=int,
        default=None,
        help="optional assertion; SCORE rows derive this from retained recovery evidence",
    )
    record.add_argument("--wedges", type=int, default=0)
    record.add_argument("--n", type=int)

    status = commands.add_parser("status", help="validate and summarize a campaign ledger")
    status.add_argument("--ledger", required=True)
    status.add_argument("--skip-artifact-check", action="store_true")

    stop = commands.add_parser("stop", help="persist an immutable terminal incident")
    stop.add_argument("--ledger", required=True)
    stop.add_argument(
        "--category",
        required=True,
        choices=(
            "subject_to_violation",
            "knob_violation",
            "contract_change_required",
            "board_unreachable_after_two_cycles",
            "wedge_limit",
            "operator_stop",
        ),
    )
    stop.add_argument("--reason", required=True)
    stop.add_argument("--source-artifact-sha256")

    successor = commands.add_parser(
        "successor", help="archive a stopped shift and open its successor"
    )
    successor.add_argument("--campaign-dir", required=True)
    successor.add_argument("--expected-stop-sha256", required=True)
    successor.add_argument("--note", required=True)
    return parser


def _canonical_campaign_directory() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "campaigns" / CAMPAIGN_ID


def _validate_production_cli_scope(args: argparse.Namespace) -> None:
    """Keep the production CLI on the one authoritative campaign chain."""

    campaign_dir = Path(os.path.abspath(_canonical_campaign_directory()))
    if args.command == "successor":
        requested = Path(os.path.abspath(args.campaign_dir))
        if requested != campaign_dir:
            raise GuardrailViolation(
                "production successor command requires the canonical C-001 directory"
            )
        return
    campaign_dir = campaign_dir.resolve(strict=True)
    if args.command in {"record", "status", "stop"}:
        ledger = Path(args.ledger).resolve(strict=False)
        if ledger != campaign_dir / "ledger.jsonl":
            raise GuardrailViolation(
                "production C-001 CLI requires the canonical campaign ledger.jsonl"
            )
    if args.command in {"prepare", "host", "score"}:
        output = Path(args.out).resolve(strict=False)
        try:
            relative = output.relative_to(campaign_dir)
        except ValueError as exc:
            raise GuardrailViolation(
                "production C-001 outputs must remain under the canonical campaign directory"
            ) from exc
        if not relative.parts:
            raise GuardrailViolation("a campaign output may not replace the campaign directory")


def _run_cli_command(args: argparse.Namespace) -> None:
    if args.command == "prepare":
        prepared = prepare_probe(
            args.out, kind=args.kind, seed=args.seed, total_frames=args.total_frames
        )
        print(
            json.dumps(
                {
                    "manifest": str(prepared.manifest_path),
                    "manifest_sha256": prepared.manifest_sha256,
                    "clip_sha256": prepared.clip_sha256,
                    "truth_sha256": prepared.truth_sha256,
                },
                sort_keys=True,
            )
        )
        return
    if args.command == "host":
        artifact = run_host_discriminator(
            args.manifest,
            args.out,
            knobs=_json_argument(args.knobs),
            board=args.board,
            board_fg_masks_path=args.board_fg_masks,
            board_ccl_log_path=args.board_ccl_log,
        )
        print(json.dumps({"artifact": str(artifact.path), "sha256": artifact.sha256}))
        return
    if args.command == "score":
        artifact = score_board_run(
            args.stats,
            args.ccl_log,
            args.packets,
            args.manifest,
            args.run_binding,
            board=args.board,
            knobs=_json_argument(args.knobs),
            output_path=args.out,
            board_fg_masks_path=args.board_fg_masks,
            provision_path=args.provision,
            exit_status_path=args.exit_status,
            run_log_path=args.run_log,
        )
        assert isinstance(artifact, WrittenArtifact)
        print(json.dumps({"artifact": str(artifact.path), "sha256": artifact.sha256}))
        return
    if args.command == "record":
        artifact_payload = _read_json_object(Path(args.artifact))
        artifact_result = artifact_payload.get("result", artifact_payload.get("summary"))
        if not isinstance(artifact_result, Mapping):
            raise CampaignError("artifact contains neither a result nor host summary object")
        row = append_ledger(
            args.ledger,
            args.artifact,
            args.artifact_sha256,
            hypothesis=args.hypothesis,
            knobs=_json_argument(args.knobs),
            seed=args.seed,
            board=args.board,
            verdict=args.verdict,
            note=args.note,
            wall_minutes=args.wall_minutes,
            subject_to=_json_argument(args.subject_to),
            phase=args.phase,
            phase1_step=args.phase1_step,
            power_cycles=args.power_cycles,
            wedges=args.wedges,
            n=args.n,
        )
        print(json.dumps(row, sort_keys=True))
        return
    if args.command == "status":
        rows = read_ledger(args.ledger, verify_artifacts=not args.skip_artifact_check)
        shift = evaluate_shift(rows).as_dict()
        stop_marker = read_campaign_stop(args.ledger)
        if stop_marker is not None:
            shift["can_continue"] = False
            shift["stop_reasons"] = [
                *shift["stop_reasons"],
                f"{stop_marker['category']}: {stop_marker['reason']}",
            ]
        payload: dict[str, object] = {
            "shift": shift,
            "artifact_verification": "skipped" if args.skip_artifact_check else "verified",
        }
        if stop_marker is not None:
            payload["stop"] = stop_marker
        if args.skip_artifact_check:
            payload["confirmation"] = {
                "status": "omitted_unverified_artifacts",
                "confirmed": False,
                "d0_ratified": False,
            }
        else:
            confirmation = _confirmation_from_rows(rows)
            if confirmation is not None:
                payload["confirmation"] = confirmation
        print(json.dumps(payload, sort_keys=True))
        return
    if args.command == "stop":
        payload = record_campaign_stop(
            args.ledger,
            category=args.category,
            reason=args.reason,
            source_artifact_sha256=args.source_artifact_sha256,
        )
        print(json.dumps(payload, sort_keys=True))
        return
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    _validate_production_cli_scope(args)
    campaign = _canonical_campaign_directory()
    if args.command == "successor":
        state = start_successor_shift(
            args.campaign_dir,
            expected_stop_sha256=args.expected_stop_sha256,
            note=args.note,
        )
        print(json.dumps(state, sort_keys=True))
        return
    with campaign_shift_lock(campaign):
        validate_current_shift(
            campaign,
            verify_artifacts=not (
                args.command == "status" and args.skip_artifact_check
            ),
        )
        _run_cli_command(args)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "BoardIdentity",
    "CAMPAIGN_ID",
    "SHIFT_HISTORY_DIRECTORY",
    "SHIFT_LINEAGE_NAME",
    "SHIFT_LINEAGE_SCHEMA",
    "SUCCESSOR_NAME",
    "SUCCESSOR_SCHEMA",
    "CampaignError",
    "ConfirmationRun",
    "ConfirmationTracker",
    "GuardrailViolation",
    "LedgerIntegrityError",
    "PoERecovery",
    "PreparedProbe",
    "RecoveryState",
    "RecoveryStateMachine",
    "ShiftStatus",
    "WrittenArtifact",
    "aggregate_ccl_rows",
    "append_ledger",
    "campaign_shift_lock",
    "classify_ccl_failure",
    "compare_fg_masks",
    "compute_objective",
    "detector_config_for",
    "evaluate_shift",
    "iter_looped_probe_frames",
    "load_ccl_log",
    "load_failed_fg_masks",
    "load_probe_manifest",
    "load_observation_rows",
    "load_truth_slots",
    "main",
    "normalize_knobs",
    "overlapping_bbox_pairs",
    "prepare_probe",
    "read_ledger",
    "result_is_win",
    "run_host_discriminator",
    "score_board_run",
    "score_mover_recall",
    "sha256_file",
    "subject_to_template",
    "start_successor_shift",
    "summarize_host_rows",
    "validate_frozen_settings",
    "validate_ledger_rows",
    "validate_current_shift",
    "validate_seed",
    "validate_subject_to",
]
