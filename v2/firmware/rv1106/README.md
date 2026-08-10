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
- **No allocation in the frame loop.** Every buffer is taken at startup.
- **No H.264 on the measurement path.** Ever. Standing rule.

## Deployment checklist (D8.1, not done here)

The node design's section 11 list, with the parts this daemon covers marked:

- [x] one C daemon, no Python, no OpenCV, no systemd
- [x] binary observations, one UDP datagram per capture event
- [x] a fixed per-frame bound (the cap) so uplink load is scene-independent
- [x] 1 Hz health with real drop counters
- [x] validated against the host oracle by golden frame->packet fixtures
- [ ] Buildroot rootfs booting straight into the daemon
- [ ] ISP manual 3A, dual output (full-res ch0 + downscaled ch1)
- [ ] BNO055 over I2C into the health packet
- [ ] hardware watchdog + OTA path
- [ ] (phase 2) RKNN classifier as the noise/bandwidth gate

The unchecked lines need the flashed node and are D8.1's work; the brief
gates them on Samuel confirming the board.
