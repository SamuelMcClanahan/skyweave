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
import os
import re
import subprocess
import sys

import pytest

from skyweave2.edge import image

FIRMWARE = image.FIRMWARE_ROOT
DOCS = image.V2_ROOT / "docs"
BUILD_SCRIPT = image.BUILD_SCRIPT_PATH

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
# The three findings the script answers about ITSELF (D8-F12, D8-F13, D8-F14)
# ---------------------------------------------------------------------------
#
# A build takes an hour and needs docker, so none of these run one. What they
# run is the parts of the script that carry the fixes: the manifest block, and
# the host probe, both LIFTED OUT OF THE COMMITTED FILE rather than copied into
# this test. A copy would go on passing after the script changed underneath it,
# which is the failure mode all three findings share.


def _embedded_python(name: str) -> str:
    """The manifest block, as the committed script would run it.

    The script has two `<<'PYTHON'` heredocs — the truncated-output sweep and
    the manifest writer — so they are told apart by what is in them, not by
    which comes first.
    """
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    blocks = []
    for chunk in text.split("<<'PYTHON'\n")[1:]:
        blocks.append(chunk.split("\nPYTHON\n")[0])
    matching = [block for block in blocks if name in block]
    assert len(matching) == 1, f"expected one python block containing {name!r}"
    return matching[0]


