"""Executable evidence production for the bounded C-001 campaign."""

from __future__ import annotations

import inspect
import json
import os
import py_compile
import shlex
import struct
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from skyweave2.edge import campaign_c001 as c001
from skyweave2.edge import campaign_c001_evidence as evidence
from skyweave2.edge import provision


def _command(*argv: str, cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)


def _source_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "v2/pkg").mkdir(parents=True)
    (root / "v2/tests/edge").mkdir(parents=True)
    (root / "v2/docs/campaigns/C-001").mkdir(parents=True)
    (root / "v2/firmware/rv1106/image").mkdir(parents=True)
    (root / "v1/src").mkdir(parents=True)
    (root / "v2/pkg/tracked.py").write_text("TRACKED = 1\n")
    (root / "v2/tests/edge/test_e2_nanopb_parity.py").write_text(
        "def test_e2():\n    assert True\n"
    )
    (root / "v2/tests/edge/test_e5_fixture_replay.py").write_text(
        "def test_e5():\n    assert True\n"
    )
    (root / "v2/docs/campaigns/C-001/SHIFT.md").write_text("runtime output\n")
    (root / "v2/firmware/rv1106/image/image-manifest.json").write_text("{}\n")
    (root / "v1/src/v1_support.py").write_text("V1_SUPPORT = True\n")
    (root / ".gitignore").write_text(
        "v2/pkg/ignored.py\n"
        "v2/firmware/rv1106/image/*.img\n"
    )
    (root / "v2/pkg/ignored.py").write_text("IGNORED = True\n")
    _command("git", "init", "-q", cwd=root)
    _command("git", "config", "user.email", "test@example.com", cwd=root)
    _command("git", "config", "user.name", "C001 Test", cwd=root)
    _command("git", "add", ".gitignore", "v1/src", "v2", cwd=root)
    _command("git", "commit", "-qm", "base", cwd=root)
    (root / "v2/pkg/untracked.py").write_text("UNTRACKED = 2\n")
    os.symlink("tracked.py", root / "v2/pkg/link.py")
    return root


def _snapshot(tmp_path: Path) -> tuple[Path, Path]:
    repo = _source_repo(tmp_path)
    manifest = tmp_path / "source.json"
    evidence.create_source_snapshot(repo, manifest)
    return repo, manifest


def _fixture_manifest(repo: Path, campaign: Path, source_manifest: Path) -> Path:
    for raw_root in evidence.GATE_DATA_ROOTS:
        fixture_root = repo / raw_root
        fixture_root.mkdir(parents=True, exist_ok=True)
        (fixture_root / "fixture.bin").write_bytes(raw_root.encode("utf-8"))
    image_root = repo / evidence.GATE_IMAGE_ROOT
    (image_root / "payload.img").write_bytes(b"image payload")
    destination = campaign / "gate-support.json"
    evidence.create_fixture_manifest(repo, source_manifest, destination)
    return destination


def _detached_fixture_root(
    repo: Path,
    destination: Path,
    source_manifest: Path,
    fixture_manifest: Path,
) -> Path:
    source_payload = json.loads(source_manifest.read_text())
    fixture_payload = json.loads(fixture_manifest.read_text())
    relative_paths = [
        entry["path"]
        for entry in fixture_payload["files"]
        if not entry["path"].startswith(f"{evidence.GATE_V1_ROOT}/")
    ]
    relative_paths.extend(
        entry["path"]
        for entry in source_payload["files"]
        if entry["path"].startswith(f"{evidence.GATE_IMAGE_ROOT}/")
    )
    for relative in relative_paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((repo / relative).read_bytes())
    return destination


def _add_tracked_failure(repo: Path) -> None:
    (repo / "v2/tests/test_must_fail.py").write_text(
        "def test_must_fail():\n    assert False, 'must execute'\n"
    )
    _command("git", "add", "v2/tests/test_must_fail.py", cwd=repo)
    _command("git", "commit", "-qm", "tracked failing gate test", cwd=repo)


def _gate_platform() -> dict[str, object]:
    return {
        "os": "Linux",
        "arch": "x86_64",
        "python": "cpython 3.10.14",
        "toolchain": (
            "python_compiler=GCC 13.2;python_optimize=0;rmem_max=8388608;"
            "rmem_default=8388608"
        ),
        "rmem_max_bytes": 8_388_608,
        "rmem_default_bytes": 8_388_608,
    }


class FakeRun:
    def __init__(self, *results: subprocess.CompletedProcess[str]):
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return self.results.pop(0)


def _arm_elf(path: Path) -> None:
    payload = bytearray(52)
    payload[:7] = b"\x7fELF\x01\x01\x01"
    struct.pack_into("<H", payload, 16, 2)
    struct.pack_into("<H", payload, 18, 40)
    path.write_bytes(payload)
    path.chmod(0o755)


