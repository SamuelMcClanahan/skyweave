"""Identity-bound board execution for the C-001 campaign.

``campaign_c001`` owns probe validation, the detector-knob guardrail, and the
schemas consumed by scoring.  This module is the deliberately small rig edge:
it proves which board is reachable, provisions the exact RAM-loop clip, and
retains the two proof objects that make the resulting raw files scoreable.

There is no PoE-switch implementation here.  A caller may inject the existing
``PoERecovery`` protocol, whose implementation owns credentials and network
I/O, only together with a shared locked recovery ledger that persists the
two-per-board/six-per-shift budget.  After recovery this module always repeats
its own MAC/image/kernel probe.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from skyweave2.edge import campaign_c001 as c001
from skyweave2.edge import daemon, provision

_INTERFACE = re.compile(r"[A-Za-z0-9_.:-]+\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RECOVERY_SCHEMA = "skyweave-c001-recovery-ledger/1"
_ATTEMPT_SCHEMA = "skyweave-c001-attempt-ledger/1"
_ATTEMPT_LEDGER_NAME = "attempt-ledger.jsonl"
_RECOVERY_LEDGER_NAME = "recovery-ledger.jsonl"
_SCORE_LEDGER_NAME = "ledger.jsonl"
_STOP_SOURCE_NAME = "STOP.source.json"
_STOP_RECOVERY_SNAPSHOT_NAME = "STOP.recovery-ledger.jsonl"
_STOP_ATTEMPT_SNAPSHOT_NAME = "STOP.attempt-ledger.jsonl"
_RECOVERY_STOP_SCHEMA = "skyweave-c001-recovery-stop-evidence/1"
_IDENTITY_STOP_SCHEMA = "skyweave-c001-identity-stop-evidence/1"
_MANDATORY_NONPHYSICAL_PHASE1_ROWS = 3
_RUNTIME_IVE_LIBRARY = "/oem/usr/lib/librve.so"
_CANONICAL_CAMPAIGN_DIR = (
    Path(__file__).resolve().parents[3] / "docs" / "campaigns" / c001.CAMPAIGN_ID
)
_RUN_OUTPUTS = frozenset(
    {
        "stats.json",
        "ccl.jsonl",
        "packets.hex",
        "run.log",
        "exit.status",
        "fg-masks.swfm",
        "provision.json",
        "run_binding.json",
        "recovery-ledger-snapshot.jsonl",
        "attempt-ledger-snapshot.jsonl",
    }
)
_REQUIRED_RAW_OUTPUTS = frozenset({"stats.json", "ccl.jsonl", "packets.hex"})


class IdentityProbeUnavailable(provision.ProvisionError):
    """The board could not answer an identity-preflight command."""


class IdentityMismatch(c001.GuardrailViolation):
    """Transport or recovery evidence violated the campaign identity binding."""

    def __init__(
        self,
        message: str,
        *,
        expected: Mapping[str, object],
        observed: Mapping[str, object],
    ) -> None:
        super().__init__(message)
        self.expected = dict(expected)
        self.observed = dict(observed)


class ProvisionFunction(Protocol):
    """Injectable seam used by tests; production passes ``provision_node``."""

    def __call__(self, **kwargs: Any) -> provision.ProvisionResult: ...


@dataclass(frozen=True)
class IdentityEvidence:
    """Values read directly through the transport before any file is pushed."""

    board: str
    mac: str
    image_marker: str
    kernel: str
    interface: str

    def as_dict(self) -> dict[str, str]:
        return {
            "board": self.board,
            "mac": self.mac,
            "image_marker": self.image_marker,
            "kernel": self.kernel,
            "interface": self.interface,
        }

    def campaign_identity(self) -> c001.BoardIdentity:
        return c001.BoardIdentity(self.board, self.mac, self.image_marker)


@dataclass(frozen=True)
class BoardRunArtifacts:
    run_id: str
    attempt_n: int
    remote_run_dir: str
    output_dir: Path
    provision_path: Path
    run_binding_path: Path
    stats_path: Path
    ccl_log_path: Path
    packet_log_path: Path
    exit_status_path: Path
    run_log_path: Path
    fg_mask_path: Path | None
    identity: IdentityEvidence
    wall_s: float
    power_cycles: int
    recovery_ledger_snapshot: Path | None
    attempt_ledger_snapshot: Path


def _checked_probe(
    transport: provision.NodeTransport, command: str, *, description: str
) -> str:
    try:
        result = transport.run(command, timeout_s=15.0)
    except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
        raise IdentityProbeUnavailable(f"board {description} probe failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise IdentityProbeUnavailable(
            f"board {description} probe failed ({result.returncode}): {detail}"
        )
    value = result.stdout.strip()
    if not value:
        raise IdentityProbeUnavailable(f"board {description} probe returned no value")
    return value


def _image_marker(os_release: str) -> str:
    fields: dict[str, str] = {}
    for line in os_release.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        try:
            words = shlex.split(value, posix=True)
        except ValueError as exc:
            raise IdentityProbeUnavailable("board /etc/os-release is malformed") from exc
        fields[name] = " ".join(words)
    if fields.get("PRETTY_NAME"):
        return fields["PRETTY_NAME"]
    if fields.get("NAME") and fields.get("VERSION"):
        return f"{fields['NAME']} {fields['VERSION']}"
    raise IdentityProbeUnavailable(
        "board /etc/os-release lacks PRETTY_NAME (or NAME plus VERSION)"
    )


def preflight_identity(
    transport: provision.NodeTransport,
    expected: c001.BoardIdentity,
    *,
    expected_kernel: str,
    interface: str = "eth0",
) -> IdentityEvidence:
    """Read and exactly match MAC, image marker, and kernel before provisioning."""

    if not isinstance(interface, str) or _INTERFACE.fullmatch(interface) is None:
        raise c001.CampaignError("identity interface contains unsupported characters")
    kernel_expected = expected_kernel.strip()
    if not kernel_expected:
        raise c001.CampaignError("expected kernel must be non-empty")

    mac = _checked_probe(
        transport,
        f"cat /sys/class/net/{interface}/address",
        description=f"{interface} MAC",
    ).splitlines()[-1].strip()
    marker = _image_marker(
        _checked_probe(transport, "cat /etc/os-release", description="image marker")
    )
    kernel = _checked_probe(transport, "uname -r", description="kernel").splitlines()[0]
    expected_board, expected_mac, expected_marker = expected.normalized()
    expected_evidence = {
        "board": expected_board,
        "mac": expected_mac,
        "image_marker": expected_marker,
        "kernel": kernel_expected,
        "interface": interface,
    }
    try:
        actual_identity = c001.BoardIdentity(expected.board, mac, marker)
    except c001.CampaignError as exc:
        raise IdentityMismatch(
            f"board returned an invalid identity: {exc}",
            expected=expected_evidence,
            observed={
                "board": expected_board,
                "mac": mac.lower(),
                "image_marker": marker,
                "kernel": kernel.strip(),
                "interface": interface,
            },
        ) from exc
    actual = IdentityEvidence(
        board=actual_identity.board.strip(),
        mac=actual_identity.mac.lower(),
        image_marker=actual_identity.image_marker.strip(),
        kernel=kernel.strip(),
        interface=interface,
    )
    mismatches: dict[str, tuple[str, str]] = {}
    for name, observed, wanted in (
        ("board", actual.board, expected_board),
        ("mac", actual.mac, expected_mac),
        ("image_marker", actual.image_marker, expected_marker),
        ("kernel", actual.kernel, kernel_expected),
    ):
        if observed != wanted:
            mismatches[name] = (observed, wanted)
    if mismatches:
        rendered = ", ".join(
            f"{name}=observed {observed!r}, expected {wanted!r}"
            for name, (observed, wanted) in mismatches.items()
        )
        raise IdentityMismatch(
            f"board identity mismatch: {rendered}",
            expected=expected_evidence,
            observed=actual.as_dict(),
        )
    return actual


def runtime_ive_library_sha256(
    transport: provision.NodeTransport, spec: provision.NodeSpec
) -> str:
    """Hash the exact IVE runtime selected by the campaign launch environment."""

    if spec.ld_library_path != "/oem/usr/lib":
        raise c001.GuardrailViolation(
            "C-001 LD_LIBRARY_PATH must be exactly /oem/usr/lib"
        )
    value = _checked_probe(
        transport,
        f"sha256sum {shlex.quote(_RUNTIME_IVE_LIBRARY)}",
        description="IVE runtime SHA256",
    )
    lines = value.splitlines()
    words = lines[-1].split() if len(lines) == 1 else []
    if (
        len(words) != 2
        or _SHA256.fullmatch(words[0]) is None
        or words[1] != _RUNTIME_IVE_LIBRARY
    ):
        raise IdentityProbeUnavailable(
            "board IVE runtime SHA256 probe returned malformed or aliased output"
        )
    return words[0]


def _preflight_with_recovery(
    transport: provision.NodeTransport,
    expected: c001.BoardIdentity,
    *,
    expected_kernel: str,
    interface: str,
    recovery: c001.PoERecovery | None,
    reserve_cycle: Callable[[], dict[str, object]] | None,
    terminal_stop: Callable[[str, Mapping[str, object]], None] | None,
) -> tuple[IdentityEvidence, list[dict[str, object]]]:
    try:
        return (
            preflight_identity(
                transport,
                expected,
                expected_kernel=expected_kernel,
                interface=interface,
            ),
            [],
        )
    except IdentityProbeUnavailable:
        if recovery is None:
            raise
        if reserve_cycle is None:  # guarded by run_board, kept total here
            raise c001.GuardrailViolation(
                "PoE recovery has no durable recovery-budget authority"
            ) from None

    # The adapter supplies only operations, never credentials or a switch
    # protocol.  Its identity read is checked by the campaign state model, and
    # then the independent transport probe (including kernel) is repeated.
    reservation = reserve_cycle()
    is_last_board_cycle = (
        int(reservation["board_cycle_n"]) >= c001.MAX_RECOVERY_CYCLES_PER_BOARD
    )
    terminal_recorded = False
    stage = "cycle_port"
    try:
        state = c001.RecoveryStateMachine(
            expected,
            shift_cycles_used=int(reservation["shift_cycle_n"]) - 1,
            board_cycles_used=int(reservation["board_cycle_n"]) - 1,
        )
        state.begin_cycle()
        recovery.cycle_port(expected)
        state.cycle_dispatched()
        stage = "wait_for_boot"
        reachable = recovery.wait_for_boot(expected, timeout_s=120.0)
        state.boot_result(reachable)
        if not reachable:
            if is_last_board_cycle and terminal_stop is not None:
                terminal_stop(
                    state.reason or "board unreachable after two recovery cycles",
                    reservation,
                )
                terminal_recorded = True
            raise IdentityProbeUnavailable(
                state.reason or "board did not return after recovery"
            )
        stage = "read_identity"
        adapter_identity = recovery.read_identity(expected)
        state.identity_result(adapter_identity)
        if state.state is not c001.RecoveryState.READY:
            expected_board, expected_mac, expected_marker = expected.normalized()
            observed_board, observed_mac, observed_marker = adapter_identity.normalized()
            raise IdentityMismatch(
                state.reason or "identity mismatch after recovery",
                expected={
                    "board": expected_board,
                    "mac": expected_mac,
                    "image_marker": expected_marker,
                    "kernel": expected_kernel,
                    "interface": interface,
                },
                observed={
                    "board": observed_board,
                    "mac": observed_mac,
                    "image_marker": observed_marker,
                    "kernel": None,
                    "interface": "recovery_adapter",
                },
            )
        stage = "transport_revalidation"
        identity = preflight_identity(
            transport,
            expected,
            expected_kernel=expected_kernel,
            interface=interface,
        )
    except IdentityMismatch:
        # Identity is an independent Subject-to hard stop and gets the richer
        # expected/observed proof in run_board's attempt-finalization path.
        raise
    except Exception as exc:
        if is_last_board_cycle and not terminal_recorded and terminal_stop is not None:
            terminal_stop(
                f"board not ready after second recovery cycle during {stage}: {exc}",
                reservation,
            )
        raise
    attempt = {
        **reservation,
        "outcome": "ready",
        "identity_revalidated": True,
    }
    return identity, [attempt]


def _new_run_id() -> str:
    return secrets.token_hex(16)


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _validated_recovery_rows(data: bytes) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    previous = "0" * 64
    board_counts: dict[str, int] = {}
    seen_run_ids: set[str] = set()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise c001.GuardrailViolation("recovery ledger is not UTF-8") from exc
    for line_number, line in enumerate(text.splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise c001.GuardrailViolation(
                f"recovery ledger line {line_number} is invalid JSON"
            ) from exc
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
        if not isinstance(row, dict) or set(row) != required:
            raise c001.GuardrailViolation(
                f"recovery ledger line {line_number} fields are incomplete or unknown"
            )
        if (
            row["schema"] != _RECOVERY_SCHEMA
            or row["campaign_id"] != c001.CAMPAIGN_ID
            or row["event"] != "poe_cycle_reserved"
        ):
            raise c001.GuardrailViolation("recovery ledger schema/event drifted")
        run_id = row["run_id"]
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise c001.GuardrailViolation("recovery ledger run_id is malformed")
        if run_id in seen_run_ids:
            raise c001.GuardrailViolation("one run_id reserved more than one PoE cycle")
        seen_run_ids.add(run_id)
        try:
            identity = c001.BoardIdentity(str(row["board"]), str(row["mac"]), "ledger")
        except c001.CampaignError as exc:
            raise c001.GuardrailViolation("recovery ledger board identity is malformed") from exc
        key = identity.mac.lower()
        board_counts[key] = board_counts.get(key, 0) + 1
        shift_n = row["shift_cycle_n"]
        board_n = row["board_cycle_n"]
        if (
            isinstance(shift_n, bool)
            or not isinstance(shift_n, int)
            or shift_n != len(rows) + 1
            or isinstance(board_n, bool)
            or not isinstance(board_n, int)
            or board_n != board_counts[key]
            or shift_n > c001.MAX_POWER_CYCLES
            or board_n > c001.MAX_RECOVERY_CYCLES_PER_BOARD
        ):
            raise c001.GuardrailViolation("recovery ledger cycle counters are invalid")
        if row["previous_sha256"] != previous:
            raise c001.GuardrailViolation("recovery ledger hash chain is broken")
        row_hash = row["row_sha256"]
        if not isinstance(row_hash, str) or _SHA256.fullmatch(row_hash) is None:
            raise c001.GuardrailViolation("recovery ledger row hash is malformed")
        material = {name: value for name, value in row.items() if name != "row_sha256"}
        expected_hash = hashlib.sha256(_canonical_bytes(material)).hexdigest()
        if row_hash != expected_hash:
            raise c001.GuardrailViolation("recovery ledger row hash does not match its content")
        if not isinstance(row["recorded_at"], str) or not str(row["recorded_at"]).strip():
            raise c001.GuardrailViolation("recovery ledger timestamp is missing")
        previous = row_hash
        rows.append(row)
    return rows


def _reserve_recovery_cycle(
    ledger_path: Path, *, run_id: str, expected: c001.BoardIdentity
) -> dict[str, object]:
    """Atomically spend one durable shift/board recovery-cycle budget unit."""

    if ledger_path.is_symlink():
        raise c001.GuardrailViolation("recovery ledger may not be a symlink")
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        rows = _validated_recovery_rows(_read_descriptor(descriptor))
        board, mac, _ = expected.normalized()
        board_rows = [
            row
            for row in rows
            if str(row["mac"]).lower() == mac
        ]
        if len(rows) >= c001.MAX_POWER_CYCLES:
            raise c001.GuardrailViolation("six-cycle C-001 shift recovery budget exhausted")
        if len(board_rows) >= c001.MAX_RECOVERY_CYCLES_PER_BOARD:
            raise c001.GuardrailViolation("board recovery budget exhausted after two cycles")
        if any(row["run_id"] == run_id for row in rows):
            raise c001.GuardrailViolation("run_id already reserved a recovery cycle")
        material: dict[str, object] = {
            "schema": _RECOVERY_SCHEMA,
            "campaign_id": c001.CAMPAIGN_ID,
            "event": "poe_cycle_reserved",
            "run_id": run_id,
            "board": board,
            "mac": mac,
            "shift_cycle_n": len(rows) + 1,
            "board_cycle_n": len(board_rows) + 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "previous_sha256": rows[-1]["row_sha256"] if rows else "0" * 64,
        }
        row_hash = hashlib.sha256(_canonical_bytes(material)).hexdigest()
        row = {**material, "row_sha256": row_hash}
        encoded = _canonical_bytes(row)
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        directory = os.open(ledger_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)
    return {
        "run_id": run_id,
        "board": board,
        "mac": mac,
        "shift_cycle_n": row["shift_cycle_n"],
        "board_cycle_n": row["board_cycle_n"],
        "reservation_sha256": row_hash,
    }


def _recovery_ledger_snapshot(ledger_path: Path) -> tuple[bytes, str]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        data = _read_descriptor(descriptor)
        rows = _validated_recovery_rows(data)
    finally:
        os.close(descriptor)
    return data, str(rows[-1]["row_sha256"]) if rows else "0" * 64


def _check_recovery_budget(ledger_path: Path, expected: c001.BoardIdentity) -> None:
    """Refuse a run when the durable shift or expected-MAC budget is exhausted."""

    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        rows = _validated_recovery_rows(_read_descriptor(descriptor))
    finally:
        os.close(descriptor)
    if len(rows) >= c001.MAX_POWER_CYCLES:
        raise c001.GuardrailViolation("six-cycle C-001 shift recovery budget exhausted")
    expected_mac = expected.normalized()[1]
    board_cycles = sum(str(row["mac"]).lower() == expected_mac for row in rows)
    if board_cycles >= c001.MAX_RECOVERY_CYCLES_PER_BOARD:
        raise c001.GuardrailViolation("board recovery budget exhausted after two cycles")


def _validated_attempt_rows(data: bytes) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    reservations: dict[str, dict[str, object]] = {}
    outcomes: dict[str, int] = {}
    previous = "0" * 64
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise c001.GuardrailViolation("attempt ledger is not UTF-8") from exc
    for line_number, line in enumerate(text.splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise c001.GuardrailViolation(
                f"attempt ledger line {line_number} is invalid JSON"
            ) from exc
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
        if not isinstance(row, dict) or not common <= set(row):
            raise c001.GuardrailViolation("attempt ledger row fields are incomplete")
        if row["schema"] != _ATTEMPT_SCHEMA or row["campaign_id"] != c001.CAMPAIGN_ID:
            raise c001.GuardrailViolation("attempt ledger schema/campaign drifted")
        run_id = row["run_id"]
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise c001.GuardrailViolation("attempt ledger run_id is malformed")
        event = row["event"]
        if event == "attempt_reserved":
            required = common | {
                "board",
                "mac",
                "seed",
                "manifest_sha256",
            }
            if set(row) != required or run_id in reservations:
                raise c001.GuardrailViolation("attempt reservation fields/run_id are invalid")
            identity = c001.BoardIdentity(str(row["board"]), str(row["mac"]), "attempt")
            del identity
            c001.validate_seed(row["seed"])
            if not isinstance(row["manifest_sha256"], str) or _SHA256.fullmatch(
                row["manifest_sha256"]
            ) is None:
                raise c001.GuardrailViolation("attempt manifest digest is malformed")
            attempt_n = row["attempt_n"]
            if (
                isinstance(attempt_n, bool)
                or not isinstance(attempt_n, int)
                or attempt_n != len(reservations) + 1
                or attempt_n > c001.MAX_EXPERIMENTS
            ):
                raise c001.GuardrailViolation("attempt reservation number is invalid")
            reservations[run_id] = row
            outcomes[run_id] = 0
        elif event == "attempt_outcome":
            required = common | {"outcome_n", "outcome", "wall_s", "wedge", "error"}
            if set(row) != required or run_id not in reservations:
                raise c001.GuardrailViolation("attempt outcome fields/run_id are invalid")
            if row["attempt_n"] != reservations[run_id]["attempt_n"]:
                raise c001.GuardrailViolation("attempt outcome number differs from reservation")
            outcome_n = row["outcome_n"]
            if (
                isinstance(outcome_n, bool)
                or not isinstance(outcome_n, int)
                or outcome_n != outcomes[run_id] + 1
                or not isinstance(row["outcome"], str)
                or re.fullmatch(r"[a-z][a-z0-9_]*", row["outcome"]) is None
                or isinstance(row["wall_s"], bool)
                or not isinstance(row["wall_s"], (int, float))
                or not math.isfinite(float(row["wall_s"]))
                or float(row["wall_s"]) < 0
                or not isinstance(row["wedge"], bool)
                or (row["error"] is not None and not isinstance(row["error"], str))
            ):
                raise c001.GuardrailViolation("attempt outcome values are invalid")
            outcomes[run_id] = outcome_n
        else:
            raise c001.GuardrailViolation("attempt ledger event is unknown")
        if row["previous_sha256"] != previous:
            raise c001.GuardrailViolation("attempt ledger hash chain is broken")
        row_hash = row["row_sha256"]
        if not isinstance(row_hash, str) or _SHA256.fullmatch(row_hash) is None:
            raise c001.GuardrailViolation("attempt ledger row hash is malformed")
        material = {name: value for name, value in row.items() if name != "row_sha256"}
        if hashlib.sha256(_canonical_bytes(material)).hexdigest() != row_hash:
            raise c001.GuardrailViolation("attempt ledger row hash does not match content")
        if not isinstance(row["recorded_at"], str) or not str(row["recorded_at"]).strip():
            raise c001.GuardrailViolation("attempt ledger timestamp is missing")
        previous = row_hash
        rows.append(row)
    return rows


def _append_attempt_row(descriptor: int, material: dict[str, object]) -> dict[str, object]:
    row_hash = hashlib.sha256(_canonical_bytes(material)).hexdigest()
    row = {**material, "row_sha256": row_hash}
    encoded = _canonical_bytes(row)
    offset = 0
    while offset < len(encoded):
        offset += os.write(descriptor, encoded[offset:])
    os.fsync(descriptor)
    return row


def _reserve_attempt(
    ledger_path: Path,
    *,
    run_id: str,
    expected: c001.BoardIdentity,
    seed: int,
    manifest_sha256: str,
    max_physical_attempts: int,
) -> dict[str, object]:
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        rows = _validated_attempt_rows(_read_descriptor(descriptor))
        reservations = [row for row in rows if row["event"] == "attempt_reserved"]
        if len(reservations) >= max_physical_attempts:
            raise c001.GuardrailViolation(
                "40-experiment campaign budget is exhausted after accounting for "
                "non-physical ledger rows"
            )
        if any(row["run_id"] == run_id for row in reservations):
            raise c001.GuardrailViolation("run_id already reserved a physical attempt")
        board, mac, _ = expected.normalized()
        material: dict[str, object] = {
            "schema": _ATTEMPT_SCHEMA,
            "campaign_id": c001.CAMPAIGN_ID,
            "event": "attempt_reserved",
            "run_id": run_id,
            "attempt_n": len(reservations) + 1,
            "board": board,
            "mac": mac,
            "seed": seed,
            "manifest_sha256": manifest_sha256,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "previous_sha256": rows[-1]["row_sha256"] if rows else "0" * 64,
        }
        row = _append_attempt_row(descriptor, material)
        directory = os.open(ledger_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)
    return {
        "run_id": run_id,
        "attempt_n": row["attempt_n"],
        "reservation_sha256": row["row_sha256"],
    }


def record_attempt_outcome(
    attempt_ledger_path: str | Path,
    *,
    run_id: str,
    outcome: str,
    wall_s: float,
    wedge: bool,
    error: str | None = None,
) -> dict[str, object]:
    """Append one durable outcome to an already-reserved physical attempt."""

    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise c001.CampaignError("attempt outcome run_id is malformed")
    if not isinstance(outcome, str) or re.fullmatch(r"[a-z][a-z0-9_]*", outcome) is None:
        raise c001.CampaignError("attempt outcome label is malformed")
    if (
        isinstance(wall_s, bool)
        or not isinstance(wall_s, (int, float))
        or not math.isfinite(float(wall_s))
        or float(wall_s) < 0
    ):
        raise c001.CampaignError("attempt outcome wall_s must be finite and nonnegative")
    if not isinstance(wedge, bool) or (error is not None and not isinstance(error, str)):
        raise c001.CampaignError("attempt outcome wedge/error fields are malformed")
    ledger_path = Path(os.path.abspath(attempt_ledger_path))
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger_path, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        rows = _validated_attempt_rows(_read_descriptor(descriptor))
        reservation = next(
            (
                row
                for row in rows
                if row["event"] == "attempt_reserved" and row["run_id"] == run_id
            ),
            None,
        )
        if reservation is None:
            raise c001.GuardrailViolation("attempt outcome has no durable reservation")
        outcome_n = 1 + sum(
            row["event"] == "attempt_outcome" and row["run_id"] == run_id for row in rows
        )
        material: dict[str, object] = {
            "schema": _ATTEMPT_SCHEMA,
            "campaign_id": c001.CAMPAIGN_ID,
            "event": "attempt_outcome",
            "run_id": run_id,
            "attempt_n": reservation["attempt_n"],
            "outcome_n": outcome_n,
            "outcome": outcome,
            "wall_s": float(wall_s),
            "wedge": wedge,
            "error": error,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "previous_sha256": rows[-1]["row_sha256"],
        }
        row = _append_attempt_row(descriptor, material)
    finally:
        os.close(descriptor)
    # Re-use the full semantic validator on the prospective row values too.
    _validated_attempt_rows(_attempt_ledger_snapshot(ledger_path)[0])
    return row


def _attempt_ledger_snapshot(ledger_path: Path) -> tuple[bytes, str]:
    descriptor = os.open(ledger_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        data = _read_descriptor(descriptor)
        rows = _validated_attempt_rows(data)
    finally:
        os.close(descriptor)
    if not rows:
        raise c001.GuardrailViolation("attempt ledger has no physical reservation")
    return data, str(rows[-1]["row_sha256"])


def _campaign_ledger_path(
    value: str | Path,
    *,
    output_dir: Path,
    expected_name: str,
    label: str,
) -> Path:
    """Resolve one injected campaign ledger without permitting path rotation."""

    path = Path(os.path.abspath(value))
    if path.name != expected_name:
        raise c001.GuardrailViolation(
            f"{label} must use the canonical campaign filename {expected_name!r}"
        )
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise c001.GuardrailViolation(f"{label} must be a non-symlink regular file")
    if path.parent.resolve(strict=True) != path.parent:
        raise c001.GuardrailViolation(f"{label} parent may not be a path alias")
    if path.parent not in output_dir.resolve(strict=True).parents:
        raise c001.GuardrailViolation(
            f"{label} must live in the ancestor campaign directory"
        )
    return path


def _reserve_fresh_remote_dir(
    transport: provision.NodeTransport, remote_base: str, remote_run_dir: str
) -> None:
    base_command = f"mkdir -p {shlex.quote(remote_base)}"
    reserve_command = f"mkdir {shlex.quote(remote_run_dir)}"
    try:
        base = transport.run(base_command, timeout_s=15.0)
    except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
        raise IdentityProbeUnavailable(
            f"remote run-directory freshness probe failed: {exc}"
        ) from exc
    if base.returncode != 0:
        detail = base.stderr.strip() or base.stdout.strip() or "no output"
        raise IdentityProbeUnavailable(f"cannot create remote campaign base: {detail}")
    try:
        result = transport.run(reserve_command, timeout_s=15.0)
    except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
        raise IdentityProbeUnavailable(
            f"remote run-directory reservation failed: {exc}"
        ) from exc
    if result.returncode != 0:
        raise c001.GuardrailViolation(
            f"cryptographically unique remote run directory already exists: {remote_run_dir}"
        )


def _write_new_bytes(path: Path, data: bytes) -> None:
    """Durably install an artifact without replacing earlier evidence."""

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
            raise c001.CampaignError(f"refusing to replace retained artifact {path}") from exc
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new_json(path: Path, payload: Mapping[str, object]) -> None:
    """Durably install a proof object without replacing earlier evidence."""

    _write_new_bytes(path, _canonical_bytes(payload))


def _record_terminal_recovery_stop(
    *,
    campaign_dir: Path,
    expected: c001.BoardIdentity,
    attempt_reservation: Mapping[str, object],
    recovery_reservation: Mapping[str, object],
    reason: str,
) -> None:
    """Retain the two chains and stop the shift after a second failed boot."""

    recovery_source = campaign_dir / _RECOVERY_LEDGER_NAME
    attempt_source = campaign_dir / _ATTEMPT_LEDGER_NAME
    recovery_bytes, recovery_tip = _recovery_ledger_snapshot(recovery_source)
    attempt_bytes, attempt_tip = _attempt_ledger_snapshot(attempt_source)
    recovery_snapshot = campaign_dir / _STOP_RECOVERY_SNAPSHOT_NAME
    attempt_snapshot = campaign_dir / _STOP_ATTEMPT_SNAPSHOT_NAME
    _write_new_bytes(recovery_snapshot, recovery_bytes)
    _write_new_bytes(attempt_snapshot, attempt_bytes)
    board, mac, image_marker = expected.normalized()
    source_payload = {
        "schema": _RECOVERY_STOP_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "category": "board_unreachable_after_two_cycles",
        "reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "run_id": attempt_reservation["run_id"],
        "identity": {
            "board": board,
            "mac": mac,
            "image_marker": image_marker,
        },
        "recovery_attempt": {
            **recovery_reservation,
            "outcome": "unreachable",
            "identity_revalidated": False,
        },
        "recovery_ledger": {
            "path": recovery_snapshot.name,
            "sha256": c001.sha256_file(recovery_snapshot),
            "tip_sha256": recovery_tip,
        },
        "attempt_reservation": dict(attempt_reservation),
        "attempt_ledger": {
            "path": attempt_snapshot.name,
            "sha256": c001.sha256_file(attempt_snapshot),
            "tip_sha256": attempt_tip,
        },
    }
    source_path = campaign_dir / _STOP_SOURCE_NAME
    _write_new_json(source_path, source_payload)
    c001.record_campaign_stop(
        campaign_dir / _SCORE_LEDGER_NAME,
        category="board_unreachable_after_two_cycles",
        reason=reason,
        source_artifact_sha256=c001.sha256_file(source_path),
    )


def _record_terminal_identity_stop(
    *,
    campaign_dir: Path,
    attempt_reservation: Mapping[str, object],
    mismatch: IdentityMismatch,
) -> None:
    """Bind a Subject-to identity violation to the durable attempt chain."""

    attempt_bytes, attempt_tip = _attempt_ledger_snapshot(
        campaign_dir / _ATTEMPT_LEDGER_NAME
    )
    attempt_snapshot = campaign_dir / _STOP_ATTEMPT_SNAPSHOT_NAME
    _write_new_bytes(attempt_snapshot, attempt_bytes)
    compared_fields = ("board", "mac", "image_marker", "kernel")
    mismatched_fields = [
        name
        for name in compared_fields
        if mismatch.observed.get(name) is not None
        and mismatch.observed.get(name) != mismatch.expected.get(name)
    ]
    if not mismatched_fields:
        raise c001.GuardrailViolation(
            "identity stop evidence does not contain a concrete Subject-to mismatch"
        )
    source_payload = {
        "schema": _IDENTITY_STOP_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "category": "subject_to_violation",
        "reason": str(mismatch),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "run_id": attempt_reservation["run_id"],
        "expected": mismatch.expected,
        "observed": mismatch.observed,
        "mismatched_fields": mismatched_fields,
        "attempt_reservation": dict(attempt_reservation),
        "attempt_ledger": {
            "path": attempt_snapshot.name,
            "sha256": c001.sha256_file(attempt_snapshot),
            "tip_sha256": attempt_tip,
        },
    }
    source_path = campaign_dir / _STOP_SOURCE_NAME
    _write_new_json(source_path, source_payload)
    c001.record_campaign_stop(
        campaign_dir / _SCORE_LEDGER_NAME,
        category="subject_to_violation",
        reason=str(mismatch),
        source_artifact_sha256=c001.sha256_file(source_path),
    )


def _prepare_paths(
    manifest_path: Path, binary: Path, output_dir: Path, clip_path: Path
) -> None:
    for label, path in (("binary", binary), ("probe manifest", manifest_path), ("clip", clip_path)):
        if path.is_symlink():
            raise c001.GuardrailViolation(f"{label} may not be a symlink: {path}")
        if not path.is_file():
            raise c001.CampaignError(f"{label} does not exist as a regular file: {path}")
    if output_dir.is_symlink():
        raise c001.GuardrailViolation(f"output directory may not be a symlink: {output_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise c001.CampaignError(f"output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    inputs = {
        "binary": binary.resolve(strict=True),
        "probe manifest": manifest_path.resolve(strict=True),
        "RAM-loop clip": clip_path.resolve(strict=True),
    }
    if len(set(inputs.values())) != len(inputs):
        raise c001.GuardrailViolation(f"board-run input paths collide: {inputs}")
    destinations = {name: (output_dir / name).absolute() for name in _RUN_OUTPUTS}
    for output_name, destination in destinations.items():
        if destination.exists() or destination.is_symlink():
            raise c001.CampaignError(
                f"refusing to replace retained board-run artifact {destination}"
            )
        for source_name, source in inputs.items():
            if destination == source:
                raise c001.GuardrailViolation(
                    f"{source_name} collides with output {output_name}: {source}"
                )


def _flag_value(argv: list[str], flag: str) -> str:
    if argv.count(flag) != 1:
        raise c001.GuardrailViolation(f"provision argv must carry {flag} exactly once")
    index = argv.index(flag)
    if index + 1 == len(argv):
        raise c001.GuardrailViolation(f"provision argv {flag} has no value")
    return argv[index + 1]


def _validate_provision_result(
    result: provision.ProvisionResult,
    *,
    spec: provision.NodeSpec,
    output_dir: Path,
    manifest: Mapping[str, object],
    manifest_path: Path,
    knobs: Mapping[str, object],
    binary: Path,
    mask_limit: int,
    transport: provision.NodeTransport,
) -> dict[str, object]:
    if not isinstance(result, provision.ProvisionResult):
        raise c001.CampaignError("provision function returned no ProvisionResult")
    if result.node.as_dict() != spec.as_dict():
        raise c001.GuardrailViolation("provision result belongs to a different node spec")
    if not result.binary_verified or result.local_sha256 != c001.sha256_file(binary):
        raise c001.GuardrailViolation("provision result does not bind the selected ARM binary")
    if not result.source_verified or result.source_remote_sha256 != manifest["clip_sha256"]:
        raise c001.GuardrailViolation("provision result does not bind the selected RAM clip")
    if result.source_local_sha256 != result.source_remote_sha256:
        raise c001.GuardrailViolation("local and remote RAM clip hashes differ")
    if not result.source_remote_path:
        raise c001.GuardrailViolation("provision result lacks the verified remote RAM path")
    if (
        isinstance(result.wall_s, bool)
        or not isinstance(result.wall_s, (int, float))
        or not math.isfinite(float(result.wall_s))
        or not 0.0 <= float(result.wall_s) <= c001.MAX_EXPERIMENT_MINUTES * 60
    ):
        raise c001.GuardrailViolation("provision measured wall time is outside 0..20 minutes")
    if result.pid is None or result.pid <= 0:
        raise c001.GuardrailViolation("provision result lacks a daemon process id")
    process_group_alive = transport.alive(result.pid)
    if result.stop_succeeded is False or process_group_alive or not result.daemon_stopped:
        raise c001.GuardrailViolation(
            "board daemon remained alive after the stop deadline (campaign wedge)"
        )
    if not result.completed_before_deadline:
        raise c001.GuardrailViolation(
            "board daemon did not complete the exact RAM-loop run before its deadline"
        )
    if result.exit_status != 0:
        raise c001.GuardrailViolation(
            f"board daemon exit status must be zero, got {result.exit_status!r}"
        )

    try:
        argv = shlex.split(result.argv)
    except ValueError as exc:
        raise c001.GuardrailViolation("provision result argv is malformed") from exc
    if _flag_value(argv, "--inject-ram") != result.source_remote_path:
        raise c001.GuardrailViolation(
            "provision argv source path differs from the verified remote RAM path"
        )
    if "--inject-file" in argv or "--inject-listen" in argv:
        raise c001.GuardrailViolation("provision argv contains colliding source modes")
    remote_outputs = {
        result.remote_binary,
        f"{spec.remote_dir.rstrip('/')}/stats.json",
        f"{spec.remote_dir.rstrip('/')}/packets.hex",
        f"{spec.remote_dir.rstrip('/')}/ccl.jsonl",
        f"{spec.remote_dir.rstrip('/')}/run.log",
    }
    if mask_limit:
        remote_outputs.add(f"{spec.remote_dir.rstrip('/')}/fg-masks.swfm")
    if result.source_remote_path in remote_outputs:
        raise c001.GuardrailViolation("verified remote RAM path collides with a run output")

    required = {*_REQUIRED_RAW_OUTPUTS, "exit.status", "run.log"}
    if mask_limit:
        required.add("fg-masks.swfm")
    if not required <= set(result.collected):
        missing = sorted(required - set(result.collected))
        raise c001.GuardrailViolation(
            f"provision did not collect required outputs: {missing}"
        )
    for name in required:
        path = output_dir / name
        if path.is_symlink() or not path.is_file():
            raise c001.GuardrailViolation(f"collected output is missing or aliased: {path}")
    if (output_dir / "exit.status").read_bytes() not in {b"0", b"0\n"}:
        raise c001.GuardrailViolation("collected exit.status does not prove daemon exit zero")
    if (output_dir / "run.log").stat().st_size == 0:
        raise c001.GuardrailViolation("collected daemon run.log is empty")
    try:
        retained_stats = json.loads((output_dir / "stats.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise c001.CampaignError(f"cannot read collected stats.json: {exc}") from exc
    if not isinstance(retained_stats, dict) or retained_stats != result.stats:
        raise c001.GuardrailViolation("ProvisionResult stats differ from collected stats.json")
    c001.validate_board_run_binding(
        retained_stats,
        manifest={**manifest, "manifest_sha256": c001.sha256_file(manifest_path)},
        knobs=knobs,
    )
    return retained_stats


def _run_board_once(
    *,
    manifest_path: str | Path,
    knobs: Mapping[str, object],
    expected_identity: c001.BoardIdentity,
    expected_kernel: str,
    spec: provision.NodeSpec,
    binary: str | Path,
    output_dir: str | Path,
    identity_interface: str = "eth0",
    phase1_failed_mask_limit: int = 10,
    wait_s: float = 120.0,
    ssh_identity: str | None = None,
    jump_host: str | None = None,
    strict_host_key_checking: str = "yes",
    transport: provision.NodeTransport | None = None,
    provision_fn: ProvisionFunction = provision.provision_node,
    recovery: c001.PoERecovery | None = None,
    recovery_ledger_path: str | Path | None = None,
    run_id_factory: Callable[[], str] = _new_run_id,
    attempt_reservation: Mapping[str, object],
    attempt_ledger_source: Path,
    finish_attempt: Callable[[str, float, bool, str | None], None],
    defer_terminal_recovery_stop: Callable[
        [str, Mapping[str, object]], None
    ],
    reserved_at: float,
) -> BoardRunArtifacts:
    """Execute one validated C-001 RAM-loop run and retain its proof objects."""

    experiment_started = reserved_at
    manifest_source = Path(os.path.abspath(manifest_path))
    manifest = c001.load_probe_manifest(manifest_source)
    normalized_knobs = c001.normalize_knobs(knobs)
    config = c001.detector_config_for(normalized_knobs)
    if not isinstance(expected_identity, c001.BoardIdentity):
        raise c001.CampaignError("expected_identity must be a C-001 BoardIdentity")
    if spec.name.strip() != expected_identity.board.strip():
        raise c001.GuardrailViolation("node spec name differs from expected board identity")
    if (
        isinstance(phase1_failed_mask_limit, bool)
        or not isinstance(phase1_failed_mask_limit, int)
        or not 0 <= phase1_failed_mask_limit <= 10
    ):
        raise c001.GuardrailViolation("Phase-1 failed-mask limit must be an integer in 0..10")
    if (
        isinstance(wait_s, bool)
        or not isinstance(wait_s, (int, float))
        or not math.isfinite(float(wait_s))
        or not 0 < float(wait_s) <= c001.MAX_EXPERIMENT_MINUTES * 60
    ):
        raise c001.GuardrailViolation("board wait must be finite and within 0..20 minutes")
    run_id = run_id_factory()
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise c001.CampaignError("run-id factory must return 32 lowercase hexadecimal characters")
    remote_base = spec.remote_dir.rstrip("/")
    if not remote_base.startswith("/") or any(part == ".." for part in Path(remote_base).parts):
        raise c001.GuardrailViolation("C-001 remote base directory must be an absolute clean path")
    remote_run_dir = f"{remote_base}/run-{run_id}"
    run_spec = replace(spec, remote_dir=remote_run_dir)

    clip_source = (manifest_source.parent / str(manifest["clip_path"])).resolve(strict=True)
    binary_source = Path(os.path.abspath(binary))
    out = Path(os.path.abspath(output_dir))
    _prepare_paths(manifest_source, binary_source, out, clip_source)
    ledger_source: Path | None = None
    if recovery is not None and recovery_ledger_path is None:
        raise c001.GuardrailViolation(
            "PoE recovery requires a shared durable recovery_ledger_path"
        )
    if recovery_ledger_path is not None:
        ledger_source = _campaign_ledger_path(
            recovery_ledger_path,
            output_dir=out,
            expected_name=_RECOVERY_LEDGER_NAME,
            label="recovery ledger",
        )
        if ledger_source.parent != attempt_ledger_source.parent:
            raise c001.GuardrailViolation(
                "attempt and recovery ledgers must share one campaign directory"
            )
        if ledger_source in {
            manifest_source.resolve(strict=True),
            binary_source.resolve(strict=True),
            clip_source.resolve(strict=True),
        }:
            raise c001.GuardrailViolation("recovery ledger collides with a board-run input")
        _check_recovery_budget(ledger_source, expected_identity)

    if transport is None:
        if not spec.ssh_host.strip():
            raise c001.CampaignError("an SSH board run needs a non-empty node ssh_host")
        if not jump_host:
            raise c001.CampaignError(
                "C-001 rig boards require an explicit SSH jump host (for example jetson-ts)"
            )
        transport = provision.SshTransport(
            spec=spec,
            strict_host_key_checking=strict_host_key_checking,
            identity=ssh_identity,
            jump_host=jump_host,
        )
    elif isinstance(transport, provision.SshTransport):
        if transport.spec != spec:
            raise c001.GuardrailViolation(
                "injected SSH transport belongs to a different node spec"
            )
        if not transport.jump_host:
            raise c001.GuardrailViolation(
                "injected SSH transport lacks the rig's explicit jump host"
            )

    identity, recovery_attempts = _preflight_with_recovery(
        transport,
        expected_identity,
        expected_kernel=expected_kernel,
        interface=identity_interface,
        recovery=recovery,
        reserve_cycle=(
            lambda: _reserve_recovery_cycle(
                ledger_source, run_id=run_id, expected=expected_identity
            )
        )
        if ledger_source is not None
        else None,
        terminal_stop=defer_terminal_recovery_stop,
    )
    runtime_ive_sha256_before = runtime_ive_library_sha256(transport, spec)
    _reserve_fresh_remote_dir(transport, remote_base, remote_run_dir)
    loop = daemon.RamLoopDeclaration(
        clip_frames=int(manifest["ram_clip_frames"]),
        total_frames=int(manifest["ram_loop_total_frames"]),
        pts_stride_ns=int(manifest["ram_loop_pts_stride_ns"]),
        budget_mb=int(manifest["ram_budget_mb"]),
        period_ns=0,
    )
    provisioning_started = time.monotonic()
    result = provision_fn(
        transport=transport,
        binary=binary_source,
        config=config,
        spec=run_spec,
        out_dir=out,
        ram_clip_local=clip_source,
        ram_loop=loop,
        detector="ive",
        wait_s=float(wait_s),
        packet_log=True,
        ccl_log=True,
        fg_mask_limit=phase1_failed_mask_limit,
    )
    provision_returned = time.monotonic()
    retained_stats = _validate_provision_result(
        result,
        spec=run_spec,
        output_dir=out,
        manifest=manifest,
        manifest_path=manifest_source,
        knobs=normalized_knobs,
        binary=binary_source,
        mask_limit=phase1_failed_mask_limit,
        transport=transport,
    )
    runtime_ive_sha256_after = runtime_ive_library_sha256(transport, spec)
    if runtime_ive_sha256_after != runtime_ive_sha256_before:
        raise c001.GuardrailViolation(
            "board IVE runtime library changed during the physical experiment"
        )
    recovery_snapshot: Path | None = None
    recovery_ledger_proof: dict[str, str] | None = None
    if ledger_source is not None:
        snapshot_bytes, tip_hash = _recovery_ledger_snapshot(ledger_source)
        recovery_snapshot = out / "recovery-ledger-snapshot.jsonl"
        _write_new_bytes(recovery_snapshot, snapshot_bytes)
        recovery_ledger_proof = {
            "path": recovery_snapshot.name,
            "sha256": c001.sha256_file(recovery_snapshot),
            "tip_sha256": tip_hash,
        }

    experiment_wall_s = (
        provisioning_started
        - experiment_started
        + float(result.wall_s)
        + (time.monotonic() - provision_returned)
    )
    if experiment_wall_s > c001.MAX_EXPERIMENT_MINUTES * 60:
        raise c001.GuardrailViolation(
            "total experiment wall time including identity/recovery exceeds 20 minutes"
        )
    finish_attempt("run_complete", experiment_wall_s, False, None)
    attempt_snapshot_bytes, attempt_tip = _attempt_ledger_snapshot(attempt_ledger_source)
    attempt_snapshot = out / "attempt-ledger-snapshot.jsonl"
    _write_new_bytes(attempt_snapshot, attempt_snapshot_bytes)
    attempt_ledger_proof = {
        "path": attempt_snapshot.name,
        "sha256": c001.sha256_file(attempt_snapshot),
        "tip_sha256": attempt_tip,
    }

    provision_path = out / "provision.json"
    provision_payload = {
        **result.as_dict(),
        "wall_s": experiment_wall_s,
        "provision_wall_s": result.wall_s,
        "run_id": run_id,
        "remote_run_dir": remote_run_dir,
        "daemon_exit_code": result.exit_status,
        "daemon_stopped": result.daemon_stopped,
        "collected_sha256": {
            name: c001.sha256_file(out / Path(name).name)
            for name in sorted(result.collected)
            if (out / Path(name).name).is_file()
        },
        "power_cycles": len(recovery_attempts),
        "recovery_attempts": recovery_attempts,
        "recovery_ledger": recovery_ledger_proof,
        "attempt_reservation": dict(attempt_reservation),
        "attempt_ledger": attempt_ledger_proof,
        "identity_preflight": identity.as_dict(),
        "runtime_ive_library": {
            "path": _RUNTIME_IVE_LIBRARY,
            "sha256_before": runtime_ive_sha256_before,
            "sha256_after": runtime_ive_sha256_after,
            "stable": True,
        },
        "probe_manifest_sha256": c001.sha256_file(manifest_source),
    }
    _write_new_json(provision_path, provision_payload)
    stats_path = out / "stats.json"
    ccl_path = out / "ccl.jsonl"
    packets_path = out / "packets.hex"
    mask_path = out / "fg-masks.swfm" if phase1_failed_mask_limit else None
    c001.validate_provision_artifact(
        provision_path,
        board=identity.board,
        stats=retained_stats,
        manifest=manifest,
        stats_path=stats_path,
        ccl_log_path=ccl_path,
        packet_log_path=packets_path,
        board_fg_masks_path=mask_path,
        exit_status_path=out / "exit.status",
        run_log_path=out / "run.log",
    )

    binding_path = out / "run_binding.json"
    binding = {
        "schema": c001.RUN_BINDING_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "board": identity.board,
        "run_id": run_id,
        "remote_run_dir": remote_run_dir,
        "seed": manifest["seed"],
        "identity": {
            "board": identity.board,
            "mac": identity.mac,
            "image_marker": identity.image_marker,
        },
        "source_mode": manifest["source_mode"],
        "proc_width": c001.PROC_WIDTH,
        "proc_height": c001.PROC_HEIGHT,
        "total_frames": manifest["total_frames"],
        "ram_clip_frames": manifest["ram_clip_frames"],
        "probe_manifest_sha256": c001.sha256_file(manifest_source),
        "remote_clip_sha256": result.source_remote_sha256,
        "stats_sha256": c001.sha256_file(stats_path),
        "ccl_log_sha256": c001.sha256_file(ccl_path),
        "packet_log_sha256": c001.sha256_file(packets_path),
    }
    _write_new_json(binding_path, binding)
    c001.validate_external_run_binding(
        binding_path,
        board=identity.board,
        manifest_path=manifest_source,
        manifest=manifest,
        stats_path=stats_path,
        ccl_log_path=ccl_path,
        packet_log_path=packets_path,
    )
    return BoardRunArtifacts(
        run_id=run_id,
        attempt_n=int(attempt_reservation["attempt_n"]),
        remote_run_dir=remote_run_dir,
        output_dir=out,
        provision_path=provision_path,
        run_binding_path=binding_path,
        stats_path=stats_path,
        ccl_log_path=ccl_path,
        packet_log_path=packets_path,
        exit_status_path=out / "exit.status",
        run_log_path=out / "run.log",
        fg_mask_path=mask_path,
        identity=identity,
        wall_s=experiment_wall_s,
        power_cycles=len(recovery_attempts),
        recovery_ledger_snapshot=recovery_snapshot,
        attempt_ledger_snapshot=attempt_snapshot,
    )


def _attempt_failure_label(exc: Exception) -> tuple[str, bool]:
    text = str(exc).lower()
    if "wedge" in text or "remained alive" in text:
        return "wedge", True
    if isinstance(exc, IdentityProbeUnavailable):
        return "preflight_failure", False
    if "identity mismatch" in text:
        return "identity_failure", False
    if "already exists" in text and "remote run" in text:
        return "freshness_failure", False
    if "exit status" in text or "exit zero" in text:
        return "nonzero_exit", False
    if "deadline" in text or "wall time" in text or "20 minutes" in text:
        return "timeout", False
    return "run_failed", False


def run_board(
    *,
    manifest_path: str | Path,
    knobs: Mapping[str, object],
    expected_identity: c001.BoardIdentity,
    expected_kernel: str,
    spec: provision.NodeSpec,
    binary: str | Path,
    output_dir: str | Path,
    attempt_ledger_path: str | Path,
    identity_interface: str = "eth0",
    phase1_failed_mask_limit: int = 10,
    wait_s: float = 120.0,
    ssh_identity: str | None = None,
    jump_host: str | None = None,
    strict_host_key_checking: str = "yes",
    transport: provision.NodeTransport | None = None,
    provision_fn: ProvisionFunction = provision.provision_node,
    recovery: c001.PoERecovery | None = None,
    recovery_ledger_path: str | Path | None = None,
    run_id_factory: Callable[[], str] = _new_run_id,
) -> BoardRunArtifacts:
    """Reserve one physical attempt, execute it, and durably record its outcome."""

    manifest_source = Path(os.path.abspath(manifest_path))
    manifest = c001.load_probe_manifest(manifest_source)
    c001.normalize_knobs(knobs)
    if not isinstance(expected_identity, c001.BoardIdentity):
        raise c001.CampaignError("expected_identity must be a C-001 BoardIdentity")
    if spec.name.strip() != expected_identity.board.strip():
        raise c001.GuardrailViolation("node spec name differs from expected board identity")
    if (
        isinstance(phase1_failed_mask_limit, bool)
        or not isinstance(phase1_failed_mask_limit, int)
        or not 0 <= phase1_failed_mask_limit <= 10
    ):
        raise c001.GuardrailViolation("Phase-1 failed-mask limit must be an integer in 0..10")
    if (
        isinstance(wait_s, bool)
        or not isinstance(wait_s, (int, float))
        or not math.isfinite(float(wait_s))
        or not 0 < float(wait_s) <= c001.MAX_EXPERIMENT_MINUTES * 60
    ):
        raise c001.GuardrailViolation("board wait must be finite and within 0..20 minutes")
    run_id = run_id_factory()
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise c001.CampaignError("run-id factory must return 32 lowercase hexadecimal characters")
    clip_source = (manifest_source.parent / str(manifest["clip_path"])).resolve(strict=True)
    binary_source = Path(os.path.abspath(binary))
    out = Path(os.path.abspath(output_dir))
    _prepare_paths(manifest_source, binary_source, out, clip_source)

    attempt_ledger = _campaign_ledger_path(
        attempt_ledger_path,
        output_dir=out,
        expected_name=_ATTEMPT_LEDGER_NAME,
        label="attempt ledger",
    )
    if attempt_ledger in {
        manifest_source.resolve(strict=True),
        binary_source.resolve(strict=True),
        clip_source.resolve(strict=True),
    }:
        raise c001.GuardrailViolation("attempt ledger collides with a board-run input")

    campaign_dir = attempt_ledger.parent
    score_ledger = campaign_dir / _SCORE_LEDGER_NAME
    stopped = c001.read_campaign_stop(score_ledger)
    if stopped is not None:
        raise c001.GuardrailViolation(
            f"shift is stopped by {stopped['category']}: {stopped['reason']}"
        )
    terminal_members = (
        _STOP_SOURCE_NAME,
        _STOP_RECOVERY_SNAPSHOT_NAME,
        _STOP_ATTEMPT_SNAPSHOT_NAME,
    )
    if any(
        (campaign_dir / name).exists() or (campaign_dir / name).is_symlink()
        for name in terminal_members
    ):
        raise c001.GuardrailViolation(
            "terminal recovery evidence exists without its immutable STOP.json"
        )
    campaign_rows = c001.read_ledger(score_ledger, verify_artifacts=True)
    nonphysical_rows = sum(
        not isinstance(row.get("attempt_budget"), Mapping) for row in campaign_rows
    )
    # Phase 1 rows n=1..3 are mandatory non-physical experiments.  Reserve
    # their three slots prospectively: n=2/n=3 may not exist yet when a board
    # attempt starts, so subtracting only currently retained rows would allow
    # physical reservations to consume their future budget.
    max_physical_attempts = min(
        c001.MAX_EXPERIMENTS - _MANDATORY_NONPHYSICAL_PHASE1_ROWS,
        c001.MAX_EXPERIMENTS - nonphysical_rows,
    )
    if max_physical_attempts < 0:  # read_ledger normally catches this first
        raise c001.GuardrailViolation("campaign ledger already exceeds 40 experiments")

    reserved_at = time.monotonic()
    reservation = _reserve_attempt(
        attempt_ledger,
        run_id=run_id,
        expected=expected_identity,
        seed=int(manifest["seed"]),
        manifest_sha256=c001.sha256_file(manifest_source),
        max_physical_attempts=max_physical_attempts,
    )
    finished = False

    def finish(outcome: str, wall_s: float, wedge: bool, error: str | None) -> None:
        nonlocal finished
        record_attempt_outcome(
            attempt_ledger,
            run_id=run_id,
            outcome=outcome,
            wall_s=wall_s,
            wedge=wedge,
            error=error,
        )
        finished = True

    pending_recovery_stop: tuple[str, dict[str, object]] | None = None

    def defer_recovery_stop(
        reason: str, recovery_reservation: Mapping[str, object]
    ) -> None:
        nonlocal pending_recovery_stop
        if pending_recovery_stop is not None:
            raise c001.GuardrailViolation(
                "one physical attempt produced more than one terminal recovery stop"
            )
        pending_recovery_stop = (reason, dict(recovery_reservation))

    try:
        return _run_board_once(
            manifest_path=manifest_source,
            knobs=knobs,
            expected_identity=expected_identity,
            expected_kernel=expected_kernel,
            spec=spec,
            binary=binary_source,
            output_dir=out,
            identity_interface=identity_interface,
            phase1_failed_mask_limit=phase1_failed_mask_limit,
            wait_s=wait_s,
            ssh_identity=ssh_identity,
            jump_host=jump_host,
            strict_host_key_checking=strict_host_key_checking,
            transport=transport,
            provision_fn=provision_fn,
            recovery=recovery,
            recovery_ledger_path=recovery_ledger_path,
            run_id_factory=lambda: run_id,
            attempt_reservation=reservation,
            attempt_ledger_source=attempt_ledger,
            finish_attempt=finish,
            defer_terminal_recovery_stop=defer_recovery_stop,
            reserved_at=reserved_at,
        )
    except Exception as exc:
        outcome, wedge = _attempt_failure_label(exc)
        # If the physical run had completed, this second outcome records a
        # later proof/validation failure rather than rewriting that fact.
        if finished:
            outcome = "artifact_failure"
        finish(outcome, time.monotonic() - reserved_at, wedge, str(exc))
        if pending_recovery_stop is not None:
            reason, recovery_reservation = pending_recovery_stop
            _record_terminal_recovery_stop(
                campaign_dir=attempt_ledger.parent,
                expected=expected_identity,
                attempt_reservation=reservation,
                recovery_reservation=recovery_reservation,
                reason=reason,
            )
        if isinstance(exc, IdentityMismatch):
            _record_terminal_identity_stop(
                campaign_dir=attempt_ledger.parent,
                attempt_reservation=reservation,
                mismatch=exc,
            )
        raise


def _json_object(value: str) -> dict[str, object]:
    try:
        payload = json.loads(
            Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError("knobs must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run one identity-bound C-001 board probe")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--knobs", required=True, type=_json_object, help="JSON or @JSON-file")
    parser.add_argument("--board", required=True, help="stable campaign board label")
    parser.add_argument("--host", required=True, help="board SSH host")
    parser.add_argument("--expected-mac", required=True)
    parser.add_argument("--expected-image-marker", required=True)
    parser.add_argument("--expected-kernel", required=True)
    parser.add_argument("--identity-interface", default="eth0")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-identity")
    parser.add_argument(
        "--jump-host", required=True, help="SSH ProxyJump host or config alias"
    )
    parser.add_argument("--accept-new-host-key", action="store_true")
    parser.add_argument("--binary", default=str(daemon.FIRMWARE_ROOT / "build-board/skyweave-edge"))
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--campaign-dir",
        required=True,
        help="canonical v2/docs/campaigns/C-001 evidence and budget directory",
    )
    parser.add_argument("--remote-dir", default="/userdata/skyweave/c001")
    parser.add_argument("--jetson", default="127.0.0.1")
    parser.add_argument("--camera-id", type=int, default=0)
    parser.add_argument("--measurement-port", type=int, default=5601)
    parser.add_argument("--control-port", type=int, default=5602)
    parser.add_argument("--health-period-ms", type=int, default=1000)
    parser.add_argument("--failed-mask-limit", type=int, default=10)
    parser.add_argument("--wait-s", type=float, default=120.0)
    args = parser.parse_args(argv)

    campaign_dir = Path(args.campaign_dir).resolve(strict=True)
    if campaign_dir != _CANONICAL_CAMPAIGN_DIR.resolve(strict=True):
        parser.error(
            "--campaign-dir must resolve to the canonical repository "
            "v2/docs/campaigns/C-001 directory"
        )
    output_dir = Path(os.path.abspath(args.out))
    if campaign_dir not in output_dir.parents:
        parser.error("--out must be a child of the canonical C-001 campaign directory")

    spec = provision.NodeSpec(
        name=args.board,
        camera_id=args.camera_id,
        jetson_host=args.jetson,
        measurement_port=args.measurement_port,
        control_port=args.control_port,
        health_period_ms=args.health_period_ms,
        remote_dir=args.remote_dir,
        ssh_host=args.host,
        ssh_user=args.user,
        ssh_port=args.ssh_port,
    )
    expected = c001.BoardIdentity(
        board=args.board,
        mac=args.expected_mac,
        image_marker=args.expected_image_marker,
    )
    # Keep the stable, sibling shift lock for the complete physical operation.
    # A successor rollover takes the exclusive form of this lock, so it cannot
    # archive the campaign root after validation but before the reservation or
    # while board artifacts are still being retained.
    with c001.campaign_shift_lock(campaign_dir):
        c001.validate_current_shift(campaign_dir)
        artifacts = run_board(
            manifest_path=args.manifest,
            knobs=args.knobs,
            expected_identity=expected,
            expected_kernel=args.expected_kernel,
            spec=spec,
            binary=args.binary,
            output_dir=output_dir,
            attempt_ledger_path=campaign_dir / _ATTEMPT_LEDGER_NAME,
            identity_interface=args.identity_interface,
            phase1_failed_mask_limit=args.failed_mask_limit,
            wait_s=args.wait_s,
            ssh_identity=args.ssh_identity,
            jump_host=args.jump_host,
            strict_host_key_checking=(
                "accept-new" if args.accept_new_host_key else "yes"
            ),
            recovery_ledger_path=campaign_dir / _RECOVERY_LEDGER_NAME,
        )
    print(
        json.dumps(
            {
                "run_id": artifacts.run_id,
                "attempt_n": artifacts.attempt_n,
                "remote_run_dir": artifacts.remote_run_dir,
                "provision": str(artifacts.provision_path),
                "run_binding": str(artifacts.run_binding_path),
                "stats": str(artifacts.stats_path),
                "ccl_log": str(artifacts.ccl_log_path),
                "packet_log": str(artifacts.packet_log_path),
                "exit_status": str(artifacts.exit_status_path),
                "run_log": str(artifacts.run_log_path),
                "fg_masks": (
                    str(artifacts.fg_mask_path) if artifacts.fg_mask_path else None
                ),
                "recovery_ledger_snapshot": (
                    str(artifacts.recovery_ledger_snapshot)
                    if artifacts.recovery_ledger_snapshot
                    else None
                ),
                "attempt_ledger_snapshot": str(artifacts.attempt_ledger_snapshot),
                "wall_s": artifacts.wall_s,
                "power_cycles": artifacts.power_cycles,
                "identity": artifacts.identity.as_dict(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "BoardRunArtifacts",
    "IdentityEvidence",
    "IdentityMismatch",
    "IdentityProbeUnavailable",
    "main",
    "preflight_identity",
    "record_attempt_outcome",
    "run_board",
]
