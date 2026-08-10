# The pinned build container for the RV1106 edge daemon (D8.0).
#
# The image IS the build: the brief records its tag in the report and calls it
# the reproducible build. So everything here is pinned by digest or by commit,
# and nothing is fetched at build time from a moving reference.
#
# Platform note: the Luckfox SDK's toolchain binaries are x86_64 ELF, so this
# image is linux/amd64 and runs under emulation on Apple Silicon. That is slow
# and it is correct — running the SAME image on the Mac and on the Linux PC is
# the point, and a second arm64 image would be a second build.
#
#   docker build --platform linux/amd64 -t skyweave-edge-build:d8.0 \
#       -f docker/Dockerfile docker/
#   docker run --rm --platform linux/amd64 -v "$PWD:/src" -w /src \
#       skyweave-edge-build:d8.0 ./scripts/build-board.sh

FROM debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241

# The base is pinned by DIGEST, which is the strong pin. The apt packages
# below are not version-pinned on purpose: the Debian stable archive moves
# under a point release, so a pinned version turns "reproducible" into
# "unbuildable in three months". What they resolved to is RECORDED in the
# provenance file instead, so a build can always be described exactly even
# though it cannot be frozen exactly.
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
        xz-utils \
        file \
        python3 \
    ; \
    rm -rf /var/lib/apt/lists/*

# The Luckfox SDK, pinned to a COMMIT rather than a branch. A branch would
# make "the reproducible build" a statement about the day it was run.
ENV LUCKFOX_REPO=https://github.com/LuckfoxTECH/luckfox-pico.git
ENV LUCKFOX_COMMIT=824b817f889c2cbff1d48fcdb18ab494a68f69d1
ENV SKYWEAVE_SDK=/opt/luckfox-pico

# Blobless + sparse: the full SDK is several GB and this build needs the
# toolchain plus three media SDKs. Nothing else is downloaded.
RUN set -eux; \
    mkdir -p "${SKYWEAVE_SDK}"; \
    cd "${SKYWEAVE_SDK}"; \
    git init -q .; \
    git remote add origin "${LUCKFOX_REPO}"; \
    git config core.sparseCheckout true; \
    git sparse-checkout set \
        tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf \
        media/ive/ive/include \
        media/ive/ive/lib \
        media/rga/release_rga_rv1106_arm-rockchip830-linux-uclibcgnueabihf/include \
        media/rga/release_rga_rv1106_arm-rockchip830-linux-uclibcgnueabihf/lib \
        media/rockit/rockit/mpi/sdk/include \
        media/rockit/rockit/lib/lib32 \
    ; \
    git fetch --depth 1 --filter=blob:none origin "${LUCKFOX_COMMIT}"; \
    git checkout -q FETCH_HEAD; \
    test -x "tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin/arm-rockchip830-linux-uclibcgnueabihf-gcc"; \
    test -f "media/ive/ive/include/rk_mpi_ive.h"; \
    rm -rf .git

ENV PATH="${SKYWEAVE_SDK}/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin:${PATH}"

# Recorded in the image so a container can be asked what it is, and the
# report can quote the answer instead of a note somebody typed.
RUN set -eux; \
    { \
      echo "base=debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241"; \
      echo "luckfox_repo=${LUCKFOX_REPO}"; \
      echo "luckfox_commit=${LUCKFOX_COMMIT}"; \
      echo -n "toolchain="; \
      arm-rockchip830-linux-uclibcgnueabihf-gcc -dumpmachine; \
      echo -n "gcc="; \
      arm-rockchip830-linux-uclibcgnueabihf-gcc -dumpversion; \
      cmake --version | head -n1 | sed 's/^/cmake=/'; \
      git --version | sed 's/^/git=/'; \
      echo -n "debian="; cat /etc/debian_version; \
    } > /etc/skyweave-build-provenance; \
    cat /etc/skyweave-build-provenance

CMD ["/bin/bash"]