def _run_manifest_block(tmp_path, shell_status="complete", failed_stage=""):
    out = tmp_path / "out"
    (out / "logs").mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-c", _embedded_python("image-manifest.json")],
        env={
            # Inherited rather than minimal: this runs the interpreter the
            # suite is already running under, on whichever machine the gate is.
            **os.environ,
            "IMAGE_OUT_DIR": str(out),
            "IMAGE_SDK": str(tmp_path / "no-sdk"),
            "IMAGE_BOARD_CONFIG": "BoardConfig-SD_CARD-Buildroot-RV1106.mk",
            "IMAGE_STATUS": shell_status,
            "IMAGE_FAILED_STAGE": failed_stage,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    manifest = json.loads((out / "image-manifest.json").read_text(encoding="utf-8"))
    return result, out, manifest


def test_the_manifest_block_exits_with_the_verdict_it_wrote(tmp_path):
    """D8-F12: every stage passed, the collect produced nothing.

    The block already reached the right judgement and wrote it down; the bug
    was that the judgement stayed inside python while the shell went on
    believing the build had completed and exited 0. An operator's only clue
    was a manifest nobody had to read.
    """
    result, _out, manifest = _run_manifest_block(tmp_path)
    assert manifest["status"] == "FAILED"
    assert manifest["failed_stage"] == "collect (no image files were produced)"
    assert result.returncode == 1, "a FAILED manifest must not leave a zero exit"


def test_the_manifest_block_exits_zero_when_files_came_out(tmp_path):
    """The other direction, so the exit code is a verdict and not a mood."""
    out = tmp_path / "out"
    (out / "logs").mkdir(parents=True)
    (out / "boot.img").write_bytes(b"not really an image")
    result, _out, manifest = _run_manifest_block(tmp_path)
    assert manifest["status"] == "complete"
    assert [item["name"] for item in manifest["files"]] == ["boot.img"]
    assert result.returncode == 0


def test_the_manifest_carries_the_build_host_the_probe_recorded(tmp_path):
    """D8-F14: the probe's answer reaches the artifact, parsed not retyped."""
    out = tmp_path / "out"
    (out / "logs").mkdir(parents=True)
    (out / "boot.img").write_bytes(b"not really an image")
    (out / "logs" / "build-host.tsv").write_text(
        "uname\tLinux 6.8.0-100-generic x86_64\nemulated\tno\nprobe\tstubbed\n",
        encoding="utf-8",
    )
    _result, _out, manifest = _run_manifest_block(tmp_path)
    assert manifest["build_host"] == {
        "uname": "Linux 6.8.0-100-generic x86_64",
        "emulated": "no",
        "probe": "stubbed",
    }
    # And the reader hands it to the report rather than dropping it.
    parsed = image.load_manifest(out / "image-manifest.json")
    assert parsed is not None and parsed.build_host["emulated"] == "no"


def _stub_sdk(tmp_path):
    """Enough SDK for the script to accept, and a `build.sh` that does nothing.

    The point is the script's own control flow — the stages loop, the collect,
    the manifest block and the exit code — with a build that cannot fail and
    cannot produce anything unless the test puts it there.
    """
    sdk = tmp_path / "sdk"
    configs = sdk / "project" / "cfg" / "BoardConfig_IPC"
    configs.mkdir(parents=True, exist_ok=True)
    board = "BoardConfig-SD_CARD-Buildroot-RV1106_Luckfox_Pico_Pro_Max-IPC.mk"
    (configs / board).write_text(
        'export RK_CHIP=rv1106\nexport RK_BOOT_MEDIUM="sd_card"\n', encoding="utf-8"
    )
    build = sdk / "build.sh"
    build.write_text("#!/bin/sh\necho stub build.sh \"$@\"\nexit 0\n", encoding="utf-8")
    build.chmod(0o755)
    provenance = tmp_path / "provenance"
    provenance.write_text("base=stub\nluckfox_commit=stub\n", encoding="utf-8")
    return sdk, provenance


def _run_build_script(tmp_path, out_dir):
    sdk, provenance = _stub_sdk(tmp_path)
    return subprocess.run(
        ["sh", str(BUILD_SCRIPT)],
        cwd=tmp_path,
        env={
            **os.environ,
            "SKYWEAVE_SDK": str(sdk),
            "IMAGE_OUT_DIR": str(out_dir),
            "IMAGE_PROVENANCE": str(provenance),
            "IMAGE_STAGES": "uboot",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_script_itself_exits_with_its_manifests_verdict(tmp_path):
    """D8-F12 end to end, which no amount of reading the file can establish.

    An adversarial review of the marker table deleted the `exit 1` from this
    script's else branch and every text check still called the fix present.
    This runs the committed script: stages all pass, the collect produces
    nothing, and the exit code has to be the manifest's own verdict.

    Skipped where the script cannot run at all — it is a container script and
    `ln -r` is GNU-only, so a macOS run has nothing to say. That is the W6
    split: the Linux gate is authoritative, this host is advisory.
    """
    probe = subprocess.run(
        ["sh", "-c", f"cd {tmp_path} && touch a && ln -rfs a b"],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        pytest.skip("no GNU `ln -r` on this host; the gate platform is Linux")

    out = tmp_path / "out"
    failed = _run_build_script(tmp_path, out)
    manifest = json.loads((out / "image-manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "FAILED"
    assert manifest["failed_stage"] == "collect (no image files were produced)"
    assert failed.returncode == 1, (
        "the script exited 0 on a build its own manifest calls FAILED "
        f"(stdout tail: {failed.stdout[-400:]})"
    )
    # The probe ran, announced itself, and its answer reached the manifest.
    assert "emulated:" in failed.stdout
    assert set(manifest["build_host"]) == {"uname", "emulated", "probe"}
    assert manifest["build_host"]["emulated"] in {"yes", "no", "unknown"}

    # And the other direction: something was collected, so it completed.
    (out / "boot.img").write_bytes(b"not really an image")
    ok = _run_build_script(tmp_path, out)
    manifest = json.loads((out / "image-manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert ok.returncode == 0, ok.stdout[-400:]


ARM_CPUINFO = "processor\t: 0\nFeatures\t: fp asimd\nCPU implementer\t: 0x61\n"
X86_CPUINFO = "processor\t: 0\nvendor_id\t: GenuineIntel\nflags\t\t: fpu vme de\n"


def _run_host_probe(tmp_path, machine, cpuinfo):
    """The committed `detect_build_host`, over a `/proc/cpuinfo` we choose.

    The whole point of the probe is to tell a native x86 build from an emulated
    one, and this project owns exactly one of those machines. So the function is
    lifted out of the script and given each case's `uname` and cpuinfo — the
    script reads `PROC_CPUINFO` for no other reason.
    """
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    start = text.index("detect_build_host() {")
    body = text[start : text.index("\n}\n", start) + 3]
    (tmp_path / "cpuinfo").write_text(cpuinfo, encoding="utf-8")
    harness = (
        "set -eu\n"
        f'OUT_DIR="{tmp_path}"\n'
        f'PROC_CPUINFO="{tmp_path}/cpuinfo"\n'
        "uname() {\n"
        '  case "$1" in\n'
        f"    -m) echo {machine} ;;\n"
        "    -s) echo Linux ;;\n"
        "    -r) echo 6.8.0-test ;;\n"
        "  esac\n"
        "}\n"
        f"{body}\n"
        "detect_build_host\n"
    )
    subprocess.run(["sh", "-c", harness], check=True, capture_output=True, text=True)
    recorded = {}
    for line in (tmp_path / "logs" / "build-host.tsv").read_text().splitlines():
        key, value = line.split("\t", 1)
        recorded[key] = value
    return recorded


@pytest.mark.parametrize(
    ("machine", "cpuinfo", "emulated"),
    [
        # The Linux mirror A2 asks for: x86 machine, x86 host CPU.
        ("x86_64", X86_CPUINFO, "no"),
        # The emulated Mac: qemu reports the TARGET, so `uname -m` says x86_64
        # while the kernel's cpuinfo still describes the ARM host it runs on.
        # Measured on this project's Mac; the fixture is that measurement.
        ("x86_64", ARM_CPUINFO, "yes"),
        ("aarch64", ARM_CPUINFO, "no"),
        # A machine or a cpuinfo this probe does not recognise gets no verdict.
        # An "unknown" that reads as "native" is how a wrong build host gets
        # published as a right one.
        ("x86_64", "processor\t: 0\n", "unknown"),
        ("riscv64", ARM_CPUINFO, "unknown"),
    ],
)
def test_the_host_probe_tells_emulated_from_native(
    tmp_path, machine, cpuinfo, emulated
):
    """D8-F13/D8-F14: the check the container's comment has always claimed."""
    recorded = _run_host_probe(tmp_path, machine, cpuinfo)
    assert recorded["emulated"] == emulated
    assert recorded["uname"] == f"Linux 6.8.0-test {machine}"
    # The verdict travels with what it was derived from: a bare yes/no is a
    # claim, and this row has to survive being disagreed with.
    assert machine in recorded["probe"]


def test_the_committed_script_carries_all_three_fixes():
    """The report DERIVES those findings' status from these markers.

    If a fix is reverted the marker goes with it, section 9 reopens the
    finding, and this fails — which is the point: the alternative is a report
    sentence that keeps saying "fixed" because somebody typed it once.
    """
    assert image.build_script_fixes() == {
        "f12_exit_follows_manifest": True,
        "f13_ice_grep_scoped": True,
        "f13_f14_emulation_probe": True,
    }


def test_the_fix_reader_reports_absence_rather_than_assuming_it(tmp_path):
    """Both directions, and an absent script closes nothing."""
    stub = tmp_path / "build-image.sh"
    stub.write_text("#!/bin/sh\necho nothing to see here\n", encoding="utf-8")
    assert set(image.build_script_fixes(stub).values()) == {False}
    assert set(image.build_script_fixes(tmp_path / "gone.sh").values()) == {False}


def test_the_ice_diagnosis_reads_only_the_current_attempt():
    """D8-F13, the half that is about the retry loop.

    The stage log is appended across attempts, so a whole-file grep hands the
    operator a diagnosis from an earlier crash — on a native host, one that
    never happened at all.
    """
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'grep -q "internal compiler error" "${OUT_DIR}/logs/${stage}.log"' not in text
    assert "log_bytes_before" in text
    assert 'tail -c "+$((log_bytes_before + 1))"' in text


# ---------------------------------------------------------------------------
# Where the partitions land, and what Phase B actually writes (D8-F15)
# ---------------------------------------------------------------------------


def test_the_layout_reads_offsets_sizes_and_the_growup_partition(tmp_path):
    """The cmdline's own grammar: `SIZE[@OFFSET](name)`, and `-` grows.

    The grow-up entry is the one that matters. It is why the SDK's update
    scripts skip rootfs — they have no size to write — and it is where the
    rootfs sits on the card, so a parser that quietly dropped it would make
    the finding's arithmetic stop at userdata and still look tidy.
    """
    payload = dict(
        GOOD,
        declared={"RK_PARTITION_CMD_IN_ENV": "32K(env),512K@32K(idblock),32M(boot),-(rootfs)"},
        files=[
            {"name": "env.img", "bytes": 32768, "sha256": "ab" * 32},
            {"name": "boot.img", "bytes": 1024, "sha256": "cd" * 32},
        ],
    )
    manifest = image.load_manifest(_write(tmp_path, payload))
    assert manifest is not None
    layout = image.medium_layout(manifest)
    assert [(part.name, part.offset, part.size) for part in layout] == [
        ("env", 0, 32768),
        ("idblock", 32768, 524288),
        ("boot", 557056, 33554432),
        ("rootfs", 34111488, None),
    ]
    # An image the build did not produce reads as zero bytes, not as a crash:
    # a FAILED build's manifest has partitions and no images.
    assert [part.image_bytes for part in layout] == [32768, 0, 1024, 0]
    assert layout[2].reserved_bytes == 33554432 - 1024


def test_the_committed_layout_accounts_for_the_medium_and_carries_the_rootfs():
    """D8-F15: the claim Phase B rests on, checked against the bytes.

    `sd_update.img` is the whole card, not a loader: every declared partition
    at its declared offset, rootfs included. The accounting has to close — if
    it does not, either the manifest or the packer is describing a different
    medium than the other one, which is exactly the case that puts a node on
    the bench running the wrong root filesystem.
    """
    manifest = image.load_manifest()
    if manifest is None:
        pytest.skip("no image has been built in this repository")
    layout = image.medium_layout(manifest)
    assert [part.name for part in layout][-1] == "rootfs"
    if manifest.declared.get("RK_BOOT_MEDIUM") != "sd_card":
        # The medium is only a single packed file on the SD path. If D8-F15 is
        # answered by moving to the SPI_NAND config, the layout above still has
        # to parse and still has to end in a rootfs, and the rest of this test
        # is about a file that build does not produce.
        return
    medium = next((f for f in manifest.files if f.name == "sd_update.img"), None)
    assert medium is not None, "an SD build with no sd_update.img is not an SD build"
    content = sum(part.image_bytes for part in layout)
    reserved = sum(part.reserved_bytes for part in layout)
    rootfs = layout[-1]
    padding = medium.bytes - (rootfs.offset + rootfs.image_bytes)
    assert content + reserved + padding == medium.bytes
    assert 0 <= padding < 1024 * 1024, "the packer trims; this is alignment, not budget"
    # A negative reserve is an image written over the partition after it, and
    # the identity above closes anyway because the sign cancels. Checked here
    # because this is the test that runs on the gate, where the bytes are not.
    assert all(part.reserved_bytes >= 0 for part in layout), (
        "an image is larger than the partition declared for it: "
        f"{[(p.name, p.reserved_bytes) for p in layout if p.reserved_bytes < 0]}"
    )

    # And when the gigabytes are on this machine, the derived offset is checked
    # against them. Absent is normal — the images are gitignored — but present
    # and disagreeing is the failure this test exists for.
    on_disk = image.IMAGE_DIR / "sd_update.img"
    rootfs_img = image.IMAGE_DIR / "rootfs.img"
    if on_disk.exists() and rootfs_img.exists():
        import hashlib

        digest = hashlib.sha256()
        with on_disk.open("rb") as handle:
            handle.seek(rootfs.offset)
            remaining = rootfs.image_bytes
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                assert chunk, "the medium ends before the rootfs partition does"
                digest.update(chunk)
                remaining -= len(chunk)
        expected = next(f.sha256 for f in manifest.files if f.name == "rootfs.img")
        assert digest.hexdigest() == expected


def test_the_medium_is_exactly_its_partitions_and_nothing_else():
    """The packed card, recomputed from the parts, hashes to the packed card.

    Two things fall out of this and both matter at the bench. C0c writes
    partitions one at a time from the per-partition images; this says those
    images ARE what the card holds at those offsets, so a per-partition write
    and a whole-card write land the same bytes. And it says the medium carries
    nothing else — no filesystem of its own, no metadata, no packer padding
    beyond alignment — so there is no fat to trim out of the 488 MiB (D8-F15).

    Streamed, never materialised: this reads the images and hashes, and writes
    nothing.
    """
    import hashlib

    manifest = image.load_manifest()
    if manifest is None or manifest.declared.get("RK_BOOT_MEDIUM") != "sd_card":
        pytest.skip("no SD-card image build is committed here")
    medium = next((f for f in manifest.files if f.name == "sd_update.img"), None)
    if medium is None or not (image.IMAGE_DIR / "sd_update.img").exists():
        pytest.skip("the images are gitignored and this machine does not have them")

    layout = image.medium_layout(manifest)
    if any(not (image.IMAGE_DIR / part.image).exists() for part in layout):
        pytest.skip("the images are gitignored and this machine does not have them")

    digest = hashlib.sha256()
    cursor = 0
    zeros = bytes(1024 * 1024)
    for part in layout:
        while cursor < part.offset:
            step = min(len(zeros), part.offset - cursor)
            digest.update(zeros[:step])
            cursor += step
        with (image.IMAGE_DIR / part.image).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                cursor += len(chunk)
    while cursor < medium.bytes:
        step = min(len(zeros), medium.bytes - cursor)
        digest.update(zeros[:step])
        cursor += step
    assert digest.hexdigest() == medium.sha256


def test_the_committed_medium_is_inside_the_declared_size_budget():
    """The budget is a gate, not an aspiration (Samuel, 2026-08-14).

    A number in a document is not a constraint; a number the suite enforces is.
    The medium is what goes on the card, so this is the one that has to hold —
    and it has to hold for a manifest alone, because the gate platform does not
    carry the gigabytes.
    """
    manifest = image.load_manifest()
    if manifest is None:
        pytest.skip("no image has been built in this repository")
    medium = next((f for f in manifest.files if f.name == image.MEDIUM_NAME), None)
    if medium is None:
        pytest.skip("this build produced no packed medium")
    assert medium.bytes <= image.MEDIUM_MAX_BYTES, (
        f"{image.MEDIUM_NAME} is {medium.bytes} B, over the declared "
        f"{image.MEDIUM_MAX_BYTES} B budget"
    )


def test_the_packer_writes_the_layout_and_pads_to_a_whole_mib(tmp_path):
    """`pack_medium`, on images small enough to read in a test."""
    payload = dict(
        GOOD,
        declared={
            "RK_BOOT_MEDIUM": "sd_card",
            "RK_PARTITION_CMD_IN_ENV": "1K(env),4K@1K(boot),-(rootfs)",
        },
        files=[{"name": "env.img", "bytes": 1, "sha256": "ab" * 32}],
    )
    manifest = image.load_manifest(_write(tmp_path, payload))
    (tmp_path / "env.img").write_bytes(b"E" * 1024)
    (tmp_path / "boot.img").write_bytes(b"B" * 2048)
    (tmp_path / "rootfs.img").write_bytes(b"R" * 4096)
    written = image.pack_medium(manifest, tmp_path, tmp_path / "sd_update.img")

    assert written == image.MEDIUM_ALIGN_BYTES, "content is under 1 MiB, so one MiB"
    blob = (tmp_path / "sd_update.img").read_bytes()
    assert blob[:1024] == b"E" * 1024
    assert blob[1024:3072] == b"B" * 2048
    # The gap between boot's image and the rootfs offset is zeros, and so is
    # everything after the last byte of content.
    assert blob[3072:5120] == bytes(2048)
    assert blob[5120:9216] == b"R" * 4096
    assert not blob[9216:].strip(b"\x00")
    # No leftovers: a partial write must not survive as a file.
    assert not list(tmp_path.glob("*.partial"))


def test_the_packer_refuses_a_manifest_with_no_layout_and_a_dir_with_no_images(
    tmp_path,
):
    no_layout = image.load_manifest(_write(tmp_path, dict(GOOD, declared={})))
    with pytest.raises(image.ImageManifestError, match="no partition cmdline"):
        image.pack_medium(no_layout, tmp_path, tmp_path / "out.img")

    payload = dict(GOOD, declared={"RK_PARTITION_CMD_IN_ENV": "1K(env),-(rootfs)"})
    manifest = image.load_manifest(_write(tmp_path, payload))
    with pytest.raises(image.ImageManifestError, match="would be zeros"):
        image.pack_medium(manifest, tmp_path, tmp_path / "out.img")


def test_the_packer_refuses_an_image_larger_than_its_partition(tmp_path):
    """The packing error that matters, and the one nothing else catches.

    An image bigger than its declared partition is written over the partition
    after it — on this medium, a rootfs over userdata. The layout arithmetic
    does not notice: the reserve goes negative and the accounting identity
    still closes, because the sign cancels.
    """
    payload = dict(
        GOOD,
        declared={"RK_PARTITION_CMD_IN_ENV": "1K(env),4K@1K(boot),-(rootfs)"},
        files=[
            {"name": "env.img", "bytes": 1024, "sha256": "ab" * 32},
            {"name": "boot.img", "bytes": 8192, "sha256": "cd" * 32},
        ],
    )
    manifest = image.load_manifest(_write(tmp_path, payload))
    (tmp_path / "env.img").write_bytes(b"E" * 1024)
    (tmp_path / "boot.img").write_bytes(b"B" * 8192)  # 8K image, 4K partition
    (tmp_path / "rootfs.img").write_bytes(b"R" * 16)
    with pytest.raises(image.ImageManifestError, match="write over the next"):
        image.pack_medium(manifest, tmp_path, tmp_path / "sd_update.img")
    assert not (tmp_path / "sd_update.img").exists()

    layout = image.medium_layout(manifest)
    assert layout[1].reserved_bytes == -4096, "the reserve goes negative, silently"


def test_the_report_shouts_about_an_overfull_partition(monkeypatch, tmp_path):
    """And the report is where a person would see it."""
    payload = dict(
        GOOD,
        declared={
            "RK_BOOT_MEDIUM": "sd_card",
            "RK_PARTITION_CMD_IN_ENV": "32K(env),32M(boot),-(rootfs)",
        },
        files=[
            {"name": "boot.img", "bytes": 40 * 1024 * 1024, "sha256": "ab" * 32},
            {"name": "rootfs.img", "bytes": 1024, "sha256": "cd" * 32},
        ],
    )
    manifest = image.load_manifest(_write(tmp_path, payload))
    text = _generate_with(monkeypatch, manifest)
    assert "**OVERFULL PARTITIONS.**" in text
    assert f"over by {40 * 1024 * 1024 - 32 * 1024 * 1024} B" in text


def test_the_accounting_is_checked_before_it_is_claimed(monkeypatch, tmp_path):
    """D-8: "accounts to the byte" is an assertion about four numbers.

    It holds when the declared offsets are contiguous with the cumulative
    sizes. A layout with an explicit `@offset` that leaves a gap breaks it, and
    the sentence must not be printed over numbers that do not add up.
    """
    payload = dict(
        GOOD,
        declared={
            "RK_BOOT_MEDIUM": "sd_card",
            "RK_PARTITION_CMD_IN_ENV": "1K(env),1K@8K(boot),-(rootfs)",
        },
        files=[
            {"name": "env.img", "bytes": 1024, "sha256": "ab" * 32},
            {"name": "boot.img", "bytes": 512, "sha256": "cd" * 32},
            {"name": "rootfs.img", "bytes": 1024, "sha256": "ef" * 32},
            {"name": "sd_update.img", "bytes": 1024 * 1024, "sha256": "12" * 32},
        ],
    )
    manifest = image.load_manifest(_write(tmp_path, payload))
    text = _generate_with(monkeypatch, manifest)
    assert "That accounts for `sd_update.img` to the byte" not in text
    assert "do NOT account for `sd_update.img`" in text


def test_rehashing_keeps_the_build_record_and_refuses_an_over_budget_medium(tmp_path):
    """The manifest's two halves: what the bytes are, and where they came from.

    Re-hashing knows the first and must not touch the second — the defconfigs,
    the container provenance, the stage table and the build host describe a
    build that happened on another machine, and no amount of local repacking
    makes this one their author.
    """
    payload = dict(
        GOOD,
        provenance={"luckfox_commit": "824b817", "gcc": "8.3.0"},
        stages=[{"stage": "uboot", "status": "ok", "seconds": 12, "attempts": 3}],
        build_host={"uname": "Linux x86_64", "emulated": "no"},
    )
    path = _write(tmp_path, payload)
    (tmp_path / "sd_update.img").write_bytes(b"m" * 32)
    (tmp_path / "rootfs.img").write_bytes(b"r" * 16)

    step = {"step": "shrink-rootfs", "file": "rootfs.img", "tool": "resize2fs"}
    updated = image.rehash_manifest(path, tmp_path, [step], max_bytes=1024)
    assert updated["provenance"]["luckfox_commit"] == "824b817"
    assert updated["stages"][0]["attempts"] == 3
    assert updated["build_host"]["emulated"] == "no"
    assert updated["post_build"] == [step]
    names = sorted(item["name"] for item in updated["files"])
    assert names == ["rootfs.img", "sd_update.img"]

    # Re-running over the same log records the same history once, not twice.
    again = image.rehash_manifest(path, tmp_path, [step], max_bytes=1024)
    assert again["post_build"] == [step]

    # And an over-budget medium is refused with the manifest left alone.
    before = path.read_text(encoding="utf-8")
    with pytest.raises(image.ImageManifestError, match="over the declared"):
        image.rehash_manifest(path, tmp_path, [step], max_bytes=8)
    assert path.read_text(encoding="utf-8") == before


def test_the_post_build_log_is_what_the_manifest_publishes(tmp_path):
    log = tmp_path / "post-build.jsonl"
    log.write_text(
        '{"step": "shrink-rootfs", "file": "rootfs.img"}\n'
        "\n"
        '{"step": "pack-medium", "file": "sd_update.img"}\n',
        encoding="utf-8",
    )
    assert [entry["step"] for entry in image.read_post_build_log(log)] == [
        "shrink-rootfs",
        "pack-medium",
    ]
    assert image.read_post_build_log(tmp_path / "absent.jsonl") == []


def test_the_shrink_script_refuses_to_lose_files():
    """The invariant that makes an unattended shrink safe to publish.

    A resize relocates blocks; it must not change what is in the tree. The
    script compares inodes-in-use before and after and refuses on any
    difference, which is the check that stands between a smaller image and a
    quietly emptier one.
    """
    script = FIRMWARE / "scripts" / "shrink-rootfs.sh"
    assert script.exists() and script.stat().st_mode & 0o111
    text = script.read_text(encoding="utf-8")
    assert "REFUSING: inodes in use went" in text
    assert "REFUSING: blocks in use ROSE during a shrink." in text
    # e2fsck on both sides of the resize, or the refusals have nothing to say.
    assert text.count("e2fsck -fy") >= 2
    assert "post-build.jsonl" in text


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


def test_the_findings_about_the_script_follow_the_script(monkeypatch, tmp_path):
    """D8-F12 and D8-F13 close when the script carries the fix, not before.

    Both directions, driven through `generate`, because the failure being
    guarded against is a report that goes on describing a defect somebody
    fixed — or announces a fix nothing in the repository can confirm.
    """
    from skyweave2.edge import report as report_module

    manifest = image.load_manifest(_write(tmp_path, GOOD))
    monkeypatch.setattr(
        report_module.image, "build_script_fixes",
        lambda *a, **k: dict.fromkeys(image.BUILD_SCRIPT_FIXES, False),
    )
    open_text = _generate_with(monkeypatch, manifest)
    assert "D8-F12 — the image build exits 0" in open_text
    assert "D8-F12 — CLOSED" not in open_text
    assert "D8-F13 — the image container advertises an emulation check" in open_text
    assert "no `uname`, no `arch`, no qemu or binfmt probe" in open_text

    monkeypatch.setattr(
        report_module.image, "build_script_fixes",
        lambda *a, **k: dict.fromkeys(image.BUILD_SCRIPT_FIXES, True),
    )
    closed_text = _generate_with(monkeypatch, manifest)
    assert "D8-F12 — CLOSED" in closed_text
    assert "D8-F13 — CLOSED" in closed_text
    assert 'sys.exit(0 if status == "complete" else 1)' in closed_text
    # And 2.1's caption stops blaming a grep that has been scoped.
    assert "the script cannot tell that case from any other" not in closed_text


def test_f14_distinguishes_no_probe_from_a_manifest_older_than_the_probe(
    monkeypatch, tmp_path
):
    """Three states, not two.

    A script with a probe and a manifest without a host block is neither
    "unfixed" nor "closed": it is a record written before the fix existed, and
    re-issuing it on this machine to fill the gap would name a host that did
    not build these bytes — the finding, inverted.
    """
    from skyweave2.edge import report as report_module

    manifest = image.load_manifest(_write(tmp_path, GOOD))
    assert manifest is not None and manifest.build_host == {}
    monkeypatch.setattr(
        report_module.image, "build_script_fixes",
        lambda *a, **k: dict.fromkeys(image.BUILD_SCRIPT_FIXES, True),
    )
    text = _generate_with(monkeypatch, manifest)
    assert "D8-F14 — CLOSED" not in text
    assert "Fixed in the script, not yet in an artifact." in text
    assert "Re-hashing a file table is not building an image." in text
    assert "no build has run since the probe landed" in text
    assert "it was written before `build-image.sh` grew the probe" in text


def test_section_2_1_says_where_the_rootfs_lands_and_2_1_accounts_for_the_card(
    monkeypatch, tmp_path
):
    """D8-F15: Phase B writes one file, and the report says what is in it."""
    payload = dict(
        GOOD,
        declared={
            "RK_BOOT_MEDIUM": "sd_card",
            "RK_PARTITION_CMD_IN_ENV":
                "32K(env),512K@32K(idblock),256K(uboot),32M(boot),256M(userdata),-(rootfs)",
        },
        files=[
            {"name": "rootfs.img", "bytes": 1024, "sha256": "ab" * 32},
            {"name": "sd_update.img", "bytes": 302809088 + 1024, "sha256": "cd" * 32},
        ],
    )
    nand = dict(payload, declared=dict(payload["declared"], RK_BOOT_MEDIUM="spi_nand"))
    # Both parsed BEFORE the first generate: `_generate_with` monkeypatches
    # `load_manifest` itself, so a second call to it would hand back the first
    # manifest and this test would pass without testing anything.
    manifest = image.load_manifest(_write(tmp_path, payload))
    nand_manifest = image.load_manifest(_write(tmp_path, nand))

    text = _generate_with(monkeypatch, manifest)
    assert "| rootfs | 302809088 | grows to end | `rootfs.img` | 1024 | - |" in text
    assert "D8-F15" in text
    # The NUMBERS, not the sentence containing them: a marker-string assertion
    # would pass over any four figures the generator felt like printing.
    accounting = re.search(
        r"That accounts for `sd_update\.img` to the byte: (\d+) B of image "
        r"content, (\d+) B reserved inside declared partitions that their "
        r"images do not fill, (\d+) B of zeros after the last one, (\d+) B in "
        r"total",
        text,
    )
    assert accounting, "2.1 does not publish the accounting it claims to"
    content, reserved, padding, total = (int(g) for g in accounting.groups())
    assert content + reserved + padding == total
    # The manifest declares a medium that ends exactly at the rootfs image, so
    # the figures have to say so: no trailing zeros, and the rootfs offset the
    # declared cmdline implies.
    assert total == 302809088 + 1024
    assert padding == 0
    # The finding has to name the trap, not just the reassurance: the SDK's
    # update scripts skip the grow-up partition, so rootfs is absent from them
    # by construction and a network update through them is a partial one.
    assert "is growup partiton, ignore!!!" in text
    assert "byte-identical to `rootfs.img`" in text
    assert "remove the card" in text

    # And if D8-F15 is answered by moving off the SD path, the table survives
    # and the sentence about a card does not: the layout is what the cmdline
    # says, "the card is the medium" is what the boot medium says, and only one
    # of those two is true on a NAND build.
    nand_text = _generate_with(monkeypatch, nand_manifest)
    assert "| rootfs | 302809088 |" in nand_text
    assert "one raw image of the whole card" not in nand_text
    assert "That accounts for `sd_update.img` to the byte" not in nand_text


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
