#!/bin/sh
# Build the flashable image set for an RV1106 node, in the pinned container.
#
#   docker build --platform linux/amd64 -t skyweave-image-build:d8.1 \
#       -f docker/Dockerfile.image docker/
#   docker run --rm --platform linux/amd64 -v "$PWD:/src" -w /src \
#       skyweave-image-build:d8.1 ./scripts/build-image.sh
#
# What it produces: u-boot, the kernel, the Buildroot rootfs and the packed
# partition images, copied into ./image/ (gitignored — they are hundreds of
# megabytes and they are derived), plus ./image/image-manifest.json, which IS
# committed. The manifest is the deliverable: defconfig, SDK commit, container
# provenance and the SHA-256 of every produced file, which is what the D8
# report's build-provenance section publishes.
#
# The daemon is deliberately NOT baked into this rootfs. The D8.1-prep
# amendment says hand-start is acceptable until D8.2, and a rootfs that
# carries a binary would have to be rebuilt every time that binary changed —
# which would make the image provenance a moving target during bring-up, when
# it is most useful for it to be still.
#
# Every stage is announced, timed and logged separately. A failure writes the
# manifest anyway, with status=FAILED and the failing stage named: an absent
# manifest and a failed build look the same from the outside, and they are not
# the same thing.

set -eu

SDK="${SKYWEAVE_SDK:-/opt/luckfox-pico}"
OUT_DIR="${IMAGE_OUT_DIR:-image}"
BUILD_USER="${IMAGE_BUILD_USER:-builder}"

# The board this project flashes. DECLARED here rather than chosen at a lunch
# menu, because the menu's numbering is a property of the SDK's day and this
# has to be the same board on the Mac, on the Linux mirror and in six months.
#
# Why this one: RV1106 with 256 MB and 100M Ethernet is the variant the node
# design's constraint table describes (RV1106_EDGE_NODE.md section 1); the
# Buildroot rootfs is what section 5 ships; SD_CARD is the bring-up medium,
# because a node you can reflash by moving a card is the right thing to have
# during D8.1 and OTA is a phase-2 item. This board config also ships the
# SC3336 IQ file, which is the sensor this project uses.
#
# The exact variant Samuel flashes is HIS call — override it here and the
# manifest records what was actually built, which is the point of recording it.
BOARD_CONFIG="${SKYWEAVE_BOARD_CONFIG:-BoardConfig-SD_CARD-Buildroot-RV1106_Luckfox_Pico_Pro_Max-IPC.mk}"

# Parallel by default, which is the opposite of build-board.sh — and the
# difference is worth stating. That script builds twelve files, so a serial
# build costs seconds and a retry is free. This one builds u-boot, a kernel and
# a Buildroot rootfs: tens of thousands of compiler invocations, each of which
# is a chance for the emulated cc1 to segfault (see the retry loop below).
# Serial would multiply an already long build by the number of cores for no
# gain in reliability, because the failures are per-process and independent.
JOBS="${BUILD_JOBS:-4}"

# Under linux/amd64 emulation on Apple Silicon the SDK's cc1 dies with
# "internal compiler error: Segmentation fault" on a random file, a different
# one each time; the same file compiles on the next attempt. It is an emulation
# artifact, not a compiler bug and not a property of this source — the D8.0
# report records the same behaviour for the daemon build.
#
# Each retry RESUMES: make keeps every object it already produced, so an
# attempt that dies half way through the kernel picks up where it stopped
# rather than starting over. That is what makes a bounded retry converge here
# instead of just burning the same minutes repeatedly.
#
# Bounded and announced, never infinite: a stage that fails ATTEMPTS times in a
# row is a real failure and must be allowed to be one.
ATTEMPTS="${BUILD_ATTEMPTS:-8}"

# The stages of `build.sh all`, minus `save` (which stamps a copy with the wall
# clock — this project does not put wall-clock values in artifacts).
STAGES="${IMAGE_STAGES:-uboot kernel rootfs media app firmware}"

say() { printf '\n=== %s\n' "$*"; }

