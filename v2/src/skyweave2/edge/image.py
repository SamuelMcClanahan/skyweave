"""The flashable image set, as the repo records it.

``scripts/build-image.sh`` builds boot, kernel and the Buildroot rootfs in the
pinned ``skyweave-image-build:d8.1`` container and writes
``firmware/rv1106/image/image-manifest.json``. The images themselves are
hundreds of megabytes of derived bytes and are gitignored; the manifest is
committed, and it is what the D8 report's build-provenance section publishes:
defconfig, SDK commit, container provenance and the SHA-256 of every file the
build produced.

This module is the reader. It does two jobs and refuses a third:

- it PARSES the manifest into something the report can print;
- it VALIDATES it, so a manifest that claims a complete build with no files in
  it, or a file entry with no hash, is an error rather than an empty table row;
- it does not, ever, produce a manifest from an absent build. No manifest means
  the report says PENDING with the command that would fill it. A "not built
  yet" and a "built and here is what came out" must not render the same.

``verify_against_disk`` re-hashes the files if they are still present, which is
the check worth having before a card is written: the manifest is a claim about
bytes, and the bytes are right there.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parents[3]
FIRMWARE_ROOT = V2_ROOT / "firmware" / "rv1106"
IMAGE_DIR = FIRMWARE_ROOT / "image"
MANIFEST_PATH = IMAGE_DIR / "image-manifest.json"
MANIFEST_SCHEMA = "d8-image/1"
BUILD_SCRIPT_PATH = FIRMWARE_ROOT / "scripts" / "build-image.sh"

#: The packed whole-medium image the SD path writes to a card.
MEDIUM_NAME = "sd_update.img"

#: DECLARED size budget for that file (Samuel, 2026-08-14): the packed image
#: must come in under 500 MB. Decimal MB, not MiB — the smaller reading, so a
#: manifest that satisfies this satisfies either. It is a gate rather than an
#: aspiration: the suite fails a committed manifest that busts it, which is the
#: only way a budget survives the build that ignores it.
MEDIUM_MAX_BYTES = 500_000_000

#: The packer pads the medium to a whole MiB, and reproducing that exactly is
#: what lets this project repack without the SDK (see `pack_medium`).
MEDIUM_ALIGN_BYTES = 1024 * 1024

#: Three findings are about the build SCRIPT rather than about any image it
#: produced, so the only artifact that can answer "is this fixed?" is the
#: script itself. Each entry lists EVERY fragment the mechanism needs, and all
#: of them must be present.
#:
#: All of them, because a single marker is a thing to keep rather than a thing
#: that works: an adversarial review of this file deleted the `exit 1` from the
#: F12 guard and deleted the CALL to the probe while leaving its definition,
#: and a one-marker-per-finding reader called both fixes present. So F12 names
#: the python verdict AND the shell branch that acts on it, and the probe is
#: named by its call site rather than by its definition.
#:
#: This is still a text reader, and text is not behaviour. What checks the
#: behaviour is `test_d81_image_build.py`, which runs the committed script
#: end to end against a stub SDK and asserts the exit codes and the manifest
#: it writes. These markers exist so the REPORT can derive a finding's status
#: from the repository; the suite is what makes the derivation true.
BUILD_SCRIPT_FIXES = {
    # D8-F12: the manifest block exits with its own verdict, AND the shell
    # branches on that exit rather than on the status it carried in.
    "f12_exit_follows_manifest": (
        'sys.exit(0 if status == "complete" else 1)',
        "PYTHON\nthen\n",
        # The whole else branch, ending in its exit. Not a bare `exit 1\nfi`:
        # this script has three of those in its REFUSING checks, and a marker
        # that any of them satisfies is a marker that checks nothing here.
        'that is the failure." >&2\n    exit 1\nfi',
    ),
    # D8-F13, half one: the "internal compiler error" grep reads only the
    # bytes THIS attempt appended, not the whole stage log.
    "f13_ice_grep_scoped": (
        "log_bytes_before",
        'tail -c "+$((log_bytes_before + 1))"',
    ),
    # D8-F13 half two and D8-F14: the probe exists AND is called. A definition
    # nothing calls announces nothing and records nothing.
    "f13_f14_emulation_probe": (
        "detect_build_host() {",
        "\ndetect_build_host\n",
        "build-host.tsv",
    ),
}

#: The one-liner a report prints where a manifest would be. Kept here so the
#: instruction and the thing it produces live in the same file.
BUILD_COMMAND = (
    "docker run --rm --platform linux/amd64 -v \"$PWD:/src\" -w /src "
    "skyweave-image-build:d8.1 ./scripts/build-image.sh"
)


class ImageManifestError(RuntimeError):
    """A manifest that cannot be believed. Never downgraded to a warning."""


@dataclass(frozen=True)
class ImageFile:
    name: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ImageManifest:
    schema: str
    status: str
    failed_stage: str
    container_tag: str
    board_config: str
    declared: dict
    provenance: dict
    stages: list[dict]
    files: list[ImageFile]
    daemon_baked_in: bool
    #: Anything done to these files AFTER the SDK produced them, in order.
    #: Empty for a manifest that is purely the build's output. A non-empty list
    #: means at least one file in `files` is not byte-for-byte what the
    #: container wrote, and the report says which and why: a hash table that
    #: silently mixed built bytes with post-processed ones would be a
    #: provenance record that misleads precisely where it is trusted most.
    post_build: list[dict]
    #: WHICH machine ran the BUILD, if the build recorded it. ``build-image.sh``
    #: probes for it now (``detect_build_host``), so a manifest written by a
    #: build after 2026-08-14 carries one; a manifest from before that is empty
    #: here and stays empty, because the only thing that can fill it honestly is
    #: another build. Re-hashing a file table on some other machine does not
    #: make that machine the builder. ``provenance`` cannot answer the question
    #: either: it describes the CONTAINER, which is byte-identical native or
    #: emulated. The field exists so the report's build-host row is DERIVED —
    #: NOT-MEASURED while this is empty, recorded the moment a build fills it —
    #: rather than being a sentence in the generator that nothing can
    #: contradict. Forced by the finding "neither the manifest nor the report
    #: records which machine produced the eleven hashes" (D8-F14).
    build_host: dict

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def sdk_commit(self) -> str:
        return self.provenance.get("luckfox_commit", "")

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.files)

    def defconfig_rows(self) -> list[tuple[str, str]]:
        """The declared build inputs, in a fixed order.

        Fixed rather than sorted-by-whatever-the-file-had: the report is
        byte-identical for identical inputs, and dict order from a JSON file is
        not something to rely on for that.
        """
        order = (
            ("RK_CHIP", "Chip"),
            ("RK_BOOT_MEDIUM", "Boot medium"),
            ("LF_TARGET_ROOTFS", "Rootfs"),
            ("RK_BUILDROOT_DEFCONFIG", "Buildroot defconfig"),
            ("RK_KERNEL_DEFCONFIG", "Kernel defconfig"),
            ("RK_KERNEL_DTS", "Kernel DTS"),
            ("RK_UBOOT_DEFCONFIG", "U-Boot defconfig"),
            ("RK_TOOLCHAIN_CROSS", "Toolchain"),
            ("RK_PARTITION_CMD_IN_ENV", "Partitions"),
            ("RK_CAMERA_SENSOR_IQFILES", "Sensor IQ files"),
        )
        return [(label, self.declared[key]) for key, label in order if key in self.declared]


def _parse(payload: dict) -> ImageManifest:
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise ImageManifestError(
            f"image manifest schema is {payload.get('schema')!r}, this reader "
            f"knows {MANIFEST_SCHEMA!r}"
        )
    files = []
    for item in payload.get("files", []):
        name = item.get("name")
        digest = item.get("sha256")
        if not name or not digest:
            raise ImageManifestError(
                f"image manifest carries a file entry with no name or no hash: "
                f"{item!r}. The hash is the whole reason this file exists."
            )
        files.append(
            ImageFile(name=name, bytes=int(item.get("bytes", 0)), sha256=digest)
        )
    status = payload.get("status", "")
    if status == "complete" and not files:
        raise ImageManifestError(
            "image manifest says the build is complete and lists no files. "
            "A build that produced nothing did not complete."
        )
    return ImageManifest(
        schema=payload["schema"],
        status=status,
        failed_stage=payload.get("failed_stage", ""),
        container_tag=payload.get("container_tag", ""),
        board_config=payload.get("board_config", ""),
        declared=payload.get("declared", {}),
        provenance=payload.get("provenance", {}),
        stages=payload.get("stages", []),
        files=files,
        daemon_baked_in=bool(payload.get("daemon_baked_in", False)),
        post_build=list(payload.get("post_build") or []),
        # Read rather than assumed, so the report's row follows the artifact
        # instead of the generator's memory of what the script does.
        build_host=dict(payload.get("build_host") or {}),
    )


def load_manifest(path: str | Path | None = None) -> ImageManifest | None:
    """The committed manifest, or None when no image has been built.

    None is a fact about this repository, not an error: the D8.1-prep
    amendment sanctions building the image, and until one has been built the
    report's provenance row says so and names the command.
    """
    path = Path(path or MANIFEST_PATH)
    if not path.exists():
        return None
    return _parse(json.loads(path.read_text(encoding="utf-8")))


_UNITS = {"K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}


def _bytes_from(token: str) -> int | None:
    """`32K` -> 32768, `-` -> None (a partition that grows to the end)."""
    token = token.strip()
    if token == "-":
        return None
    if token[-1].upper() in _UNITS:
        return int(token[:-1]) * _UNITS[token[-1].upper()]
    return int(token, 0)


@dataclass(frozen=True)
class MediumPartition:
    """One partition of the flashed medium, and what the build put in it."""

    name: str
    offset: int
    #: None for the grow-to-the-end partition, whose size is the medium's.
    size: int | None
    image: str
    image_bytes: int

    @property
    def reserved_bytes(self) -> int:
        """Declared space this partition's image does not fill."""
        return 0 if self.size is None else self.size - self.image_bytes


