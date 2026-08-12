"""The flashable image build: its provenance record, and its two containers.

D8.1-prep item 1 is "full Buildroot image set (boot, kernel, rootfs) from the
pinned SDK commit in the pinned container. Record defconfig, SDK commit, and
SHA-256 of every produced image file in the report's build-provenance section."

The BUILD needs docker and an hour; these tests need neither. What they gate is
everything around it that can silently go wrong:

- the manifest reader's refusals, because a manifest that cannot be believed
  must be an error and not an empty table;
- the two Dockerfiles pinning the SAME SDK commit, because a daemon built from
  one commit and a rootfs from another is two builds under one heading;
- the report saying either what was built or that nothing was, with no third
  state where an absent build renders like a present one.

A missing docker is NOT a skip here. The only sanctioned skip in this directory
is a missing C toolchain (D8.0 review finding 11); everything else asserts.
"""

from __future__ import annotations

import json

import pytest

from skyweave2.edge import image

FIRMWARE = image.FIRMWARE_ROOT
DOCS = image.V2_ROOT / "docs"

GOOD = {
    "schema": "d8-image/1",
    "status": "complete",
    "failed_stage": "",
    "container_tag": "skyweave-image-build:d8.1",
    "board_config": "BoardConfig-SD_CARD-Buildroot-RV1106_Luckfox_Pico_Pro_Max-IPC.mk",
    "declared": {
        "RK_CHIP": "rv1106",
        "RK_BOOT_MEDIUM": "sd_card",
        "LF_TARGET_ROOTFS": "buildroot",
        "RK_BUILDROOT_DEFCONFIG": "luckfox_pico_defconfig",
        "RK_KERNEL_DEFCONFIG": "luckfox_rv1106_linux_defconfig",
    },
    "provenance": {"luckfox_commit": "824b817f889c2cbff1d48fcdb18ab494a68f69d1"},
    "stages": [{"stage": "uboot", "status": "ok", "seconds": 12, "attempts": 3}],
    "files": [{"name": "boot.img", "bytes": 4, "sha256": "ab" * 32}],
    "daemon_baked_in": False,
}


def _write(tmp_path, payload):
    path = tmp_path / "image-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def test_a_complete_manifest_reads_back_everything_the_report_prints(tmp_path):
    manifest = image.load_manifest(_write(tmp_path, GOOD))
    assert manifest is not None
    assert manifest.complete
    assert manifest.sdk_commit == "824b817f889c2cbff1d48fcdb18ab494a68f69d1"
    assert manifest.daemon_baked_in is False
    assert manifest.total_bytes == 4
    labels = [label for label, _ in manifest.defconfig_rows()]
    # Fixed order, not dict order: the report is byte-identical for identical
    # inputs and JSON key order is not something to rest that on.
    assert labels == ["Chip", "Boot medium", "Rootfs", "Buildroot defconfig",
                      "Kernel defconfig"]


def test_no_manifest_is_a_fact_and_not_an_error(tmp_path):
    """A repo where nobody has built an image yet is a normal repo."""
    assert image.load_manifest(tmp_path / "nothing.json") is None


def test_a_complete_build_that_produced_nothing_is_refused(tmp_path):
    """The failure this file exists for.

    Every stage returning zero and no image coming out is not a complete
    build, and a manifest saying otherwise would publish a provenance record
    for bytes that do not exist.
    """
    payload = dict(GOOD, files=[])
    with pytest.raises(image.ImageManifestError, match="did not complete"):
        image.load_manifest(_write(tmp_path, payload))


def test_a_file_entry_without_a_hash_is_refused(tmp_path):
    payload = dict(GOOD, files=[{"name": "boot.img", "bytes": 4}])
    with pytest.raises(image.ImageManifestError, match="no name or no hash"):
        image.load_manifest(_write(tmp_path, payload))


def test_an_unknown_schema_is_refused(tmp_path):
    payload = dict(GOOD, schema="d8-image/99")
    with pytest.raises(image.ImageManifestError, match="schema"):
        image.load_manifest(_write(tmp_path, payload))


