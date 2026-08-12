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
from dataclasses import dataclass
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parents[3]
FIRMWARE_ROOT = V2_ROOT / "firmware" / "rv1106"
IMAGE_DIR = FIRMWARE_ROOT / "image"
MANIFEST_PATH = IMAGE_DIR / "image-manifest.json"
MANIFEST_SCHEMA = "d8-image/1"

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
    #: WHICH machine ran the build, if the build recorded it. Empty for every
    #: manifest written so far: ``build-image.sh`` has no ``uname``/``arch``/
    #: binfmt probe, and ``provenance`` describes the CONTAINER, which is
    #: byte-identical native or emulated. The field exists so the report's
    #: build-host row is DERIVED from the manifest — NOT-MEASURED while this is
    #: empty, filled the moment a build records it — rather than being a
    #: sentence in the generator that nothing can contradict. Forced by the
    #: finding "neither the manifest nor the report records which machine
    #: produced the eleven hashes" (D8-F14).
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
        # Absent in every manifest the current script writes; read rather than
        # assumed, so the report's row follows the artifact instead of the
        # generator's memory of what the script used to do.
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


__all__ = [
    "BUILD_COMMAND",
    "IMAGE_DIR",
    "MANIFEST_PATH",
    "MANIFEST_SCHEMA",
    "ImageFile",
    "ImageManifest",
    "ImageManifestError",
    "load_manifest",
    "verify_against_disk",
]