def test_source_manifest_is_deterministic_complete_and_portable(tmp_path):
    repo = _source_repo(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_bundle = tmp_path / "first.tar"
    second_bundle = tmp_path / "second.tar"
    one = evidence.create_source_snapshot(repo, first, bundle_path=first_bundle)
    two = evidence.create_source_snapshot(repo, second, bundle_path=second_bundle)

    assert first.read_bytes() == second.read_bytes()
    assert first_bundle.read_bytes() == second_bundle.read_bytes()
    assert one.source_tree_sha256 == two.source_tree_sha256
    payload = json.loads(first.read_text())
    paths = [entry["path"] for entry in payload["files"]]
    assert "v2/pkg/tracked.py" in paths
    assert "v2/pkg/untracked.py" in paths
    assert "v2/pkg/link.py" in paths
    assert "v2/pkg/ignored.py" not in paths
    assert not any(path.startswith(evidence.CAMPAIGN_RUNTIME_PREFIX) for path in paths)
    link = next(entry for entry in payload["files"] if entry["path"].endswith("link.py"))
    assert link["type"] == "symlink"
    assert link["sha256"] == evidence._sha256_bytes(b"tracked.py")

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(first_bundle) as archive:
        archive.extractall(extracted, filter="data")
    verified = evidence.verify_source_snapshot(
        extracted, extracted / evidence.BUNDLE_MANIFEST_NAME
    )
    assert verified["source_tree_sha256"] == one.source_tree_sha256
    assert (extracted / "v1/src/v1_support.py").is_file()


def test_source_manifest_excludes_shift_history_from_git_tree_and_manifest(tmp_path):
    repo = _source_repo(tmp_path)
    history = repo / evidence.CAMPAIGN_HISTORY_PREFIX.rstrip("/")
    tracked = history / "shift-0001-deadbeef/archive.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked archive\n")
    _command("git", "add", tracked.relative_to(repo).as_posix(), cwd=repo)
    _command("git", "commit", "-qm", "tracked shift archive", cwd=repo)
    untracked = history / "shift-0002-cafebabe/archive.json"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("untracked archive\n")
    neighbor = repo / "v2/docs/campaigns/C-001-shifts-extra/kept.txt"
    neighbor.parent.mkdir(parents=True)
    neighbor.write_text("source input\n")

    manifest = tmp_path / "source.json"
    bundle = tmp_path / "source.tar"
    snapshot = evidence.create_source_snapshot(repo, manifest, bundle_path=bundle)
    payload = json.loads(manifest.read_text())
    paths = [entry["path"] for entry in payload["files"]]
    assert not any(path.startswith(evidence.CAMPAIGN_HISTORY_PREFIX) for path in paths)
    assert neighbor.relative_to(repo).as_posix() in paths
    with tarfile.open(bundle) as archive:
        assert not any(
            name.startswith(evidence.CAMPAIGN_HISTORY_PREFIX)
            for name in archive.getnames()
        )

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(bundle) as archive:
        archive.extractall(extracted, filter="data")
    extracted_history = extracted / evidence.CAMPAIGN_HISTORY_PREFIX / "late.json"
    extracted_history.parent.mkdir(parents=True)
    extracted_history.write_text("ignored archive\n")
    verified = evidence.verify_source_snapshot(
        extracted, extracted / evidence.BUNDLE_MANIFEST_NAME
    )
    assert verified["source_tree_sha256"] == snapshot.source_tree_sha256

    forged = dict(payload)
    forged["files"] = sorted(
        [*payload["files"], evidence._source_entry(repo, tracked.relative_to(repo).as_posix())],
        key=lambda entry: entry["path"],
    )
    forged["source_tree_sha256"] = evidence._tree_digest(
        forged["revision_sha"], forged["files"], forged["v1_head"]
    )
    forged_manifest = tmp_path / "forged-history.json"
    forged_manifest.write_text(json.dumps(forged))
    with pytest.raises(evidence.EvidenceError, match="out-of-scope path"):
        evidence.verify_source_snapshot(repo, forged_manifest)


def test_source_manifest_refuses_content_and_member_set_tampering(tmp_path):
    repo, manifest = _snapshot(tmp_path)
    (repo / "v2/pkg/tracked.py").write_text("TRACKED = 999\n")
    with pytest.raises(evidence.EvidenceError, match="differs from manifest"):
        evidence.verify_source_snapshot(repo, manifest)

    (repo / "v2/pkg/tracked.py").write_text("TRACKED = 1\n")
    (repo / "v2/pkg/late.py").write_text("LATE = True\n")
    with pytest.raises(evidence.EvidenceError, match="member set differs"):
        evidence.verify_source_snapshot(repo, manifest)

    (repo / "v2/pkg/late.py").unlink()
    payload = json.loads(manifest.read_text())
    payload["revision_sha"] = "b" * 40
    payload["v1_head"]["tree_sha256"] = evidence._sha256_bytes(
        evidence._canonical_json(
            {
                "revision_sha": payload["revision_sha"],
                "git_tree_oid": payload["v1_head"]["git_tree_oid"],
                "files": payload["v1_head"]["files"],
            }
        )
    )
    payload["source_tree_sha256"] = evidence._tree_digest(
        payload["revision_sha"], payload["files"], payload["v1_head"]
    )
    changed_revision = tmp_path / "changed-revision.json"
    changed_revision.write_text(json.dumps(payload))
    with pytest.raises(evidence.EvidenceError, match="base HEAD differs"):
        evidence.verify_source_snapshot(repo, changed_revision)


@pytest.mark.parametrize("target", ["ignored.py", "missing.py", "../../../outside.py"])
def test_source_manifest_refuses_unmanifested_dangling_or_outside_symlinks(
    tmp_path, target
):
    repo = _source_repo(tmp_path)
    if target.startswith("../"):
        (tmp_path / "outside.py").write_text("SECRET = 1\n")
    os.symlink(target, repo / "v2/pkg/bad.py")
    with pytest.raises(evidence.EvidenceError, match="not a manifested input"):
        evidence.create_source_snapshot(repo, tmp_path / "source.json")


def test_source_manifest_never_overwrites_or_self_excludes_build_inputs(tmp_path):
    repo = _source_repo(tmp_path)
    destination = tmp_path / "source.json"
    evidence.create_source_snapshot(repo, destination)
    with pytest.raises(evidence.EvidenceError, match="overwrite"):
        evidence.create_source_snapshot(repo, destination)
    with pytest.raises(evidence.EvidenceError, match="outside v2 build inputs"):
        evidence.create_source_snapshot(repo, repo / "v2/source.json")

    live_destination = repo / evidence.CAMPAIGN_RUNTIME_PREFIX / "source.json"
    evidence.create_source_snapshot(repo, live_destination)
    assert live_destination.is_file()
    history_destination = repo / evidence.CAMPAIGN_HISTORY_PREFIX / "source.json"
    with pytest.raises(evidence.EvidenceError, match="outside v2 build inputs"):
        evidence.create_source_snapshot(repo, history_destination)
    assert not history_destination.exists()


def test_source_manifest_ignores_known_generated_v1_package_metadata(tmp_path):
    repo = _source_repo(tmp_path)
    egg_info = repo / "v1/src/skyweave.egg-info"
    dist_info = repo / "v1/src/skyweave-0.0.dist-info"
    egg_info.mkdir()
    dist_info.mkdir()
    (egg_info / "PKG-INFO").write_text("generated metadata\n")
    (dist_info / "METADATA").write_text("generated metadata\n")
    manifest = tmp_path / "source.json"

    evidence.create_source_snapshot(repo, manifest)

    v1_paths = [entry["path"] for entry in json.loads(manifest.read_text())["v1_head"]["files"]]
    assert v1_paths == ["v1/src/v1_support.py"]


