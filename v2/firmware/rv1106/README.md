# skyweave-edge — the RV1106 edge daemon (D8)

One statically-or-minimally-linked C daemon per the
[`v1/docs/RV1106_EDGE_NODE.md`](../../../v1/docs/RV1106_EDGE_NODE.md)
checklist. It captures (or is injected with) luma frames, finds motion,
bounds what it sends, encodes observations with nanopb against the frozen
`v2/proto/skyweave.proto`, and puts one capture event in one UDP datagram.

No Python. No OpenCV. No systemd. Those are refusals from the node design,
not omissions.

## What is scored this phase, and what is not

| Path | Status |
| --- | --- |
| C1 app-layer Y injection -> IVE GMM2/CCL -> observations -> wire | **the scored path** |
| nanopb encoding vs the host codec | **exact**, gated by test E2 |
| portable C GMM2 vs the `ive_approx` oracle | toleranced, gated by E5 |
| hardware IVE GMM2 vs the same oracle | toleranced, D8.2, bounds declared in advance |
| real CSI capture through VI | smoke test only; not scored, no PTS claim |
| RGA evidence crops | present, evidence plane only, never on the measurement path |
| NPU appearance classifier | out of scope (phase 2 of the node design) |

## Layout

```
include/        headers; every struct here mirrors a frozen contract
src/
  main.c              the frame loop
  sw_wire.c           framing + nanopb encode/decode   <- the byte-identity gate
  sw_pipeline.c       persistence -> per-frame cap -> capture event
  sw_detect_soft.c    portable GMM2 + CCL (host, QEMU)
  sw_detect_ive.c     RK_MPI_IVE_GMM2 + RK_MPI_IVE_CCL (the board)
  sw_inject.c         SWIJ injection reader (file or TCP)
  sw_capture.c        RKMPI VI capture + RGA crop (the board)
  sw_net.c            UDP measurement plane, TCP control plane
  sw_obsfixture.c     SWOB observation-fixture reader (test E2)
proto/          skyweave.pb.{c,h}, generated from v2/proto/ and CHECKED IN
third_party/    nanopb 0.4.9, vendored
tools/          sw-fixture-tool: the daemon's own encoder, exposed for E2
docker/         the pinned build container
cmake/          the Luckfox cross toolchain file
```

## Building

**Host** (what the E-series runs against):

```sh
cmake -S . -B build-host -DCMAKE_BUILD_TYPE=Release
cmake --build build-host -j4
```

Produces `skyweave-edge` with the portable detector, and `sw-fixture-tool`.
The RKMPI/IVE/RGA paths compile as *refusals* — asking for `--detector ive`
or `--capture-vi` in a host build is an error, never a silent fallback to
something else, because the D8 tolerance table is only meaningful if every
number says which detector produced it.

**Board**, in the pinned container:

```sh
docker build --platform linux/amd64 -t skyweave-edge-build:d8.0 -f docker/Dockerfile docker/
docker run --rm --platform linux/amd64 -v "$PWD:/src" -w /src \
    skyweave-edge-build:d8.0 ./scripts/build-board.sh
```

**The flashable image set** (boot, kernel, Buildroot rootfs), in a SECOND
pinned container at the same SDK commit:

```sh
docker build --platform linux/amd64 -t skyweave-image-build:d8.1 \
    -f docker/Dockerfile.image docker/
docker run --rm --platform linux/amd64 -v "$PWD:/src" -w /src \
    skyweave-image-build:d8.1 ./scripts/build-image.sh
```

Two images because they need different things: the daemon build needs a
toolchain and three media SDKs, the image build needs the whole SDK and the
Debian packages its README asks for. Keeping them apart means an image build
cannot change the tag the D8.0 report quotes for the binary it measured.

Output lands in `image/`, which is gitignored except for
`image/image-manifest.json` — the defconfig, the SDK commit and the SHA-256 of
every produced file. That manifest is the committed deliverable and it is what
the report's build-provenance section prints. `IMAGE_STAGES`, `BUILD_JOBS`,
`BUILD_ATTEMPTS` and `SKYWEAVE_BOARD_CONFIG` are the knobs; the board defaults
to `BoardConfig-SD_CARD-Buildroot-RV1106_Luckfox_Pico_Pro_Max-IPC.mk` and
whatever it is set to is what the manifest records.