def medium_layout(manifest: ImageManifest) -> list[MediumPartition]:
    """Where every partition lands on the medium, derived from the manifest.

    `RK_PARTITION_CMD_IN_ENV` is the layout U-Boot puts in the kernel command
    line, so it is also the layout of the whole-medium image the SD build packs
    — and the offsets in it are what says whether `sd_update.img` carries a
    rootfs or stops before one. Computed from the manifest rather than read out
    of the image, so it stays true for a manifest whose gigabytes are not on
    this machine, and so it can DISAGREE with the image and be caught.

    Empty when the manifest declares no partition cmdline.
    """
    declared = manifest.declared.get("RK_PARTITION_CMD_IN_ENV", "")
    sizes = {item.name: item.bytes for item in manifest.files}
    parts: list[MediumPartition] = []
    cursor = 0
    for entry in (chunk.strip() for chunk in declared.split(",") if chunk.strip()):
        head, _, tail = entry.partition("(")
        name = tail.rstrip(")").strip()
        size_token, _, offset_token = head.partition("@")
        size = _bytes_from(size_token)
        offset = _bytes_from(offset_token) if offset_token else cursor
        image = f"{name}.img"
        parts.append(
            MediumPartition(
                name=name,
                offset=offset or 0,
                size=size,
                image=image,
                image_bytes=sizes.get(image, 0),
            )
        )
        cursor = (offset or 0) + (size or 0)
    return parts


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pack_medium(
    manifest: ImageManifest,
    image_dir: str | Path | None = None,
    out_path: str | Path | None = None,
) -> int:
    """Write the whole-medium image from the partition images. Returns bytes.

    This is the SDK's `lf_blkenvpackage --trim` reproduced: every partition
    image at the offset the declared cmdline gives it, zeros everywhere else,
    the whole thing padded to a whole MiB. It is reproduced rather than invoked
    because the SDK tool lives in a Buildroot tree that is not on this machine.

    That it IS the SDK packer is a measurement, made once and not repeatable
    afterwards: run over the build's own partition images, this produced the
    SDK's `sd_update.img` byte for byte, `9c88bf53e1f08eeaf256aabea97c008fb...`
    — the hash `post_build` records as that file's `from_sha256`. The suite's
    standing check is the weaker one it can still make: that the medium on disk
    is the composition of the partition images on disk, so a partition image
    that changed without a repack is caught.

    Only the partitions whose images exist are written. A partition image that
    is absent leaves zeros, which is what an unwritten partition is. An image
    LARGER than its declared partition is refused: it would overwrite the next
    partition, which on this medium means a rootfs written over userdata.
    """
    image_dir = Path(image_dir or IMAGE_DIR)
    out_path = Path(out_path or (image_dir / MEDIUM_NAME))
    layout = medium_layout(manifest)
    if not layout:
        raise ImageManifestError(
            "this manifest declares no partition cmdline, so there is no "
            "medium layout to pack"
        )
    present = [part for part in layout if (image_dir / part.image).exists()]
    if not present:
        raise ImageManifestError(
            f"none of the partition images are in {image_dir}; the packed "
            "medium would be zeros"
        )
    for part in present:
        size = (image_dir / part.image).stat().st_size
        if part.size is not None and size > part.size:
            raise ImageManifestError(
                f"{part.image} is {size} B and the {part.name} partition is "
                f"declared {part.size} B. Packing it would write over the next "
                "partition; the SDK's own generator refuses the same case."
            )
    last = present[-1]
    content_end = last.offset + (image_dir / last.image).stat().st_size
    total = -(-content_end // MEDIUM_ALIGN_BYTES) * MEDIUM_ALIGN_BYTES

    tmp = out_path.with_suffix(out_path.suffix + ".partial")
    with tmp.open("wb") as out:
        out.truncate(total)
        for part in present:
            source = image_dir / part.image
            out.seek(part.offset)
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    out.write(chunk)
    tmp.replace(out_path)
    return total


def rehash_manifest(
    manifest_path: str | Path | None = None,
    image_dir: str | Path | None = None,
    post_build: list[dict] | None = None,
    max_bytes: int = MEDIUM_MAX_BYTES,
) -> dict:
    """Re-hash the files on disk into the committed manifest, and gate the size.

    Everything the BUILD recorded — the defconfigs, the container provenance,
    the stage table, the build host — is preserved untouched: this function
    knows what the bytes are, and nothing about where they came from. What it
    updates is the file table, and what it appends is the record of why those
    bytes are no longer the ones the build wrote.

    Refuses a medium over `max_bytes`. The budget is a declared constraint, and
    a tool that writes an over-budget manifest and leaves the discovery to a
    person is the same failure as a build that exits 0 on a FAILED manifest.
    """
    manifest_path = Path(manifest_path or MANIFEST_PATH)
    image_dir = Path(image_dir or IMAGE_DIR)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    files = []
    for path in sorted(image_dir.iterdir()):
        if not path.is_file() or path.name == manifest_path.name:
            continue
        files.append(
            {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256_of(path)}
        )
    if not files:
        raise ImageManifestError(f"no image files in {image_dir} to hash")

    medium = next((item for item in files if item["name"] == MEDIUM_NAME), None)
    if medium is not None and medium["bytes"] > max_bytes:
        raise ImageManifestError(
            f"{MEDIUM_NAME} is {medium['bytes']} B, over the declared "
            f"{max_bytes} B budget. The manifest is NOT written."
        )

    payload["files"] = files
    if post_build is not None:
        # REPLACED, not appended: the step log is the history, and this is the
        # manifest's copy of it. Appending would grow a duplicate every time
        # the pack is re-run over the same log.
        payload["post_build"] = list(post_build)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def build_script_fixes(path: str | Path | None = None) -> dict[str, bool]:
    """Which recorded build-script findings the committed script actually fixes.

    An absent script answers False to everything rather than raising: the safe
    direction is the report saying a finding is open when it has been closed,
    never the other way round.
    """
    script = Path(path or BUILD_SCRIPT_PATH)
    text = script.read_text(encoding="utf-8") if script.exists() else ""
    return {
        key: all(marker in text for marker in markers)
        for key, markers in BUILD_SCRIPT_FIXES.items()
    }


def verify_against_disk(
    manifest: ImageManifest, image_dir: str | Path | None = None
) -> list[str]:
    """Re-hash whatever is still on disk. Every disagreement, named.

    Missing files are NOT a disagreement — the images are gitignored and a
    fresh clone has none of them. A file that is present and hashes differently
    is, and that is the case worth catching before a card gets written.
    """
    image_dir = Path(image_dir or IMAGE_DIR)
    problems = []
    for item in manifest.files:
        path = image_dir / item.name
        if not path.exists():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != item.sha256:
            problems.append(
                f"{item.name}: on disk {digest.hexdigest()}, manifest {item.sha256}"
            )
        elif path.stat().st_size != item.bytes:
            problems.append(
                f"{item.name}: on disk {path.stat().st_size} B, manifest {item.bytes} B"
            )
    return problems


#: Where `scripts/shrink-rootfs.sh` records what it did, one JSON object per
#: line, for the pack step to fold into the manifest. Same idiom as the build's
#: `logs/stages.tsv`: the step that did the work writes the record, and the
#: step that publishes it parses that record rather than being told.
POST_BUILD_LOG = IMAGE_DIR / "logs" / "post-build.jsonl"


def read_post_build_log(path: str | Path | None = None) -> list[dict]:
    path = Path(path or POST_BUILD_LOG)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def main(argv: list[str] | None = None) -> None:
    """`pack` the medium from the partition images and republish the manifest.

        uv run python -m skyweave2.edge.image pack

    The SD path's medium image is derived from the other files, so when one of
    them changes it has to be rebuilt and everything re-hashed together. Doing
    that by hand is how a manifest ends up describing bytes that no longer
    exist.
    """
    import argparse

    parser = argparse.ArgumentParser(description="the flashable image set")
    sub = parser.add_subparsers(dest="command", required=True)
    pack = sub.add_parser("pack", help="repack the medium and re-hash the manifest")
    pack.add_argument("--image-dir", default=None)
    pack.add_argument("--manifest", default=None)
    pack.add_argument("--max-bytes", type=int, default=MEDIUM_MAX_BYTES)
    verify = sub.add_parser("verify", help="re-hash what is on disk against the manifest")
    verify.add_argument("--image-dir", default=None)
    verify.add_argument("--manifest", default=None)
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest or MANIFEST_PATH)
    image_dir = Path(args.image_dir or IMAGE_DIR)
    manifest = load_manifest(manifest_path)
    if manifest is None:
        raise SystemExit(f"no manifest at {manifest_path}")

    if args.command == "verify":
        problems = verify_against_disk(manifest, image_dir)
        for problem in problems:
            print(problem)
        print(f"{len(manifest.files)} files in the manifest, {len(problems)} disagree")
        raise SystemExit(1 if problems else 0)

    before = {item.name: item for item in manifest.files}
    written = pack_medium(manifest, image_dir)
    medium_path = image_dir / MEDIUM_NAME
    was = before.get(MEDIUM_NAME)
    entries = read_post_build_log(image_dir / "logs" / "post-build.jsonl")
    host = os.uname()
    entries.append(
        {
            "step": "pack-medium",
            "tool": "skyweave2.edge.image.pack_medium",
            # WHICH machine did this step, for the reason D8-F14 exists: a
            # record of what was done to a file that does not say where it was
            # done is the same absence, one layer down.
            "host": f"{host.sysname} {host.release} {host.machine}",
            "file": MEDIUM_NAME,
            "from_bytes": was.bytes if was else None,
            "to_bytes": written,
            "from_sha256": was.sha256 if was else None,
            "to_sha256": sha256_of(medium_path),
            "note": (
                "every partition image at its declared offset, zeros elsewhere, "
                "padded to a whole MiB — the SDK packer's output reproduced"
            ),
        }
    )
    rehash_manifest(manifest_path, image_dir, entries, max_bytes=args.max_bytes)
    print(f"{MEDIUM_NAME}: {written} B (budget {args.max_bytes} B)")
    print(f"wrote {manifest_path}")


if __name__ == "__main__":
    main()


__all__ = [
    "BUILD_COMMAND",
    "BUILD_SCRIPT_FIXES",
    "BUILD_SCRIPT_PATH",
    "IMAGE_DIR",
    "MANIFEST_PATH",
    "MANIFEST_SCHEMA",
    "ImageFile",
    "ImageManifest",
    "ImageManifestError",
    "MEDIUM_ALIGN_BYTES",
    "MEDIUM_MAX_BYTES",
    "MEDIUM_NAME",
    "MediumPartition",
    "POST_BUILD_LOG",
    "build_script_fixes",
    "load_manifest",
    "main",
    "medium_layout",
    "pack_medium",
    "read_post_build_log",
    "rehash_manifest",
    "sha256_of",
    "verify_against_disk",
]