def test_source_manifest_refuses_git_replace_commit_rewriting_head(tmp_path):
    repo = _source_repo(tmp_path)
    base_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    evil = tmp_path / "evil-clone"
    _command("git", "clone", "-q", str(repo), str(evil), cwd=tmp_path)
    _command("git", "config", "user.email", "test@example.com", cwd=evil)
    _command("git", "config", "user.name", "C001 Test", cwd=evil)
    (evil / "v1/src/v1_support.py").write_text("EVIL = True\n")
    (evil / "v1/src/sitecustomize.py").write_text("print('forged')\n")
    _command("git", "add", "v1/src", cwd=evil)
    _command("git", "commit", "-qm", "replacement", cwd=evil)
    evil_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=evil,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _command(
        "git",
        "fetch",
        "-q",
        str(evil),
        f"{evil_revision}:refs/heads/evil-replacement",
        cwd=repo,
    )
    _command("git", "replace", base_revision, evil_revision, cwd=repo)
    (repo / "v1/src/v1_support.py").write_text("EVIL = True\n")
    (repo / "v1/src/sitecustomize.py").write_text("print('forged')\n")

    with pytest.raises(evidence.EvidenceError, match="replace refs"):
        evidence.create_source_snapshot(repo, tmp_path / "source.json")


def test_source_manifest_refuses_active_git_grafts(tmp_path):
    repo = _source_repo(tmp_path)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / ".git/info/grafts").write_text(f"{revision} {revision}\n")

    with pytest.raises(evidence.EvidenceError, match="active Git grafts"):
        evidence.create_source_snapshot(repo, tmp_path / "source.json")


def test_gate_support_manifest_is_deterministic_and_exact(tmp_path):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    first = _fixture_manifest(repo, campaign, manifest)
    second = campaign / "gate-support-second.json"
    evidence.create_fixture_manifest(repo, manifest, second)

    assert first.read_bytes() == second.read_bytes()
    payload = evidence.verify_fixture_manifest(
        repo,
        first,
        source_manifest=json.loads(manifest.read_text()),
    )
    assert payload["roots"] == list(evidence.GATE_FIXTURE_ROOTS)
    assert {
        next(root for root in evidence.GATE_FIXTURE_ROOTS if entry["path"].startswith(root))
        for entry in payload["files"]
    } == set(evidence.GATE_FIXTURE_ROOTS)


@pytest.mark.parametrize("tamper", ["mutation", "missing", "extra", "symlink"])
def test_gate_support_manifest_refuses_member_tampering(tmp_path, tamper):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    target = repo / evidence.GATE_DATA_ROOTS[0] / "fixture.bin"
    if tamper == "mutation":
        target.write_bytes(b"changed")
    elif tamper == "missing":
        target.unlink()
    elif tamper == "extra":
        (target.parent / "extra.bin").write_bytes(b"extra")
    else:
        target.unlink()
        os.symlink(repo / "v1/src/v1_support.py", target)

    with pytest.raises(evidence.EvidenceError):
        evidence.verify_fixture_manifest(
            repo,
            fixtures,
            source_manifest=json.loads(manifest.read_text()),
        )


@pytest.mark.parametrize("change", ["tracked-edit", "untracked-python"])
def test_gate_support_manifest_requires_v1_exactly_from_head(tmp_path, change):
    repo, manifest = _snapshot(tmp_path)
    if change == "tracked-edit":
        (repo / "v1/src/v1_support.py").write_text("MALICIOUS = True\n")
    else:
        (repo / "v1/src/sitecustomize.py").write_text("MALICIOUS = True\n")
    campaign = tmp_path / "campaign"
    campaign.mkdir()

    with pytest.raises(evidence.EvidenceError, match="v1 gate support must match HEAD|differs"):
        _fixture_manifest(repo, campaign, manifest)


def test_detached_gate_support_maps_exact_members_and_image_overlay(tmp_path):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    detached = _detached_fixture_root(
        repo,
        tmp_path / "detached-support",
        manifest,
        fixtures,
    )
    source_payload = evidence.verify_source_snapshot(repo, manifest)
    fixture_payload = evidence.verify_fixture_manifest(
        detached,
        fixtures,
        source_manifest=source_payload,
    )
    assert not (detached / "v1").exists()

    with evidence._staged_source_tree(
        repo,
        source_payload,
        fixture_manifest=fixture_payload,
        fixture_root=detached,
    ) as stage:
        assert (stage / evidence.CAMPAIGN_RUNTIME_PREFIX).is_dir()
        assert not any((stage / evidence.CAMPAIGN_RUNTIME_PREFIX).iterdir())
        assert (stage / "v1/src/v1_support.py").is_file()
        assert not (stage / "v1/src/v1_support.py").is_symlink()
        assert not (stage / "v1/src/sitecustomize.py").exists()
        image_manifest = stage / evidence.GATE_IMAGE_ROOT / "image-manifest.json"
        image_payload = stage / evidence.GATE_IMAGE_ROOT / "payload.img"
        assert image_manifest.is_file() and not image_manifest.is_symlink()
        assert image_payload.is_symlink()
        assert os.readlink(image_payload) == str(
            detached / evidence.GATE_IMAGE_ROOT / "payload.img"
        )
        import_environment = evidence._clean_gate_environment()
        import_environment["PYTHONPATH"] = "../v1/src"
        completed = subprocess.run(
            [sys.executable, "-c", "import v1_support"],
            cwd=stage / "v2",
            capture_output=True,
            text=True,
            check=False,
            env=import_environment,
        )
        assert completed.returncode == 0
        assert not (stage / "v1/src/__pycache__").exists()


