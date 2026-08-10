#!/bin/sh
# Cross-compile the edge daemon for the RV1106, inside the pinned container.
#
# Run from the firmware root (the container mounts it at /src):
#
#   docker run --rm --platform linux/amd64 -v "$PWD:/src" -w /src \
#       skyweave-edge-build:d8.0 ./scripts/build-board.sh
#
# Everything the build depends on is in the image and pinned there; this
# script only wires the SDK into CMake and reports what it produced.

set -eu

BUILD_DIR="${BUILD_DIR:-build-board}"
SDK="${SKYWEAVE_SDK:-/opt/luckfox-pico}"
# Serial by default. The image is linux/amd64, so on Apple Silicon every
# compiler invocation runs under emulation, and a parallel build there
# produced an intermittent cc1 SEGV (an emulation artifact, not a compiler
# bug — the same file compiles fine serially). The Linux PC mirror runs the
# image natively and should pass BUILD_JOBS=$(nproc).
BUILD_JOBS="${BUILD_JOBS:-1}"

if [ ! -d "${SDK}" ]; then
    echo "SKYWEAVE_SDK=${SDK} does not exist; run inside the pinned container" >&2
    exit 1
fi

echo "--- build provenance ---"
cat /etc/skyweave-build-provenance 2>/dev/null || echo "(not the pinned container)"

cmake -S . -B "${BUILD_DIR}" \
    -DCMAKE_TOOLCHAIN_FILE=cmake/luckfox-arm.cmake \
    -DCMAKE_BUILD_TYPE=Release \
    -DSKYWEAVE_SDK="${SDK}"

# Bounded retry, and a loud one.
#
# Under linux/amd64 EMULATION on Apple Silicon, cc1 and `as` segfault
# intermittently — different files each time, and the same file compiles
# cleanly on the next attempt. It is an emulation artifact, not a compiler
# bug and not a defect in this source. `make` is incremental, so each retry
# resumes where the last one died and the build converges.
#
# The retry is capped and every attempt is announced, because a build that
# quietly retried forever would hide a real, reproducible compile failure.
# On a native x86-64 host (the Linux PC mirror) the first attempt succeeds
# and this loop never runs twice.
ATTEMPTS="${BUILD_ATTEMPTS:-6}"
attempt=1
while :; do
    if cmake --build "${BUILD_DIR}" -j "${BUILD_JOBS}"; then
        break
    fi
    if [ "${attempt}" -ge "${ATTEMPTS}" ]; then
        echo "build failed ${ATTEMPTS} times; this is not the emulation flake" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    echo "--- build attempt ${attempt}/${ATTEMPTS} (previous one died; see above) ---" >&2
done

echo "--- artifacts ---"
for binary in "${BUILD_DIR}/skyweave-edge" "${BUILD_DIR}/sw-fixture-tool"; do
    [ -f "${binary}" ] || continue
    file "${binary}"
    # A daemon that silently came out x86 would run fine in this container
    # and not at all on the node.
    file "${binary}" | grep -q "ARM" || {
        echo "REFUSING: ${binary} is not an ARM binary" >&2
        exit 1
    }
    ls -l "${binary}"
done
