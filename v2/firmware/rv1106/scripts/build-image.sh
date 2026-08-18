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

# The file the emulation probe reads (see detect_build_host). Overridable for
# one reason: it is the only way to test the classification against a machine
# this project does not own. The suite runs the real function over fixture
# cpuinfos, including the native-x86 case the mirror produces and nothing here
# can produce.
PROC_CPUINFO="${PROC_CPUINFO:-/proc/cpuinfo}"

# The container's provenance file, overridable for the same reason: the suite
# runs THIS script end to end against a stub SDK and asserts the exit code it
# gives a FAILED manifest (D8-F12). That test is the only thing standing
# between the guard at the bottom of this file and a silent regression, and it
# cannot run if the script insists on a path only the container has.
PROVENANCE_FILE="${IMAGE_PROVENANCE:-/etc/skyweave-image-provenance}"

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

# WHICH machine this build ran on, and whether that machine was emulating
# another one. Announced out loud and recorded in the manifest.
#
# Runbook A2 makes the answer load-bearing — "build on the Linux mirror
# (native x86 Linux; the emulated-Mac path is what crashed)" — and nothing
# else in the record can tell the two cases apart: `provenance` is the
# CONTAINER's, byte-identical native or emulated, and `uname` alone is no help
# because qemu reports the TARGET machine, so an amd64 container on Apple
# Silicon says `x86_64` exactly like the mirror does (D8-F13, D8-F14).
#
# What does not lie is /proc/cpuinfo: the kernel writes it and it describes the
# HOST CPU, which qemu-user does not fake. Measured on this project's Apple
# Silicon Mac, two containers from the same pinned base image:
#
#   native arm64   uname -m=aarch64  cpuinfo: `CPU implementer`, no `flags`
#   emulated amd64 uname -m=x86_64   cpuinfo: `CPU implementer`, no `flags`
#
# So the disagreement between the reported machine and the host CPU's family
# IS the emulation, and a native x86 build disagrees with nothing. Unknown
# stays unknown: a probe that cannot tell must not answer.
detect_build_host() {
    machine="$(uname -m)"
    kernel="$(uname -s) $(uname -r)"

    if grep -q '^flags[[:space:]]*:' "${PROC_CPUINFO}" 2>/dev/null ||
        grep -q '^vendor_id' "${PROC_CPUINFO}" 2>/dev/null; then
        host_cpu="x86"
    elif grep -q '^CPU implementer' "${PROC_CPUINFO}" 2>/dev/null; then
        host_cpu="arm"
    else
        host_cpu="unknown"
    fi

    case "${machine}" in
        x86_64 | i?86) machine_cpu="x86" ;;
        aarch64 | arm*) machine_cpu="arm" ;;
        *) machine_cpu="unknown" ;;
    esac

    if [ "${machine_cpu}" = "unknown" ] || [ "${host_cpu}" = "unknown" ]; then
        emulated="unknown"
    elif [ "${machine_cpu}" = "${host_cpu}" ]; then
        emulated="no"
    else
        emulated="yes"
    fi

    # Written as a file for the manifest block to parse, the same way the
    # stage table and the container's provenance are: recorded once, read
    # back, never retyped into two places that can disagree.
    mkdir -p "${OUT_DIR}/logs"
    {
        printf 'uname\t%s %s\n' "${kernel}" "${machine}"
        printf 'emulated\t%s\n' "${emulated}"
        printf 'probe\tuname -m=%s, /proc/cpuinfo describes %s\n' \
            "${machine}" "${host_cpu}"
    } > "${OUT_DIR}/logs/build-host.tsv"

    echo "build host: ${kernel} ${machine}"
    echo "emulated: ${emulated} (uname -m=${machine}, /proc/cpuinfo describes ${host_cpu})"
    if [ "${emulated}" = "yes" ]; then
        echo "This build is EMULATED. Expect the SDK's cc1 to segfault on random"
        echo "files and the retry loop below to earn its keep; expect hours."
    fi
}

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
cat "${PROVENANCE_FILE}"

say "build host"
detect_build_host

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
        # Where THIS attempt's output begins. The stage log is appended across
        # attempts, so a whole-file grep reports every later failure in the
        # stage as a dead cc1 once any earlier attempt produced one —
        # including, on a native host, a failure with no compiler in it at all
        # (D8-F13). The diagnosis has to be about the failure in hand.
        if [ -f "${OUT_DIR}/logs/${stage}.log" ]; then
            log_bytes_before=$(wc -c <"${OUT_DIR}/logs/${stage}.log")
        else
            log_bytes_before=0
        fi
        if as_builder "cd '${SDK}' && ./build.sh ${stage}" \
                >>"${OUT_DIR}/logs/${stage}.log" 2>&1; then
            stage_status="ok"
            break
        fi
        if tail -c "+$((log_bytes_before + 1))" "${OUT_DIR}/logs/${stage}.log" |
                grep -q "internal compiler error"; then
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
# The manifest's verdict is this script's verdict (D8-F12).
#
# The block below can DOWNGRADE a build the shell believed had completed:
# every stage returns zero and the collect step — a `cp` whose errors are
# swallowed — produces nothing, which is a FAILED build that only the python
# can see. It used to see it, write it, and exit 0, because `status` in there
# is a python-local name while the shell's was still `complete`. So the block
# now exits with its own verdict, and the guard below tests THAT rather than
# the expectation the shell carried into it.
if IMAGE_OUT_DIR="${OUT_DIR}" \
IMAGE_STATUS="${status}" \
IMAGE_FAILED_STAGE="${failed_stage}" \
IMAGE_BOARD_CONFIG="${BOARD_CONFIG}" \
IMAGE_PROVENANCE="${PROVENANCE_FILE}" \
IMAGE_SDK="${SDK}" \
python3 - <<'PYTHON'
import hashlib
import json
import os
import re
import sys
from pathlib import Path

out = Path(os.environ["IMAGE_OUT_DIR"])
sdk = Path(os.environ["IMAGE_SDK"])
board_config = os.environ["IMAGE_BOARD_CONFIG"]

# The container's own provenance file, parsed rather than retyped.
provenance = {}
prov_path = Path(os.environ.get("IMAGE_PROVENANCE", "/etc/skyweave-image-provenance"))
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

# The machine this build ran on, as the shell's probe measured it. Parsed
# rather than retyped, same as the provenance file above: the row the report
# prints is the probe's answer, not a second copy of it (D8-F13, D8-F14).
build_host = {}
host_file = out / "logs" / "build-host.tsv"
if host_file.exists():
    for line in host_file.read_text(encoding="utf-8").splitlines():
        if "\t" in line:
            key, value = line.split("\t", 1)
            build_host[key.strip()] = value.strip()

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
    "build_host": build_host,
    "stages": stages,
    "files": files,
    "daemon_baked_in": False,
}
(out / "image-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print(json.dumps({"status": status, "files": len(files), "failed_stage": failed},
                 indent=2))
# The exit code IS the manifest's status. Anything else lets a build the
# manifest calls FAILED leave a zero behind it (D8-F12).
sys.exit(0 if status == "complete" else 1)
PYTHON
then
    say "done"
else
    echo "IMAGE BUILD FAILED. That verdict is the manifest block's own: read" >&2
    echo "${OUT_DIR}/image-manifest.json for which stage, and if the block died" >&2
    echo "before writing one, that is the failure." >&2
    exit 1
fi