def test_gate_refuses_self_hashed_detached_v1_manifest_forgery(tmp_path):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    detached = _detached_fixture_root(
        repo,
        tmp_path / "detached-support",
        manifest,
        fixtures,
    )
    forged = json.loads(fixtures.read_text())
    forged_entry = {
        "path": "v1/src/sitecustomize.py",
        "type": "file",
        "size": 21,
        "sha256": evidence._sha256_bytes(b"print('FORGED HOOK')\n"),
    }
    forged["files"].append(forged_entry)
    forged["files"].sort(key=lambda entry: entry["path"])
    forged_v1 = [
        entry
        for entry in forged["files"]
        if entry["path"].startswith("v1/src/")
    ]
    forged["fixture_tree_sha256"] = evidence._fixture_tree_digest(forged["files"])
    forged["v1_head_tree_sha256"] = evidence._sha256_bytes(
        evidence._canonical_json(
            {
                "revision_sha": forged["revision_sha"],
                "git_tree_oid": forged["v1_head_tree_oid"],
                "files": forged_v1,
            }
        )
    )
    forged_path = campaign / "forged-support.json"
    forged_path.write_text(json.dumps(forged) + "\n")
    malicious = detached / "v1/src/sitecustomize.py"
    malicious.parent.mkdir(parents=True)
    malicious.write_text("print('FORGED HOOK')\n")

    with pytest.raises(evidence.EvidenceError, match="authenticated source manifest"):
        evidence.run_gate_evidence(
            repo,
            manifest,
            campaign,
            "subject/gate",
            fixture_manifest_path=forged_path,
            fixture_root=detached,
        )


def test_gate_rechecks_support_manifest_after_the_long_running_process(
    tmp_path, monkeypatch
):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)

    def mutate_support(argv, **kwargs):
        del argv, kwargs
        (repo / evidence.GATE_DATA_ROOTS[0] / "fixture.bin").write_bytes(b"changed")
        return subprocess.CompletedProcess([], 0, "10 passed in 1.00s\n", "")

    monkeypatch.setattr(evidence, "_evidence_process", mutate_support)
    monkeypatch.setattr(evidence, "_default_gate_platform", _gate_platform)
    with pytest.raises(evidence.EvidenceError, match="differs from manifest"):
        evidence.run_gate_evidence(
            repo,
            manifest,
            campaign,
            "subject/gate",
            fixture_manifest_path=fixtures,
            fixture_root=repo,
        )
    assert not (campaign / "subject/gate.json").exists()


def test_gate_support_cli_requires_source_and_support_manifest_paths(tmp_path):
    args = evidence._parser().parse_args(
        [
            "fixtures",
            "--repo-root",
            str(tmp_path),
            "--manifest",
            str(tmp_path / "source.json"),
            "--out",
            str(tmp_path / "gate-support.json"),
        ]
    )
    assert args.manifest == tmp_path / "source.json"
    assert args.out == tmp_path / "gate-support.json"


def test_production_cli_refuses_arbitrary_repo_and_campaign_roots(tmp_path):
    repo = _source_repo(tmp_path)
    with pytest.raises(evidence.EvidenceError, match="canonical"):
        evidence.main(
            [
                "manifest",
                "--repo-root",
                str(repo),
                "--out",
                str(tmp_path / "source.json"),
            ]
        )
    with pytest.raises(evidence.EvidenceError, match="campaign root"):
        evidence.main(
            [
                "subject",
                "--campaign-root",
                str(tmp_path),
                "--manifest",
                str(tmp_path / "source.json"),
                "--gate-transcript",
                "gate.json",
                "--fenced-transcript",
                "fenced.json",
                "--out",
                "subject.json",
                "--phase",
                "phase1",
            ]
        )


def test_production_cli_holds_shared_shift_lock_and_validates_current_shift(
    tmp_path, monkeypatch
):
    repo = _source_repo(tmp_path)
    campaign = repo / evidence.CAMPAIGN_RUNTIME_PREFIX.rstrip("/")
    destination = campaign / "source.json"
    events: list[tuple[str, object]] = []
    real_lock = c001.campaign_shift_lock
    real_validate = c001.validate_current_shift
    real_snapshot = evidence.create_source_snapshot

    @contextmanager
    def observed_lock(campaign_dir, *, exclusive=False):
        events.append(("lock-enter", exclusive))
        try:
            with real_lock(campaign_dir, exclusive=exclusive):
                yield
        finally:
            events.append(("lock-exit", exclusive))

    def observed_validate(campaign_dir, *, verify_artifacts=True):
        events.append(("validate", verify_artifacts))
        return real_validate(campaign_dir, verify_artifacts=verify_artifacts)

    def observed_snapshot(*args, **kwargs):
        events.append(("operation", None))
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(evidence, "_module_repo_root", lambda: repo)
    monkeypatch.setattr(c001, "campaign_shift_lock", observed_lock)
    monkeypatch.setattr(c001, "validate_current_shift", observed_validate)
    monkeypatch.setattr(evidence, "create_source_snapshot", observed_snapshot)

    evidence.main(
        [
            "manifest",
            "--repo-root",
            str(repo),
            "--out",
            str(destination),
        ]
    )

    assert events == [
        ("lock-enter", False),
        ("validate", True),
        ("operation", None),
        ("lock-exit", False),
    ]
    assert (repo / evidence.CAMPAIGN_HISTORY_PREFIX / ".lock").is_file()
    paths = [entry["path"] for entry in json.loads(destination.read_text())["files"]]
    assert not any(path.startswith(evidence.CAMPAIGN_HISTORY_PREFIX) for path in paths)


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("10 passed, 1 skipped in 1.00s\n", ""),
        ("10 passed, 1 xfailed in 1.00s\n", ""),
        ("10 passed, 1 xpassed in 1.00s\n", ""),
        ("10 passed, 2 deselected in 1.00s\n", ""),
        ("10 passed, 1 warning in 1.00s\n", ""),
        ("9 passed, 1 failed in 1.00s\n", ""),
        ("10 passed in 1.00s\n", "warning emitted on stderr\n"),
    ],
)
def test_gate_never_turns_non_authoritative_pytest_output_into_pass(
    tmp_path, stdout, stderr, monkeypatch
):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    runner = FakeRun(subprocess.CompletedProcess([], 0, stdout, stderr))
    monkeypatch.setattr(evidence, "_evidence_process", runner)
    monkeypatch.setattr(evidence, "_default_gate_platform", _gate_platform)
    with pytest.raises(evidence.EvidenceError, match="did not prove"):
        evidence.run_gate_evidence(
            repo,
            manifest,
            campaign,
            "subject/gate",
            fixture_manifest_path=fixtures,
            fixture_root=repo,
        )
    raw = (campaign / "subject/gate.stdout").read_text()
    transcript = json.loads((campaign / "subject/gate.json").read_text())
    assert "C001_EVIDENCE_PASS" not in raw
    if stderr:
        assert "--- captured stderr ---\n" + stderr in raw
    assert transcript["asserted_outcome"] is False


