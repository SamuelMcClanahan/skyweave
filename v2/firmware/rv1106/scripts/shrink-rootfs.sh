#!/bin/sh
# Shrink the built rootfs ext4 so the packed medium fits its declared budget.
#
#   docker run --rm --platform linux/amd64 -v "$PWD:/src" -w /src \
#       debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241 \
#       sh -c 'apt-get update && apt-get install -y e2fsprogs && ./scripts/shrink-rootfs.sh'
#
#   # then, on the host, repack the medium and republish the manifest:
#   cd v2 && uv run python -m skyweave2.edge.image pack
#
# WHY this exists. The medium image is the partition cmdline's geometry, so
# `boot` at 32M and `userdata` at 256M put the rootfs partition at byte
# 302,809,088 no matter how little is in the partitions before it — 288 MB of
# the packed image is space reserved for partitions their images do not fill,
# and no packer can trim that. The only part of the image whose SIZE this
# project can change without rebuilding the SDK tree is the rootfs filesystem
# itself, which the build sizes to its partition rather than to its contents:
# 199 MiB of ext4 holding about 112 MiB of files.
#
# So this shrinks the filesystem, not the partition. `-(rootfs)` still grows to
# the end of whatever card it is written to; the ext4 inside it is simply
# smaller than the partition, which is the normal state of an ext4 and is what
# the kernel reads out of the superblock. The RIGHT fix, for the next build
# that has the Buildroot tree in front of it, is to declare a smaller
# `userdata` — 256M of /userdata on a bring-up node buys nothing and costs 246
# MB of every card write. This is the fix available without it.
#
# It runs in a container because e2fsprogs is a Linux tool and this project's
# workstation is a Mac. The base is the pinned digest the image containers use.
#
# Not idempotent by accident: shrinking an already-shrunk filesystem to the
# same size is a no-op that resize2fs reports as such, and the size is declared
# here rather than computed from the budget, because a target computed from a
# byte budget is a target nobody chose.

set -eu

IMAGE_DIR="${IMAGE_OUT_DIR:-image}"
ROOTFS="${IMAGE_DIR}/rootfs.img"

# 168 MiB: about 112 MiB of files, so roughly 47 MB free on / for logs and a
# hand-pushed daemon, and a packed medium of 457 MiB against a 500 MB budget.
# The node's writable data lives on the separate 256 MB `userdata` partition,
# so this number is about headroom on the OS, not about storage.
ROOTFS_SIZE="${ROOTFS_SIZE:-168M}"

if [ ! -f "${ROOTFS}" ]; then
    echo "REFUSING: no ${ROOTFS}. Build the image set first." >&2
    exit 1
fi
if ! command -v resize2fs >/dev/null 2>&1; then
    echo "REFUSING: no resize2fs. This script runs in a Linux container with" >&2
    echo "e2fsprogs installed; see the header." >&2
    exit 1
fi

say() { printf '\n=== %s\n' "$*"; }

# What was there before, so the record names both ends and the invariants below
# have something to compare against.
before_bytes=$(wc -c <"${ROOTFS}")
before_sha=$(sha256sum "${ROOTFS}" | cut -d' ' -f1)
before_counts=$(dumpe2fs -h "${ROOTFS}" 2>/dev/null | awk -F: '
    /^Inode count/ {inodes=$2} /^Free inodes/ {free_inodes=$2}
    /^Block count/ {blocks=$2} /^Free blocks/ {free_blocks=$2}
    END {printf "%d %d", inodes - free_inodes, blocks - free_blocks}')
used_inodes_before=$(echo "${before_counts}" | cut -d' ' -f1)
used_blocks_before=$(echo "${before_counts}" | cut -d' ' -f2)

say "rootfs before: ${before_bytes} B, ${used_inodes_before} inodes and ${used_blocks_before} blocks in use"

say "fsck"
e2fsck -fy "${ROOTFS}" || true

say "resize to ${ROOTFS_SIZE}"
# -f because resize2fs's own minimum-size ESTIMATE for this image is its
# current size, which would refuse every shrink. The estimate is advisory; the
# resize itself relocates what it must and fails if it cannot, and the fsck
# below is what says whether it worked.
resize2fs -f "${ROOTFS}" "${ROOTFS_SIZE}"

say "fsck after"
e2fsck -fy "${ROOTFS}"

after_bytes=$(wc -c <"${ROOTFS}")
after_sha=$(sha256sum "${ROOTFS}" | cut -d' ' -f1)
after_counts=$(dumpe2fs -h "${ROOTFS}" 2>/dev/null | awk -F: '
    /^Inode count/ {inodes=$2} /^Free inodes/ {free_inodes=$2}
    /^Block count/ {blocks=$2} /^Free blocks/ {free_blocks=$2}
    END {printf "%d %d %d", inodes - free_inodes, blocks - free_blocks, free_blocks}')
used_inodes_after=$(echo "${after_counts}" | cut -d' ' -f1)
used_blocks_after=$(echo "${after_counts}" | cut -d' ' -f2)
free_blocks_after=$(echo "${after_counts}" | cut -d' ' -f3)

# The invariant that makes this safe to run unattended: a shrink moves blocks,
# it does not drop files. Same inodes in use, same blocks in use. If either
# moved, something was lost and the manifest must not be republished over it.
say "rootfs after: ${after_bytes} B, ${used_inodes_after} inodes and ${used_blocks_after} blocks in use"
if [ "${used_inodes_before}" != "${used_inodes_after}" ]; then
    echo "REFUSING: inodes in use went ${used_inodes_before} -> ${used_inodes_after}." >&2
    echo "A shrink relocates blocks; it does not change what is in the tree." >&2
    exit 1
fi
if [ "${used_blocks_before}" != "${used_blocks_after}" ]; then
    # Metadata does move: fewer block groups means fewer group descriptors and
    # a smaller inode table, so used blocks may DROP. It must never rise, and a
    # large fall is worth seeing.
    echo "note: blocks in use ${used_blocks_before} -> ${used_blocks_after} (metadata shrank with the group count)"
    if [ "${used_blocks_after}" -gt "${used_blocks_before}" ]; then
        echo "REFUSING: blocks in use ROSE during a shrink." >&2
        exit 1
    fi
fi

mkdir -p "${IMAGE_DIR}/logs"
# One JSON object per line, appended: the pack step folds this into the
# manifest so the published hashes carry the reason they changed.
cat >> "${IMAGE_DIR}/logs/post-build.jsonl" <<JSON
{"step": "shrink-rootfs", "tool": "$(resize2fs -V 2>&1 | head -n1 | tr -d '"')", "host": "$(uname -s) $(uname -r) $(uname -m)", "file": "rootfs.img", "from_bytes": ${before_bytes}, "to_bytes": ${after_bytes}, "from_sha256": "${before_sha}", "to_sha256": "${after_sha}", "note": "ext4 resized to ${ROOTFS_SIZE}; the partition still grows to the end of the card. e2fsck clean; ${used_inodes_after} inodes and ${used_blocks_after} blocks still in use, ${free_blocks_after} free"}
JSON

say "done: rootfs.img ${before_bytes} -> ${after_bytes} B"
echo "Now repack the medium and republish the manifest, from v2/:"
echo "  uv run python -m skyweave2.edge.image pack"