def test_a_failed_build_still_parses_and_says_which_stage(tmp_path):
    """A failed build and an absent build must not look the same."""
    payload = dict(GOOD, status="FAILED", failed_stage="rootfs", files=[])
    manifest = image.load_manifest(_write(tmp_path, payload))
    assert manifest is not None
    assert not manifest.complete
    assert manifest.failed_stage == "rootfs"


def test_verification_catches_a_changed_byte_and_ignores_an_absent_file(tmp_path):
    """The images are gitignored, so absent is normal and different is not."""
    manifest = image.load_manifest(_write(tmp_path, GOOD))
    assert image.verify_against_disk(manifest, tmp_path) == []
    (tmp_path / "boot.img").write_bytes(b"XXXX")
    problems = image.verify_against_disk(manifest, tmp_path)
    assert len(problems) == 1 and "boot.img" in problems[0]


# ---------------------------------------------------------------------------
# The two containers
# ---------------------------------------------------------------------------


def _env_value(text: str, key: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"ENV {key}="):
            return stripped.split("=", 1)[1].strip()
    return ""


def test_both_containers_pin_the_same_sdk_commit():
    """A daemon from one commit and a rootfs from another are two builds.

    The report publishes them under one build-provenance heading, so if these
    ever drift the heading becomes a lie without any single line being wrong.
    """
    daemon_dockerfile = (FIRMWARE / "docker" / "Dockerfile").read_text(encoding="utf-8")
    image_dockerfile = (FIRMWARE / "docker" / "Dockerfile.image").read_text(
        encoding="utf-8"
    )
    for key in ("LUCKFOX_COMMIT", "LUCKFOX_REPO"):
        left = _env_value(daemon_dockerfile, key)
        right = _env_value(image_dockerfile, key)
        assert left and left == right, f"{key}: {left!r} vs {right!r}"