# A retry alone is not enough, and finding out why cost a build.
#
# When the emulated toolchain dies mid-write it can leave a build output that
# EXISTS and is newer than its source. Make then considers it up to date and
# never rebuilds it, so the next attempt sails past the damage and dies at the
# LINK instead — u-boot's first run here ended with "undefined reference to
# `stdio_devices`", from a `common/stdio.o` whose compile had been interrupted
# five attempts earlier. A retry loop that does not clean up after the crash
# converges on a different error rather than on a build.
#
# So between attempts, every object/archive/module newer than the attempt's
# stamp is checked for its magic number and deleted if it is truncated or
# empty. Targeted, not a clean: only files this attempt touched, and only the
# ones that are not what they claim to be.
sanitise_build_outputs() {
    stamp="$1"
    python3 - "${SDK}" "${stamp}" <<'PYTHON'
import os
import sys

sdk, stamp = sys.argv[1], sys.argv[2]
since = os.path.getmtime(stamp)
magic = {".o": b"\x7fELF", ".ko": b"\x7fELF", ".so": b"\x7fELF", ".a": b"!<arch>"}
removed = 0
for root, _dirs, names in os.walk(os.path.join(sdk, "sysdrv")):
    for name in names:
        suffix = os.path.splitext(name)[1]
        want = magic.get(suffix)
        if want is None:
            continue
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) < since:
                continue
            with open(path, "rb") as handle:
                head = handle.read(len(want))
        except OSError:
            continue
        if head != want:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
print(f"sanitise: removed {removed} truncated build outputs")
PYTHON
}

say "build provenance"
cat /etc/skyweave-image-provenance

if [ ! -d "${SDK}/project/cfg/BoardConfig_IPC" ]; then
    echo "REFUSING: ${SDK} does not look like the Luckfox SDK. This script runs" >&2
    echo "inside skyweave-image-build:d8.1, not on a workstation." >&2
    exit 1
fi
if [ ! -f "${SDK}/project/cfg/BoardConfig_IPC/${BOARD_CONFIG}" ]; then
    echo "REFUSING: no board config ${BOARD_CONFIG} in this SDK." >&2
    echo "Available:" >&2
    ls "${SDK}/project/cfg/BoardConfig_IPC" >&2
    exit 1
fi

mkdir -p "${OUT_DIR}/logs"
: > "${OUT_DIR}/logs/stages.tsv"

# `build.sh` sources .BoardConfig.mk only when it is a SYMLINK (build.sh:2776),
# which is exactly what its own `lunch` creates. Creating it directly is the
# non-interactive path with no menu-order assumption in it.
say "selecting board ${BOARD_CONFIG}"
ln -rfs "${SDK}/project/cfg/BoardConfig_IPC/${BOARD_CONFIG}" "${SDK}/.BoardConfig.mk"
chown -h "${BUILD_USER}:${BUILD_USER}" "${SDK}/.BoardConfig.mk" 2>/dev/null || true

as_builder() {
    # Buildroot declines to run as root and several packages configure
    # differently when they find one, so the SDK build runs unprivileged. This
    # script stays root so it can write through the bind mount afterwards.
    if [ "$(id -u)" = "0" ] && id "${BUILD_USER}" >/dev/null 2>&1; then
        runuser -u "${BUILD_USER}" -- env PATH="${PATH}" RK_JOBS="${JOBS}" \
            SYSDRV_JOBS="${JOBS}" sh -c "$1"
    else
        env RK_JOBS="${JOBS}" SYSDRV_JOBS="${JOBS}" sh -c "$1"
    fi
}

status="complete"
failed_stage=""
for stage in ${STAGES}; do
    say "stage ${stage}"
    start=$(date +%s)
    stage_status="FAILED"
    attempt=0
    while [ "${attempt}" -lt "${ATTEMPTS}" ]; do
        attempt=$((attempt + 1))
        echo "--- ${stage}: attempt ${attempt} of ${ATTEMPTS} (jobs=${JOBS})"
        touch "${OUT_DIR}/logs/.attempt-stamp"
        if as_builder "cd '${SDK}' && ./build.sh ${stage}" \
                >>"${OUT_DIR}/logs/${stage}.log" 2>&1; then
            stage_status="ok"
            break
        fi
        if grep -q "internal compiler error" "${OUT_DIR}/logs/${stage}.log"; then
            echo "--- ${stage}: emulated cc1 died; retrying (make resumes)"
        else
            echo "--- ${stage}: failed without a compiler crash; retrying anyway"
        fi
        sanitise_build_outputs "${OUT_DIR}/logs/.attempt-stamp"
    done
    if [ "${stage_status}" != "ok" ]; then
        status="FAILED"
        failed_stage="${stage}"
    fi
    end=$(date +%s)
    printf '%s\t%s\t%s\t%s\n' "${stage}" "${stage_status}" "$((end - start))" \
        "${attempt}" >> "${OUT_DIR}/logs/stages.tsv"
    tail -n 20 "${OUT_DIR}/logs/${stage}.log" || true
    echo "--- stage ${stage}: ${stage_status} in $((end - start)) s"
    if [ "${stage_status}" = "FAILED" ]; then
        break
    fi