With the SD_CARD board config, `image/sd_update.img` is not an updater despite
its name: it is a raw image of the declared partition layout — every partition,
rootfs included, at its offset — and the card it is written to becomes the
system the node boots and runs from. The two `*_update.txt` files beside it are
the SDK's U-Boot scripts for the OTHER SD flow, the one that reads them off a
FAT card; on this board config they address an eMMC this board does not have
and they omit the rootfs by construction. Report section 9's D8-F15 has the
whole of it, and Phase B of the board runbook does not match it yet.

The packed medium has a DECLARED size budget (`image.MEDIUM_MAX_BYTES`, 500 MB)
and the build overshoots it, because the medium's size is the partition
cmdline's geometry plus the rootfs image, and the build sizes the rootfs
filesystem to its partition rather than to its contents. To fit:

```sh
# in a Linux container with e2fsprogs (see the script header), from firmware/rv1106
./scripts/shrink-rootfs.sh
# then on the host, from v2/
uv run python -m skyweave2.edge.image pack
```

That resizes the ext4, repacks the medium from the partition images, re-hashes
everything and records both steps in the manifest's `post_build` block, which
is what report section 2.1 prints. The packer refuses to publish a manifest
over budget. `uv run python -m skyweave2.edge.image verify` re-hashes what is on
disk against the manifest, which is runbook J1.

The daemon is NOT baked into the rootfs. Hand-start is the D8.1 arrangement;
`skyweave2.edge.provision` is what does it.

The image pins the Luckfox SDK by commit and records its own provenance in
`/etc/skyweave-build-provenance`. It is `linux/amd64` because the SDK's
toolchain binaries are x86_64 ELF; on Apple Silicon it runs under emulation,
which is slow and is the price of the Mac and the Linux PC running the *same*
image.

## Regenerating the nanopb sources

`proto/skyweave.pb.{c,h}` are generated from `v2/proto/skyweave.proto` +
`skyweave.options` and are checked in, exactly like the host's
`skyweave_pb2.py`. Regenerate with `scripts/regenerate-proto.sh` and commit
the result; test E2 fails if the bytes they produce ever stop matching the
host codec, so drift cannot pass quietly.

## Running

```sh
# replay an injection stream from storage
./build-host/skyweave-edge --inject-file clip.swij --jetson 192.168.1.10 \
    --packet-log sent.hex --stats run.json

# accept one over Ethernet
./build-host/skyweave-edge --inject-listen 5600 --jetson 192.168.1.10

# preload a clip into DDR and loop it for a declared number of frames
./build-host/skyweave-edge --inject-ram clip.swij --ram-loop-frames 120 \
    --ram-loop-pts-stride-ns 400000000 --ram-budget-mb 160 --stats run.json

# the same, paced at 30 fps (an integer nanosecond period, never an fps float)
./build-host/skyweave-edge --inject-ram clip.swij --ram-loop-frames 108000 \
    --ram-loop-pts-stride-ns 400000000 --ram-loop-period-ns 33333333 \
    --stats run.json

# real capture (board build only)
./build-host/skyweave-edge --capture-vi --jetson 192.168.1.10
```

`--help` lists every knob. The detector knobs mirror `DetectorConfig` field
for field; `skyweave2.edge.daemon.daemon_args` builds the argv from a host
config and refuses on any field it does not know, so a new detector knob
cannot be added on one side only.

## The rules this daemon does not break

- **One capture event, one datagram.** Never split, never truncated. An
  event that does not fit is a loud refusal and a counted drop.
- **Nothing is dropped silently.** Components removed by the per-frame cap,
  frames the detector failed, events that could not be encoded: all counted,
  all in `--stats`, and their total rides in the 1 Hz health packet.
- **Capture time is never invented.** Under injection the daemon copies the
  harness's timestamp and its honest `time_sync_error_ms` and touches
  neither. A stream that claims a board clock domain is refused outright.
  *One named exception, and it is narrow:* under `--inject-ram` the daemon
  numbers `frame_seq` continuously across wraps and advances `capture_ts_ns`
  by the harness's **declared** `--ram-loop-pts-stride-ns` once per pass,
  reproducing the harness's own looping feed. It reads no clock, computes no
  timestamp, copies `time_sync_error_ms` untouched, announces the transform
  at every startup, refuses any clip whose frames are not numbered `0..N-1`
  or whose declaration is `OVERRIDDEN`, and records the stride in every run
  record.