def test_gate_emits_exact_schema_and_sanitizes_pytest_selection_environment(
    tmp_path, monkeypatch
):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k not_authoritative")
    monkeypatch.setenv("PYTEST_PLUGINS", "outside.plugin")
    monkeypatch.setenv("PYTHONOPTIMIZE", "2")
    monkeypatch.setenv("PYTHONNOUSERSITE", "0")
    monkeypatch.setenv("PYTHONUSERBASE", "/tmp/forged-userbase")
    monkeypatch.setenv("SKYWEAVE_EDGE_BIN", "/tmp/forged-edge")
    monkeypatch.setenv("SKYWEAVE_FIXTURE_TOOL", "/tmp/forged-fixture-tool")
    monkeypatch.setenv("SKYWEAVE_REGENERATE_WIRE_GOLDEN", "1")
    runner = FakeRun(subprocess.CompletedProcess([], 0, "572 passed in 284.00s\n", ""))
    monkeypatch.setattr(evidence, "_evidence_process", runner)
    monkeypatch.setattr(evidence, "_default_gate_platform", _gate_platform)
    transcript_path = evidence.run_gate_evidence(
        repo,
        manifest,
        campaign,
        "subject/gate",
        fixture_manifest_path=fixtures,
        fixture_root=repo,
    )
    argv, kwargs = runner.calls[0]
    assert argv == [sys.executable, "-m", "pytest", "-q"]
    assert "PYTEST_ADDOPTS" not in kwargs["env"]
    assert "PYTEST_PLUGINS" not in kwargs["env"]
    assert "PYTHONOPTIMIZE" not in kwargs["env"]
    assert "PYTHONUSERBASE" not in kwargs["env"]
    assert not any(name.startswith("SKYWEAVE_") for name in kwargs["env"])
    assert kwargs["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert kwargs["env"]["PYTHONNOUSERSITE"] == "1"
    assert kwargs["env"]["PYTHONPATH"] == "src:../v1/src"
    transcript = json.loads(transcript_path.read_text())
    assert set(transcript) == {
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
    assert transcript["platform"] == _gate_platform()
    assert transcript["fixture_manifest_path"] == "gate-support.json"
    assert transcript["fixture_manifest_sha256"] == c001.sha256_file(fixtures)
    assert transcript["pythonpath"] == "src:../v1/src"
    assert transcript["command"] == shlex.join([sys.executable, "-m", "pytest", "-q"])
    assert "C001_EVIDENCE_PASS kind=gate_platform_suite" in (
        campaign / "subject/gate.stdout"
    ).read_text()


def test_gate_rechecks_manifest_after_the_long_running_process(tmp_path, monkeypatch):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)

    def mutate_source(argv, **kwargs):
        del argv, kwargs
        (repo / "v2/pkg/tracked.py").write_text("MUTATED_DURING_GATE = True\n")
        return subprocess.CompletedProcess([], 0, "10 passed in 1.00s\n", "")

    monkeypatch.setattr(evidence, "_evidence_process", mutate_source)
    monkeypatch.setattr(evidence, "_default_gate_platform", _gate_platform)
    with pytest.raises(evidence.EvidenceError, match="differs from manifest"):
        evidence.run_gate_evidence(
            repo,
            manifest,
            campaign,
            "subject/gate",
            fixture_manifest_path=fixtures,
            fixture_root=repo,
        )
    assert not (campaign / "subject/gate.json").exists()


def test_gate_staging_excludes_ignored_conftest_that_hides_a_failure(
    tmp_path, monkeypatch
):
    repo = _source_repo(tmp_path)
    _add_tracked_failure(repo)
    with (repo / ".gitignore").open("a") as handle:
        handle.write("v2/conftest.py\n__pycache__/\n*.pyc\n")
    (repo / "v2/conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items[:] = [item for item in items if 'must_fail' not in item.nodeid]\n"
    )
    manifest = tmp_path / "source.json"
    evidence.create_source_snapshot(repo, manifest)
    live = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo / "v2",
        capture_output=True,
        text=True,
        check=False,
        env=evidence._clean_gate_environment(),
    )
    assert live.returncode == 0 and "passed" in live.stdout
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    observed_cwds: list[Path] = []

    def real_process(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        observed_cwds.append(cwd)
        assert not (cwd / "conftest.py").exists()
        return subprocess.run(argv, **kwargs)

    monkeypatch.setattr(evidence, "_evidence_process", real_process)
    monkeypatch.setattr(evidence, "_default_gate_platform", _gate_platform)
    with pytest.raises(evidence.EvidenceError, match="did not prove"):
        evidence.run_gate_evidence(
            repo,
            manifest,
            campaign,
            "subject/gate",
            fixture_manifest_path=fixtures,
            fixture_root=repo,
        )
    assert observed_cwds and observed_cwds[0] != repo / "v2"
    assert "C001_EVIDENCE_PASS" not in (campaign / "subject/gate.stdout").read_text()


def test_gate_staging_excludes_ignored_sourceless_sitecustomize(
    tmp_path, monkeypatch
):
    repo = _source_repo(tmp_path)
    _add_tracked_failure(repo)
    with (repo / ".gitignore").open("a") as handle:
        handle.write("v2/sitecustomize.pyc\n__pycache__/\n*.pyc\n")
    payload_source = tmp_path / "sitecustomize-source.py"
    payload_source.write_text(
        "import os\n"
        "print('1 passed in 0.01s', flush=True)\n"
        "os._exit(0)\n"
    )
    py_compile.compile(
        str(payload_source),
        cfile=str(repo / "v2/sitecustomize.pyc"),
        doraise=True,
    )
    manifest = tmp_path / "source.json"
    evidence.create_source_snapshot(repo, manifest)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    observed_cwds: list[Path] = []

    def real_process(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        observed_cwds.append(cwd)
        assert not (cwd / "sitecustomize.pyc").exists()
        return subprocess.run(argv, **kwargs)

    monkeypatch.setattr(evidence, "_evidence_process", real_process)
    monkeypatch.setattr(evidence, "_default_gate_platform", _gate_platform)
    with pytest.raises(evidence.EvidenceError, match="did not prove"):
        evidence.run_gate_evidence(
            repo,
            manifest,
            campaign,
            "subject/gate",
            fixture_manifest_path=fixtures,
            fixture_root=repo,
        )
    assert observed_cwds and observed_cwds[0] != repo / "v2"
    assert "C001_EVIDENCE_PASS" not in (campaign / "subject/gate.stdout").read_text()


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"os": "Darwin"}, "must run on Linux"),
        ({"arch": "arm64"}, "x86_64/amd64"),
        (
            {"rmem_default_bytes": 262_144},
            "at least 4 MiB",
        ),
    ],
)
def test_gate_refuses_wrong_platform_or_receive_buffers(
    tmp_path, updates, match, monkeypatch
):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    facts = {**_gate_platform(), **updates}
    runner = FakeRun(subprocess.CompletedProcess([], 0, "1 passed in 1.00s\n", ""))
    monkeypatch.setattr(evidence, "_evidence_process", runner)
    monkeypatch.setattr(evidence, "_default_gate_platform", lambda: facts)
    with pytest.raises(evidence.EvidenceError, match=match):
        evidence.run_gate_evidence(
            repo,
            manifest,
            campaign,
            "gate",
            fixture_manifest_path=fixtures,
            fixture_root=repo,
        )
    assert runner.calls == []


