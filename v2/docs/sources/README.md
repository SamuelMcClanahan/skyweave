# Skyweave source catalog

This directory is the research index for the v2 rewrite. It records which
sources support a decision, where the source is pinned, and whether a local
copy is appropriate. It is not a second implementation specification.

The fixed-camera v2 design authority remains the two human-authored notes
attached to the 2026-07-15 conversation. For the drone-system working document,
Samuel's `skyweave_interceptor_notes(1).md` rev 2 and subsequent direct
decisions take priority. These catalog entries are evidence for evaluating
those ideas. A source entry does not turn a vendor claim, simulation result, or
paper result into a Skyweave performance claim.

Current project-side synthesis:

- [drone system working document](../SKYWEAVE_DRONE_WORKING_DOCUMENT.md);
- [follow-up decisions](../FOLLOWUP_DECISIONS_2026-07-17.md);
- [EXP-001 800 ft synthetic full-stack test](../experiments/EXP-001_800FT_SYNTHETIC_FULL_STACK.md);
- [research report](../RESEARCH_REPORT.md); and
- [implementation framework](../IMPLEMENTATION_FRAMEWORK.md).

## Status vocabulary

| Status | Meaning |
| --- | --- |
| `local-reference` | Already present in this repository. Do not duplicate it. |
| `local-reference-restricted` | Present locally, but the license has non-standard use restrictions. Study only; do not copy into v2 without review. |
| `link-pinned` | Keep a URL plus an immutable commit, DOI, RFC, or document ID. |
| `archive-candidate` | A small stable document is useful offline; archive it only after checking redistribution terms and recording SHA-256. |
| `restricted-link` | Vendor or proprietary material should be accessed from its official location, not copied into a public repository. |
| `later` | Relevant after the offline vertical slice; do not make it an early dependency. |

The machine-readable inventory is [`manifest.yaml`](./manifest.yaml).

## Recommended layout

```text
v2/docs/sources/
  README.md                 # policy and reading order
  manifest.yaml             # source IDs, URLs, revisions, status
  archive/                  # optional PDFs; each file gets a .sha256 and metadata entry
  notes/                    # short project-specific extracts, never unreviewed AI summaries
```

Do not vendor the complete Luckfox/Rockchip SDK, binary blobs, Blender, Isaac
Sim, or a paper corpus. They are large, mutable, or have redistribution
constraints. Pin the exact external revision and keep a short, reviewed note
of the API symbols or equations Skyweave relies on.

## Reading order

### P0: first implementation decisions

1. Luckfox `RK_MPI_IVE_GMM2` and CCL declarations/samples, pinned to a commit.
2. V4L2 buffer timestamp semantics, Linux clock/PTP and network timestamping.
3. OpenCV ChArUco and `calib3d` documentation.
4. Hartley-Sturm or angular-error triangulation, plus the mrcal uncertainty
   analysis.
5. The audited Pixeltovoxelprojector source and its non-standard restricted
   license; use it as provenance/reference, not source to copy.
6. Protobuf encoding/compatibility guidance before freezing the wire schema.

### P1: first synthetic and hardware experiments

1. Blender camera/output/motion-blur documentation and the deterministic render
   sidecar contract below.
2. SmartSens SC3336 product information and the exact vendor sensor datasheet
   supplied with the module.
3. Radxa Cubie A7Z product/specification, MIPI CSI, and fan documentation;
   verify the purchased board revision, power limits, and camera support.
4. u-blox ZED-F9P product summary and integration manual.
5. Bosch BNO055 datasheet, strictly as a setup-prior reference.
6. RFC 3550 and RFC 8085 for the optional video/debug and UDP transport paths.

### P2: later validation or optimization

Bundle adjustment, visual hulls, voxel hashing, Isaac Sim camera sensors,
rolling-shutter calibration papers, ADS-B quality metrics, and IMM/association
literature become important after the deterministic offline spine exists.

## Archive policy

For a stable PDF that needs to work offline, save the file under `archive/`,
then record all of the following in `manifest.yaml`:

```text
source URL
document/revision identifier
retrieval date
file size
SHA-256
license or redistribution note
```

The following are good archive candidates for a private working copy:

- u-blox `ZED-F9P_ProductSummary_UBX-17005151.pdf`;
- u-blox `ZED-F9P_IntegrationManual_UBX-18010802.pdf`;
- Bosch `bst-bno055-ds000.pdf`; and
- FAA `AC_20-165B.pdf` if ADS-B remains in the validation plan.

Do not assume that an official PDF is freely redistributable in a public
repository. If the repository may become public, retain the URL and checksum
in the manifest and keep the PDF outside git unless the publisher's terms
permit redistribution. Academic papers should normally remain link-only.

The Pixeltovoxelprojector source is already present at
[`v1/reference/pixel-to-voxel-projector`](../../../v1/reference/pixel-to-voxel-projector),
but its `LICENSE` is **not plain Apache-2.0**. It contains the Apache 2.0 text
followed by an “ADDITIONAL USE RESTRICTION” for defined defense entities that
states that it prevails over inconsistent Apache terms. Preserve the snapshot
and exact license for provenance, do not silently replace it with moving
GitHub `main`, and do not copy/derive v2 implementation code from it without
legal review. Skyweave should independently implement the general
back-projection/DDA ideas from public geometry literature and its own tests.

Project documentation in this folder is technical guidance, not legal advice.

## Blender synthetic-data contract

Blender is the first scene generator, not a substitute for the SC3336 sensor
or RV1106 ISP. A normal Blender render is not Bayer RAW, RAW8, or an
SC3336-specific rolling-shutter capture. Treat sensor effects as explicit,
independently switchable transforms:

```text
Blender scene/render (ground truth)
  -> optional lens/distortion render model
  -> optional Bayer mosaic + quantization + noise model
  -> optional rolling-shutter/exposure model
  -> Y/luma or encoded frame presented to the edge reference
  -> the same packet, transport, alignment, localization, and tracking code
```

Every generated frame set should contain:

```text
dataset.json                 # generator version, seed, units, clock model
truth/trajectory.jsonl       # target pose/velocity at exposure times
truth/cameras.json           # camera poses and intrinsics used to render
frames/cam-*/frame-*.exr     # high precision source/render output
frames/cam-*/frame-*.png     # optional 8-bit presentation output
sensor-model.json            # injected Bayer/noise/rolling-shutter settings
```

Keep the renderer and estimator camera models independently configurable. A
render that uses exactly the estimator's intrinsics and timestamps proves only
that the same equations agree with themselves.

## Local Skyweave references

These are useful project artifacts, but they are not external authority:

- `v1/docs/RV1106_EDGE_NODE.md` - prior edge dataflow and resource budget.
- `v1/docs/THROUGHPUT_30FPS_JETSON.md` - measured/estimated voxel costs and
  the camera-bitmask optimization idea.
- `v1/docs/CUDA_DETECTOR_DESIGN.md` - candidate back-projection and CUDA
  strategies; treat performance numbers as hypotheses until remeasured.
- `v1/docs/conversations/2026-05-28-architecture-review.md` - earlier design
  discussion and alternatives.
- `v1/docs/viz-data-contract.md` - v1 visualizer payload vocabulary.
- `v1/data/golden/peak_baselines.json` - characterization fixture, not field
  ground truth.

Large recordings and generated frames should be referenced by manifest and
checksum, not copied into this source directory.