def test_both_containers_pin_the_same_base_image_digest():
    daemon_dockerfile = (FIRMWARE / "docker" / "Dockerfile").read_text(encoding="utf-8")
    image_dockerfile = (FIRMWARE / "docker" / "Dockerfile.image").read_text(
        encoding="utf-8"
    )

    def base(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("FROM "):
                return line.split()[1]
        return ""

    assert base(daemon_dockerfile).startswith("debian:bookworm-slim@sha256:")
    assert base(daemon_dockerfile) == base(image_dockerfile)


def test_the_build_script_is_executable_and_declares_its_board():
    script = FIRMWARE / "scripts" / "build-image.sh"
    assert script.exists()
    assert script.stat().st_mode & 0o111, "build-image.sh is not executable"
    text = script.read_text(encoding="utf-8")
    # The board is declared in the script, not chosen from a menu whose
    # numbering is a property of the SDK's day.
    assert "SKYWEAVE_BOARD_CONFIG:-BoardConfig-SD_CARD-Buildroot-RV1106" in text
    assert "RV1106" in text
    # Bounded retry, not an infinite one: the emulated toolchain crashes and a
    # loop that never gives up would hide a real failure.
    assert "BUILD_ATTEMPTS:-" in text


def test_the_image_output_is_ignored_but_the_manifest_is_not():
    """The bytes are derived; the record of which bytes is the deliverable."""
    ignore = (FIRMWARE / ".gitignore").read_text(encoding="utf-8")
    assert "image/*" in ignore
    assert "!image/image-manifest.json" in ignore


# ---------------------------------------------------------------------------
# What the report says about it
# ---------------------------------------------------------------------------


def test_the_report_publishes_the_manifest_it_was_given_or_says_there_is_none():
    """Section 2.1, both ways round.

    With a manifest the report must carry every SHA-256 — that is literally
    what the brief asks the build-provenance section to record. Without one it
    must say so and name the command, so a reader can tell "not built" from
    "built and unremarkable".
    """
    report_path = DOCS / "D8_EDGE_REPORT.md"
    if not report_path.exists():
        pytest.skip("D8_EDGE_REPORT.md has not been written yet")
    text = report_path.read_text(encoding="utf-8")
    assert "### 2.1 The flashable image set" in text
    manifest = image.load_manifest()
    if manifest is None:
        assert "no image has been built in this repository" in text
        assert image.BUILD_COMMAND in text
        return
    assert manifest.board_config in text
    assert manifest.sdk_commit in text
    for item in manifest.files:
        assert item.sha256 in text, f"{item.name} has no hash in the report"
    if not manifest.complete:
        assert "BUILD FAILED at stage" in text


# ---------------------------------------------------------------------------
# Section 2.1's PROSE, which the tests above never looked at
# ---------------------------------------------------------------------------
#
# The finding: 2.1 narrated an abandoned emulated-Mac attempt ("stopped
# deliberately", "the hours belong on the Linux mirror") unconditionally, and a
# committed manifest then rendered a COMPLETE build's tables directly beneath
# it, with nothing saying they are different runs and nothing recording which
# machine produced the hashes. These drive `generate` with a manifest of each
# kind rather than reading the committed document, so they fail today if the
# generator regresses instead of when somebody forgets to regenerate.


def _generate_with(monkeypatch, manifest):
    """The document as if `manifest` were the committed one, hard wrap removed.

    Unwrapped because these assertions are about SENTENCES: the generator wraps
    at whatever column reads well, and a needle that straddles a line break
    would turn a re-wrap into a failure and let a real regression hide behind
    one.
    """
    from skyweave2.edge import report as report_module

    monkeypatch.setattr(report_module.image, "load_manifest", lambda *a, **k: manifest)
    return " ".join(report_module.generate({}).split())


def test_the_abandoned_mac_attempt_is_told_only_while_no_image_exists(monkeypatch):
    """A status claim about this item may not outlive the status it describes."""
    text = _generate_with(monkeypatch, None)
    assert "no image has been built in this repository" in text
    assert "stopped deliberately" in text
    assert "the hours belong on the Linux mirror" in text
    # The mechanical lesson is why the script sweeps object files, and it is
    # true either way, so it stays.
    assert "Why the script sweeps object files" in text


def test_a_committed_manifest_retires_the_mac_narrative_and_says_it_is_another_run(
    monkeypatch, tmp_path
):
    manifest = image.load_manifest(_write(tmp_path, GOOD))
    text = _generate_with(monkeypatch, manifest)
    assert "stopped deliberately" not in text
    assert "the hours belong on the Linux mirror" not in text
    assert "The tooling is what this item delivers" not in text
    assert "**The build below is not that attempt.**" in text
    assert "Why the script sweeps object files" in text
    # The Attempts column means a compile or link step died — the script has no
    # emulation probe at all (D8-F13), so the caption must not claim one.
    assert "emulated toolchain crashed and the retry resumed" not in text


def test_a_manifest_with_no_build_host_says_not_measured_with_its_reason(
    monkeypatch, tmp_path
):
    """A2 makes the build machine load-bearing; the manifest records none.

    An absence is NOT-MEASURED with a reason, never a zero and never a silence
    the reader is invited to fill from the paragraph above.
    """
    from skyweave2.edge import metrics

    manifest = image.load_manifest(_write(tmp_path, GOOD))
    assert manifest is not None and manifest.build_host == {}
    text = _generate_with(monkeypatch, manifest)
    assert f"| Build host | {metrics.NOT_MEASURED} — the manifest carries no" in text
    assert "D8-F14" in text
    assert "the image manifest does not record which machine built" in text


def test_a_manifest_that_records_its_build_host_publishes_it_and_closes_f14(
    monkeypatch, tmp_path
):
    """The other direction, so the row and the finding are not decoration."""
    payload = dict(GOOD, build_host={"uname": "Linux 6.8.0 x86_64", "emulated": "no"})
    manifest = image.load_manifest(_write(tmp_path, payload))
    assert manifest is not None
    assert manifest.build_host["uname"] == "Linux 6.8.0 x86_64"
    text = _generate_with(monkeypatch, manifest)
    assert "| Build host uname | `Linux 6.8.0 x86_64` |" in text
    assert "| Build host emulated | `no` |" in text
    assert "D8-F14 — CLOSED" in text