def test_fenced_and_subject_outputs_pass_the_existing_strict_validator(
    tmp_path, monkeypatch
):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    fixtures = _fixture_manifest(repo, campaign, manifest)
    gate_runner = FakeRun(subprocess.CompletedProcess([], 0, "572 passed in 2.00s\n", ""))
    fence_runner = FakeRun(subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(evidence, "_evidence_process", gate_runner)
    monkeypatch.setattr(evidence, "_default_gate_platform", _gate_platform)
    gate = evidence.run_gate_evidence(
        repo,
        manifest,
        campaign,
        "subject/gate",
        fixture_manifest_path=fixtures,
        fixture_root=repo,
    )
    for name in (
        "GIT_LITERAL_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_NAMESPACE",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
    ):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    monkeypatch.setattr(evidence, "_evidence_process", fence_runner)
    fenced = evidence.run_fenced_evidence(
        repo, manifest, campaign, "subject/fenced"
    )
    assert fence_runner.calls[0][0] == list(evidence.FENCED_COMMAND)
    fenced_env = fence_runner.calls[0][1]["env"]
    assert not any(
        name in fenced_env
        for name in (
            "GIT_LITERAL_PATHSPECS",
            "GIT_GLOB_PATHSPECS",
            "GIT_NOGLOB_PATHSPECS",
            "GIT_ICASE_PATHSPECS",
            "GIT_CONFIG_PARAMETERS",
            "GIT_NAMESPACE",
            "GIT_REPLACE_REF_BASE",
            "GIT_SHALLOW_FILE",
        )
    )
    assert fenced_env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert fenced_env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert fenced_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert fenced_env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert fenced_env["GIT_CONFIG_KEY_2"] == "core.excludesFile"
    assert fenced_env["GIT_CONFIG_VALUE_2"] == os.devnull
    assert fenced_env["GIT_CONFIG_KEY_3"] == "core.fsmonitor"
    assert fenced_env["GIT_CONFIG_VALUE_3"] == "false"
    fenced_payload = json.loads(fenced.read_text())
    assert fenced_payload["checked_paths"] == list(evidence.FENCED_PATHS)
    assert fenced_payload["changed_paths"] == []

    subject = evidence.package_subject_evidence(
        campaign,
        manifest,
        gate.relative_to(campaign).as_posix(),
        fenced.relative_to(campaign).as_posix(),
        "subject/phase1.json",
        phase="phase1",
    )
    packaged = json.loads(subject.read_text())
    c001.validate_subject_to(
        packaged,
        "phase1",
        evidence_root=campaign / "validation-root.json",
    )
    assert packaged["gate_platform_suite_green"] is True
    assert packaged["host_board_parity_within_tolerance"] is None


@pytest.mark.parametrize(
    "key,value,match",
    [
        ("status.showUntrackedFiles", "no", "showUntrackedFiles"),
        ("core.fileMode", "false", "fileMode=true"),
        ("core.excludesFile", "/tmp/global-ignore", "core.excludesFile"),
        ("core.fsmonitor", "true", "core.fsmonitor"),
    ],
)
def test_fenced_proof_refuses_git_config_that_can_hide_changes(
    tmp_path, key, value, match
):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    _command("git", "config", key, value, cwd=repo)
    with pytest.raises(evidence.EvidenceError, match=match):
        evidence.run_fenced_evidence(repo, manifest, campaign, "subject/fenced")


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_fenced_proof_refuses_index_flags_that_hide_worktree_changes(tmp_path, flag):
    repo = _source_repo(tmp_path)
    (repo / "v1").mkdir(exist_ok=True)
    fenced_file = repo / "v1/fenced"
    fenced_file.write_text("original\n")
    _command("git", "add", "v1/fenced", cwd=repo)
    _command("git", "commit", "-qm", "fenced input", cwd=repo)
    manifest = tmp_path / "source.json"
    evidence.create_source_snapshot(repo, manifest)
    _command("git", "update-index", flag, "v1/fenced", cwd=repo)
    fenced_file.write_text("hidden edit\n")
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    with pytest.raises(evidence.EvidenceError, match="index entries"):
        evidence.run_fenced_evidence(repo, manifest, campaign, "subject/fenced")


def test_fenced_proof_refuses_malicious_fsmonitor_and_clean_index_bit(tmp_path):
    repo = _source_repo(tmp_path)
    fenced_file = repo / "v1/fenced"
    fenced_file.write_text("original\n")
    _command("git", "add", "v1/fenced", cwd=repo)
    _command("git", "commit", "-qm", "fenced input", cwd=repo)
    manifest = tmp_path / "source.json"
    evidence.create_source_snapshot(repo, manifest)
    _command("git", "config", "core.fsmonitor", "/bin/echo token", cwd=repo)
    _command("git", "update-index", "--fsmonitor-valid", "v1/fenced", cwd=repo)
    fenced_file.write_text("hidden edit\n")
    campaign = tmp_path / "campaign"
    campaign.mkdir()

    with pytest.raises(evidence.EvidenceError, match="core.fsmonitor"):
        evidence.run_fenced_evidence(repo, manifest, campaign, "subject/fenced")


def test_fenced_proof_refuses_modified_root_gitignore_that_hides_critical_file(
    tmp_path,
):
    repo, manifest = _snapshot(tmp_path)
    with (repo / ".gitignore").open("a") as handle:
        handle.write("v2/proto/forbidden.proto\n")
    (repo / "v2/proto").mkdir(parents=True)
    (repo / "v2/proto/forbidden.proto").write_text("hidden contract\n")
    campaign = tmp_path / "campaign"
    campaign.mkdir()

    with pytest.raises(evidence.EvidenceError, match="root .gitignore"):
        evidence.run_fenced_evidence(repo, manifest, campaign, "subject/fenced")


def test_fenced_proof_refuses_ignored_member_in_critical_fence_root(tmp_path):
    repo = _source_repo(tmp_path)
    with (repo / ".gitignore").open("a") as handle:
        handle.write("v2/proto/forbidden.proto\n")
    _command("git", "add", ".gitignore", cwd=repo)
    _command("git", "commit", "-qm", "pin ignore policy", cwd=repo)
    manifest = tmp_path / "source.json"
    evidence.create_source_snapshot(repo, manifest)
    (repo / "v2/proto").mkdir(parents=True)
    (repo / "v2/proto/forbidden.proto").write_text("hidden contract\n")
    campaign = tmp_path / "campaign"
    campaign.mkdir()

    with pytest.raises(evidence.EvidenceError, match="ignored members"):
        evidence.run_fenced_evidence(repo, manifest, campaign, "subject/fenced")


def test_fenced_proof_refuses_active_git_info_exclude(tmp_path):
    repo, manifest = _snapshot(tmp_path)
    (repo / ".git/info/exclude").write_text("v2/proto/forbidden.proto\n")
    campaign = tmp_path / "campaign"
    campaign.mkdir()

    with pytest.raises(evidence.EvidenceError, match="info/exclude"):
        evidence.run_fenced_evidence(repo, manifest, campaign, "subject/fenced")


def test_fenced_proof_rechecks_ignore_policy_after_status(tmp_path, monkeypatch):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()

    def mutate_ignore_policy(argv, **kwargs):
        del argv, kwargs
        with (repo / ".gitignore").open("a") as handle:
            handle.write("v2/proto/hidden.proto\n")
        hidden = repo / "v2/proto/hidden.proto"
        hidden.parent.mkdir(parents=True)
        hidden.write_text("hidden during status\n")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(evidence, "_evidence_process", mutate_ignore_policy)
    with pytest.raises(evidence.EvidenceError, match="root .gitignore"):
        evidence.run_fenced_evidence(repo, manifest, campaign, "subject/fenced")
    assert not (campaign / "subject/fenced.json").exists()


@pytest.mark.parametrize(
    ("ignore_rule", "hidden_path"),
    [
        ("v1/forbidden.py", "v1/forbidden.py"),
        ("output/new/golden/", "output/new/golden/forbidden.bin"),
    ],
)
def test_fenced_proof_refuses_ignored_v1_or_new_golden_members(
    tmp_path, ignore_rule, hidden_path
):
    repo = _source_repo(tmp_path)
    with (repo / ".gitignore").open("a") as handle:
        handle.write(f"{ignore_rule}\n")
    _command("git", "add", ".gitignore", cwd=repo)
    _command("git", "commit", "-qm", "pin ignore policy", cwd=repo)
    manifest = tmp_path / "source.json"
    evidence.create_source_snapshot(repo, manifest)
    hidden = repo / hidden_path
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_bytes(b"hidden fenced bytes")
    campaign = tmp_path / "campaign"
    campaign.mkdir()

    with pytest.raises(evidence.EvidenceError, match="ignored members"):
        evidence.run_fenced_evidence(repo, manifest, campaign, "subject/fenced")


class FakeBoardTransport:
    name = "fake-ssh-via-jump"

    def __init__(
        self, binary_sha256: str, runtime_library_hashes: list[str] | None = None
    ):
        self.binary_sha256 = binary_sha256
        self.runtime_library_hashes = list(runtime_library_hashes or ["7" * 64])
        self.commands: list[str] = []
        self.pushes: list[tuple[Path, str]] = []

    def push(self, local: Path, remote: str) -> None:
        self.pushes.append((Path(local), remote))

    def run(self, command: str, timeout_s: float = 60.0) -> provision.CommandResult:
        del timeout_s
        self.commands.append(command)
        if command == "cat /sys/class/net/eth0/address":
            stdout = "02:00:00:00:00:aa\n"
        elif command == "cat /etc/os-release":
            stdout = 'PRETTY_NAME="Buildroot 2023.02.6"\n'
        elif command == "uname -r":
            stdout = "5.10.160\n"
        elif command == "sha256sum /oem/usr/lib/librve.so":
            digest = (
                self.runtime_library_hashes.pop(0)
                if len(self.runtime_library_hashes) > 1
                else self.runtime_library_hashes[0]
            )
            stdout = f"{digest}  /oem/usr/lib/librve.so\n"
        elif command.startswith("sha256sum "):
            stdout = f"{self.binary_sha256}  /remote/skyweave-edge\n"
        elif "--self-test-ccl-measure" in command:
            stdout = (
                json.dumps(
                    {
                        "schema": "skyweave-ccl-selftest/1",
                        "full_254_slot_region_scan": True,
                        "mask_moment_centroid": True,
                        "overlap_counter": True,
                    }
                )
                + "\n"
            )
        else:
            stdout = ""
        return provision.CommandResult(["ssh", command], 0, stdout, "")

    def fetch(self, remote: str, local: Path) -> None:  # pragma: no cover - not used
        raise AssertionError((remote, local))

    def spawn(self, command, log_remote, exit_status_remote=None):  # pragma: no cover
        raise AssertionError((command, log_remote, exit_status_remote))

    def terminate(self, pid):  # pragma: no cover - not used
        raise AssertionError(pid)

    def alive(self, pid):  # pragma: no cover - not used
        raise AssertionError(pid)


def test_bug_producer_packages_real_results_through_existing_validator(tmp_path):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    binary = tmp_path / "skyweave-edge"
    _arm_elf(binary)
    build_log = tmp_path / "build.log"
    build_log.write_text("fresh pinned Docker build succeeded\n")
    digest = c001.sha256_file(binary)
    board = FakeBoardTransport(digest)
    host = FakeRun(
        subprocess.CompletedProcess([], 0, "17 passed in 1.00s\n", ""),
        subprocess.CompletedProcess([], 0, "20 passed in 1.00s\n", ""),
    )
    artifact = evidence._produce_bug_verification(
        repo,
        manifest,
        campaign,
        "artifacts/bug-proof",
        binary=binary,
        approved_binary_sha256=digest,
        build_log=build_log,
        docker_image_digest="sha256:" + "c" * 64,
        build_command="docker run --rm pinned-image ./build.sh",
        expected_identity=c001.BoardIdentity(
            "board-a", "02:00:00:00:00:AA", "Buildroot 2023.02.6"
        ),
        expected_kernel="5.10.160",
        ssh_host="192.0.2.104",
        jump_host="jetson-ts",
        toolchain="pinned-arm-sdk plus host pytest",
        python="python",
        transport=board,
        run=host,
        token_factory=lambda: "a" * 32,
    )
    payload = json.loads(artifact.read_text())
    c001.validate_bug_verification_bundle(artifact, payload)
    assert payload["summary"] == {
        "bug_a_verified": True,
        "bug_b_verified": True,
        "e2_green": True,
        "e5_green": True,
    }
    assert payload["binding"]["binary_sha256"] == digest
    assert payload["binding"]["runtime_ive_library"] == {
        "path": "/oem/usr/lib/librve.so",
        "sha256_before": "7" * 64,
        "sha256_after": "7" * 64,
        "stable": True,
    }
    assert payload["provenance"]["build"]["binary_sha256"] == digest
    assert payload["provenance"]["build"]["sha256"] == c001.sha256_file(
        artifact.parent / "build.log"
    )
    assert payload["binding"]["identity"]["mac"] == "02:00:00:00:00:aa"
    assert payload["provenance"]["toolchain"].endswith("board_kernel=5.10.160")
    assert payload["provenance"]["commands"]["bug_a_board"].startswith(
        "LD_LIBRARY_PATH=/oem/usr/lib "
    )
    selftests = [command for command in board.commands if "--self-test-ccl-measure" in command]
    assert len(selftests) == 2
    assert all(command.startswith("LD_LIBRARY_PATH=/oem/usr/lib ") for command in selftests)
    assert board.commands.count("sha256sum /oem/usr/lib/librve.so") == 2
    assert host.calls[0][0] == [
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/edge/test_e2_nanopb_parity.py",
    ]
    assert host.calls[1][0][-1] == "tests/edge/test_e5_fixture_replay.py"
    retained_log = artifact.parent / "build.log"
    retained_bytes = retained_log.read_bytes()
    retained_log.write_text("tampered build log\n")
    with pytest.raises(c001.LedgerIntegrityError, match="retained input digest"):
        c001.validate_bug_verification_bundle(artifact, payload)
    retained_log.write_bytes(retained_bytes)
    with pytest.raises(evidence.EvidenceError, match="overwrite"):
        evidence._produce_bug_verification(
            repo,
            manifest,
            campaign,
            "artifacts/bug-proof",
            binary=binary,
            approved_binary_sha256=digest,
            build_log=build_log,
            docker_image_digest="sha256:" + "c" * 64,
            build_command="docker run --rm pinned-image ./build.sh",
            expected_identity=c001.BoardIdentity(
                "board-a", "02:00:00:00:00:AA", "Buildroot 2023.02.6"
            ),
            expected_kernel="5.10.160",
            ssh_host="192.0.2.104",
            jump_host="jetson-ts",
            toolchain="pinned-arm-sdk plus host pytest",
            transport=board,
            run=host,
        )


def test_public_bug_producer_exposes_no_result_injection_seams():
    parameters = inspect.signature(evidence.produce_bug_verification).parameters
    assert "transport" not in parameters
    assert "run" not in parameters
    assert "token_factory" not in parameters


def test_bug_producer_refuses_non_executable_non_arm_bytes_before_board_contact(tmp_path):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    binary = tmp_path / "skyweave-edge"
    binary.write_bytes(b"not an ARM ELF")
    build_log = tmp_path / "build.log"
    build_log.write_text("fresh build\n")
    board = FakeBoardTransport(c001.sha256_file(binary))
    with pytest.raises(evidence.EvidenceError, match="must be executable"):
        evidence._produce_bug_verification(
            repo,
            manifest,
            campaign,
            "bug",
            binary=binary,
            approved_binary_sha256=c001.sha256_file(binary),
            build_log=build_log,
            docker_image_digest="sha256:" + "c" * 64,
            build_command="docker run pinned ./build.sh",
            expected_identity=c001.BoardIdentity(
                "board-a", "02:00:00:00:00:AA", "Buildroot 2023.02.6"
            ),
            expected_kernel="5.10.160",
            ssh_host="192.0.2.104",
            jump_host="jetson-ts",
            toolchain="pinned",
            transport=board,
        )
    assert board.commands == []


def test_bug_producer_refuses_unapproved_binary_before_board_contact(tmp_path):
    repo, manifest = _snapshot(tmp_path)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    binary = tmp_path / "skyweave-edge"
    _arm_elf(binary)
    build_log = tmp_path / "build.log"
    build_log.write_text("fresh build\n")
    board = FakeBoardTransport("f" * 64)
    with pytest.raises(evidence.EvidenceError, match="approved digest"):
        evidence._produce_bug_verification(
            repo,
            manifest,
            campaign,
            "bug",
            binary=binary,
            approved_binary_sha256="f" * 64,
            build_log=build_log,
            docker_image_digest="sha256:" + "c" * 64,
            build_command="docker run pinned ./build.sh",
            expected_identity=c001.BoardIdentity(
                "board-a", "02:00:00:00:00:AA", "Buildroot 2023.02.6"
            ),
            expected_kernel="5.10.160",
            ssh_host="192.0.2.104",
            jump_host="jetson-ts",
            toolchain="pinned",
            transport=board,
        )
    assert board.commands == []