done

say "collecting images"
if [ -d "${SDK}/output/image" ]; then
    # Copy rather than move: a second run should be able to see what the first
    # one produced without the SDK tree having been emptied under it.
    cp -f "${SDK}/output/image/"* "${OUT_DIR}/" 2>/dev/null || true
fi
ls -l "${OUT_DIR}" || true

say "writing ${OUT_DIR}/image-manifest.json"
IMAGE_OUT_DIR="${OUT_DIR}" \
IMAGE_STATUS="${status}" \
IMAGE_FAILED_STAGE="${failed_stage}" \
IMAGE_BOARD_CONFIG="${BOARD_CONFIG}" \
IMAGE_SDK="${SDK}" \
python3 - <<'PYTHON'
import hashlib
import json
import os
import re
from pathlib import Path

out = Path(os.environ["IMAGE_OUT_DIR"])
sdk = Path(os.environ["IMAGE_SDK"])
board_config = os.environ["IMAGE_BOARD_CONFIG"]

# The container's own provenance file, parsed rather than retyped.
provenance = {}
prov_path = Path("/etc/skyweave-image-provenance")
if prov_path.exists():
    for line in prov_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            provenance[key.strip()] = value.strip()

# The defconfigs the board config declares. Read out of the file that drove
# the build, so the manifest cannot disagree with what was built.
declared = {}
config_path = sdk / "project" / "cfg" / "BoardConfig_IPC" / board_config
if config_path.exists():
    wanted = (
        "RK_CHIP", "RK_KERNEL_DTS", "RK_KERNEL_DEFCONFIG", "RK_UBOOT_DEFCONFIG",
        "RK_UBOOT_DEFCONFIG_FRAGMENT", "RK_BUILDROOT_DEFCONFIG", "RK_BOOT_MEDIUM",
        "LF_TARGET_ROOTFS", "RK_PARTITION_CMD_IN_ENV", "RK_PARTITION_FS_TYPE_CFG",
        "RK_TOOLCHAIN_CROSS", "RK_CAMERA_SENSOR_IQFILES", "RK_BOOTARGS_CMA_SIZE",
    )
    for line in config_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^export\s+([A-Z_0-9]+)=(.*)$", line.strip())
        if match and match.group(1) in wanted:
            declared[match.group(1)] = match.group(2).strip().strip('"')

stages = []
stage_file = out / "logs" / "stages.tsv"
if stage_file.exists():
    for line in stage_file.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            stages.append(
                {
                    "stage": parts[0],
                    "status": parts[1],
                    "seconds": int(parts[2]),
                    # >1 means the emulated toolchain crashed and the retry
                    # resumed. Recorded because it is the difference between
                    # "this build is reproducible" and "this build is
                    # reproducible on a machine that is not emulating x86".
                    "attempts": int(parts[3]),
                }
            )

files = []
for path in sorted(out.iterdir()):
    if not path.is_file() or path.name == "image-manifest.json":
        continue
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    files.append(
        {"name": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    )

status = os.environ["IMAGE_STATUS"]
if status == "complete" and not files:
    # Every stage passed and nothing came out. That is not a complete build,
    # and a manifest that said so would be the exact lie this file exists to
    # prevent.
    status = "FAILED"
    failed = "collect (no image files were produced)"
else:
    failed = os.environ.get("IMAGE_FAILED_STAGE", "")

manifest = {
    "schema": "d8-image/1",
    "status": status,
    "failed_stage": failed,
    "container_tag": "skyweave-image-build:d8.1",
    "board_config": board_config,
    "declared": declared,
    "provenance": provenance,
    "stages": stages,
    "files": files,
    "daemon_baked_in": False,
}
(out / "image-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print(json.dumps({"status": status, "files": len(files), "failed_stage": failed},
                 indent=2))
PYTHON

if [ "${status}" != "complete" ]; then
    echo "IMAGE BUILD FAILED at stage '${failed_stage}'. The manifest records it." >&2
    exit 1
fi

say "done"
