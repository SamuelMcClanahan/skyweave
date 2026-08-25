"""Supported identity-bound provisioning path for C-001 board runs."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from skyweave2.edge import campaign_c001 as c001
from skyweave2.edge import campaign_c001_run as c001_run
from skyweave2.edge import provision


@pytest.fixture(scope="module")
def sparse_probe(tmp_path_factory):
    return c001.prepare_probe(
        tmp_path_factory.mktemp("c001-run-probe"), kind="sparse", seed=0xC001
    )


class IdentityTransport:
    name = "identity-test"

    def __init__(
        self,
        *,
        mac: str = "02:00:00:00:00:AA",
        fail_first: bool = False,
        remote_dir_exists: bool = False,
        process_alive: bool = False,
        runtime_library_hashes: list[str] | None = None,
    ):
        self.mac = mac
        self.fail_first = fail_first
        self.remote_dir_exists = remote_dir_exists
        self.process_alive = process_alive
        self.runtime_library_hashes = list(runtime_library_hashes or ["7" * 64])
        self.commands: list[str] = []

    def run(self, command: str, timeout_s: float = 60.0) -> provision.CommandResult:
        del timeout_s
        self.commands.append(command)
        if self.fail_first:
            self.fail_first = False
            return provision.CommandResult(["fake", command], 255, "", "unreachable")
        if command.startswith("mkdir -p "):
            return provision.CommandResult(["fake", command], 0, "", "")
        if command.startswith("mkdir "):
            return provision.CommandResult(
                ["fake", command], int(self.remote_dir_exists), "", ""
            )
        if command == "sha256sum /oem/usr/lib/librve.so":
            digest = (
                self.runtime_library_hashes.pop(0)
                if len(self.runtime_library_hashes) > 1
                else self.runtime_library_hashes[0]
            )
            return provision.CommandResult(
                ["fake", command], 0, f"{digest}  /oem/usr/lib/librve.so\n", ""
            )
        values = {
            "cat /sys/class/net/eth0/address": self.mac + "\n",
            "cat /etc/os-release": 'NAME=Buildroot\nPRETTY_NAME="Buildroot 2023.02.6"\n',
            "uname -r": "5.10.160\n",
        }
        return provision.CommandResult(["fake", command], 0, values[command], "")

    def push(self, local: Path, remote: str) -> None:  # pragma: no cover - provision is injected
        raise AssertionError((local, remote))

    def fetch(self, remote: str, local: Path) -> None:  # pragma: no cover
        raise AssertionError((remote, local))

    def spawn(  # pragma: no cover
        self, command: str, log_remote: str, exit_status_remote: str | None = None
    ) -> int:
        raise AssertionError((command, log_remote, exit_status_remote))

    def terminate(self, pid: int) -> None:  # pragma: no cover
        raise AssertionError(pid)

    def alive(self, pid: int) -> bool:
        assert pid == 123
        return self.process_alive


def _identity() -> c001.BoardIdentity:
    return c001.BoardIdentity("rig-b", "02:00:00:00:00:AA", "Buildroot 2023.02.6")


def _spec() -> provision.NodeSpec:
    return provision.NodeSpec(
        name="rig-b",
        ssh_host="100.64.0.104",
        remote_dir="/userdata/skyweave/c001-test",
        jetson_host="192.0.2.110",
    )


def _board_stats(manifest: dict, config, mask_limit: int) -> dict:
    return {
        "detector": "ive-gmm2",
        "source_mode": "inject-ram",
        "proc_width": 1152,
        "proc_height": 648,
        "warmup_frames": 30,
        "max_components_per_frame": 7,
        "max_area_px": 10000,
        "persistence_frames": 2,
        "persistence_gate_px": 12.0,
        "min_area_px": config.min_area_px,
        "morph_open": config.open_radius_px,
        "gmm2_match_sigmas": config.ive_approx.match_sigmas,
        "gmm2_var_min": config.ive_approx.var_min,
        "source_frames_planned": manifest["total_frames"],
        "source_frames_served": manifest["total_frames"],
        "frames_in": manifest["total_frames"],
        "ram_clip_frames": manifest["ram_clip_frames"],
        "ram_clip_bytes": 1152 * 648 * manifest["ram_clip_frames"],
        "ram_loop_pts_stride_ns": manifest["ram_loop_pts_stride_ns"],
        "ram_loop_period_ns": 0,
        "ram_budget_mb": manifest["ram_budget_mb"],
        "ccl_attempts": manifest["postwarm_frames"],
        "ccl_api_failures": 0,
        "ccl_label_failures": 0,
        "ccl_region_count_mismatch_frames": 0,
        "frames_detector_failed": 0,
        "frames_scored": manifest["postwarm_frames"],
        "fg_mask_limit": mask_limit,
    }


def _fake_provision(
    manifest_path: Path,
    calls: list[dict],
    *,
    wrong_argv: bool = False,
    exit_status: int | None = 0,
    daemon_stopped: bool = True,
    completed_before_deadline: bool = True,
    stop_succeeded: bool | None = None,
    wall_s: float = 12.75,
):
    manifest = c001.load_probe_manifest(manifest_path)

    def invoke(**kwargs) -> provision.ProvisionResult:
        calls.append(kwargs)
        output = Path(kwargs["out_dir"])
        stats = _board_stats(manifest, kwargs["config"], kwargs["fg_mask_limit"])
        (output / "stats.json").write_text(json.dumps(stats), encoding="utf-8")
        (output / "ccl.jsonl").write_text("", encoding="utf-8")
        (output / "packets.hex").write_text("", encoding="utf-8")
        (output / "run.log").write_text("complete\n", encoding="utf-8")
        (output / "exit.status").write_text(f"{exit_status}\n", encoding="ascii")
        collected = [
            "stats.json",
            "ccl.jsonl",
            "packets.hex",
            "run.log",
            "exit.status",
        ]
        if kwargs["fg_mask_limit"]:
            (output / "fg-masks.swfm").write_bytes(b"")
            collected.append("fg-masks.swfm")

        spec = kwargs["spec"]
        remote_binary = f"{spec.remote_dir}/skyweave-edge"
        remote_clip = f"{spec.remote_dir}/ram.swij"
        command = provision.daemon_command(
            remote_binary,
            kwargs["config"],
            spec,
            detector="ive",
            ram_clip_remote=(f"{spec.remote_dir}/wrong.swij" if wrong_argv else remote_clip),
            ram_loop=kwargs["ram_loop"],
            stats_remote=f"{spec.remote_dir}/stats.json",
            packet_log_remote=f"{spec.remote_dir}/packets.hex",
            ccl_log_remote=f"{spec.remote_dir}/ccl.jsonl",
            fg_mask_log_remote=(
                f"{spec.remote_dir}/fg-masks.swfm" if kwargs["fg_mask_limit"] else None
            ),
            fg_mask_limit=kwargs["fg_mask_limit"],
        )
        binary_hash = c001.sha256_file(kwargs["binary"])
        clip_hash = c001.sha256_file(kwargs["ram_clip_local"])
        return provision.ProvisionResult(
            node=spec,
            transport=kwargs["transport"].name,
            remote_binary=remote_binary,
            local_sha256=binary_hash,
            remote_sha256=binary_hash,
            source_remote_path=remote_clip,
            source_local_sha256=clip_hash,
            source_remote_sha256=clip_hash,
            wall_s=wall_s,
            completed_before_deadline=completed_before_deadline,
            stop_succeeded=stop_succeeded,
            exit_status=exit_status,
            daemon_stopped=daemon_stopped,
            pid=123,
            argv=command,
            stats=stats,
            collected=collected,
        )

    return invoke


def _run(sparse_probe, tmp_path, **overrides):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"arm-board-binary")
    calls: list[dict] = []
    arguments = {
        "manifest_path": sparse_probe.manifest_path,
        "knobs": {
            "min_area_px": 8,
            "morph_open": 0,
            "gmm2.match_sigmas": 3.25,
            "gmm2.var_min": 50.0,
        },
        "expected_identity": _identity(),
        "expected_kernel": "5.10.160",
        "spec": _spec(),
        "binary": binary,
        "output_dir": tmp_path / "run",
        "attempt_ledger_path": tmp_path / "attempt-ledger.jsonl",
        "recovery_ledger_path": tmp_path / "recovery-ledger.jsonl",
        "transport": IdentityTransport(),
        "provision_fn": _fake_provision(sparse_probe.manifest_path, calls),
        "run_id_factory": lambda: "ab" * 16,
    }
    arguments.update(overrides)
    return c001_run.run_board(**arguments), calls


def test_supported_run_binds_identity_exact_loop_knobs_source_and_measured_wall(
    sparse_probe, tmp_path
):
    artifacts, calls = _run(sparse_probe, tmp_path)
    assert len(calls) == 1
    call = calls[0]
    manifest = c001.load_probe_manifest(sparse_probe.manifest_path)
    assert call["detector"] == "ive"
    assert call["packet_log"] is call["ccl_log"] is True
    assert call["fg_mask_limit"] == 10
    assert call["ram_clip_local"] == sparse_probe.clip_path.resolve()
    assert call["ram_loop"].as_dict() == {
        "clip_frames": manifest["ram_clip_frames"],
        "total_frames": manifest["total_frames"],
        "pts_stride_ns": manifest["ram_loop_pts_stride_ns"],
        "budget_mb": manifest["ram_budget_mb"],
        "period_ns": 0,
    }
    config = call["config"]
    assert (config.proc_width, config.proc_height, config.warmup_frames) == (1152, 648, 30)
    assert (config.max_components_per_frame, config.persistence_frames) == (7, 2)
    assert config.persistence_gate_px == 12.0
    assert (config.min_area_px, config.open_radius_px) == (8, 0)
    assert (config.ive_approx.match_sigmas, config.ive_approx.var_min) == (3.25, 50.0)

    provision_payload = json.loads(artifacts.provision_path.read_text())
    binding = json.loads(artifacts.run_binding_path.read_text())
    assert provision_payload["wall_s"] == artifacts.wall_s >= 12.75
    assert provision_payload["provision_wall_s"] == 12.75
    assert provision_payload["identity_preflight"] == {
        "board": "rig-b",
        "mac": "02:00:00:00:00:aa",
        "image_marker": "Buildroot 2023.02.6",
        "kernel": "5.10.160",
        "interface": "eth0",
    }
    assert provision_payload["runtime_ive_library"] == {
        "path": "/oem/usr/lib/librve.so",
        "sha256_before": "7" * 64,
        "sha256_after": "7" * 64,
        "stable": True,
    }
    assert provision_payload["source_verified"] is True
    assert provision_payload["run_id"] == artifacts.run_id == "ab" * 16
    assert provision_payload["remote_run_dir"] == artifacts.remote_run_dir
    assert provision_payload["daemon_exit_code"] == 0
    assert provision_payload["daemon_stopped"] is True
    assert provision_payload["power_cycles"] == 0
    assert provision_payload["recovery_attempts"] == []
    assert provision_payload["recovery_ledger"] == {
        "path": "recovery-ledger-snapshot.jsonl",
        "sha256": c001.sha256_file(artifacts.recovery_ledger_snapshot),
        "tip_sha256": "0" * 64,
    }
    assert artifacts.attempt_n == 1
    assert provision_payload["attempt_reservation"] == {
        "run_id": "ab" * 16,
        "attempt_n": 1,
        "reservation_sha256": provision_payload["attempt_reservation"][
            "reservation_sha256"
        ],
    }
    assert provision_payload["attempt_ledger"]["path"] == "attempt-ledger-snapshot.jsonl"
    attempt_rows = [
        json.loads(line)
        for line in (tmp_path / "attempt-ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["event"] for row in attempt_rows] == [
        "attempt_reserved",
        "attempt_outcome",
    ]
    assert attempt_rows[-1]["outcome"] == "run_complete"
    assert provision_payload["collected_sha256"]["stats.json"] == binding["stats_sha256"]
    assert (
        "--inject-ram " + artifacts.remote_run_dir + "/ram.swij"
        in provision_payload["argv"]
    )
    assert binding["remote_clip_sha256"] == sparse_probe.clip_sha256
    assert binding["run_id"] == artifacts.run_id
    assert binding["remote_run_dir"] == artifacts.remote_run_dir
    assert binding["identity"] == {
        "board": "rig-b",
        "mac": "02:00:00:00:00:aa",
        "image_marker": "Buildroot 2023.02.6",
    }


def test_runtime_ive_library_must_remain_stable_across_run(sparse_probe, tmp_path):
    transport = IdentityTransport(runtime_library_hashes=["7" * 64, "8" * 64])
    with pytest.raises(c001.GuardrailViolation, match="runtime library changed"):
        _run(sparse_probe, tmp_path, transport=transport)


@pytest.mark.parametrize(
    ("transport", "spec", "error", "message"),
    [
        (
            IdentityTransport(runtime_library_hashes=["not-a-sha256"]),
            _spec(),
            c001_run.IdentityProbeUnavailable,
            "malformed or aliased",
        ),
        (
            IdentityTransport(),
            replace(_spec(), ld_library_path="/tmp:/oem/usr/lib"),
            c001.GuardrailViolation,
            "must be exactly",
        ),
    ],
)
def test_runtime_ive_library_proof_refuses_ambiguous_or_malformed_inputs(
    sparse_probe, tmp_path, transport, spec, error, message
):
    with pytest.raises(error, match=message):
        _run(sparse_probe, tmp_path, transport=transport, spec=spec)


def test_non_whitelisted_knob_is_refused_before_board_contact(sparse_probe, tmp_path):
    transport = IdentityTransport()
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    with pytest.raises(c001.GuardrailViolation, match="not a C-001 knob"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={"warmup_frames": 31},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "run",
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=transport,
        )
    assert transport.commands == []


@pytest.mark.parametrize(
    ("transport", "kernel", "message"),
    [
        (IdentityTransport(mac="02:00:00:00:00:AB"), "5.10.160", "mac"),
        (IdentityTransport(), "5.10.161", "kernel"),
    ],
)
def test_identity_mismatch_refuses_before_provision(
    sparse_probe, tmp_path, transport, kernel, message
):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    called = False

    def forbidden(**kwargs):
        nonlocal called
        called = True
        raise AssertionError(kwargs)

    with pytest.raises(c001.GuardrailViolation, match=message):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel=kernel,
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / message,
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=transport,
            provision_fn=forbidden,
        )
    assert called is False


def test_identity_mismatch_stops_later_otherwise_valid_run(sparse_probe, tmp_path):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    attempt_ledger = tmp_path / "attempt-ledger.jsonl"
    wrong_transport = IdentityTransport(mac="02:00:00:00:00:AB")
    with pytest.raises(c001_run.IdentityMismatch, match="mac"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "mismatch",
            attempt_ledger_path=attempt_ledger,
            transport=wrong_transport,
            run_id_factory=lambda: "a1" * 16,
        )

    score_ledger = tmp_path / "ledger.jsonl"
    stop = c001.read_campaign_stop(score_ledger)
    assert stop is not None
    assert stop["category"] == "subject_to_violation"
    source_path = tmp_path / "STOP.source.json"
    assert stop["source_artifact_sha256"] == c001.sha256_file(source_path)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    assert set(source) == {
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
    assert source["schema"] == "skyweave-c001-identity-stop-evidence/1"
    assert source["mismatched_fields"] == ["mac"]
    assert source["expected"]["mac"] == "02:00:00:00:00:aa"
    assert source["observed"]["mac"] == "02:00:00:00:00:ab"
    attempt_snapshot = tmp_path / source["attempt_ledger"]["path"]
    assert source["attempt_ledger"]["sha256"] == c001.sha256_file(attempt_snapshot)

    valid_transport = IdentityTransport()
    with pytest.raises(c001.GuardrailViolation, match="shift is stopped"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "would-have-succeeded",
            attempt_ledger_path=attempt_ledger,
            transport=valid_transport,
            run_id_factory=lambda: "a2" * 16,
        )
    assert valid_transport.commands == []


def test_input_and_retained_output_collisions_are_refused(sparse_probe, tmp_path):
    transport = IdentityTransport()
    with pytest.raises(c001.GuardrailViolation, match="input paths collide"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=sparse_probe.clip_path,
            output_dir=tmp_path / "collision",
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=transport,
        )
    assert transport.commands == []

    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    out = tmp_path / "existing"
    out.mkdir()
    (out / "stats.json").write_text("do not replace", encoding="utf-8")
    with pytest.raises(c001.CampaignError, match="refusing to replace"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=out,
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=transport,
        )
    assert transport.commands == []


def test_remote_argv_must_use_the_verified_source_path(sparse_probe, tmp_path):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    calls: list[dict] = []
    out = tmp_path / "run"
    with pytest.raises(c001.GuardrailViolation, match="argv source path"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=out,
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=IdentityTransport(),
            provision_fn=_fake_provision(
                sparse_probe.manifest_path, calls, wrong_argv=True
            ),
        )
    assert len(calls) == 1
    assert not (out / "provision.json").exists()
    assert not (out / "run_binding.json").exists()


def test_phase1_mask_capture_is_hard_bounded_to_ten(sparse_probe, tmp_path):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    transport = IdentityTransport()
    with pytest.raises(c001.GuardrailViolation, match="0..10"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "run",
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            phase1_failed_mask_limit=11,
            transport=transport,
        )
    assert transport.commands == []


def test_existing_remote_session_directory_is_a_stale_output_refusal(
    sparse_probe, tmp_path
):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    transport = IdentityTransport(remote_dir_exists=True)
    called = False

    def forbidden(**kwargs):
        nonlocal called
        called = True
        raise AssertionError(kwargs)

    with pytest.raises(c001.GuardrailViolation, match="already exists"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "run",
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=transport,
            provision_fn=forbidden,
            run_id_factory=lambda: "cd" * 16,
        )
    assert called is False


def test_failed_child_cannot_turn_preloaded_raw_files_into_a_run(sparse_probe, tmp_path):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    calls: list[dict] = []
    out = tmp_path / "run"
    with pytest.raises(c001.GuardrailViolation, match="exit status"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=out,
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=IdentityTransport(),
            provision_fn=_fake_provision(
                sparse_probe.manifest_path, calls, exit_status=1
            ),
            run_id_factory=lambda: "ef" * 16,
        )
    assert len(calls) == 1
    assert (out / "stats.json").exists(), "the adversarial stale-looking file was retained"
    assert not (out / "provision.json").exists()
    assert not (out / "run_binding.json").exists()


def test_alive_after_stop_is_a_wedge_and_never_emits_proof(sparse_probe, tmp_path):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    calls: list[dict] = []
    out = tmp_path / "run"
    with pytest.raises(c001.GuardrailViolation, match="campaign wedge"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=out,
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=IdentityTransport(process_alive=True),
            provision_fn=_fake_provision(
                sparse_probe.manifest_path,
                calls,
                exit_status=None,
                daemon_stopped=False,
                completed_before_deadline=False,
                stop_succeeded=False,
            ),
            run_id_factory=lambda: "12" * 16,
        )
    assert len(calls) == 1
    assert not (out / "provision.json").exists()
    assert not (out / "run_binding.json").exists()


def test_identity_and_reservation_time_cannot_hide_outside_wall_budget(
    sparse_probe, tmp_path
):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    calls: list[dict] = []
    with pytest.raises(c001.GuardrailViolation, match="total experiment wall time"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "run",
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=IdentityTransport(),
            provision_fn=_fake_provision(
                sparse_probe.manifest_path,
                calls,
                wall_s=c001.MAX_EXPERIMENT_MINUTES * 60,
            ),
            run_id_factory=lambda: "34" * 16,
        )


def test_external_recovery_hook_is_followed_by_a_fresh_transport_identity_probe(
    sparse_probe, tmp_path
):
    class Recovery:
        events: list[str]

        def __init__(self):
            self.events = []

        def cycle_port(self, expected):
            self.events.append(f"cycle:{expected.board}")

        def wait_for_boot(self, expected, timeout_s):
            self.events.append(f"wait:{expected.board}:{timeout_s:g}")
            return True

        def read_identity(self, expected):
            self.events.append(f"identity:{expected.board}")
            return expected

    recovery = Recovery()
    transport = IdentityTransport(fail_first=True)
    artifacts, _ = _run(
        sparse_probe,
        tmp_path,
        transport=transport,
        recovery=recovery,
        recovery_ledger_path=tmp_path / "recovery-ledger.jsonl",
    )
    assert recovery.events == ["cycle:rig-b", "wait:rig-b:120", "identity:rig-b"]
    assert transport.commands.count("cat /sys/class/net/eth0/address") == 2
    assert artifacts.identity.kernel == "5.10.160"
    assert artifacts.power_cycles == 1
    assert artifacts.recovery_ledger_snapshot is not None
    proof = json.loads(artifacts.provision_path.read_text())
    assert proof["power_cycles"] == len(proof["recovery_attempts"]) == 1
    assert proof["recovery_attempts"][0]["board_cycle_n"] == 1
    assert proof["recovery_attempts"][0]["shift_cycle_n"] == 1
    assert proof["recovery_attempts"][0]["outcome"] == "ready"
    assert proof["recovery_attempts"][0]["identity_revalidated"] is True
    assert proof["recovery_ledger"]["path"] == "recovery-ledger-snapshot.jsonl"


def test_recovery_budget_persists_across_run_invocations(sparse_probe, tmp_path):
    class Recovery:
        cycles = 0

        def cycle_port(self, expected):
            del expected
            self.cycles += 1

        def wait_for_boot(self, expected, timeout_s):
            del expected, timeout_s
            return True

        def read_identity(self, expected):
            return expected

    recovery = Recovery()
    ledger = tmp_path / "recovery-ledger.jsonl"
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    for index in range(2):
        calls: list[dict] = []
        artifacts = c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / f"run-{index}",
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=IdentityTransport(fail_first=True),
            provision_fn=_fake_provision(sparse_probe.manifest_path, calls),
            recovery=recovery,
            recovery_ledger_path=ledger,
            run_id_factory=lambda index=index: f"{index + 1:032x}",
        )
        assert artifacts.power_cycles == 1
        assert len(calls) == 1

    called = False

    def forbidden(**kwargs):
        nonlocal called
        called = True
        raise AssertionError(kwargs)

    refused_transport = IdentityTransport()
    with pytest.raises(c001.GuardrailViolation, match="after two cycles"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "run-2",
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            transport=refused_transport,
            provision_fn=forbidden,
            recovery=recovery,
            recovery_ledger_path=ledger,
            run_id_factory=lambda: f"{3:032x}",
        )
    assert called is False
    assert refused_transport.commands == []
    assert recovery.cycles == 2
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2


def test_zero_cycle_run_snapshots_prior_shared_recovery_state(sparse_probe, tmp_path):
    class Recovery:
        def cycle_port(self, expected):
            del expected

        def wait_for_boot(self, expected, timeout_s):
            del expected, timeout_s
            return True

        def read_identity(self, expected):
            return expected

    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    attempt_ledger = tmp_path / "attempt-ledger.jsonl"
    recovery_ledger = tmp_path / "recovery-ledger.jsonl"
    first_calls: list[dict] = []
    c001_run.run_board(
        manifest_path=sparse_probe.manifest_path,
        knobs={},
        expected_identity=_identity(),
        expected_kernel="5.10.160",
        spec=_spec(),
        binary=binary,
        output_dir=tmp_path / "cycle-run",
        attempt_ledger_path=attempt_ledger,
        transport=IdentityTransport(fail_first=True),
        provision_fn=_fake_provision(sparse_probe.manifest_path, first_calls),
        recovery=Recovery(),
        recovery_ledger_path=recovery_ledger,
        run_id_factory=lambda: "61" * 16,
    )

    second_calls: list[dict] = []
    artifacts = c001_run.run_board(
        manifest_path=sparse_probe.manifest_path,
        knobs={},
        expected_identity=_identity(),
        expected_kernel="5.10.160",
        spec=_spec(),
        binary=binary,
        output_dir=tmp_path / "reachable-run",
        attempt_ledger_path=attempt_ledger,
        transport=IdentityTransport(),
        provision_fn=_fake_provision(sparse_probe.manifest_path, second_calls),
        recovery_ledger_path=recovery_ledger,
        run_id_factory=lambda: "62" * 16,
    )
    proof = json.loads(artifacts.provision_path.read_text(encoding="utf-8"))
    assert proof["power_cycles"] == 0
    assert proof["recovery_attempts"] == []
    assert artifacts.recovery_ledger_snapshot is not None
    assert artifacts.recovery_ledger_snapshot.read_bytes() == recovery_ledger.read_bytes()
    rows = [json.loads(line) for line in recovery_ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert proof["recovery_ledger"]["tip_sha256"] == rows[-1]["row_sha256"]


def test_second_failed_recovery_writes_global_stop_and_refuses_another_mac(
    sparse_probe, tmp_path
):
    class NeverBoots:
        cycles = 0

        def cycle_port(self, expected):
            del expected
            self.cycles += 1

        def wait_for_boot(self, expected, timeout_s):
            del expected, timeout_s
            return False

        def read_identity(self, expected):  # pragma: no cover - boot never succeeds
            raise AssertionError(expected)

    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    attempt_ledger = tmp_path / "attempt-ledger.jsonl"
    recovery_ledger = tmp_path / "recovery-ledger.jsonl"
    recovery = NeverBoots()
    for index in range(2):
        with pytest.raises(c001_run.IdentityProbeUnavailable, match="recovery"):
            c001_run.run_board(
                manifest_path=sparse_probe.manifest_path,
                knobs={},
                expected_identity=_identity(),
                expected_kernel="5.10.160",
                spec=_spec(),
                binary=binary,
                output_dir=tmp_path / f"failed-recovery-{index}",
                attempt_ledger_path=attempt_ledger,
                transport=IdentityTransport(fail_first=True),
                recovery=recovery,
                recovery_ledger_path=recovery_ledger,
                run_id_factory=lambda index=index: f"{index + 81:032x}",
            )

    score_ledger = tmp_path / "ledger.jsonl"
    stop = c001.read_campaign_stop(score_ledger)
    assert stop is not None
    assert stop["category"] == "board_unreachable_after_two_cycles"
    stop_source = tmp_path / "STOP.source.json"
    assert stop["source_artifact_sha256"] == c001.sha256_file(stop_source)
    source = json.loads(stop_source.read_text(encoding="utf-8"))
    assert set(source) == {
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
    assert source["schema"] == "skyweave-c001-recovery-stop-evidence/1"
    assert source["recovery_attempt"]["board_cycle_n"] == 2
    assert source["recovery_attempt"]["outcome"] == "unreachable"
    assert source["recovery_attempt"]["identity_revalidated"] is False
    for name in ("recovery_ledger", "attempt_ledger"):
        retained = tmp_path / source[name]["path"]
        assert source[name]["sha256"] == c001.sha256_file(retained)
    attempt_snapshot = tmp_path / source["attempt_ledger"]["path"]
    attempt_rows = [
        json.loads(line) for line in attempt_snapshot.read_text().splitlines()
    ]
    failed_outcome = attempt_rows[-1]
    assert failed_outcome["event"] == "attempt_outcome"
    assert failed_outcome["run_id"] == source["run_id"]
    assert failed_outcome["attempt_n"] == source["attempt_reservation"]["attempt_n"]
    assert failed_outcome["outcome_n"] == 1
    assert failed_outcome["outcome"] == "preflight_failure"
    assert failed_outcome["wedge"] is False
    assert source["reason"].endswith(failed_outcome["error"])
    assert source["attempt_ledger"]["tip_sha256"] == failed_outcome["row_sha256"]
    assert attempt_snapshot.read_bytes() == attempt_ledger.read_bytes()
    assert recovery.cycles == 2

    other_transport = IdentityTransport(mac="02:00:00:00:00:AB")
    other_identity = c001.BoardIdentity(
        "rig-b", "02:00:00:00:00:AB", "Buildroot 2023.02.6"
    )
    with pytest.raises(c001.GuardrailViolation, match="shift is stopped"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=other_identity,
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "other-board",
            attempt_ledger_path=attempt_ledger,
            transport=other_transport,
            recovery_ledger_path=recovery_ledger,
            run_id_factory=lambda: "91" * 16,
        )
    assert other_transport.commands == []

    # A reservation-only prefix is not durable evidence that the terminal
    # physical attempt failed, even when every enclosing hash is recomputed.
    retained_lines = attempt_snapshot.read_text().splitlines()
    attempt_snapshot.write_text("\n".join(retained_lines[:-1]) + "\n")
    retained_tip = json.loads(retained_lines[-2])["row_sha256"]
    source["attempt_ledger"]["sha256"] = c001.sha256_file(attempt_snapshot)
    source["attempt_ledger"]["tip_sha256"] = retained_tip
    stop_source.write_text(json.dumps(source) + "\n")
    stop["source_artifact_sha256"] = c001.sha256_file(stop_source)
    (tmp_path / "STOP.json").write_text(json.dumps(stop) + "\n")
    with pytest.raises(c001.LedgerIntegrityError, match="durable failed attempt outcome"):
        c001.read_campaign_stop(score_ledger)


@pytest.mark.parametrize(
    "failed_stage",
    ["cycle_port", "wait_for_boot", "read_identity", "transport_revalidation"],
)
def test_every_nonready_second_recovery_outcome_is_a_global_stop(
    sparse_probe, tmp_path, failed_stage
):
    class FirstCycleStillUnreachable:
        def cycle_port(self, expected):
            del expected

        def wait_for_boot(self, expected, timeout_s):
            del expected, timeout_s
            return False

        def read_identity(self, expected):  # pragma: no cover - boot never succeeds
            raise AssertionError(expected)

    class FailsAtStage:
        def cycle_port(self, expected):
            del expected
            if failed_stage == "cycle_port":
                raise RuntimeError("cycle dispatch failed")

        def wait_for_boot(self, expected, timeout_s):
            del expected, timeout_s
            if failed_stage == "wait_for_boot":
                raise TimeoutError("boot wait failed")
            return True

        def read_identity(self, expected):
            if failed_stage == "read_identity":
                raise RuntimeError("adapter identity read failed")
            return expected

    class FailsBothMacProbes(IdentityTransport):
        def run(self, command, timeout_s=60.0):
            if command == "cat /sys/class/net/eth0/address":
                self.commands.append(command)
                return provision.CommandResult(
                    ["fake", command], 255, "", "transport revalidation failed"
                )
            return super().run(command, timeout_s)

    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    attempt_ledger = tmp_path / "attempt-ledger.jsonl"
    recovery_ledger = tmp_path / "recovery-ledger.jsonl"
    with pytest.raises(c001_run.IdentityProbeUnavailable):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "first-cycle",
            attempt_ledger_path=attempt_ledger,
            transport=IdentityTransport(fail_first=True),
            recovery=FirstCycleStillUnreachable(),
            recovery_ledger_path=recovery_ledger,
            run_id_factory=lambda: "b1" * 16,
        )

    second_transport = (
        FailsBothMacProbes()
        if failed_stage == "transport_revalidation"
        else IdentityTransport(fail_first=True)
    )
    with pytest.raises(Exception, match="failed"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "second-cycle",
            attempt_ledger_path=attempt_ledger,
            transport=second_transport,
            recovery=FailsAtStage(),
            recovery_ledger_path=recovery_ledger,
            run_id_factory=lambda: "b2" * 16,
        )
    stop = c001.read_campaign_stop(tmp_path / "ledger.jsonl")
    assert stop is not None
    assert stop["category"] == "board_unreachable_after_two_cycles"
    assert failed_stage in stop["reason"]


def test_campaign_ledger_paths_cannot_rotate_between_files(sparse_probe, tmp_path):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    transport = IdentityTransport()
    with pytest.raises(c001.GuardrailViolation, match="canonical campaign filename"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "wrong-name",
            attempt_ledger_path=tmp_path / "fresh-attempt-ledger.jsonl",
            transport=transport,
        )
    assert transport.commands == []

    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(c001.GuardrailViolation, match="share one campaign directory"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=nested / "run",
            attempt_ledger_path=tmp_path / "attempt-ledger.jsonl",
            recovery_ledger_path=nested / "recovery-ledger.jsonl",
            transport=transport,
            run_id_factory=lambda: "63" * 16,
        )
    assert transport.commands == []


def test_cli_refuses_an_alternate_campaign_root(tmp_path, capsys):
    with pytest.raises(SystemExit, match="2"):
        c001_run.main(
            [
                "--manifest",
                "missing.json",
                "--knobs",
                "{}",
                "--board",
                "rig-b",
                "--host",
                "192.0.2.104",
                "--expected-mac",
                "02:00:00:00:00:aa",
                "--expected-image-marker",
                "Buildroot 2023.02.6",
                "--expected-kernel",
                "5.10.160",
                "--jump-host",
                "jetson-ts",
                "--campaign-dir",
                str(tmp_path),
                "--out",
                str(tmp_path / "run"),
            ]
        )
    assert "must resolve to the canonical repository" in capsys.readouterr().err


def test_production_cli_holds_shift_lock_and_validates_before_board_run(
    tmp_path, monkeypatch, capsys
):
    campaign = tmp_path / "C-001"
    campaign.mkdir()
    output = campaign / "run"
    events: list[str] = []
    lock_held = False

    @contextmanager
    def fake_lock(path, *, exclusive=False):
        nonlocal lock_held
        assert Path(path) == campaign
        assert exclusive is False
        assert lock_held is False
        lock_held = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            lock_held = False

    def validate(path, *, verify_artifacts=True):
        assert lock_held is True
        assert Path(path) == campaign
        assert verify_artifacts is True
        events.append("validate")
        return {"shift_n": 2}

    identity = c001_run.IdentityEvidence(
        board="rig-b",
        mac="02:00:00:00:00:aa",
        image_marker="Buildroot 2023.02.6",
        kernel="5.10.160",
        interface="eth0",
    )
    artifact = campaign / "placeholder"

    def fake_run_board(**kwargs):
        assert lock_held is True
        assert kwargs["attempt_ledger_path"] == campaign / "attempt-ledger.jsonl"
        assert kwargs["recovery_ledger_path"] == campaign / "recovery-ledger.jsonl"
        events.append("run")
        return c001_run.BoardRunArtifacts(
            run_id="ab" * 16,
            attempt_n=2,
            remote_run_dir="/userdata/skyweave/c001/run-" + "ab" * 16,
            output_dir=output,
            provision_path=artifact,
            run_binding_path=artifact,
            stats_path=artifact,
            ccl_log_path=artifact,
            packet_log_path=artifact,
            exit_status_path=artifact,
            run_log_path=artifact,
            fg_mask_path=None,
            identity=identity,
            wall_s=1.0,
            power_cycles=0,
            recovery_ledger_snapshot=None,
            attempt_ledger_snapshot=artifact,
        )

    monkeypatch.setattr(c001_run, "_CANONICAL_CAMPAIGN_DIR", campaign)
    monkeypatch.setattr(c001, "campaign_shift_lock", fake_lock)
    monkeypatch.setattr(c001, "validate_current_shift", validate)
    monkeypatch.setattr(c001_run, "run_board", fake_run_board)

    c001_run.main(
        [
            "--manifest",
            "probe.json",
            "--knobs",
            "{}",
            "--board",
            "rig-b",
            "--host",
            "192.0.2.104",
            "--expected-mac",
            "02:00:00:00:00:aa",
            "--expected-image-marker",
            "Buildroot 2023.02.6",
            "--expected-kernel",
            "5.10.160",
            "--jump-host",
            "jetson-ts",
            "--campaign-dir",
            str(campaign),
            "--out",
            str(output),
        ]
    )

    assert events == ["lock-enter", "validate", "run", "lock-exit"]
    assert json.loads(capsys.readouterr().out)["attempt_n"] == 2


def test_successor_carries_attempt_prefix_and_next_run_reserves_attempt_two(
    sparse_probe, tmp_path
):
    campaign = tmp_path / "C-001"
    campaign.mkdir()
    ledger = campaign / "ledger.jsonl"
    ledger.write_bytes(b"")
    attempt_ledger = campaign / "attempt-ledger.jsonl"
    recovery_ledger = campaign / "recovery-ledger.jsonl"
    recovery_ledger.write_bytes(b"")
    first_run_id = "71" * 16
    first = c001_run._reserve_attempt(
        attempt_ledger,
        run_id=first_run_id,
        expected=_identity(),
        seed=int(c001.load_probe_manifest(sparse_probe.manifest_path)["seed"]),
        manifest_sha256=c001.sha256_file(sparse_probe.manifest_path),
        max_physical_attempts=c001.MAX_EXPERIMENTS - 3,
    )
    c001_run.record_attempt_outcome(
        attempt_ledger,
        run_id=first_run_id,
        outcome="run_failed",
        wall_s=1.0,
        wedge=False,
        error="predecessor diagnostic failure",
    )
    inherited = attempt_ledger.read_bytes()
    c001.record_campaign_stop(
        ledger,
        category="operator_stop",
        reason="test rollover",
    )

    opened = c001.start_successor_shift(
        campaign,
        expected_stop_sha256=c001.sha256_file(campaign / "STOP.json"),
        note="test-authorized successor",
    )
    assert opened["shift_n"] == 2
    assert first["attempt_n"] == 1
    assert attempt_ledger.read_bytes() == inherited
    assert c001.validate_current_shift(campaign)["shift_n"] == 2

    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    transport = IdentityTransport(fail_first=True)
    with pytest.raises(c001_run.IdentityProbeUnavailable, match="probe failed"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=campaign / "attempt-002",
            attempt_ledger_path=attempt_ledger,
            recovery_ledger_path=recovery_ledger,
            transport=transport,
            run_id_factory=lambda: "72" * 16,
        )

    rows = [json.loads(line) for line in attempt_ledger.read_text().splitlines()]
    reservations = [row for row in rows if row["event"] == "attempt_reserved"]
    assert [row["attempt_n"] for row in reservations] == [1, 2]
    assert attempt_ledger.read_bytes().startswith(inherited)
    assert c001.validate_current_shift(campaign)["shift_n"] == 2


def test_failed_physical_attempts_consume_the_reserved_physical_budget(
    sparse_probe, tmp_path, monkeypatch
):
    # Other tests exercise the real durability calls.  This stress case is
    # about reservation counting, and 80 APFS fsyncs can dominate the suite.
    monkeypatch.setattr(c001_run.os, "fsync", lambda descriptor: None)
    validated_manifest = c001.load_probe_manifest(sparse_probe.manifest_path)
    monkeypatch.setattr(c001, "load_probe_manifest", lambda path: validated_manifest)
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    ledger = tmp_path / "attempt-ledger.jsonl"
    physical_budget = c001.MAX_EXPERIMENTS - 3
    for index in range(physical_budget):
        with pytest.raises(c001_run.IdentityProbeUnavailable, match="probe failed"):
            c001_run.run_board(
                manifest_path=sparse_probe.manifest_path,
                knobs={},
                expected_identity=_identity(),
                expected_kernel="5.10.160",
                spec=_spec(),
                binary=binary,
                output_dir=tmp_path / f"failed-{index}",
                attempt_ledger_path=ledger,
                transport=IdentityTransport(fail_first=True),
                run_id_factory=lambda index=index: f"{index + 1:032x}",
            )
    refused_transport = IdentityTransport(fail_first=True)
    with pytest.raises(c001.GuardrailViolation, match="40-experiment"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "failed-over-budget",
            attempt_ledger_path=ledger,
            transport=refused_transport,
            run_id_factory=lambda: f"{physical_budget + 1:032x}",
        )
    assert refused_transport.commands == []
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert sum(row["event"] == "attempt_reserved" for row in rows) == physical_budget
    assert sum(row["event"] == "attempt_outcome" for row in rows) == physical_budget
    assert all(
        row.get("outcome") == "preflight_failure"
        for row in rows
        if row["event"] == "attempt_outcome"
    )


def test_physical_attempt_cap_reserves_future_n2_n3_after_only_n1(
    sparse_probe, tmp_path, monkeypatch
):
    monkeypatch.setattr(c001_run.os, "fsync", lambda descriptor: None)
    validated_manifest = c001.load_probe_manifest(sparse_probe.manifest_path)
    monkeypatch.setattr(c001, "load_probe_manifest", lambda path: validated_manifest)
    monkeypatch.setattr(
        c001,
        "read_ledger",
        lambda path, verify_artifacts=True: [{"n": 1, "attempt_budget": None}],
    )
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    attempt_ledger = tmp_path / "attempt-ledger.jsonl"
    physical_budget = c001.MAX_EXPERIMENTS - 3
    for index in range(physical_budget):
        with pytest.raises(c001_run.IdentityProbeUnavailable):
            c001_run.run_board(
                manifest_path=sparse_probe.manifest_path,
                knobs={},
                expected_identity=_identity(),
                expected_kernel="5.10.160",
                spec=_spec(),
                binary=binary,
                output_dir=tmp_path / f"failed-{index}",
                attempt_ledger_path=attempt_ledger,
                transport=IdentityTransport(fail_first=True),
                run_id_factory=lambda index=index: f"{index + 1:032x}",
            )
    refused_transport = IdentityTransport(fail_first=True)
    with pytest.raises(c001.GuardrailViolation, match="40-experiment"):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "failed-over-budget",
            attempt_ledger_path=attempt_ledger,
            transport=refused_transport,
            run_id_factory=lambda: "f1" * 16,
        )
    assert refused_transport.commands == []
    rows = [json.loads(line) for line in attempt_ledger.read_text().splitlines()]
    assert sum(row["event"] == "attempt_reserved" for row in rows) == physical_budget


def test_interrupted_attempt_remains_an_abandoned_budget_reservation(
    sparse_probe, tmp_path
):
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"binary")
    ledger = tmp_path / "attempt-ledger.jsonl"

    def interrupt(**kwargs):
        del kwargs
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        c001_run.run_board(
            manifest_path=sparse_probe.manifest_path,
            knobs={},
            expected_identity=_identity(),
            expected_kernel="5.10.160",
            spec=_spec(),
            binary=binary,
            output_dir=tmp_path / "abandoned",
            attempt_ledger_path=ledger,
            transport=IdentityTransport(),
            provision_fn=interrupt,
            run_id_factory=lambda: "56" * 16,
        )
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == "attempt_reserved"
    assert rows[0]["attempt_n"] == 1
