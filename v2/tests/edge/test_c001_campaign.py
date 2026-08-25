"""C-001 campaign guardrails, scoring, append-only evidence, and recovery model."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
from pathlib import Path

import numpy as np
import pytest

from skyweave2.contracts import FrameEnvelope, Observation2D
from skyweave2.detector.cap import apply_component_cap
from skyweave2.detector.components import MaskComponent
from skyweave2.detector.persistence import PersistenceFilter
from skyweave2.edge import campaign_c001 as c001
from skyweave2.transport import codec


def _binary64_bits(value: float) -> str:
    return struct.pack(">d", float(value)).hex()


def _successful_ccl_row(
    component: dict[str, object], *, frame_seq: int = c001.WARMUP_FRAMES
) -> dict[str, object]:
    return {
        "frame_seq": frame_seq,
        "api_failure": False,
        "s8_label_status": 0,
        "u8_region_num": 1,
        "u32_cur_area_thr": 2,
        "nonzero_region_slots": 1,
        "region_count_mismatch": False,
        "accepted_components": 1,
        "overlap_pairs": 0,
        "components": [component],
    }


def _subject(
    phase: str = "phase1", *, root: Path | None = None
) -> dict[str, bool | None | str | dict[str, str]]:
    evidence = c001.subject_to_template(phase)
    evidence.update(
        {
            "gate_platform_suite_green": True,
            "fenced_paths_untouched": True,
            "probe_input_only": True,
        }
    )
    if phase != "phase1":
        evidence.update(
            {
                "host_board_parity_within_tolerance": True,
                "discriminator_allows_climb": True,
            }
        )
    if root is not None:
        evidence_dir = root / "subject-evidence"
        evidence_dir.mkdir(exist_ok=True)
        evidence["revision_sha"] = "a" * 40
        evidence["source_tree_sha256"] = "f" * 64
        fixture_files = sorted(
            [
                {
                    "path": f"{fixture_root}/fixture.bin",
                    "type": "file",
                    "size": 1,
                    "sha256": "b" * 64,
                }
                for fixture_root in c001.GATE_FIXTURE_ROOTS
            ],
            key=lambda item: item["path"],
        )
        fixture_tree_sha256 = hashlib.sha256(
            c001._canonical_json(
                {
                    "roots": list(c001.GATE_FIXTURE_ROOTS),
                    "files": fixture_files,
                }
            )
        ).hexdigest()
        v1_head_tree_oid = "c" * 40
        v1_head_tree_sha256 = hashlib.sha256(
            c001._canonical_json(
                {
                    "revision_sha": "a" * 40,
                    "git_tree_oid": v1_head_tree_oid,
                    "files": [
                        entry
                        for entry in fixture_files
                        if entry["path"].startswith("v1/src/")
                    ],
                }
            )
        ).hexdigest()
        fixture_manifest_path = evidence_dir / "gate-support.json"
        fixture_manifest_path.write_bytes(
            c001._canonical_json(
                {
                    "schema": c001.GATE_FIXTURE_MANIFEST_SCHEMA,
                    "revision_sha": "a" * 40,
                    "source_tree_sha256": "f" * 64,
                    "roots": list(c001.GATE_FIXTURE_ROOTS),
                    "fixture_tree_sha256": fixture_tree_sha256,
                    "v1_head_tree_oid": v1_head_tree_oid,
                    "v1_head_tree_sha256": v1_head_tree_sha256,
                    "files": fixture_files,
                }
            )
        )
        for key, kind in (
            ("gate_evidence", "gate_platform_suite"),
            ("fenced_evidence", "fenced_paths_status"),
        ):
            path = evidence_dir / f"{kind}.json"
            stdout_path = evidence_dir / f"{kind}.stdout"
            stdout_path.write_text(
                ("123 passed in 4.56s\n" if kind == "gate_platform_suite" else "")
                + f"C001_EVIDENCE_PASS kind={kind} revision={'a' * 40}"
                + (" changed_paths=0" if kind == "fenced_paths_status" else "")
                + "\n"
            )
            path.write_text(
                json.dumps(
                    {
                        "schema": "skyweave-c001-subject-evidence/1",
                        "kind": kind,
                        "revision_sha": "a" * 40,
                        "source_tree_sha256": "f" * 64,
                        "exit_code": 0,
                        "asserted_outcome": True,
                        "command": (
                            "python -m pytest -q"
                            if kind == "gate_platform_suite"
                            else "git status --porcelain --untracked-files=all -- "
                            "v1 ':(glob)**/golden/**' "
                            "v2/docs/DETECTION_CONTRACTS_D0.md "
                            "v2/src/skyweave2/contracts v2/tests/contracts v2/proto "
                            "v2/tests/edge/fixtures/gate"
                        ),
                        "stdout_path": stdout_path.relative_to(root).as_posix(),
                        "stdout_sha256": c001.sha256_file(stdout_path),
                        "platform": (
                            {
                                "os": "Linux",
                                "arch": "x86_64",
                                "python": "test-python",
                                "toolchain": (
                                    "test-toolchain;python_optimize=0;"
                                    f"rmem_max={c001.GATE_RMEM_MIN_BYTES};"
                                    f"rmem_default={c001.GATE_RMEM_MIN_BYTES}"
                                ),
                                "rmem_max_bytes": c001.GATE_RMEM_MIN_BYTES,
                                "rmem_default_bytes": c001.GATE_RMEM_MIN_BYTES,
                            }
                            if kind == "gate_platform_suite"
                            else None
                        ),
                        "checked_paths": (
                            []
                            if kind == "gate_platform_suite"
                            else [
                                "v1",
                                ":(glob)**/golden/**",
                                "v2/docs/DETECTION_CONTRACTS_D0.md",
                                "v2/src/skyweave2/contracts",
                                "v2/tests/contracts",
                                "v2/proto",
                                "v2/tests/edge/fixtures/gate",
                            ]
                        ),
                        "changed_paths": [],
                        "fixture_manifest_path": (
                            fixture_manifest_path.relative_to(root).as_posix()
                            if kind == "gate_platform_suite"
                            else None
                        ),
                        "fixture_manifest_sha256": (
                            c001.sha256_file(fixture_manifest_path)
                            if kind == "gate_platform_suite"
                            else None
                        ),
                        "fixture_tree_sha256": (
                            fixture_tree_sha256 if kind == "gate_platform_suite" else None
                        ),
                        "pythonpath": (
                            "src:../v1/src" if kind == "gate_platform_suite" else None
                        ),
                    }
                )
                + "\n"
            )
            evidence[key] = {
                "path": path.relative_to(root).as_posix(),
                "sha256": c001.sha256_file(path),
            }
    return evidence


@pytest.fixture(scope="module")
def sparse_probe(tmp_path_factory):
    root = tmp_path_factory.mktemp("c001-probe")
    return c001.prepare_probe(root, kind="sparse", seed=101)


def _mutated_manifest(prepared, tmp_path: Path, **updates) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = json.loads(prepared.manifest_path.read_text())
    payload.update(updates)
    path = tmp_path / "probe_manifest.json"
    path.write_text(json.dumps(payload))
    return path


def _write_budget_snapshots(
    root: Path,
    *,
    run_id: str,
    identity: dict[str, str],
    seed: int,
    manifest_sha256: str,
    wall_s: float,
) -> dict[str, object]:
    recovery = root / "recovery-ledger-snapshot.jsonl"
    recovery.write_bytes(b"")
    reservation_material = {
        "schema": c001.ATTEMPT_LEDGER_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "event": "attempt_reserved",
        "run_id": run_id,
        "attempt_n": 1,
        "board": identity["board"],
        "mac": identity["mac"].lower(),
        "seed": seed,
        "manifest_sha256": manifest_sha256,
        "recorded_at": "2026-08-21T00:00:00+00:00",
        "previous_sha256": "0" * 64,
    }
    reservation_sha = hashlib.sha256(c001._canonical_json(reservation_material)).hexdigest()
    reservation = {**reservation_material, "row_sha256": reservation_sha}
    outcome_material = {
        "schema": c001.ATTEMPT_LEDGER_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "event": "attempt_outcome",
        "run_id": run_id,
        "attempt_n": 1,
        "outcome_n": 1,
        "outcome": "run_complete",
        "wall_s": wall_s,
        "wedge": False,
        "error": None,
        "recorded_at": "2026-08-21T00:00:01+00:00",
        "previous_sha256": reservation_sha,
    }
    outcome_sha = hashlib.sha256(c001._canonical_json(outcome_material)).hexdigest()
    outcome = {**outcome_material, "row_sha256": outcome_sha}
    attempts = root / "attempt-ledger-snapshot.jsonl"
    attempts.write_bytes(c001._canonical_json(reservation) + c001._canonical_json(outcome))
    return {
        "recovery_ledger": {
            "path": recovery.name,
            "sha256": c001.sha256_file(recovery),
            "tip_sha256": "0" * 64,
        },
        "attempt_reservation": {
            "run_id": run_id,
            "attempt_n": 1,
            "reservation_sha256": reservation_sha,
        },
        "attempt_ledger": {
            "path": attempts.name,
            "sha256": c001.sha256_file(attempts),
            "tip_sha256": outcome_sha,
        },
    }


def _perfect_recall_rows(prepared) -> list[dict]:
    manifest = c001.load_probe_manifest(prepared.manifest_path)
    truth = c001.load_truth_slots(manifest, prepared.manifest_path)
    rows = []
    for frame_seq in range(c001.WARMUP_FRAMES, manifest["total_frames"]):
        slot = frame_seq % manifest["ram_clip_frames"]
        components = []
        for mover in truth[slot]["movers"]:
            if not mover["visible"]:
                continue
            components.append(
                {
                    "bbox_x": math.floor(mover["u"]),
                    "bbox_y": math.floor(mover["v"]),
                    "bbox_w": 1,
                    "bbox_h": 1,
                    "area_px": 1,
                }
            )
        rows.append({"frame_seq": frame_seq, "components": components})
    return rows


def _retained_score(
    prepared,
    root: Path,
    *,
    region_num_overrides: dict[int, int] | None = None,
) -> c001.WrittenArtifact:
    """Build one internally consistent synthetic raw-input bundle for tamper tests."""

    manifest = c001.load_probe_manifest(prepared.manifest_path)
    truth = c001.load_truth_slots(manifest, prepared.manifest_path)
    with prepared.clip_path.open("rb") as handle:
        session = c001.read_injection_session(handle)
        stored_frames = list(c001.iter_injection_frames(handle))
    ccl_rows = []
    packets = []
    observation_count = 0
    persistence = PersistenceFilter(c001.detector_config_for({}))
    for frame_seq in range(c001.WARMUP_FRAMES, manifest["total_frames"]):
        slot = frame_seq % manifest["ram_clip_frames"]
        visible = [mover for mover in truth[slot]["movers"] if mover["visible"]]
        components = [
            {
                "centroid_u": mover["u"],
                "centroid_u_bits": _binary64_bits(mover["u"]),
                "centroid_v": mover["v"],
                "centroid_v_bits": _binary64_bits(mover["v"]),
                "bbox_x": math.floor(mover["u"]),
                "bbox_y": math.floor(mover["v"]),
                "bbox_w": 2,
                "bbox_h": 2,
                "area_px": 2,
            }
            for mover in visible
        ]
        region_num = (region_num_overrides or {}).get(frame_seq, len(components))
        ccl_rows.append(
            {
                "frame_seq": frame_seq,
                "api_failure": False,
                "s8_label_status": 0,
                "u8_region_num": region_num,
                "u32_cur_area_thr": 2,
                "nonzero_region_slots": len(components),
                "region_count_mismatch": region_num != len(components),
                "accepted_components": len(components),
                "overlap_pairs": 0,
                "components": components,
            }
        )
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
            for component in components
        ]
        emitted = apply_component_cap(persistence.update(raw_components), c001.COMPONENT_CAP).kept
        if emitted:
            stored_frame = stored_frames[slot]
            loop_pass = frame_seq // manifest["ram_clip_frames"]
            envelope = FrameEnvelope(
                camera_id=session.camera_id,
                session_uuid=session.session_uuid,
                frame_seq=frame_seq,
                capture_ts_ns=stored_frame.capture_ts_ns
                + loop_pass * manifest["ram_loop_pts_stride_ns"],
                clock_domain=session.clock_domain,
                time_sync_error_ms=stored_frame.time_sync_error_ms,
                exposure_us=session.exposure_us,
                gain_db=session.gain_db,
                full_width=session.full_width,
                full_height=session.full_height,
                proc_width=session.proc_width,
                proc_height=session.proc_height,
                calibration_rev=session.calibration_rev,
                detector_rev=session.detector_rev,
                line_readout_us=session.line_readout_us,
            )
            observations = [
                Observation2D(
                    envelope=envelope,
                    obs_id=index,
                    u=c001.proc_to_full(component.centroid_u, 2.0),
                    v=c001.proc_to_full(component.centroid_v, 2.0),
                    cov_uu=c001.detector_config_for({}).centroid_cov_floor_px2,
                    cov_vv=c001.detector_config_for({}).centroid_cov_floor_px2,
                    bbox_x=2 * component.bbox_x,
                    bbox_y=2 * component.bbox_y,
                    bbox_w=2 * component.bbox_w,
                    bbox_h=2 * component.bbox_h,
                    area_px=component.area_px,
                    persistence_count=persistence_count,
                    confidence=float(
                        np.float32(c001.component_confidence(component, persistence_count))
                    ),
                    local_blob_id=blob_id,
                    evidence_ref=None,
                )
                for index, (component, persistence_count, blob_id) in enumerate(emitted)
            ]
            observation_count += len(observations)
            packets.append(codec.encode_observation_packet(envelope, observations).hex())
    ccl_path = root / "ccl.jsonl"
    ccl_path.write_text("".join(json.dumps(row) + "\n" for row in ccl_rows))
    packet_path = root / "packets.hex"
    packet_path.write_text("\n".join(packets) + "\n")
    knobs = {"gmm2.match_sigmas": 3.3, "gmm2.var_min": 33.3}
    stats = {
        **_bound_stats(manifest),
        "ccl_threshold_runaway_failures": 0,
        "ccl_sub_cap_failures": 0,
        "ccl_other_failures": 0,
        "ccl_region_count_mismatch_frames": sum(
            row["region_count_mismatch"] for row in ccl_rows
        ),
        "overlapping_bbox_pairs": 0,
        "frames_with_overlapping_bboxes": 0,
        "capture_events": len(packets),
        "observations_sent": observation_count,
    }
    stats_path = root / "stats.json"
    stats_path.write_text(json.dumps(stats))
    identity = {
        "board": "board-a",
        "mac": "02:00:00:00:00:AA",
        "image_marker": "buildroot-2023.02.6-kernel-5.10.160",
    }
    binding = {
        "schema": c001.RUN_BINDING_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "board": "board-a",
        "seed": manifest["seed"],
        "identity": identity,
        "source_mode": "inject-ram",
        "proc_width": 1152,
        "proc_height": 648,
        "total_frames": manifest["total_frames"],
        "ram_clip_frames": manifest["ram_clip_frames"],
        "probe_manifest_sha256": c001.sha256_file(prepared.manifest_path),
        "remote_clip_sha256": manifest["clip_sha256"],
        "stats_sha256": c001.sha256_file(stats_path),
        "ccl_log_sha256": c001.sha256_file(ccl_path),
        "packet_log_sha256": c001.sha256_file(packet_path),
        "run_id": "1" * 32,
        "remote_run_dir": f"/root/c001-runs/{'1' * 32}",
    }
    binding_path = root / "run_binding.json"
    binding_path.write_text(json.dumps(binding))
    binary_sha = "c" * 64
    remote_run_dir = f"/root/c001-runs/{'1' * 32}"
    budget_proof = _write_budget_snapshots(
        root,
        run_id="1" * 32,
        identity=identity,
        seed=manifest["seed"],
        manifest_sha256=c001.sha256_file(prepared.manifest_path),
        wall_s=10.0,
    )
    provision = {
        "schema": "d8-provision/1",
        "node": {
            "name": "board-a",
            "remote_dir": remote_run_dir,
            "ld_library_path": "/oem/usr/lib",
        },
        "remote_binary": f"{remote_run_dir}/skyweave-edge",
        "binary_verified": True,
        "local_sha256": binary_sha,
        "remote_sha256": binary_sha,
        "source_remote_path": f"{remote_run_dir}/probe.swij",
        "source_local_sha256": manifest["clip_sha256"],
        "source_remote_sha256": manifest["clip_sha256"],
        "source_verified": True,
        "stats": stats,
        "collected": [
            "stats.json",
            "ccl.jsonl",
            "packets.hex",
            "exit.status",
            "run.log",
        ],
        "run_id": "1" * 32,
        "remote_run_dir": remote_run_dir,
        "daemon_exit_code": 0,
        "exit_status": 0,
        "daemon_stopped": True,
        "completed_before_deadline": True,
        "stop_succeeded": None,
        "power_cycles": 0,
        "recovery_attempts": [],
        **budget_proof,
        "probe_manifest_sha256": c001.sha256_file(prepared.manifest_path),
        "identity_preflight": {
            **identity,
            "kernel": "5.10.160",
            "interface": "eth0",
        },
        "runtime_ive_library": {
            "path": "/oem/usr/lib/librve.so",
            "sha256_before": "7" * 64,
            "sha256_after": "7" * 64,
            "stable": True,
        },
        "collected_sha256": {
            "stats.json": c001.sha256_file(stats_path),
            "ccl.jsonl": c001.sha256_file(ccl_path),
            "packets.hex": c001.sha256_file(packet_path),
        },
        "argv": " ".join(
            [
                f"env LD_LIBRARY_PATH=/oem/usr/lib {remote_run_dir}/skyweave-edge",
                f"--inject-ram {remote_run_dir}/probe.swij",
                f"--stats {remote_run_dir}/stats.json",
                f"--ccl-log {remote_run_dir}/ccl.jsonl",
                f"--packet-log {remote_run_dir}/packets.hex",
                f"--ram-loop-frames {manifest['total_frames']}",
                f"--ram-loop-pts-stride-ns {manifest['ram_loop_pts_stride_ns']}",
                f"--ram-budget-mb {manifest['ram_budget_mb']}",
                "--ram-loop-period-ns 0",
                "--detector ive",
                "--proc 1152x648",
                "--warmup 30",
                "--cap 7",
                "--min-area-px 2",
                "--morph-open 1",
                f"--gmm2-match-sigmas {stats['gmm2_match_sigmas']}",
                f"--gmm2-var-min {stats['gmm2_var_min']}",
            ]
        ),
        "wall_s": 10.0,
    }
    exit_status_path = root / "exit.status"
    exit_status_path.write_text("0\n")
    run_log_path = root / "run.log"
    run_log_path.write_text("C-001 daemon completed\n")
    provision["collected_sha256"].update(
        {
            "exit.status": c001.sha256_file(exit_status_path),
            "run.log": c001.sha256_file(run_log_path),
        }
    )
    provision_path = root / "provision.json"
    provision_path.write_text(json.dumps(provision))
    artifact = c001.score_board_run(
        stats_path,
        ccl_path,
        packet_path,
        prepared.manifest_path,
        binding_path,
        board="board-a",
        knobs=knobs,
        output_path=root / "score.json",
        provision_path=provision_path,
        exit_status_path=exit_status_path,
        run_log_path=run_log_path,
    )
    assert isinstance(artifact, c001.WrittenArtifact)
    return artifact


def test_knob_whitelist_aliases_and_every_range_can_fail():
    assert c001.normalize_knobs(
        {
            "min_area_px": 64,
            "morph_open": 0,
            "gmm2.match_sigmas": 4.0,
            "gmm2.var_min": 100.0,
        }
    ) == {
        "ive_approx.match_sigmas": 4.0,
        "ive_approx.var_min": 100.0,
        "min_area_px": 64,
        "open_radius_px": 0,
    }
    for knobs in (
        {"not_a_knob": 1},
        {"min_area_px": 1},
        {"min_area_px": True},
        {"morph_open": 2},
        {"open_radius_px": -1},
        {"match_sigmas": 3.0},
        {"gmm2.match_sigmas": 4.01},
        {"gmm2.var_min": 24.99},
        {"gmm2.var_min": float("nan")},
        {"morph_open": 1, "open_radius_px": 1},
    ):
        with pytest.raises(c001.CampaignError):
            c001.normalize_knobs(knobs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proc_width", 1151),
        ("proc_height", 647),
        ("warmup_frames", 29),
        ("noise_dn", 2.01),
        ("cap", 8),
        ("seed", 102),
    ],
)
def test_each_frozen_axis_can_fail(field, value):
    settings = {
        "proc_width": 1152,
        "proc_height": 648,
        "warmup_frames": 30,
        "noise_dn": 2.0,
        "cap": 7,
        "seed": 101,
    }
    settings[field] = value
    with pytest.raises(c001.GuardrailViolation, match=field):
        c001.validate_frozen_settings(settings, seed=101)


def test_subject_to_is_explicit_and_climbing_needs_discriminator_parity():
    c001.validate_subject_to(_subject(), "phase1")
    for key in (
        "gate_platform_suite_green",
        "fenced_paths_untouched",
        "probe_input_only",
    ):
        evidence = _subject()
        evidence[key] = False
        with pytest.raises(c001.GuardrailViolation, match=key):
            c001.validate_subject_to(evidence, "phase1")
    with pytest.raises(c001.GuardrailViolation, match="host_board_parity"):
        c001.validate_subject_to(_subject(), "climb")
    c001.validate_subject_to(_subject("climb"), "climb")


def test_ledgerable_subject_to_refuses_unsupported_boolean_claims(tmp_path):
    with pytest.raises(c001.GuardrailViolation, match="must contain exactly"):
        c001.validate_subject_to(_subject(), "phase1", evidence_root=tmp_path / "ledger.jsonl")
    evidence = _subject(root=tmp_path)
    gate_path = tmp_path / str(evidence["gate_evidence"]["path"])
    gate = json.loads(gate_path.read_text())
    gate["command"] = "/bin/true"
    gate_path.write_text(json.dumps(gate) + "\n")
    evidence["gate_evidence"]["sha256"] = c001.sha256_file(gate_path)
    with pytest.raises(c001.GuardrailViolation, match="full pytest suite"):
        c001.validate_subject_to(evidence, "phase1", evidence_root=tmp_path / "ledger.jsonl")
    for required_pathspec in (
        "v1",
        "':(glob)**/golden/**'",
        "v2/docs/DETECTION_CONTRACTS_D0.md",
        "v2/src/skyweave2/contracts",
        "v2/tests/contracts",
        "v2/proto",
        "v2/tests/edge/fixtures/gate",
    ):
        evidence = _subject(root=tmp_path)
        fenced_path = tmp_path / str(evidence["fenced_evidence"]["path"])
        fenced = json.loads(fenced_path.read_text())
        fenced["command"] = fenced["command"].replace(required_pathspec, "")
        fenced_path.write_text(json.dumps(fenced) + "\n")
        evidence["fenced_evidence"]["sha256"] = c001.sha256_file(fenced_path)
        with pytest.raises(c001.GuardrailViolation, match="exact scoped git status"):
            c001.validate_subject_to(evidence, "phase1", evidence_root=tmp_path / "ledger.jsonl")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("os", "Darwin", "Linux x86_64/amd64"),
        ("arch", "arm64", "Linux x86_64/amd64"),
        ("rmem_max_bytes", c001.GATE_RMEM_MIN_BYTES - 1, "rmem_max_bytes"),
        (
            "rmem_default_bytes",
            c001.GATE_RMEM_MIN_BYTES - 1,
            "rmem_default_bytes",
        ),
        (
            "toolchain",
            "test-toolchain;rmem_max=8388608;rmem_default=8388608",
            "receive-buffer facts disagree",
        ),
        (
            "toolchain",
            f"test-toolchain;python_optimize=2;"
            f"rmem_max={c001.GATE_RMEM_MIN_BYTES};"
            f"rmem_default={c001.GATE_RMEM_MIN_BYTES}",
            "optimize level zero",
        ),
        (
            "toolchain",
            f"test-toolchain;rmem_max={c001.GATE_RMEM_MIN_BYTES};"
            f"rmem_default={c001.GATE_RMEM_MIN_BYTES}",
            "optimize level zero",
        ),
    ],
)
def test_ledgerable_subject_to_requires_authoritative_gate_platform(
    tmp_path, field, value, message
):
    evidence = _subject(root=tmp_path)
    gate_path = tmp_path / str(evidence["gate_evidence"]["path"])
    gate = json.loads(gate_path.read_text())
    gate["platform"][field] = value
    gate_path.write_text(json.dumps(gate) + "\n")
    evidence["gate_evidence"]["sha256"] = c001.sha256_file(gate_path)
    with pytest.raises(c001.GuardrailViolation, match=message):
        c001.validate_subject_to(evidence, "phase1", evidence_root=tmp_path / "ledger.jsonl")


@pytest.mark.parametrize(
    "summary",
    [
        "122 passed, 1 skipped",
        "122 passed, 1 xfailed",
        "122 passed, 1 xpassed",
        "122 passed, 1 deselected",
        "122 passed, 1 warning",
    ],
)
def test_ledgerable_subject_to_rejects_incomplete_gate_suite(tmp_path, summary):
    evidence = _subject(root=tmp_path)
    gate_path = tmp_path / str(evidence["gate_evidence"]["path"])
    gate = json.loads(gate_path.read_text())
    stdout_path = tmp_path / gate["stdout_path"]
    stdout = stdout_path.read_text()
    stdout_path.write_text(stdout.replace("123 passed", summary))
    gate["stdout_sha256"] = c001.sha256_file(stdout_path)
    gate_path.write_text(json.dumps(gate) + "\n")
    evidence["gate_evidence"]["sha256"] = c001.sha256_file(gate_path)
    with pytest.raises(c001.GuardrailViolation, match="full pytest suite"):
        c001.validate_subject_to(evidence, "phase1", evidence_root=tmp_path / "ledger.jsonl")


def test_ledgerable_subject_to_rejects_canonicalized_fixture_path_escape(tmp_path):
    evidence = _subject(root=tmp_path)
    gate_path = tmp_path / str(evidence["gate_evidence"]["path"])
    gate = json.loads(gate_path.read_text())
    fixture_path = tmp_path / gate["fixture_manifest_path"]
    fixture = json.loads(fixture_path.read_text())
    fixture["files"][0]["path"] = (
        f"{c001.GATE_FIXTURE_ROOTS[0]}/../../outside"
    )
    fixture_tree = hashlib.sha256(
        c001._canonical_json(
            {
                "roots": list(c001.GATE_FIXTURE_ROOTS),
                "files": fixture["files"],
            }
        )
    ).hexdigest()
    fixture["fixture_tree_sha256"] = fixture_tree
    fixture_path.write_text(json.dumps(fixture) + "\n")
    gate["fixture_manifest_sha256"] = c001.sha256_file(fixture_path)
    gate["fixture_tree_sha256"] = fixture_tree
    gate_path.write_text(json.dumps(gate) + "\n")
    evidence["gate_evidence"]["sha256"] = c001.sha256_file(gate_path)

    with pytest.raises(c001.GuardrailViolation, match="out of scope"):
        c001.validate_subject_to(
            evidence,
            "phase1",
            evidence_root=tmp_path / "ledger.jsonl",
        )


def test_probe_is_short_but_truth_and_frames_repeat_exact_ram_slots(sparse_probe):
    manifest = c001.load_probe_manifest(sparse_probe.manifest_path)
    assert manifest["movers"] == c001.SPARSE_PROBE_MOVERS == 3
    assert manifest["total_frames"] == 630
    assert manifest["postwarm_frames"] == 600
    assert manifest["ram_clip_frames"] == 36 < manifest["total_frames"]
    iterator = c001.iter_looped_probe_frames(sparse_probe.manifest_path)
    frames = [next(iterator) for _ in range(38)]
    assert frames[36][1] == 0 and frames[37][1] == 1
    assert np.array_equal(frames[0][2], frames[36][2])
    assert sparse_probe.clip_sha256 == c001.sha256_file(sparse_probe.clip_path)
    assert sparse_probe.truth_sha256 == c001.sha256_file(sparse_probe.truth_path)


def test_manifest_rejects_too_few_frames_and_wrong_sparse_count(sparse_probe, tmp_path):
    short = _mutated_manifest(
        sparse_probe, tmp_path / "short", total_frames=629, postwarm_frames=599
    )
    with pytest.raises(c001.GuardrailViolation):
        c001.load_probe_manifest(short, verify_artifacts=False)
    wrong = _mutated_manifest(sparse_probe, tmp_path / "wrong", movers=4)
    with pytest.raises(c001.GuardrailViolation, match="mover count"):
        c001.load_probe_manifest(wrong, verify_artifacts=False)


def test_gate_acceptance_and_symlink_aliases_are_refused(sparse_probe, tmp_path):
    acceptance_dir = tmp_path / "acceptance"
    acceptance_dir.mkdir()
    gate = acceptance_dir / "probe_manifest.json"
    gate.write_bytes(sparse_probe.manifest_path.read_bytes())
    with pytest.raises(c001.GuardrailViolation, match="gate/acceptance"):
        c001.load_probe_manifest(gate, verify_artifacts=False)

    alias = tmp_path / "probe_manifest.json"
    alias.symlink_to(sparse_probe.manifest_path)
    with pytest.raises(c001.GuardrailViolation, match="symlink"):
        c001.load_probe_manifest(alias)


def test_truth_digest_and_generator_alignment_both_can_fail(sparse_probe, tmp_path):
    root = tmp_path / "probe"
    root.mkdir()
    truth = root / "truth_slots.jsonl"
    truth.write_bytes(sparse_probe.truth_path.read_bytes())
    rows = truth.read_text().splitlines()
    first = json.loads(rows[0])
    first["movers"][0]["u"] += 1.0
    rows[0] = json.dumps(first)
    truth.write_text("\n".join(rows) + "\n")
    payload = json.loads(sparse_probe.manifest_path.read_text())
    payload["truth_sha256"] = c001.sha256_file(truth)
    payload["truth_path"] = truth.name
    payload["clip_path"] = "probe.swij"
    (root / "probe.swij").write_bytes(sparse_probe.clip_path.read_bytes())
    manifest = root / "probe_manifest.json"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(c001.GuardrailViolation, match="exact RAM-loop slots"):
        c001.load_probe_manifest(manifest)


def test_self_consistent_clip_digest_cannot_relabel_noncanonical_luma(sparse_probe, tmp_path):
    root = tmp_path / "forged-probe"
    root.mkdir()
    clip = root / "probe.swij"
    clip.write_bytes(sparse_probe.clip_path.read_bytes())
    forged = bytearray(clip.read_bytes())
    forged[-9] ^= 1  # final luma byte; the eight-byte SWIJ trailer follows it
    clip.write_bytes(forged)
    truth = root / "truth_slots.jsonl"
    truth.write_bytes(sparse_probe.truth_path.read_bytes())
    manifest_payload = json.loads(sparse_probe.manifest_path.read_text())
    manifest_payload["clip_sha256"] = c001.sha256_file(clip)
    manifest = root / "probe_manifest.json"
    manifest.write_text(json.dumps(manifest_payload) + "\n")
    with pytest.raises(c001.GuardrailViolation, match="differs from edge.benchmark"):
        c001.load_probe_manifest(manifest)


def test_failure_classifier_is_exact_and_objective_uses_explicit_denominator():
    assert c001.classify_ccl_failure(0) == "threshold_runaway"
    assert c001.classify_ccl_failure(1) == "sub_cap"
    assert c001.classify_ccl_failure(253) == "sub_cap"
    assert c001.classify_ccl_failure(254) == "other"
    assert c001.classify_ccl_failure(255) == "other"
    stats = {
        "frames_in": 630,
        "ccl_attempts": 600,
        "ccl_api_failures": 0,
        "ccl_label_failures": 12,
        "ccl_threshold_runaway_failures": 5,
        "ccl_sub_cap_failures": 6,
        "ccl_other_failures": 1,
        "ccl_region_count_mismatch_frames": 0,
        "overlapping_bbox_pairs": 4,
        "frames_with_overlapping_bboxes": 3,
    }
    result = c001.compute_objective(stats)
    assert result["detector_fail_rate"] == 12 / 600
    assert result["denominator"] == "ccl_attempts"
    for mutation in (
        {"ccl_attempts": 599},
        {"frames_in": 629},
        {"ccl_other_failures": 0},
        {"ccl_region_count_mismatch_frames": 589},
    ):
        broken = {**stats, **mutation}
        with pytest.raises(c001.CampaignError):
            c001.compute_objective(broken)
    del stats["ccl_attempts"]
    with pytest.raises(c001.CampaignError, match="explicit ccl_attempts"):
        c001.compute_objective(stats)


def test_ccl_log_distinguishes_api_failures_slots_and_accepted_components(tmp_path):
    rows = [
        {
            "frame_seq": 30,
            "api_failure": False,
            "s8_label_status": 0,
            "u8_region_num": 2,
            "u32_cur_area_thr": 2,
            "nonzero_region_slots": 2,
            "region_count_mismatch": False,
            "accepted_components": 1,
            "overlap_pairs": 0,
            "components": [
                {
                    "centroid_u": 2.0,
                    "centroid_u_bits": _binary64_bits(2.0),
                    "centroid_v": 3.0,
                    "centroid_v_bits": _binary64_bits(3.0),
                    "bbox_x": 1,
                    "bbox_y": 2,
                    "bbox_w": 3,
                    "bbox_h": 4,
                    "area_px": 8,
                }
            ],
        },
        {
            "frame_seq": 31,
            "api_failure": False,
            "s8_label_status": -1,
            "u8_region_num": 0,
            "u32_cur_area_thr": 99,
            "nonzero_region_slots": 0,
            "region_count_mismatch": False,
            "accepted_components": 0,
            "overlap_pairs": 0,
        },
        {
            "frame_seq": 32,
            "api_failure": True,
            "s8_label_status": None,
            "u8_region_num": 0,
            "u32_cur_area_thr": 0,
            "nonzero_region_slots": 0,
            "region_count_mismatch": False,
            "accepted_components": 0,
            "overlap_pairs": 0,
        },
    ]
    path = tmp_path / "ccl.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    parsed = c001.load_ccl_log(path)
    assert parsed[0]["nonzero_region_slots"] == 2
    assert parsed[0]["accepted_components"] == 1
    assert parsed[0]["region_count_mismatch"] is False
    assert parsed[1]["failure_class"] == "threshold_runaway"
    assert parsed[2]["failure_class"] == "api_failure"
    aggregate = c001.aggregate_ccl_rows(parsed)
    assert aggregate["ccl_attempts"] == 3
    assert aggregate["ccl_label_failures"] == 1
    assert aggregate["ccl_api_failures"] == 1
    invalid_stats = {"frames_in": 630, **aggregate}
    with pytest.raises(c001.GuardrailViolation, match="ccl_api_failures"):
        c001.compute_objective(invalid_stats)

    legacy_rows = json.loads(json.dumps(rows))
    legacy_rows[0].pop("region_count_mismatch")
    path.write_text("".join(json.dumps(row) + "\n" for row in legacy_rows))
    with pytest.raises(c001.CampaignError, match="lacks explicit diagnostic fields"):
        c001.load_ccl_log(path)

    excess_rows = json.loads(json.dumps(rows))
    excess_rows[0]["u8_region_num"] = 2
    excess_rows[0]["nonzero_region_slots"] = 1
    excess_rows[0]["region_count_mismatch"] = True
    excess_rows[0]["accepted_components"] = 2
    excess_rows[0]["components"].append(
        {
            "centroid_u": 20.0,
            "centroid_u_bits": _binary64_bits(20.0),
            "centroid_v": 30.0,
            "centroid_v_bits": _binary64_bits(30.0),
            "bbox_x": 19,
            "bbox_y": 29,
            "bbox_w": 2,
            "bbox_h": 2,
            "area_px": 4,
        }
    )
    path.write_text("".join(json.dumps(row) + "\n" for row in excess_rows))
    with pytest.raises(c001.CampaignError, match="exceeds nonzero_region_slots"):
        c001.load_ccl_log(path)

    rows[0]["u8_region_num"] = 6
    rows[0]["region_count_mismatch"] = True
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    parsed = c001.load_ccl_log(path)
    assert parsed[0]["region_count_mismatch"] is True
    assert c001.aggregate_ccl_rows(parsed)["ccl_region_count_mismatch_frames"] == 1

    rows[0]["u8_region_num"] = 0
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    parsed = c001.load_ccl_log(path)
    assert parsed[0]["region_count_mismatch"] is True

    rows[0]["region_count_mismatch"] = False
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(c001.CampaignError, match="region_count_mismatch"):
        c001.load_ccl_log(path)

    rows[0]["u8_region_num"] = 2
    rows[0]["region_count_mismatch"] = False
    rows[1]["region_count_mismatch"] = True
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(c001.CampaignError, match="failed CCL row"):
        c001.load_ccl_log(path)

    rows[0]["u8_region_num"] = 2
    rows[0]["region_count_mismatch"] = True
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(c001.CampaignError, match="region_count_mismatch"):
        c001.load_ccl_log(path)


def test_ccl_centroid_bits_restore_exact_value_for_wire_lineage(tmp_path):
    authoritative_u = 175930 / 342
    logged_lower_neighbor = math.nextafter(authoritative_u, -math.inf)
    authoritative_v = 138.25
    assert authoritative_u == 514.4152046783626
    assert logged_lower_neighbor == 514.4152046783624
    assert _binary64_bits(authoritative_u) == "4080135256d495b5"

    def component(advisory_u: float) -> dict[str, object]:
        return {
            "centroid_u": advisory_u,
            "centroid_u_bits": _binary64_bits(authoritative_u),
            "centroid_v": authoritative_v,
            "centroid_v_bits": _binary64_bits(authoritative_v),
            "bbox_x": 504,
            "bbox_y": 128,
            "bbox_w": 22,
            "bbox_h": 22,
            "area_px": 342,
        }

    path = tmp_path / "current-ccl.jsonl"
    path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                _successful_ccl_row(component(logged_lower_neighbor)),
                _successful_ccl_row(component(authoritative_u), frame_seq=31),
            )
        )
    )
    parsed = c001.load_ccl_log(path)
    assert parsed[0]["components"][0]["centroid_u"] == authoritative_u
    assert parsed[0]["components"][0]["centroid_u"] != logged_lower_neighbor

    bbox = {
        "bbox_x": 1008,
        "bbox_y": 256,
        "bbox_w": 44,
        "bbox_h": 44,
        "area_px": 342,
        "persistence_count": 2,
    }
    observation_rows = [
        {"frame_seq": 30, "components": [], "wire_observations": []},
        {
            "frame_seq": 31,
            "components": [bbox],
            "wire_observations": [
                {
                    "obs_id": 0,
                    "u": c001.proc_to_full(authoritative_u, 2.0),
                    "v": c001.proc_to_full(authoritative_v, 2.0),
                    "cov_uu": 0.25,
                    "cov_uv": 0.0,
                    "cov_vv": 0.25,
                    **bbox,
                    "confidence": 1.0,
                    "local_blob_id": 0,
                    "evidence_ref": None,
                }
            ],
        },
    ]
    c001.validate_observation_lineage(parsed, observation_rows)
    forged_wire = json.loads(json.dumps(observation_rows))
    forged_wire[1]["wire_observations"][0]["u"] = c001.proc_to_full(
        logged_lower_neighbor, 2.0
    )
    with pytest.raises(c001.GuardrailViolation, match="wire observation fields"):
        c001.validate_observation_lineage(parsed, forged_wire)


def test_current_ccl_centroid_bits_reject_missing_forged_and_malformed_values(tmp_path):
    authoritative_u = 175930 / 342
    authoritative_v = 138.25
    base = {
        "centroid_u": authoritative_u,
        "centroid_u_bits": _binary64_bits(authoritative_u),
        "centroid_v": authoritative_v,
        "centroid_v_bits": _binary64_bits(authoritative_v),
        "bbox_x": 504,
        "bbox_y": 128,
        "bbox_w": 22,
        "bbox_h": 22,
        "area_px": 342,
    }
    missing_u = dict(base)
    missing_u.pop("centroid_u_bits")
    missing_v = dict(base)
    missing_v.pop("centroid_v_bits")
    forged = {**base, "centroid_u_bits": _binary64_bits(authoritative_u + 1.0)}
    malformed = {**base, "centroid_u_bits": _binary64_bits(authoritative_u).upper()}
    beyond_one_ulp = {
        **base,
        "centroid_u": math.nextafter(
            math.nextafter(authoritative_u, -math.inf), -math.inf
        ),
    }
    cases = (
        ("missing-u", missing_u, "incomplete or unknown"),
        ("missing-v", missing_v, "incomplete or unknown"),
        ("forged", forged, "more than 1 binary64 ULP"),
        ("malformed", malformed, "16 lowercase hexadecimal digits"),
        ("beyond-one-ulp", beyond_one_ulp, "more than 1 binary64 ULP"),
    )
    for name, candidate, error in cases:
        path = tmp_path / f"{name}.jsonl"
        path.write_text(json.dumps(_successful_ccl_row(candidate)) + "\n")
        with pytest.raises(c001.CampaignError, match=error):
            c001.load_ccl_log(path)


def test_archived_ccl_without_centroid_bits_remains_replayable(tmp_path):
    legacy_component = {
        "centroid_u": 514.4152046783624,
        "centroid_v": 138.34210526315787,
        "bbox_x": 504,
        "bbox_y": 128,
        "bbox_w": 22,
        "bbox_h": 22,
        "area_px": 342,
    }
    archive = (
        tmp_path
        / c001.SHIFT_HISTORY_DIRECTORY
        / "shift-0003-c30775ed3845"
        / "artifacts"
    )
    archive.mkdir(parents=True)
    archived_path = archive / "ccl.jsonl"
    archived_path.write_text(json.dumps(_successful_ccl_row(legacy_component)) + "\n")
    parsed = c001.load_ccl_log(archived_path)
    assert parsed[0]["components"][0]["centroid_u"] == legacy_component["centroid_u"]

    current_path = tmp_path / "C-001" / "artifacts" / "ccl.jsonl"
    current_path.parent.mkdir(parents=True)
    current_path.write_bytes(archived_path.read_bytes())
    with pytest.raises(c001.CampaignError, match="incomplete or unknown"):
        c001.load_ccl_log(current_path)


def test_bbox_recall_is_one_to_one_and_has_no_invented_radius(sparse_probe):
    rows = _perfect_recall_rows(sparse_probe)
    perfect = c001.score_mover_recall(rows, manifest_path=sparse_probe.manifest_path)
    assert perfect["all_truth_recalled"] is True
    assert perfect["recall"] == 1.0
    assert "no radius" in perfect["definition"]

    # One broad bbox contains all three truth points but is one component, so
    # maximum one-to-one matching may credit it to only one mover.
    shared = []
    for row in rows:
        shared.append(
            {
                "frame_seq": row["frame_seq"],
                "components": [
                    {
                        "bbox_x": 0,
                        "bbox_y": 0,
                        "bbox_w": 1152,
                        "bbox_h": 648,
                        "area_px": 1,
                    }
                ],
            }
        )
    scored = c001.score_mover_recall(shared, manifest_path=sparse_probe.manifest_path)
    assert scored["all_truth_recalled"] is False
    assert all(frame["matched"] <= 1 for frame in scored["frames"])

    # A point one subpixel outside the half-open box does not acquire a radius.
    first = rows[0]
    first["components"][0]["bbox_x"] += 1
    missed = c001.score_mover_recall(rows, manifest_path=sparse_probe.manifest_path)
    assert missed["matched"] == perfect["matched"] - 1


def test_raw_components_suppressed_before_emission_cannot_pass_win_recall(sparse_probe):
    raw_rows = _perfect_recall_rows(sparse_probe)
    raw = c001.score_mover_recall(raw_rows, manifest_path=sparse_probe.manifest_path)
    emitted_rows = [{"frame_seq": row["frame_seq"], "components": []} for row in raw_rows]
    emitted = c001.score_mover_recall(
        emitted_rows,
        manifest_path=sparse_probe.manifest_path,
        coordinate_space="full",
    )
    result = {
        "detector_fail_rate": 0.0,
        "raw_component_mover_recall": raw,
        "mover_recall": emitted,
    }
    assert raw["all_truth_recalled"] is True
    assert emitted["all_truth_recalled"] is False
    assert c001.result_is_win(result) is False


def test_packet_bbox_requires_distinct_scaled_raw_component_lineage():
    component = {
        "centroid_u": 10.5,
        "centroid_v": 20.5,
        "bbox_x": 10,
        "bbox_y": 20,
        "bbox_w": 2,
        "bbox_h": 3,
        "area_px": 6,
    }
    raw = [
        {"frame_seq": 30, "components": [component]},
        {"frame_seq": 31, "components": [component]},
    ]
    valid = [
        {"frame_seq": 30, "components": [], "wire_observations": []},
        {
            "frame_seq": 31,
            "components": [
                {
                    "bbox_x": 20,
                    "bbox_y": 40,
                    "bbox_w": 4,
                    "bbox_h": 6,
                    "area_px": 6,
                    "persistence_count": 2,
                }
            ],
            "wire_observations": [
                {
                    "obs_id": 0,
                    "u": 21.5,
                    "v": 41.5,
                    "cov_uu": 0.25,
                    "cov_uv": 0.0,
                    "cov_vv": 0.25,
                    "bbox_x": 20,
                    "bbox_y": 40,
                    "bbox_w": 4,
                    "bbox_h": 6,
                    "area_px": 6,
                    "persistence_count": 2,
                    "confidence": float(np.float32(6 / 50)),
                    "local_blob_id": 0,
                    "evidence_ref": None,
                }
            ],
        },
    ]
    c001.validate_observation_lineage(raw, valid)
    forged = json.loads(json.dumps(valid))
    forged[1]["components"][0]["bbox_x"] += 1
    with pytest.raises(c001.GuardrailViolation, match="persistence/cap replay"):
        c001.validate_observation_lineage(raw, forged)
    too_early = json.loads(json.dumps(valid))
    too_early[1]["components"][0]["persistence_count"] = 1
    with pytest.raises(c001.GuardrailViolation, match="persistence/cap replay"):
        c001.validate_observation_lineage(raw, too_early)
    wrong_covariance = json.loads(json.dumps(valid))
    wrong_covariance[1]["wire_observations"][0]["cov_uu"] = 1.0
    with pytest.raises(c001.GuardrailViolation, match="wire observation fields"):
        c001.validate_observation_lineage(raw, wrong_covariance)


def test_lineage_replay_resets_persistence_across_a_ccl_failure():
    component = {
        "centroid_u": 10.5,
        "centroid_v": 20.5,
        "bbox_x": 10,
        "bbox_y": 20,
        "bbox_w": 2,
        "bbox_h": 3,
        "area_px": 6,
    }
    raw = [
        {"frame_seq": 30, "components": [component]},
        {"frame_seq": 31, "components": []},
        {"frame_seq": 32, "components": [component]},
        {"frame_seq": 33, "components": [component]},
    ]
    observations = [
        {"frame_seq": 30, "components": []},
        {"frame_seq": 31, "components": []},
        {"frame_seq": 32, "components": []},
        {
            "frame_seq": 33,
            "components": [
                {
                    "bbox_x": 20,
                    "bbox_y": 40,
                    "bbox_w": 4,
                    "bbox_h": 6,
                    "area_px": 6,
                    "persistence_count": 2,
                }
            ],
        },
    ]
    c001.validate_observation_lineage(raw, observations)
    forged = json.loads(json.dumps(observations))
    forged[2]["components"] = list(forged[3]["components"])
    with pytest.raises(c001.GuardrailViolation, match="persistence/cap replay"):
        c001.validate_observation_lineage(raw, forged)


def test_packet_log_rejects_an_empty_observation_datagram(sparse_probe, tmp_path):
    manifest = c001.load_probe_manifest(sparse_probe.manifest_path)
    with sparse_probe.clip_path.open("rb") as handle:
        session = c001.read_injection_session(handle)
        stored = list(c001.iter_injection_frames(handle))
    frame_seq = 30
    slot = frame_seq % manifest["ram_clip_frames"]
    loop_pass = frame_seq // manifest["ram_clip_frames"]
    envelope = FrameEnvelope(
        camera_id=session.camera_id,
        session_uuid=session.session_uuid,
        frame_seq=frame_seq,
        capture_ts_ns=stored[slot].capture_ts_ns + loop_pass * manifest["ram_loop_pts_stride_ns"],
        clock_domain=session.clock_domain,
        time_sync_error_ms=stored[slot].time_sync_error_ms,
        exposure_us=session.exposure_us,
        gain_db=session.gain_db,
        full_width=session.full_width,
        full_height=session.full_height,
        proc_width=session.proc_width,
        proc_height=session.proc_height,
        calibration_rev=session.calibration_rev,
        detector_rev=session.detector_rev,
        line_readout_us=session.line_readout_us,
    )
    packet_log = tmp_path / "packets.hex"
    packet_log.write_text(codec.encode_observation_packet(envelope, []).hex() + "\n")
    with pytest.raises(c001.GuardrailViolation, match="empty observation event"):
        c001.load_observation_rows(packet_log, manifest_path=sparse_probe.manifest_path)


def test_persistence_eligible_emitted_recall_excludes_only_structural_frames(sparse_probe):
    manifest = c001.load_probe_manifest(sparse_probe.manifest_path)
    truth = c001.load_truth_slots(manifest, sparse_probe.manifest_path)
    eligibility = c001.persistence_eligibility(manifest_path=sparse_probe.manifest_path)
    emitted_rows = []
    for frame_seq in range(30, manifest["total_frames"]):
        slot = frame_seq % manifest["ram_clip_frames"]
        components = []
        for mover in truth[slot]["movers"]:
            key = (frame_seq, mover["mover_id"])
            if key not in eligibility["eligible"]:
                continue
            components.append(
                {
                    "bbox_x": math.floor(mover["full_u"]),
                    "bbox_y": math.floor(mover["full_v"]),
                    "bbox_w": 1,
                    "bbox_h": 1,
                    "area_px": 1,
                }
            )
        emitted_rows.append({"frame_seq": frame_seq, "components": components})
    unfiltered = c001.score_mover_recall(
        emitted_rows,
        manifest_path=sparse_probe.manifest_path,
        coordinate_space="full",
    )
    eligible = c001.score_mover_recall(
        emitted_rows,
        manifest_path=sparse_probe.manifest_path,
        coordinate_space="full",
        eligibility="persistence",
    )
    raw = c001.score_mover_recall(
        _perfect_recall_rows(sparse_probe), manifest_path=sparse_probe.manifest_path
    )
    assert unfiltered["all_truth_recalled"] is False
    assert eligible["all_truth_recalled"] is True
    assert eligible["structural_exclusions"]["count"] > 0
    assert c001.result_is_win(
        {
            "detector_fail_rate": 0.0,
            "raw_component_mover_recall": raw,
            "mover_recall": eligible,
        }
    )
    first_nonempty = next(row for row in emitted_rows if row["components"])
    first_nonempty["components"].pop()
    missing = c001.score_mover_recall(
        emitted_rows,
        manifest_path=sparse_probe.manifest_path,
        coordinate_space="full",
        eligibility="persistence",
    )
    assert missing["all_truth_recalled"] is False


def test_host_discriminator_requires_exact_frames_and_raw_pre_persistence_counts():
    rows = [
        {
            "frame_seq": seq,
            "raw_components": 2,
            "components": [
                {"bbox_x": 1, "bbox_y": 1, "bbox_w": 2, "bbox_h": 2},
                {"bbox_x": 10, "bbox_y": 10, "bbox_w": 2, "bbox_h": 2},
            ],
            "overlap_pairs": 0,
        }
        for seq in range(30, 630)
    ]
    summary = c001.summarize_host_rows(rows, total_frames=630)
    assert summary["postwarm_frames"] == 600
    assert summary["raw_component_counts"] == [2] * 600
    assert summary["clean_host"] is None
    rows[-1]["raw_components"] = 254
    rows[-1]["components"] = rows[-1]["components"] * 127
    rows[-1]["overlap_pairs"] = c001.overlapping_bbox_pairs(rows[-1]["components"])
    assert c001.summarize_host_rows(rows, total_frames=630)["clean_host"] is None
    with pytest.raises(c001.CampaignError, match="every post-warm-up"):
        c001.summarize_host_rows(rows[:-1], total_frames=630)


def test_paired_discriminator_needs_zero_extras_and_no_missing_truth(sparse_probe):
    host_rows = _perfect_recall_rows(sparse_probe)
    failure = {
        "frame_seq": 30,
        "api_failure": False,
        "s8_label_status": -1,
    }
    host_8 = {30: list(host_rows[0]["components"])}
    clean = c001.evaluate_host_discriminator(
        host_rows,
        [failure],
        manifest_path=sparse_probe.manifest_path,
        host_8_components=host_8,
    )
    assert clean["clean_host"] is True
    assert clean["discriminator_allows_climb"] is False

    host_rows[0]["components"].append(
        {"bbox_x": 0, "bbox_y": 0, "bbox_w": 1, "bbox_h": 1, "area_px": 1}
    )
    host_8[30] = list(host_rows[0]["components"])
    symmetric = c001.evaluate_host_discriminator(
        host_rows,
        [failure],
        manifest_path=sparse_probe.manifest_path,
        host_8_components=host_8,
        mask_diff_within_tolerance=True,
    )
    assert symmetric["clean_host"] is False
    assert symmetric["discriminator_allows_climb"] is True
    no_mask_parity = c001.evaluate_host_discriminator(
        host_rows,
        [failure],
        manifest_path=sparse_probe.manifest_path,
        host_8_components=host_8,
        mask_diff_within_tolerance=False,
    )
    assert no_mask_parity["discriminator_allows_climb"] is False
    host_rows[0]["components"] = []
    host_8[30] = []
    ambiguous = c001.evaluate_host_discriminator(
        host_rows,
        [failure],
        manifest_path=sparse_probe.manifest_path,
        host_8_components=host_8,
    )
    assert ambiguous["clean_host"] is None
    assert ambiguous["discriminator_allows_climb"] is False


def test_diagonal_fragmentation_cannot_masquerade_as_symmetric_speckle():
    mask = np.zeros((648, 1152), dtype=np.uint8)
    mask[10, 10] = 1
    mask[11, 11] = 1
    four = c001.components_with_connectivity(mask, connectivity=4, min_area_px=1, max_area_px=10)
    eight = c001.components_with_connectivity(mask, connectivity=8, min_area_px=1, max_area_px=10)
    assert len(four) == 2
    assert len(eight) == 1


def test_any_connectivity_divergence_frame_precedes_symmetric_evidence(sparse_probe):
    host_rows = _perfect_recall_rows(sparse_probe)
    extra = {"bbox_x": 0, "bbox_y": 0, "bbox_w": 1, "bbox_h": 1, "area_px": 1}
    host_rows[0]["components"].append(extra)
    host_rows[1]["components"].append(extra)
    failures = [
        {"frame_seq": frame_seq, "api_failure": False, "s8_label_status": -1}
        for frame_seq in (30, 31)
    ]
    host_8 = {
        30: host_rows[0]["components"][:-1],
        31: list(host_rows[1]["components"]),
    }
    decision = c001.evaluate_host_discriminator(
        host_rows,
        failures,
        manifest_path=sparse_probe.manifest_path,
        host_8_components=host_8,
        mask_diff_within_tolerance=True,
    )
    assert decision["decision"] == "connectivity_divergence_re_scope"
    assert decision["discriminator_allows_climb"] is False


def _bound_stats(manifest, *, match_sigmas=3.3, var_min=33.3):
    attempts = manifest["postwarm_frames"]
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
        "min_area_px": 2,
        "morph_open": 1,
        "gmm2_match_sigmas": float(np.float32(match_sigmas)),
        "gmm2_var_min": float(np.float32(var_min)),
        "source_frames_planned": manifest["total_frames"],
        "source_frames_served": manifest["total_frames"],
        "frames_in": manifest["total_frames"],
        "ram_clip_frames": manifest["ram_clip_frames"],
        "ram_clip_bytes": 1152 * 648 * manifest["ram_clip_frames"],
        "ram_loop_pts_stride_ns": manifest["ram_loop_pts_stride_ns"],
        "ram_loop_period_ns": 0,
        "ram_budget_mb": manifest["ram_budget_mb"],
        "fg_mask_limit": 0,
        "ccl_attempts": attempts,
        "ccl_api_failures": 0,
        "ccl_label_failures": 0,
        "ccl_region_count_mismatch_frames": 0,
        "frames_detector_failed": 0,
        "frames_scored": attempts,
    }


def test_board_run_binding_checks_every_echo_and_float32_knobs(sparse_probe):
    manifest = c001.load_probe_manifest(sparse_probe.manifest_path)
    knobs = {"gmm2.match_sigmas": 3.3, "gmm2.var_min": 33.3}
    stats = _bound_stats(manifest)
    c001.validate_board_run_binding(stats, manifest=manifest, knobs=knobs)
    for key, value in (
        ("proc_width", 1151),
        ("warmup_frames", 29),
        ("source_mode", "inject-file"),
        ("gmm2_match_sigmas", 3.4),
        ("max_area_px", 9999),
        ("persistence_frames", 1),
        ("persistence_gate_px", 11.9),
        ("ram_clip_frames", manifest["ram_clip_frames"] - 1),
    ):
        broken = {**stats, key: value}
        with pytest.raises(c001.GuardrailViolation, match=key):
            c001.validate_board_run_binding(broken, manifest=manifest, knobs=knobs)
    broken = {
        **stats,
        "ccl_label_failures": 1,
        "frames_detector_failed": 1,
        "frames_scored": stats["ccl_attempts"] - 1,
        "ccl_region_count_mismatch_frames": stats["ccl_attempts"],
    }
    with pytest.raises(c001.GuardrailViolation, match="region-count mismatches"):
        c001.validate_board_run_binding(broken, manifest=manifest, knobs=knobs)


def test_retained_score_result_is_semantically_recomputed(sparse_probe, tmp_path):
    artifact = _retained_score(sparse_probe, tmp_path)
    c001.validate_retained_score_bundle(artifact.path, artifact.payload)
    forged = json.loads(artifact.path.read_text())
    forged["result"]["detector_fail_rate"] = 0.01
    forged_path = tmp_path / "forged-score.json"
    forged_path.write_text(json.dumps(forged) + "\n")
    with pytest.raises(c001.LedgerIntegrityError, match="semantic replay"):
        c001.validate_retained_score_bundle(forged_path, forged)
    forged = json.loads(artifact.path.read_text())
    forged["provision"]["runtime_ive_library"]["sha256_after"] = "8" * 64
    with pytest.raises(c001.LedgerIntegrityError, match="provision mismatch"):
        c001.validate_retained_score_bundle(artifact.path, forged)


def test_retained_score_reconciles_opaque_region_count_telemetry(sparse_probe, tmp_path):
    root = tmp_path / "mismatch-score"
    root.mkdir()
    artifact = _retained_score(
        sparse_probe,
        root,
        region_num_overrides={30: 6, 31: 0},
    )
    assert artifact.payload["explicit_ccl_counters"][
        "ccl_region_count_mismatch_frames"
    ] == 2
    assert artifact.payload["result"]["ccl_region_count_mismatch_frames"] == 2
    c001.validate_retained_score_bundle(artifact.path, artifact.payload)

    forged = json.loads(artifact.path.read_text())
    forged["explicit_ccl_counters"]["ccl_region_count_mismatch_frames"] = 1
    forged_path = root / "forged-region-count-score.json"
    forged_path.write_text(json.dumps(forged) + "\n")
    with pytest.raises(c001.LedgerIntegrityError, match="semantic replay"):
        c001.validate_retained_score_bundle(forged_path, forged)


def test_phase1_bug_identity_and_binary_cross_bind_to_score(sparse_probe, tmp_path):
    score = json.loads(json.dumps(_retained_score(sparse_probe, tmp_path).payload))
    inputs = score["inputs"]
    inputs["board_fg_masks_sha256"] = "9" * 64
    common = {
        "board": score["board"],
        "seed": score["seed"],
        "manifest_sha256": score["manifest"]["sha256"],
        "clip_sha256": inputs["clip_sha256"],
        "truth_sha256": inputs["truth_sha256"],
        "knobs": score["knobs"],
        "inputs": {
            "board_ccl_log_sha256": inputs["ccl_log_sha256"],
            "board_fg_masks_sha256": "9" * 64,
        },
        "summary": {
            "paired_discriminator": {
                "board_ccl_attempts": 600,
                "board_label_failures": 0,
                "board_region_count_mismatch_frames": 0,
                "board_failure_frame_sequences": [],
                "failure_frames_compared": 0,
                "zero_failure_candidate_path": True,
                "discriminator_allows_climb": False,
            }
        },
    }
    bug = {
        "binding": {
            "identity": score["run_binding"]["identity"],
            "binary_sha256": score["provision"]["binary_sha256"],
            "runtime_ive_library": score["provision"]["runtime_ive_library"],
            "git_sha": "a" * 40,
            "source_tree_sha256": "f" * 64,
        }
    }
    payloads = [bug, common, common, score]
    c001._validate_phase1_chain(payloads)
    omitted_masks = json.loads(json.dumps(payloads))
    omitted_masks[3]["inputs"].pop("board_fg_masks_sha256")
    with pytest.raises(c001.LedgerIntegrityError, match="omitted masks"):
        c001._validate_phase1_chain(omitted_masks)
    wrong_binary = json.loads(json.dumps(payloads))
    wrong_binary[0]["binding"]["binary_sha256"] = "d" * 64
    with pytest.raises(c001.LedgerIntegrityError, match="binary mismatch"):
        c001._validate_phase1_chain(wrong_binary)
    wrong_identity = json.loads(json.dumps(payloads))
    wrong_identity[0]["binding"]["identity"]["mac"] = "02:00:00:00:00:BB"
    with pytest.raises(c001.LedgerIntegrityError, match="identity mismatch"):
        c001._validate_phase1_chain(wrong_identity)
    mismatched_host_mask = json.loads(json.dumps(payloads))
    mismatched_host_mask[1]["summary"]["paired_discriminator"][
        "board_region_count_mismatch_frames"
    ] = 1
    with pytest.raises(c001.LedgerIntegrityError, match="discriminator mismatch"):
        c001._validate_phase1_chain(mismatched_host_mask)
    mismatched_mask_score = json.loads(json.dumps(payloads))
    for index in (1, 2):
        mismatched_mask_score[index]["summary"]["paired_discriminator"][
            "board_region_count_mismatch_frames"
        ] = 1
    with pytest.raises(c001.LedgerIntegrityError, match="score region-count diagnostics"):
        c001._validate_phase1_chain(mismatched_mask_score)
    for phase in ("climb", "confirmation"):
        changed_binary = json.loads(json.dumps(score))
        changed_binary["provision"]["binary_sha256"] = "8" * 64
        with pytest.raises(c001.LedgerIntegrityError, match="binary mismatch"):
            c001.validate_campaign_runtime_binding(bug, changed_binary)
        changed_image = json.loads(json.dumps(score))
        changed_image["run_binding"]["identity"]["image_marker"] = f"other-{phase}"
        with pytest.raises(c001.LedgerIntegrityError, match="image marker"):
            c001.validate_campaign_runtime_binding(bug, changed_image)
        changed_kernel = json.loads(json.dumps(score))
        changed_kernel["provision"]["identity_preflight"]["kernel"] = "other-kernel"
        with pytest.raises(c001.LedgerIntegrityError, match="image/kernel"):
            c001.validate_campaign_runtime_binding(bug, changed_kernel, phase1_score=score)
        changed_runtime = json.loads(json.dumps(score))
        changed_runtime["provision"]["runtime_ive_library"]["sha256_after"] = "8" * 64
        with pytest.raises(c001.LedgerIntegrityError, match="IVE runtime mismatch"):
            c001.validate_campaign_runtime_binding(bug, changed_runtime)


def test_retained_host_summary_is_semantically_replayed(sparse_probe, tmp_path):
    artifact = c001.run_host_discriminator(
        sparse_probe.manifest_path,
        tmp_path / "host.json",
    )
    c001.validate_retained_host_bundle(artifact.path, artifact.payload)
    forged = json.loads(artifact.path.read_text())
    forged["summary"]["postwarm_frames"] = 599
    forged_path = tmp_path / "forged-host.json"
    forged_path.write_text(json.dumps(forged) + "\n")
    with pytest.raises(c001.LedgerIntegrityError, match="semantic replay"):
        c001.validate_retained_host_bundle(forged_path, forged)


def test_provision_binding_rejects_a_different_remote_clip(sparse_probe, tmp_path):
    manifest = c001.load_probe_manifest(sparse_probe.manifest_path)
    stats = _bound_stats(manifest)
    binary_sha = "a" * 64
    remote_run_dir = f"/root/c001-runs/{'2' * 32}"
    identity = {
        "board": "board-a",
        "mac": "02:00:00:00:00:AA",
        "image_marker": "buildroot-2023.02.6-kernel-5.10.160",
    }
    budget_proof = _write_budget_snapshots(
        tmp_path,
        run_id="2" * 32,
        identity=identity,
        seed=manifest["seed"],
        manifest_sha256=c001.sha256_file(sparse_probe.manifest_path),
        wall_s=12.5,
    )
    argv = " ".join(
        [
            f"env LD_LIBRARY_PATH=/oem/usr/lib {remote_run_dir}/skyweave-edge",
            f"--inject-ram {remote_run_dir}/ram.swij",
            f"--stats {remote_run_dir}/stats.json",
            f"--ccl-log {remote_run_dir}/ccl.jsonl",
            f"--packet-log {remote_run_dir}/packets.hex",
            f"--ram-loop-frames {manifest['total_frames']}",
            f"--ram-loop-pts-stride-ns {manifest['ram_loop_pts_stride_ns']}",
            f"--ram-budget-mb {manifest['ram_budget_mb']}",
            "--ram-loop-period-ns 0",
            "--detector ive",
            "--proc 1152x648",
            "--warmup 30",
            "--cap 7",
            "--min-area-px 2",
            "--morph-open 1",
            f"--gmm2-match-sigmas {stats['gmm2_match_sigmas']}",
            f"--gmm2-var-min {stats['gmm2_var_min']}",
        ]
    )
    payload = {
        "schema": "d8-provision/1",
        "node": {
            "name": "board-a",
            "remote_dir": remote_run_dir,
            "ld_library_path": "/oem/usr/lib",
        },
        "remote_binary": f"{remote_run_dir}/skyweave-edge",
        "binary_verified": True,
        "local_sha256": binary_sha,
        "remote_sha256": binary_sha,
        "source_remote_path": f"{remote_run_dir}/ram.swij",
        "source_local_sha256": manifest["clip_sha256"],
        "source_remote_sha256": manifest["clip_sha256"],
        "source_verified": True,
        "stats": stats,
        "collected": [
            "stats.json",
            "ccl.jsonl",
            "packets.hex",
            "exit.status",
            "run.log",
        ],
        "argv": argv,
        "wall_s": 12.5,
        "run_id": "2" * 32,
        "remote_run_dir": remote_run_dir,
        "daemon_exit_code": 0,
        "exit_status": 0,
        "daemon_stopped": True,
        "completed_before_deadline": True,
        "stop_succeeded": None,
        "power_cycles": 0,
        "recovery_attempts": [],
        **budget_proof,
        "probe_manifest_sha256": c001.sha256_file(sparse_probe.manifest_path),
        "identity_preflight": {
            **identity,
            "kernel": "5.10.160",
            "interface": "eth0",
        },
        "runtime_ive_library": {
            "path": "/oem/usr/lib/librve.so",
            "sha256_before": "7" * 64,
            "sha256_after": "7" * 64,
            "stable": True,
        },
        "collected_sha256": {
            "stats.json": "3" * 64,
            "ccl.jsonl": "4" * 64,
            "packets.hex": "5" * 64,
            "exit.status": "6" * 64,
            "run.log": "7" * 64,
        },
    }
    path = tmp_path / "provision.json"
    path.write_text(json.dumps(payload))
    c001.validate_provision_artifact(path, board="board-a", stats=stats, manifest=manifest)
    valid_runtime = json.loads(json.dumps(payload["runtime_ive_library"]))
    payload["runtime_ive_library"]["path"] = "/tmp/librve.so"
    path.write_text(json.dumps(payload))
    with pytest.raises(c001.GuardrailViolation, match="exact /oem/usr/lib/librve.so"):
        c001.validate_provision_artifact(path, board="board-a", stats=stats, manifest=manifest)
    payload["runtime_ive_library"] = json.loads(json.dumps(valid_runtime))
    payload["runtime_ive_library"]["sha256_after"] = "8" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(c001.GuardrailViolation, match="stable IVE runtime"):
        c001.validate_provision_artifact(path, board="board-a", stats=stats, manifest=manifest)
    payload["runtime_ive_library"] = valid_runtime
    path.write_text(json.dumps(payload))
    valid_argv = payload["argv"]
    payload["completed_before_deadline"] = False
    payload["stop_succeeded"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(c001.GuardrailViolation, match="natural exit"):
        c001.validate_provision_artifact(path, board="board-a", stats=stats, manifest=manifest)
    payload["completed_before_deadline"] = True
    payload["stop_succeeded"] = None
    payload["argv"] = valid_argv.replace(
        f"{remote_run_dir}/skyweave-edge", "/tmp/modified-daemon", 1
    )
    path.write_text(json.dumps(payload))
    with pytest.raises(c001.GuardrailViolation, match="verified fresh binary"):
        c001.validate_provision_artifact(path, board="board-a", stats=stats, manifest=manifest)
    payload["argv"] = f"env LD_PRELOAD=/tmp/evil.so {valid_argv}"
    path.write_text(json.dumps(payload))
    with pytest.raises(c001.GuardrailViolation, match="verified fresh binary"):
        c001.validate_provision_artifact(path, board="board-a", stats=stats, manifest=manifest)
    payload["argv"] = valid_argv
    valid_source = payload["source_remote_path"]
    payload["source_remote_path"] = f"/root/c001-runs/{'2' * 32}/../stale/ram.swij"
    path.write_text(json.dumps(payload))
    with pytest.raises(c001.GuardrailViolation, match="path alias"):
        c001.validate_provision_artifact(path, board="board-a", stats=stats, manifest=manifest)
    payload["source_remote_path"] = valid_source
    payload["source_remote_sha256"] = "b" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(c001.GuardrailViolation, match="RAM clip SHA256"):
        c001.validate_provision_artifact(path, board="board-a", stats=stats, manifest=manifest)


def test_failed_mask_parser_and_diff_are_bounded_and_frame_aligned(tmp_path):
    mask = np.zeros((648, 1152), dtype=np.uint8)
    mask[10, 20] = 255
    header = struct.pack(">4sB3sQIII", b"SWFM", 1, b"\0\0\0", 33, 1152, 648, mask.size)
    path = tmp_path / "failed.swfm"
    path.write_bytes(header + mask.tobytes())
    board = c001.load_failed_fg_masks(path)
    assert list(board) == [33]
    assert bool(board[33][10, 20])
    identical = c001.compare_fg_masks(board, {33: board[33].copy()})
    assert identical["differing_pixels"] == 0
    changed = board[33].copy()
    changed[10, 20] = False
    assert c001.compare_fg_masks(board, {33: changed})["differing_pixels"] == 1
    with pytest.raises(c001.GuardrailViolation, match="identical frame_seq"):
        c001.compare_fg_masks(board, {34: changed})
    path.write_bytes(header + mask.tobytes()[:-1])
    with pytest.raises(c001.CampaignError, match="truncated"):
        c001.load_failed_fg_masks(path)


def test_ledger_appends_only_verified_artifacts_with_sequential_hash_chain(tmp_path):
    campaign = tmp_path / "C-001"
    artifacts = campaign / "artifacts"
    artifacts.mkdir(parents=True)
    first_artifact = artifacts / "first.json"
    build_log = artifacts / "build.log"
    build_log.write_text("pinned SDK build completed\n")
    evidence = {}
    commands = {}
    checks = {
        "bug_a_board": "full_254_slot_region_scan",
        "bug_b_board": "mask_moment_centroid_and_overlap_counter",
        "e2": "nanopb_byte_identity",
        "e5": "host_fixture_replay",
    }
    for name, check in checks.items():
        transcript = artifacts / f"{name}.json"
        is_board_check = name in {"bug_a_board", "bug_b_board"}
        command = (
            "LD_LIBRARY_PATH=/oem/usr/lib /root/skyweave-edge --self-test-ccl-measure"
            if is_board_check
            else (
                "python -m pytest -q tests/edge/test_e2_nanopb_parity.py"
                if name == "e2"
                else "python -m pytest -q tests/edge/test_e5_fixture_replay.py"
            )
        )
        stdout = artifacts / f"{name}.stdout"
        stderr = artifacts / f"{name}.stderr"
        stdout.write_text(
            (
                json.dumps(
                    {
                        "schema": "skyweave-ccl-selftest/1",
                        "full_254_slot_region_scan": True,
                        "mask_moment_centroid": True,
                        "overlap_counter": True,
                    }
                )
                if is_board_check
                else "10 passed in 1.00s"
            )
            + "\n"
        )
        stderr.write_text("")
        transcript.write_text(
            json.dumps(
                {
                    "schema": "skyweave-c001-check-transcript/1",
                    "check": check,
                    "exit_code": 0,
                    "asserted_outcome": True,
                    "git_sha": "a" * 40,
                    "source_tree_sha256": "f" * 64,
                    "toolchain": "pinned-arm-sdk plus host pytest",
                    "command": command,
                    "board_identity": {
                        "board": "board-a",
                        "mac": "02:00:00:00:00:AA",
                        "image_marker": "buildroot-2023.02.6-kernel-5.10.160",
                    },
                    "binary_sha256": "b" * 64,
                    "check_binary_sha256": ("b" * 64 if is_board_check else None),
                    "check_binary_remote_sha256": ("b" * 64 if is_board_check else None),
                    "runtime_ive_library_sha256": (
                        "7" * 64 if is_board_check else None
                    ),
                    "stdout_path": stdout.name,
                    "stdout_sha256": c001.sha256_file(stdout),
                    "stderr_path": stderr.name,
                    "stderr_sha256": c001.sha256_file(stderr),
                }
            )
            + "\n"
        )
        evidence[name] = {
            "path": transcript.name,
            "sha256": c001.sha256_file(transcript),
        }
        commands[name] = command
    first_artifact.write_text(
        json.dumps(
            {
                "schema": c001.BUG_VERIFICATION_SCHEMA,
                "campaign_id": c001.CAMPAIGN_ID,
                "summary": {
                    "bug_a_verified": True,
                    "bug_b_verified": True,
                    "e2_green": True,
                    "e5_green": True,
                },
                "provenance": {
                    "git_sha": "a" * 40,
                    "source_tree_sha256": "f" * 64,
                    "toolchain": "pinned-arm-sdk plus host pytest",
                    "commands": commands,
                    "build": {
                        "path": build_log.name,
                        "sha256": c001.sha256_file(build_log),
                        "image_digest": "sha256:" + "c" * 64,
                        "command": "docker run --rm pinned-image ./build.sh",
                        "binary_sha256": "b" * 64,
                    },
                },
                "binding": {
                    "identity": {
                        "board": "board-a",
                        "mac": "02:00:00:00:00:AA",
                        "image_marker": "buildroot-2023.02.6-kernel-5.10.160",
                    },
                    "binary_sha256": "b" * 64,
                    "runtime_ive_library": {
                        "path": "/oem/usr/lib/librve.so",
                        "sha256_before": "7" * 64,
                        "sha256_after": "7" * 64,
                        "stable": True,
                    },
                    "git_sha": "a" * 40,
                    "source_tree_sha256": "f" * 64,
                },
                "evidence": evidence,
            }
        )
        + "\n"
    )
    stopped_campaign = tmp_path / "subject-stop"
    shutil.copytree(artifacts, stopped_campaign / "artifacts")
    stopped_artifact = stopped_campaign / "artifacts/first.json"
    stopped_subject = _subject(root=stopped_campaign)
    stopped_gate_path = stopped_campaign / str(stopped_subject["gate_evidence"]["path"])
    stopped_gate = json.loads(stopped_gate_path.read_text())
    stopped_gate["platform"]["os"] = "Darwin"
    stopped_gate_path.write_text(json.dumps(stopped_gate) + "\n")
    stopped_subject["gate_evidence"]["sha256"] = c001.sha256_file(stopped_gate_path)
    stopped_ledger = stopped_campaign / "ledger.jsonl"
    stopped_kwargs = {
        "hypothesis": "invalid gate must terminally stop",
        "knobs": {},
        "seed": 101,
        "board": "host",
        "result": {
            "bug_a_verified": True,
            "bug_b_verified": True,
            "e2_green": True,
            "e5_green": True,
        },
        "verdict": "measurement",
        "note": "phase 1",
        "wall_minutes": 1.0,
        "subject_to": stopped_subject,
        "phase": "phase1",
        "phase1_step": 1,
        "n": 1,
    }
    with pytest.raises(c001.SubjectToViolation, match="Linux x86_64/amd64"):
        c001.append_ledger(
            stopped_ledger,
            stopped_artifact,
            c001.sha256_file(stopped_artifact),
            **stopped_kwargs,
        )
    assert c001.read_campaign_stop(stopped_ledger)["category"] == ("subject_to_violation")
    with pytest.raises(c001.GuardrailViolation, match="shift is stopped"):
        c001.append_ledger(
            stopped_ledger,
            stopped_artifact,
            c001.sha256_file(stopped_artifact),
            **stopped_kwargs,
        )
    failed_transcript = artifacts / "e2-failed.json"
    failed_payload = json.loads((artifacts / "e2.json").read_text())
    failed_payload["exit_code"] = 1
    failed_payload["asserted_outcome"] = False
    failed_transcript.write_text(json.dumps(failed_payload) + "\n")
    false_claim = json.loads(first_artifact.read_text())
    false_claim["evidence"]["e2"] = {
        "path": failed_transcript.name,
        "sha256": c001.sha256_file(failed_transcript),
    }
    false_artifact = artifacts / "false-claim.json"
    false_artifact.write_text(json.dumps(false_claim) + "\n")
    with pytest.raises(c001.LedgerIntegrityError, match="did not prove PASS"):
        c001.validate_bug_verification_bundle(false_artifact, false_claim)
    noisy_stderr = artifacts / "e2-noisy.stderr"
    noisy_stderr.write_text("warning emitted on stderr\n")
    noisy_payload = json.loads((artifacts / "e2.json").read_text())
    noisy_payload["stderr_path"] = noisy_stderr.name
    noisy_payload["stderr_sha256"] = c001.sha256_file(noisy_stderr)
    noisy_transcript = artifacts / "e2-noisy.json"
    noisy_transcript.write_text(json.dumps(noisy_payload) + "\n")
    noisy_claim = json.loads(first_artifact.read_text())
    noisy_claim["evidence"]["e2"] = {
        "path": noisy_transcript.name,
        "sha256": c001.sha256_file(noisy_transcript),
    }
    with pytest.raises(c001.LedgerIntegrityError, match="pytest emitted stderr"):
        c001.validate_bug_verification_bundle(first_artifact, noisy_claim)
    for loader in ("", "LD_LIBRARY_PATH=/tmp"):
        loader_payload = json.loads((artifacts / "bug_a_board.json").read_text())
        loader_command = (
            f"{loader} " if loader else ""
        ) + "/root/skyweave-edge --self-test-ccl-measure"
        loader_payload["command"] = loader_command
        loader_name = "missing-loader" if not loader else "wrong-loader"
        loader_transcript = artifacts / f"bug-a-{loader_name}.json"
        loader_transcript.write_text(json.dumps(loader_payload) + "\n")
        loader_claim = json.loads(first_artifact.read_text())
        loader_claim["provenance"]["commands"]["bug_a_board"] = loader_command
        loader_claim["evidence"]["bug_a_board"] = {
            "path": loader_transcript.name,
            "sha256": c001.sha256_file(loader_transcript),
        }
        with pytest.raises(c001.LedgerIntegrityError, match="production daemon self-test"):
            c001.validate_bug_verification_bundle(first_artifact, loader_claim)
    for incomplete in ("skipped", "xfailed", "xpassed", "deselected", "warning"):
        incomplete_stdout = artifacts / f"e2-{incomplete}.stdout"
        incomplete_stdout.write_text(f"10 passed, 1 {incomplete} in 1.00s\n")
        incomplete_payload = json.loads((artifacts / "e2.json").read_text())
        incomplete_payload["stdout_path"] = incomplete_stdout.name
        incomplete_payload["stdout_sha256"] = c001.sha256_file(incomplete_stdout)
        incomplete_transcript = artifacts / f"e2-{incomplete}.json"
        incomplete_transcript.write_text(json.dumps(incomplete_payload) + "\n")
        incomplete_claim = json.loads(first_artifact.read_text())
        incomplete_claim["evidence"]["e2"] = {
            "path": incomplete_transcript.name,
            "sha256": c001.sha256_file(incomplete_transcript),
        }
        with pytest.raises(c001.LedgerIntegrityError, match="pytest transcript did not pass"):
            c001.validate_bug_verification_bundle(first_artifact, incomplete_claim)
    selected_payload = json.loads((artifacts / "e2.json").read_text())
    selected_command = (
        "python -m pytest -q --ignore=tests/edge/test_e2_nanopb_parity.py "
        "tests/edge/test_c001_campaign.py"
    )
    selected_payload["command"] = selected_command
    selected_transcript = artifacts / "e2-selected.json"
    selected_transcript.write_text(json.dumps(selected_payload) + "\n")
    selected_claim = json.loads(first_artifact.read_text())
    selected_claim["provenance"]["commands"]["e2"] = selected_command
    selected_claim["evidence"]["e2"] = {
        "path": selected_transcript.name,
        "sha256": c001.sha256_file(selected_transcript),
    }
    with pytest.raises(c001.LedgerIntegrityError, match="command target is wrong"):
        c001.validate_bug_verification_bundle(first_artifact, selected_claim)
    ledger = campaign / "ledger.jsonl"
    row = c001.append_ledger(
        ledger,
        first_artifact,
        c001.sha256_file(first_artifact),
        hypothesis="measure exact host component counts",
        knobs={},
        seed=101,
        board="host",
        result={
            "bug_a_verified": True,
            "bug_b_verified": True,
            "e2_green": True,
            "e5_green": True,
        },
        verdict="measurement",
        note="phase 1",
        wall_minutes=1.0,
        subject_to=_subject(root=campaign),
        phase="phase1",
        phase1_step=1,
        n=1,
    )
    assert row["n"] == 1 and row["previous_entry_sha256"] is None
    assert c001.read_ledger(ledger) == [row]

    missing = artifacts / "missing.json"
    with pytest.raises(c001.CampaignError):
        c001.append_ledger(
            ledger,
            missing,
            "0" * 64,
            hypothesis="must not append",
            knobs={},
            seed=102,
            board="host",
            result={},
            verdict="failed",
            note="",
            wall_minutes=1,
            subject_to=_subject(root=campaign),
            phase="phase1",
            phase1_step=2,
        )
    assert len(ledger.read_text().splitlines()) == 1

    second = artifacts / "second.json"
    second.write_text('{"summary":{}}\n')
    with pytest.raises(c001.LedgerIntegrityError, match="next ledger n"):
        c001.append_ledger(
            ledger,
            second,
            c001.sha256_file(second),
            hypothesis="cannot skip n",
            knobs={},
            seed=102,
            board="host",
            result={},
            verdict="measurement",
            note="",
            wall_minutes=1,
            subject_to=_subject(root=campaign),
            phase="phase1",
            phase1_step=2,
            n=3,
        )
    first_artifact.write_text("tampered\n")
    with pytest.raises(c001.LedgerIntegrityError, match="digest"):
        c001.read_ledger(ledger)


def test_ledger_refuses_path_escape_and_twenty_minute_overrun(tmp_path):
    campaign = tmp_path / "C-001"
    campaign.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"summary":{}}')
    with pytest.raises(c001.LedgerIntegrityError, match="beneath"):
        c001.append_ledger(
            campaign / "ledger.jsonl",
            outside,
            c001.sha256_file(outside),
            hypothesis="escape",
            knobs={},
            seed=1,
            board="host",
            result={},
            verdict="failed",
            note="",
            wall_minutes=1,
            subject_to=_subject(root=campaign),
            phase="phase1",
            phase1_step=1,
        )
    artifact = campaign / "run.json"
    artifact.write_text('{"summary":{}}')
    with pytest.raises(c001.CampaignError):
        c001.append_ledger(
            campaign / "ledger.jsonl",
            artifact,
            c001.sha256_file(artifact),
            hypothesis="too long",
            knobs={},
            seed=1,
            board="host",
            result={},
            verdict="failed",
            note="",
            wall_minutes=20.01,
            subject_to=_subject(root=campaign),
            phase="phase1",
            phase1_step=1,
        )


def test_every_shift_stop_rule_can_fire():
    neutral = [
        {"n": n, "verdict": "unchanged", "power_cycles": 0, "wedges": 0} for n in range(1, 41)
    ]
    assert "40 experiments exhausted" in c001.evaluate_shift(neutral).stop_reasons
    regressions = [
        {"n": n, "verdict": "regressed", "power_cycles": 0, "wedges": 0} for n in range(1, 9)
    ]
    assert "8 consecutive regressions" in c001.evaluate_shift(regressions).stop_reasons
    cycles = [{"n": 1, "verdict": "unchanged", "power_cycles": 6, "wedges": 0}]
    assert "6 PoE cycles exhausted" in c001.evaluate_shift(cycles).stop_reasons
    wedge = [{"n": 1, "verdict": "failed", "power_cycles": 0, "wedges": 2}]
    assert "one experiment wedged a board twice" in c001.evaluate_shift(wedge).stop_reasons
    assert not c001.evaluate_shift([], clean_host=True).can_continue
    assert not c001.evaluate_shift([], subject_to_violation=True).can_continue
    assert not c001.evaluate_shift([], contract_change_required=True).can_continue
    assert not c001.evaluate_shift([], board_unreachable_after_two_cycles=True).can_continue

    ambiguous_phase1 = [
        {
            "n": n,
            "phase": "phase1",
            "phase1_step": n,
            "verdict": "measurement",
            "power_cycles": 0,
            "wedges": 0,
            "result": (
                {
                    "paired_discriminator": {
                        "discriminator_allows_climb": False,
                        "zero_failure_candidate_path": False,
                    }
                }
                if n == 3
                else {}
            ),
        }
        for n in range(1, 5)
    ]
    assert "re-scope required" in " ".join(c001.evaluate_shift(ambiguous_phase1).stop_reasons)
    phase1_host = [
        {"n": n, "verdict": "measurement", "power_cycles": 0, "wedges": 0} for n in range(1, 4)
    ]
    physical_attempt_37 = {
        "n": 4,
        "verdict": "failed",
        "power_cycles": 0,
        "wedges": 0,
        "attempt_budget": {"attempts_reserved": 37},
    }
    boundary = c001.evaluate_shift([*phase1_host, physical_attempt_37])
    assert boundary.experiments == 40
    assert "40 experiments exhausted" in boundary.stop_reasons
    physical_attempt_38 = {
        **physical_attempt_37,
        "attempt_budget": {"attempts_reserved": 38},
    }
    assert c001.evaluate_shift([*phase1_host, physical_attempt_38]).experiments == 41


def test_power_cycle_budget_refuses_prospective_overshoot():
    c001.validate_power_cycle_budget([{"power_cycles": 5}, {"power_cycles": 1}])
    with pytest.raises(c001.LedgerIntegrityError, match="six-cycle"):
        c001.validate_power_cycle_budget([{"power_cycles": 5}, {"power_cycles": 2}])
    identity = {
        "board": "board-a",
        "mac": "02:00:00:00:00:AA",
        "image_marker": "image-1",
    }
    c001.validate_power_cycle_budget(
        [
            {"power_cycles": 1, "identity": identity},
            {"power_cycles": 1, "identity": identity},
        ]
    )
    with pytest.raises(c001.LedgerIntegrityError, match="per-MAC"):
        c001.validate_power_cycle_budget(
            [
                {"power_cycles": 1, "identity": identity},
                {"power_cycles": 2, "identity": identity},
            ]
        )


def test_cumulative_attempt_and_recovery_chains_cannot_rotate_or_rollback():
    a = "a" * 64
    b = "b" * 64
    c = "c" * 64
    c001._validate_cumulative_chain_extension("physical-attempt", [a], [a, b], strict=True)
    with pytest.raises(c001.LedgerIntegrityError, match="strict extension"):
        c001._validate_cumulative_chain_extension("physical-attempt", [a], [a], strict=True)
    with pytest.raises(c001.LedgerIntegrityError, match="prefix extension"):
        c001._validate_cumulative_chain_extension("physical-attempt", [a], [c, b], strict=True)
    c001._validate_cumulative_chain_extension("recovery", [a], [a], strict=False)
    c001._validate_cumulative_chain_extension("recovery", [a], [a, b], strict=False)
    with pytest.raises(c001.LedgerIntegrityError, match="prefix extension"):
        c001._validate_cumulative_chain_extension("recovery", [a, b], [a], strict=False)


def test_score_and_host_wall_minutes_are_artifact_derived():
    assert c001._artifact_wall_minutes(
        {"schema": c001.SCORE_SCHEMA, "wall_s": 12.0}
    ) == pytest.approx(0.2)
    assert c001._artifact_wall_minutes(
        {"schema": c001.HOST_SCHEMA, "wall_s": 30.0}
    ) == pytest.approx(0.5)
    assert c001._artifact_wall_minutes({"schema": c001.BUG_VERIFICATION_SCHEMA}) is None
    with pytest.raises(c001.GuardrailViolation, match="artifact wall_s"):
        c001._bind_artifact_wall_minutes(
            {"schema": c001.HOST_SCHEMA, "wall_s": 30.0},
            0.4,
            retained_chain=False,
        )
    with pytest.raises(c001.LedgerIntegrityError, match="artifact wall_s"):
        c001._bind_artifact_wall_minutes(
            {"schema": c001.HOST_SCHEMA, "wall_s": 30.0},
            0.4,
            retained_chain=True,
        )
    with pytest.raises(c001.CampaignError, match="wall_s"):
        c001._artifact_wall_minutes({"schema": c001.HOST_SCHEMA, "wall_s": 1200.1})


def test_regression_verdict_is_derived_and_eight_worse_rows_stop():
    recall = {"all_truth_recalled": True}
    missed = {"all_truth_recalled": False}
    baseline = {"result": {"detector_fail_rate": 0.01}}
    worse = {
        "detector_fail_rate": 0.02,
        "raw_component_mover_recall": recall,
        "mover_recall": recall,
    }
    assert c001._derived_rate_verdict([baseline], worse) == "regressed"
    blind_worse = {
        **worse,
        "raw_component_mover_recall": missed,
        "mover_recall": missed,
    }
    assert c001._derived_rate_verdict([baseline], blind_worse) == "regressed"
    rows = [baseline] + [
        {
            "n": n,
            "verdict": "failed",
            "power_cycles": 0,
            "wedges": 0,
            "result": blind_worse,
        }
        for n in range(2, 10)
    ]
    assert "8 consecutive regressions" in c001.evaluate_shift(rows).stop_reasons


def _stopped_empty_shift(
    tmp_path: Path,
    *,
    category: str = "operator_stop",
    reason: str = "operator closed the test shift",
) -> tuple[Path, str, dict[str, bytes]]:
    campaign = tmp_path.resolve() / c001.CAMPAIGN_ID
    campaign.mkdir()
    retained = campaign / "retained/nested.bin"
    retained.parent.mkdir()
    retained.write_bytes(b"retained shift payload\n")
    retained.chmod(0o600)
    (campaign / "SHIFT.md").write_text("test shift record\n")

    attempt_material = {
        "schema": c001.ATTEMPT_LEDGER_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "event": "attempt_reserved",
        "run_id": "1" * 32,
        "attempt_n": 1,
        "board": "board-a",
        "mac": "02:00:00:00:00:aa",
        "seed": 101,
        "manifest_sha256": "a" * 64,
        "recorded_at": "2026-08-21T00:00:01+00:00",
        "previous_sha256": "0" * 64,
    }
    attempt = {
        **attempt_material,
        "row_sha256": hashlib.sha256(
            c001._canonical_json(attempt_material)
        ).hexdigest(),
    }
    recovery_material = {
        "schema": c001.RECOVERY_LEDGER_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "event": "poe_cycle_reserved",
        "run_id": "2" * 32,
        "board": "board-a",
        "mac": "02:00:00:00:00:aa",
        "shift_cycle_n": 1,
        "board_cycle_n": 1,
        "recorded_at": "2026-08-21T00:00:02+00:00",
        "previous_sha256": "0" * 64,
    }
    recovery = {
        **recovery_material,
        "row_sha256": hashlib.sha256(
            c001._canonical_json(recovery_material)
        ).hexdigest(),
    }
    budgets = {
        "attempt-ledger.jsonl": c001._canonical_json(attempt),
        "recovery-ledger.jsonl": c001._canonical_json(recovery),
    }
    for name, payload in budgets.items():
        (campaign / name).write_bytes(payload)

    ledger = campaign / "ledger.jsonl"
    c001.record_campaign_stop(ledger, category=category, reason=reason)
    assert ledger.read_bytes() == b""
    return campaign, c001.sha256_file(campaign / "STOP.json"), budgets


def _shift_history(campaign: Path) -> Path:
    return campaign.with_name(c001.SHIFT_HISTORY_DIRECTORY)


def test_successor_rollover_archives_whole_tree_and_is_idempotent(tmp_path):
    campaign, stop_sha256, budgets = _stopped_empty_shift(
        tmp_path,
        category="contract_change_required",
        reason="the source contract must change",
    )
    before = c001._archive_tree_inventory(campaign)
    before_files = {
        path.relative_to(campaign).as_posix(): path.read_bytes()
        for path in campaign.rglob("*")
        if path.is_file()
    }

    opened = c001.start_successor_shift(
        campaign,
        expected_stop_sha256=stop_sha256,
        note="authorize the source-corrected successor",
    )

    archive = Path(opened["archive"])
    assert opened["shift_n"] == 2
    assert opened["already_open"] is False
    assert archive == _shift_history(campaign) / f"shift-0001-{stop_sha256[:12]}"
    assert c001._archive_tree_inventory(archive) == before
    assert {
        path.relative_to(archive).as_posix(): path.read_bytes()
        for path in archive.rglob("*")
        if path.is_file()
    } == before_files
    assert {member.name for member in campaign.iterdir()} == {
        c001.SUCCESSOR_NAME,
        "attempt-ledger.jsonl",
        "recovery-ledger.jsonl",
    }
    assert campaign.joinpath("attempt-ledger.jsonl").read_bytes() == budgets[
        "attempt-ledger.jsonl"
    ]
    assert campaign.joinpath("recovery-ledger.jsonl").read_bytes() == budgets[
        "recovery-ledger.jsonl"
    ]
    state = c001.validate_current_shift(campaign, verify_artifacts=True)
    assert state["shift_n"] == 2
    assert state["predecessor_archive"] == str(archive)

    lineage = _shift_history(campaign) / c001.SHIFT_LINEAGE_NAME
    lineage_before = lineage.read_bytes()
    archive_before = c001._archive_tree_inventory(archive)
    repeated = c001.start_successor_shift(
        campaign,
        expected_stop_sha256=stop_sha256,
        note="authorize the source-corrected successor",
    )
    assert repeated["already_open"] is True
    assert repeated["archive"] == str(archive)
    assert lineage.read_bytes() == lineage_before
    assert c001._archive_tree_inventory(archive) == archive_before


def test_successor_rollover_refuses_wrong_stop_without_archiving(tmp_path):
    campaign, stop_sha256, _ = _stopped_empty_shift(tmp_path)
    before = c001._archive_tree_inventory(campaign)
    wrong_sha256 = "f" * 64 if stop_sha256 != "f" * 64 else "e" * 64

    with pytest.raises(c001.GuardrailViolation, match="STOP SHA256 differs"):
        c001.start_successor_shift(
            campaign,
            expected_stop_sha256=wrong_sha256,
            note="wrong authorization",
        )

    assert c001._archive_tree_inventory(campaign) == before
    history = _shift_history(campaign)
    assert not any(member.is_dir() for member in history.iterdir())
    assert not any(
        member.name.startswith((".rollover-", ".successor-"))
        for member in history.iterdir()
    )
    assert (history / c001.SHIFT_LINEAGE_NAME).read_bytes() == b""


def test_successor_rollover_rejects_archived_tree_mutation(tmp_path):
    campaign, stop_sha256, _ = _stopped_empty_shift(tmp_path)
    opened = c001.start_successor_shift(
        campaign,
        expected_stop_sha256=stop_sha256,
        note="open successor before mutation",
    )
    archive = Path(opened["archive"])
    (archive / "retained/nested.bin").write_bytes(b"mutated\n")

    with pytest.raises(c001.LedgerIntegrityError, match="archived shift tree differs"):
        c001.validate_current_shift(campaign, verify_artifacts=True)
    with pytest.raises(c001.LedgerIntegrityError, match="archived shift tree differs"):
        c001.start_successor_shift(
            campaign,
            expected_stop_sha256=stop_sha256,
            note="open successor before mutation",
        )


def test_successor_rollover_precommit_failure_restores_predecessor(
    tmp_path, monkeypatch
):
    campaign, stop_sha256, _ = _stopped_empty_shift(tmp_path)
    before = c001._archive_tree_inventory(campaign)

    def fail_precommit(*_args, **_kwargs):
        raise OSError("injected precommit failure")

    with monkeypatch.context() as patch:
        patch.setattr(c001, "_append_shift_lineage_row", fail_precommit)
        with pytest.raises(OSError, match="injected precommit failure"):
            c001.start_successor_shift(
                campaign,
                expected_stop_sha256=stop_sha256,
                note="retryable precommit rollover",
            )

    history = _shift_history(campaign)
    archive = history / f"shift-0001-{stop_sha256[:12]}"
    assert campaign.is_dir()
    assert not archive.exists()
    assert c001._archive_tree_inventory(campaign) == before
    assert (history / c001.SHIFT_LINEAGE_NAME).read_bytes() == b""

    recovered = c001.start_successor_shift(
        campaign,
        expected_stop_sha256=stop_sha256,
        note="retryable precommit rollover",
    )
    assert recovered["shift_n"] == 2
    assert Path(recovered["archive"]) == archive
    assert c001.validate_current_shift(campaign)["shift_n"] == 2


def test_successor_rollover_postcommit_failure_recovers_on_identical_retry(
    tmp_path, monkeypatch
):
    campaign, stop_sha256, _ = _stopped_empty_shift(tmp_path)
    history = _shift_history(campaign)
    archive = history / f"shift-0001-{stop_sha256[:12]}"
    real_rename = c001.os.rename

    def fail_successor_install(source, destination):
        if Path(source).name.startswith(".successor-"):
            raise OSError("injected postcommit failure")
        return real_rename(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(c001.os, "rename", fail_successor_install)
        with pytest.raises(OSError, match="injected postcommit failure"):
            c001.start_successor_shift(
                campaign,
                expected_stop_sha256=stop_sha256,
                note="retryable postcommit rollover",
            )

    assert not campaign.exists()
    assert archive.is_dir()
    assert len(c001._parse_shift_lineage_bytes(
        (history / c001.SHIFT_LINEAGE_NAME).read_bytes()
    )) == 1
    assert any(member.name.startswith(".rollover-") for member in history.iterdir())
    assert any(member.name.startswith(".successor-") for member in history.iterdir())

    recovered = c001.start_successor_shift(
        campaign,
        expected_stop_sha256=stop_sha256,
        note="retryable postcommit rollover",
    )
    assert recovered["shift_n"] == 2
    assert recovered["already_open"] is False
    assert campaign.is_dir()
    assert c001.validate_current_shift(campaign)["shift_n"] == 2
    assert not any(
        member.name.startswith((".rollover-", ".successor-"))
        for member in history.iterdir()
    )


def test_successor_rollover_supports_two_linked_generations(tmp_path):
    campaign, first_stop_sha256, budgets = _stopped_empty_shift(tmp_path)
    first = c001.start_successor_shift(
        campaign,
        expected_stop_sha256=first_stop_sha256,
        note="open shift two",
    )
    first_archive = Path(first["archive"])
    (campaign / "shift-two-note.txt").write_text("second shift evidence\n")
    c001.record_campaign_stop(
        campaign / "ledger.jsonl",
        category="operator_stop",
        reason="operator closed the second shift",
    )
    second_stop_sha256 = c001.sha256_file(campaign / "STOP.json")

    second = c001.start_successor_shift(
        campaign,
        expected_stop_sha256=second_stop_sha256,
        note="open shift three",
    )

    second_archive = Path(second["archive"])
    lineage = c001._read_shift_lineage(campaign)
    assert second["shift_n"] == 3
    assert [row["archived_shift_n"] for row in lineage] == [1, 2]
    assert [row["successor_shift_n"] for row in lineage] == [2, 3]
    assert lineage[1]["previous_entry_sha256"] == lineage[0]["entry_sha256"]
    assert first_archive.is_dir() and second_archive.is_dir()
    assert json.loads((second_archive / c001.SUCCESSOR_NAME).read_text()) == (
        c001._successor_pointer(lineage[0])
    )
    assert {member.name for member in campaign.iterdir()} == {
        c001.SUCCESSOR_NAME,
        "attempt-ledger.jsonl",
        "recovery-ledger.jsonl",
    }
    for root in (first_archive, second_archive, campaign):
        assert root.joinpath("attempt-ledger.jsonl").read_bytes() == budgets[
            "attempt-ledger.jsonl"
        ]
        assert root.joinpath("recovery-ledger.jsonl").read_bytes() == budgets[
            "recovery-ledger.jsonl"
        ]
    state = c001.validate_current_shift(campaign, verify_artifacts=True)
    assert state["shift_n"] == 3
    assert state["predecessor_archive"] == str(second_archive)


def test_stop_marker_is_append_only_and_blocks_status(tmp_path):
    ledger = tmp_path / "C-001" / "ledger.jsonl"
    stop = c001.record_campaign_stop(
        ledger,
        category="operator_stop",
        reason="operator ended the test shift",
    )
    assert stop["schema"] == c001.STOP_SCHEMA
    with pytest.raises(c001.GuardrailViolation, match="already"):
        c001.record_campaign_stop(
            ledger,
            category="operator_stop",
            reason="must not replace first stop",
        )
    assert c001.read_campaign_stop(ledger)["category"] == "operator_stop"


def test_unreachable_stop_replays_terminal_recovery_and_attempt_chains(tmp_path):
    campaign = tmp_path / "C-001"
    campaign.mkdir()
    ledger = campaign / "ledger.jsonl"
    board = "board-a"
    mac = "02:00:00:00:00:AA"
    run_ids = ("1" * 32, "2" * 32)
    recovery_rows = []
    previous = "0" * 64
    for cycle, run_id in enumerate(run_ids, 1):
        material = {
            "schema": c001.RECOVERY_LEDGER_SCHEMA,
            "campaign_id": c001.CAMPAIGN_ID,
            "event": "poe_cycle_reserved",
            "run_id": run_id,
            "board": board,
            "mac": mac.lower(),
            "shift_cycle_n": cycle,
            "board_cycle_n": cycle,
            "recorded_at": f"2026-08-21T00:00:0{cycle}+00:00",
            "previous_sha256": previous,
        }
        digest = hashlib.sha256(c001._canonical_json(material)).hexdigest()
        recovery_rows.append({**material, "row_sha256": digest})
        previous = digest
    recovery_path = campaign / "STOP.recovery-ledger.jsonl"
    recovery_path.write_bytes(b"".join(c001._canonical_json(row) for row in recovery_rows))
    attempt_material = {
        "schema": c001.ATTEMPT_LEDGER_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "event": "attempt_reserved",
        "run_id": run_ids[-1],
        "attempt_n": 1,
        "board": board,
        "mac": mac.lower(),
        "seed": 101,
        "manifest_sha256": "a" * 64,
        "recorded_at": "2026-08-21T00:00:03+00:00",
        "previous_sha256": "0" * 64,
    }
    attempt_sha = hashlib.sha256(c001._canonical_json(attempt_material)).hexdigest()
    attempt_row = {**attempt_material, "row_sha256": attempt_sha}
    reason = "board remained unreachable after its second reserved PoE cycle"
    outcome_material = {
        "schema": c001.ATTEMPT_LEDGER_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "event": "attempt_outcome",
        "run_id": run_ids[-1],
        "attempt_n": 1,
        "outcome_n": 1,
        "outcome": "preflight_failure",
        "wall_s": 1.0,
        "wedge": False,
        "error": reason,
        "recorded_at": "2026-08-21T00:00:04+00:00",
        "previous_sha256": attempt_sha,
    }
    outcome_sha = hashlib.sha256(c001._canonical_json(outcome_material)).hexdigest()
    outcome_row = {**outcome_material, "row_sha256": outcome_sha}
    attempt_path = campaign / "STOP.attempt-ledger.jsonl"
    attempt_path.write_bytes(
        c001._canonical_json(attempt_row) + c001._canonical_json(outcome_row)
    )
    source = {
        "schema": "skyweave-c001-recovery-stop-evidence/1",
        "campaign_id": c001.CAMPAIGN_ID,
        "category": "board_unreachable_after_two_cycles",
        "reason": reason,
        "recorded_at": "2026-08-21T00:00:04+00:00",
        "run_id": run_ids[-1],
        "identity": {"board": board, "mac": mac, "image_marker": "image-1"},
        "recovery_attempt": {
            "run_id": run_ids[-1],
            "board": board,
            "mac": mac.lower(),
            "shift_cycle_n": 2,
            "board_cycle_n": 2,
            "reservation_sha256": recovery_rows[-1]["row_sha256"],
            "outcome": "unreachable",
            "identity_revalidated": False,
        },
        "recovery_ledger": {
            "path": recovery_path.name,
            "sha256": c001.sha256_file(recovery_path),
            "tip_sha256": recovery_rows[-1]["row_sha256"],
        },
        "attempt_reservation": {
            "run_id": run_ids[-1],
            "attempt_n": 1,
            "reservation_sha256": attempt_sha,
        },
        "attempt_ledger": {
            "path": attempt_path.name,
            "sha256": c001.sha256_file(attempt_path),
            "tip_sha256": outcome_sha,
        },
    }
    source_path = campaign / "STOP.source.json"
    source_path.write_bytes(c001._canonical_json(source))
    stop = c001.record_campaign_stop(
        ledger,
        category="board_unreachable_after_two_cycles",
        reason=reason,
        source_artifact_sha256=c001.sha256_file(source_path),
    )
    assert stop["category"] == "board_unreachable_after_two_cycles"
    source_path.write_text("{}\n")
    with pytest.raises(c001.LedgerIntegrityError, match="source proof"):
        c001.read_campaign_stop(ledger)


def test_identity_stop_replays_mismatch_and_failed_attempt(tmp_path):
    campaign = tmp_path / "C-001"
    campaign.mkdir()
    ledger = campaign / "ledger.jsonl"
    run_id = "3" * 32
    board = "board-a"
    expected_mac = "02:00:00:00:00:aa"
    observed_mac = "02:00:00:00:00:ab"
    reservation_material = {
        "schema": c001.ATTEMPT_LEDGER_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "event": "attempt_reserved",
        "run_id": run_id,
        "attempt_n": 1,
        "board": board,
        "mac": expected_mac,
        "seed": 101,
        "manifest_sha256": "a" * 64,
        "recorded_at": "2026-08-21T00:00:01+00:00",
        "previous_sha256": "0" * 64,
    }
    reservation_sha = hashlib.sha256(c001._canonical_json(reservation_material)).hexdigest()
    reservation = {**reservation_material, "row_sha256": reservation_sha}
    reason = f"board identity mismatch: mac=observed '{observed_mac}', expected '{expected_mac}'"
    outcome_material = {
        "schema": c001.ATTEMPT_LEDGER_SCHEMA,
        "campaign_id": c001.CAMPAIGN_ID,
        "event": "attempt_outcome",
        "run_id": run_id,
        "attempt_n": 1,
        "outcome_n": 1,
        "outcome": "identity_failure",
        "wall_s": 1.0,
        "wedge": False,
        "error": reason,
        "recorded_at": "2026-08-21T00:00:02+00:00",
        "previous_sha256": reservation_sha,
    }
    outcome_sha = hashlib.sha256(c001._canonical_json(outcome_material)).hexdigest()
    outcome = {**outcome_material, "row_sha256": outcome_sha}
    attempt_path = campaign / "STOP.attempt-ledger.jsonl"
    attempt_path.write_bytes(c001._canonical_json(reservation) + c001._canonical_json(outcome))
    expected = {
        "board": board,
        "mac": expected_mac,
        "image_marker": "image-1",
        "kernel": "5.10.160",
        "interface": "eth0",
    }
    observed = {**expected, "mac": observed_mac}
    source = {
        "schema": "skyweave-c001-identity-stop-evidence/1",
        "campaign_id": c001.CAMPAIGN_ID,
        "category": "subject_to_violation",
        "reason": reason,
        "recorded_at": "2026-08-21T00:00:03+00:00",
        "run_id": run_id,
        "expected": expected,
        "observed": observed,
        "mismatched_fields": ["mac"],
        "attempt_reservation": {
            "run_id": run_id,
            "attempt_n": 1,
            "reservation_sha256": reservation_sha,
        },
        "attempt_ledger": {
            "path": attempt_path.name,
            "sha256": c001.sha256_file(attempt_path),
            "tip_sha256": outcome_sha,
        },
    }
    source_path = campaign / "STOP.source.json"
    source_path.write_bytes(c001._canonical_json(source))
    c001.record_campaign_stop(
        ledger,
        category="subject_to_violation",
        reason=reason,
        source_artifact_sha256=c001.sha256_file(source_path),
    )
    assert c001.read_campaign_stop(ledger)["category"] == "subject_to_violation"
    source["mismatched_fields"] = ["kernel"]
    source_path.write_bytes(c001._canonical_json(source))
    with pytest.raises(c001.LedgerIntegrityError, match="source proof|mismatch list"):
        c001.read_campaign_stop(ledger)


def test_confirmation_needs_fresh_seed_and_second_board_and_never_ratifies_d0():
    candidate = c001.ConfirmationRun(
        1, 101, "board-a", {}, 0.01, 100, 100, 120, 120, "02:00:00:00:00:AA", "image-1"
    )
    tracker = c001.ConfirmationTracker(candidate)
    tracker.observe(
        c001.ConfirmationRun(
            2, 102, "board-a", {}, 0.01, 100, 100, 120, 120, "02:00:00:00:00:AA", "image-1"
        )
    )
    assert tracker.as_dict()["fresh_seed_confirmed"] is True
    assert tracker.confirmed is False
    tracker.observe(
        c001.ConfirmationRun(
            3, 101, "board-b", {}, 0.02, 100, 100, 120, 120, "02:00:00:00:00:BB", "image-1"
        )
    )
    status = tracker.as_dict()
    assert status["confirmed"] is True
    assert status["d0_ratified"] is False
    assert "pending_planning_session" in status["status"]
    failed = c001.ConfirmationTracker(candidate)
    failed.observe(
        c001.ConfirmationRun(
            4, 102, "board-b", {}, 0.021, 100, 100, 120, 120, "02:00:00:00:00:BB", "image-1"
        )
    )
    assert failed.confirmed is False
    relabelled = c001.ConfirmationTracker(candidate)
    relabelled.observe(
        c001.ConfirmationRun(
            5, 102, "board-renamed", {}, 0.01, 100, 100, 120, 120, "02:00:00:00:00:AA", "image-1"
        )
    )
    assert relabelled.as_dict()["second_board_confirmed"] is False


def test_confirmation_cannot_switch_probe_family():
    result = {
        "detector_fail_rate": 0.0,
        "mover_recall": {"truth_points": 10, "matched": 10},
        "raw_component_mover_recall": {"truth_points": 10, "matched": 10},
    }
    candidate = {
        "n": 5,
        "phase": "climb",
        "verdict": "candidate",
        "seed": 101,
        "board": "board-a",
        "knobs": {},
        "identity": {
            "board": "board-a",
            "mac": "02:00:00:00:00:AA",
            "image_marker": "image-1",
        },
        "probe_semantics": {"probe_kind": "benchmark", "movers": 6},
        "result": result,
    }
    switched = {
        **candidate,
        "n": 6,
        "phase": "confirmation",
        "verdict": "confirmed",
        "seed": 102,
        "board": "board-b",
        "identity": {
            "board": "board-b",
            "mac": "02:00:00:00:00:BB",
            "image_marker": "image-1",
        },
        "probe_semantics": {"probe_kind": "sparse", "movers": 3},
    }
    assert c001._confirmation_from_rows([candidate, switched])["confirmed"] is False
    matched = {**switched, "probe_semantics": candidate["probe_semantics"]}
    assert c001._confirmation_from_rows([candidate, matched])["confirmed"] is True


def test_recovery_state_machine_revalidates_identity_and_stops_after_two_cycles():
    expected = c001.BoardIdentity("board-a", "02:00:00:00:00:AA", "image-1")
    machine = c001.RecoveryStateMachine(expected)
    machine.begin_cycle()
    machine.cycle_dispatched()
    machine.boot_result(True)
    machine.identity_result(expected)
    assert machine.state is c001.RecoveryState.READY
    machine.reset_ready()

    machine.begin_cycle()
    machine.cycle_dispatched()
    machine.boot_result(True)
    substitute = c001.BoardIdentity("board-b", "02:00:00:00:00:BB", "image-1")
    machine.identity_result(substitute)
    assert machine.state is c001.RecoveryState.EXCLUDED
    assert "identity mismatch" in machine.reason

    unreachable = c001.RecoveryStateMachine(expected)
    for _ in range(2):
        unreachable.begin_cycle()
        unreachable.cycle_dispatched()
        unreachable.boot_result(False)
    assert unreachable.state is c001.RecoveryState.EXCLUDED
    assert "two recovery cycles" in unreachable.reason


def test_production_cli_scope_and_status_for_the_canonical_runtime(tmp_path, capsys):
    root = tmp_path / "prepared"
    assert c001.prepare_probe(root, kind="sparse", seed=77).manifest_path.is_file()
    canonical = c001._canonical_campaign_directory() / "ledger.jsonl"
    c001.main(["status", "--ledger", str(canonical)])
    status = json.loads(capsys.readouterr().out)
    rows = c001.read_ledger(canonical)
    derived = c001.evaluate_shift(rows)
    assert status["shift"]["experiments"] == derived.experiments
    stopped = c001.read_campaign_stop(canonical)
    if stopped is None:
        assert status["shift"]["can_continue"] is derived.can_continue
    else:
        assert status["shift"]["can_continue"] is False
        assert status["stop"]["category"] == stopped["category"]
    with pytest.raises(c001.GuardrailViolation, match="canonical campaign ledger"):
        c001.main(["status", "--ledger", str(tmp_path / "other" / "ledger.jsonl")])
    with pytest.raises(c001.GuardrailViolation, match="outputs must remain"):
        c001.main(["prepare", "--out", str(tmp_path / "other"), "--kind", "sparse", "--seed", "1"])


def test_campaign_runtime_is_empty_or_contains_only_validated_measurements():
    campaign_dir = Path(__file__).resolve().parents[2] / "docs" / "campaigns" / "C-001"
    c001.validate_current_shift(campaign_dir, verify_artifacts=True)
    ledger = campaign_dir / "ledger.jsonl"
    if ledger.exists():
        rows = c001.read_ledger(ledger)
        assert rows, "a created campaign ledger may not be empty"
        assert all(row["artifact"]["sha256"] for row in rows)
        c001.read_campaign_stop(ledger)
        return
    retained_inputs = {
        path.name
        for path in campaign_dir.iterdir()
        if path.is_file() and path.name.endswith(".json")
    }
    assert retained_inputs <= {
        "source-tree.json",
        "gate-support.json",
        c001.SUCCESSOR_NAME,
    }