- **A RAM loop has no trailer,** so its run ends on a declared frame budget,
  never on a clock — the deterministic counters must not depend on wall time
  (E8 compares them with `==`).
- **No allocation in the frame loop.** Every buffer is taken at startup.
- **No H.264 on the measurement path.** Ever. Standing rule.

## RAM-loop source

`--ram-budget-mb N` is **decimal MB (10^6 B)** and it is a **daemon-only**
budget: the clip arena plus the detector's own allocation total plus the
daemon's fixed buffers. It is not a system total and it does not include the
kernel or the rootfs. The daemon computes the sum at startup, logs every term,
and **refuses** rather than shortening the clip to fit.

The clip lengths the harness derives against the 160 MB default, at 30 fps
with the IVE detector, are 12 / 78 / 174 frames at 2304x1296 / 1536x864 /
1152x648. Two constraints pick them: the total must clear the budget, and
`N * 1e9 / fps` must be a whole number of nanoseconds, or the looped
`capture_ts_ns` stops matching what an unrolled feed would have written.

Every figure above is **derived arithmetic, not a measurement.** And note that
peak RSS on the board will *not* include the IVE detector's share: those
allocations go through `RK_MPI_SYS_MmzAlloc` (MMZ/CMA) and are not in the
process RSS the harness samples. The two numbers are different quantities and
must never be added.

## Driving it from the host (D8.1-prep)

The node has no Python and never will; everything below runs on the host.

```sh
# push the daemon to a node, verify it by hash THERE, run it, collect stats
uv run python -m skyweave2.edge.provision --host 192.168.1.21 \
    --jetson 192.168.1.10 --stream clip.swij --out runs/node0

# the resolution sweep (unpaced: this measures a ceiling)
uv run python -m skyweave2.edge.benchmark sweep --work /tmp/sweep \
    --out runs/sweep.json --source-medium sd-card-class10

# an hour at the operating point (paced, looped scene)
uv run python -m skyweave2.edge.benchmark soak --work /tmp/soak \
    --proc 1536x864 --duration-s 3600 --out runs/soak.json

# E8: two sweeps, same config, against the declared run-to-run bounds
uv run python -m skyweave2.edge.benchmark compare runs/sweep1.json runs/sweep2.json
```

Read finding D8-F8 in `v2/docs/D8_EDGE_REPORT.md` before believing an fps
number from an injected sweep: 30 fps at 2304x1296 is 89.6 MB/s of luma, which
is eight times what the node's 100M link carries, so an injected run can be
measuring the pipe. Every run records the byte rate that fed it for exactly
that reason, and the medium is declared by the operator because the harness
cannot tell an SD card from a tmpfs.

The RAM-loop source (`--inject-ram`) is the sanctioned answer to D8-F8: it
takes the link out of the measurement path entirely, so the clip crosses the
medium once and the detector is fed from DDR. It does not make the DDR traffic
profile match real ISP writes — that remains a declared systematic, recorded
beside every result and folded into no bound.

## Deployment checklist (D8.1, not done here)

The node design's section 11 list, with the parts this daemon covers marked:

- [x] one C daemon, no Python, no OpenCV, no systemd
- [x] binary observations, one UDP datagram per capture event
- [x] a fixed per-frame bound (the cap) so uplink load is scene-independent
- [x] 1 Hz health with real drop counters
- [x] validated against the host oracle by golden frame->packet fixtures
- [~] Buildroot rootfs booting straight into the daemon — the rootfs is built
      and its provenance recorded (`scripts/build-image.sh`), but the daemon is
      hand-started rather than baked in; the amendment allows that until D8.2
- [ ] ISP manual 3A, dual output (full-res ch0 + downscaled ch1)
- [ ] BNO055 over I2C into the health packet
- [ ] hardware watchdog + OTA path
- [ ] (phase 2) RKNN classifier as the noise/bandwidth gate

The unchecked lines need the flashed node and are D8.1's work; the brief
gates them on Samuel confirming the board.
