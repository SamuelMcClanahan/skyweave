# Skyweave — RV1106 Edge Node: Minimal, Performant Design

**Goal:** a single RV1106 node that captures an SC3336 at 30 fps, finds motion, and streams full-resolution patches + centroids to the Jetson over 100M Ethernet — within 256 MB of DDR2L and one Cortex-A7 core, with no H.264 on the measurement path.

**The governing principle, stated once:** on this SoC the A7 is the *weakest* resource and the fixed-function blocks (ISP, RGA, NPU, VENC, MAC-DMA) are the *strong* ones. A minimal, performant node is therefore one where **the A7 touches as few pixels as possible** and every bulk pixel operation — capture, format conversion, downscale, crop — runs on dedicated hardware via DMA-buffer handoff, never `memcpy`. Everything below follows from that.

This expands §7 of `docs/THROUGHPUT_30FPS_JETSON.md`. It complements the patch-streaming bandwidth math and NPU role discussed there.

---

## 1. The three constraints that dictate the design

| Constraint | Value | Consequence |
|---|---|---|
| **RAM** | 256 MB DDR2L (shared by OS, frame buffers, NPU) | Cannot hoard frames; must pass DMA-buf handles, not copies; no Python/heap-heavy runtime |
| **CPU** | 1× Cortex-A7 @ ~1.2 GHz, in-order, NEON, **no SMP** | If the loop exceeds one core, there is no second core — offload to hardware blocks |
| **Uplink** | 100M Ethernet (~90 Mbit/s usable) | Cannot send frames (716 Mbit/s raw); send bounded motion patches only |
| **DDR bandwidth** | DDR2L, order ~1 GB/s, **shared** | The *real* ceiling — minimize how many times each frame is read/written |

The non-obvious one is the last: at these data rates **the A7's compute is nearly free; DDR bandwidth is the binding resource.** §6 shows the budget. Optimization on this node means *touching the frame fewer times*, not "faster code."

---

## 2. Use the silicon, not the CPU — block inventory

The RV1106 is a Rockchip IP-camera SoC; it has hardware for exactly this pipeline. The minimal node is a thin orchestrator over these blocks:

| Block | Library | Job on this node | A7 cost |
|---|---|---|---|
| **VICAP + ISP (RKISP)** | RKMPI (`RK_MPI_VI`) | MIPI-CSI capture, demosaic, **manual** 3A, emit NV12; multi-channel output (full-res + downscaled) | ~0 (DMA) |
| **RGA 2D accelerator** | `librga` | Crop full-res patches around blobs; downscale; format/copy into send buffer | ~0 (DMA) |
| **NPU 0.5-TOPS INT8** | `librknn` | (Phase 2) classify small candidate crops: target vs bird/cloud/noise | ~0 (NPU) |
| **VENC H.264/265** | RKMPI (`RK_MPI_VENC`) | Optional debug-video stream **only** — never the measurement path | ~0 (HW) |
| **A7 + NEON** | C + intrinsics | Frame-diff + threshold + sparse connected-components + packetize | the only real CPU work |
| **MAC + DMA** | sockets | UDP datagram out | ~0 (DMA) |
| **I2C** | kernel | Read BNO055 IMU orientation | negligible |

The single most important architectural choice: **the A7 only ever reads the *downscaled* Y plane for detection.** It never touches the full-resolution frame. Full-res pixels flow ISP → (stay in DMA) → RGA-crops-only-the-blob-regions → send buffer, entirely in hardware. That is what makes a 3 MP/30 fps node fit on one A7.

---

## 3. The dataflow (zero-copy, dual-stream)

```
SC3336 ──MIPI-CSI──► VICAP/ISP ──┬──► ch0: full-res NV12  (stays in DMA-buf)
   (manual 3A)                   │                        └──► RGA crop(blob bbox) ──► patch buf ──┐
                                 │                                                                  │
                                 └──► ch1: ¼-res NV12 ──► mmap Y plane ──► A7/NEON                  │
                                                                              │ frame-diff           │
                                                                              │ threshold            │
                                                                              │ sparse CC → blobs    │
                                                                              ▼                       ▼
                                                                         centroids + bboxes ──► packetize ──► UDP ──► Jetson
                                                                              │
                                                                         BNO055 (I2C) ──► orientation field
```

Key properties:
- **Two ISP output channels** (Rockchip ISP supports main + self path): full-res for patches, ¼-res for detection. No A7 downscale.
- **DMA-buf fd handoff**, not copies: the full-res frame is referenced by handle; RGA reads it directly; the A7 `mmap`s only the small ¼-res Y plane (cached, with explicit invalidate before read).
- **RGA crops only blob regions** of the full-res frame — bytes proportional to motion, not to frame size.
- The full-res frame is **never** read in its entirety by the A7 and never crosses the language/heap boundary.

---

## 4. The processing loop

Single process, driven by the ISP frame-ready event (`select()`/`poll()` on the VI channel fd). One thread is sufficient and preferable — no SMP to exploit, and threads would just add context-switch and lock cost.

```
loop (per frame, ~33 ms budget):
  1. dequeue ch1 (¼-res) + ch0 (full-res) DMA-buf handles      [ISP, ~0 A7]
  2. invalidate-cache + read ch1.Y                              [A7, cheap]
  3. NEON: diff = |Y - Y_prev|; mask = diff >= T                [A7, NEON, ~1% core]
  4. sparse connected-components on mask -> blobs (bounded)     [A7, cheap]
  5. (phase 2) RKNN classify each blob crop -> keep/drop        [NPU, ~0 A7]
  6. for kept blobs (confidence-ranked, within byte budget):
        RGA crop full-res ch0 at scaled bbox -> patch buffer    [RGA, ~0 A7]
        RLE/raw encode patch                                    [A7, bounded]
  7. read BNO055 quaternion (I2C, low-rate cached)              [~0]
  8. build binary MotionPacket; sendto() one UDP datagram       [MAC DMA]
  9. swap Y_prev = ch1.Y; release DMA-buf handles
```

No allocation in the loop — every buffer is preallocated at startup (§6). The loop is `SCHED_FIFO` real-time priority so housekeeping never preempts a frame.

---

## 5. The software stack — what to ship and what to refuse

**Ship:** Buildroot rootfs (the Luckfox/Rockchip SDK default) + kernel with rkisp/rga/rknn drivers + BusyBox + **one statically-or-minimally-linked C/C++ daemon** that links RKMPI, librga, and (phase 2) librknn. Boot straight into the daemon. That is the entire userspace.

**Refuse:**
- **No Python.** The interpreter + numpy on a 256 MB A7 is a non-starter; the current pure-Python connected-components fallback ([motion.py:357](../src/skyweave/camera/motion.py)) would be catastrophic here.
- **No OpenCV.** Everything you'd use it for (resize, crop, cvtColor, morphology) is done faster by RGA/ISP/NEON; OpenCV is megabytes of binary and DDR you don't have.
- **No general distro / systemd.** Buildroot, not Debian. Init = the daemon + a watchdog.

**The relationship to the existing Python code:** the C daemon is a *reimplementation of a frozen contract*, not a port. The Python `FrameDiffMotionPacketBuilder` ([camera/motion.py](../src/skyweave/camera/motion.py)) becomes the **reference oracle**; the C daemon is validated against it with **golden fixtures** — recorded raw frame sequences in, expected packet bytes out — exactly the oracle pattern the scorer backends already use (`docs/CODEBASE_ANALYSIS.md` §7.2). This is how you keep firmware and server provably in sync without sharing code.

---

## 6. The budgets that prove it fits

**Memory (of 256 MB):**

| Item | Size | Notes |
|---|---|---|
| Kernel + Buildroot rootfs | ~30–50 MB | minimal |
| Full-res NV12 ring (ch0), 3–4 buffers | ~18 MB | 2304×1296×1.5 = 4.48 MB each |
| ¼-res NV12 (ch1) + Y_prev | ~5 MB | detection stream |
| RGA scratch + patch/send buffers | ~5 MB | bounded by byte budget |
| **Subtotal (no NPU)** | **~60–80 MB** | very comfortable |
| RKNN runtime + INT8 model + tensors (phase 2) | ~40–80 MB | the variable; budget it |
| **Subtotal (with NPU)** | **~120–160 MB** | still fits with margin |

**A7 compute** (the loop's only CPU work): frame-diff + threshold on the ¼-res Y (1152×648 ≈ 0.75 MP) is ~2 ops/pixel; NEON does 16 uint8/instruction (`vabdq_u8`, `vcgeq_u8`), so ~94K NEON instructions/frame × 30 fps ≈ **<1% of the core.** Connected-components and RLE are bounded by `max_components`/`max_motion_pixels` and are cheap. **The A7 is mostly idle** — which is the goal.

**DDR2L bandwidth** (the actual ceiling, ~1 GB/s shared):

| Traffic | Rate | Source |
|---|---|---|
| ISP write full-res NV12 | ~134 MB/s | 4.48 MB × 30 |
| ISP write ¼-res NV12 | ~33 MB/s | self-path |
| A7 read ¼-res Y (cur+prev) | ~45 MB/s | diff |
| RGA read full-res (crops only) | small | motion-proportional |
| MAC DMA out | ~0.1–4 MB/s | patches |
| **Total** | **~215 MB/s ≈ ~20% of bus** | comfortable |

The decisive line: **detecting on the ¼-res stream keeps DDR headroom.** Running the diff on the full 3 MP frame would add ~270 MB/s and push you past 50% — the difference between a comfortable node and a saturated one. This is why §2's "A7 only reads the downscaled Y" rule is the core performance lever, not any code micro-optimization.

---

## 7. ISP configuration — lock the 3A

Run the ISP in **manual exposure/gain/white-balance**, not auto. Auto-exposure turns any global brightness change (a cloud, the sun moving) into a full-frame "motion" event, which simultaneously corrupts detection *and* floods the uplink (every pixel "changed"). Fixed 3A is both a correctness and a bandwidth requirement (the spec flags this at §6.2). Expose for the sky/scene once at deploy time; revisit only on large ambient changes. The BNO055 sun-angle could later drive a slow exposure schedule, but keep it out of the realtime loop.

---

## 8. The bandwidth governor (hard ceiling, graceful degradation)

Because patch bandwidth is set by *spurious motion*, not by the target, the node needs a fixed per-frame byte budget it cannot exceed:

- **Always send centroids + bboxes** — a few bytes each, the tracking-critical evidence, never dropped.
- **Fill the remaining byte budget with patches in descending blob confidence**, drop the rest. A noisy frame degrades to "centroids + the few best patches," never a flood.
- The existing caps (`max_components=8`, `max_motion_pixels=225`, [motion.py:31](../src/skyweave/camera/motion.py)) give the worst-case ceiling; make the byte budget an explicit config so it can be tuned to the switch→Jetson aggregate (`docs/THROUGHPUT_30FPS_JETSON.md` §7).
- One frame = one UDP datagram, ≤ MTU where possible (`docs/CODEBASE_ANALYSIS.md` §7.3) to avoid fragmentation on a lossy link.

This makes uplink load **bounded and predictable regardless of scene**, which is what lets you provision the aggregation switch deterministically.

---

## 9. The NPU — appearance gate, phase 2

The right use of the 0.5-TOPS NPU is *not* full-frame detection (it can't do YOLO on 3 MP at 30 fps). It is the hybrid pattern:

1. Cheap proposal on the A7 (frame-diff → candidate regions).
2. NPU classifies each small crop (32×32 / 64×64): drone / bird / cloud / noise.

This is the appearance-based "is this real?" gate that frame-diff fundamentally cannot do (a bird and a drone *move* alike but *look* different), and by suppressing junk crops at the source it doubles as a **bandwidth filter**. A handful of small crops/frame is trivially within 0.5 TOPS.

Caveat, hence phase 2: the **RKNN toolchain on RV1106 clone boards is finicky**, the NPU is small, and INT8 quantization needs accuracy validation. Ship classical frame-diff + the §8 byte budget first (which already works bandwidth-wise); add the NPU classifier as the noise-rejection upgrade once the pipeline is real. Interim option: send a tiny crop centrally and classify on the Jetson, then push the proven model to the edge.

---

## 10. Timestamps, IMU, and fleet reliability

- **Timestamp at the kernel.** Use the V4L2/VI buffer timestamp (capture time), not userspace send time — removes the largest jitter term for free. Discipline the clock with chrony/PTP over the LAN; stamp `capture_ts_ns` + `time_sync_error_ms` honestly so the Jetson can finally consume them (`docs/CODEBASE_ANALYSIS.md` §3.7, §7.8). The RV1106 can't do precise hardware PTP stamping on its own; software PTP/chrony (~1 ms) is the near-term answer, an STM32/PPS trigger the long-term one.
- **IMU:** read the BNO055 quaternion over I2C at its native low rate (cached between reads); attach to the health packet as a weak orientation prior for calibration, not the realtime measurement.
- **Fleet hygiene** (these are deployed, sealed nodes): enable the **hardware watchdog**; auto-restart the daemon and re-init the CSI on sensor error; emit a **1 Hz health packet** (fps, drop count, DDR/thermal if available, sync error, IMU); and provide a **remote update path** (A/B partition or atomic binary replace over the management network) — a sealed waterproof node you can't reflash by hand needs OTA from day one.

---

## 11. Minimal viable node — checklist

The smallest thing that is both correct and performant:

- [ ] Buildroot rootfs, boot straight into one C daemon; no Python, no OpenCV, no systemd.
- [ ] ISP: manual 3A, dual output (full-res ch0 + ¼-res ch1), DMA-buf handles.
- [ ] A7 reads **only** ch1.Y; NEON frame-diff + threshold + sparse CC → blobs.
- [ ] RGA crops full-res patches at blob bboxes; no A7 full-res touch; no per-loop malloc.
- [ ] Binary `MotionPacket` (centroids always, patches by confidence within byte budget), one UDP datagram, kernel capture timestamp.
- [ ] BNO055 over I2C → orientation in health packet; watchdog + 1 Hz health + OTA path.
- [ ] Validated against the Python motion extractor via golden frame→packet fixtures.
- [ ] (Phase 2) RKNN classifier on candidate crops as the noise/bandwidth gate.

**Bottom line:** an RV1106 holding 30 fps of full-res-patch streaming is realistic and well-matched to the silicon — *provided* the node is built as a thin C orchestrator of ISP/RGA/NPU/NEON with DMA-buf handoff, detection on the downscaled stream, and a fixed bandwidth governor. The A7 stays near-idle; DDR bandwidth (~20% used) is the real budget and it has margin; RAM sits comfortably under 256 MB. The engineering risk is entirely in the embedded plumbing (Rockchip MPI, dma-buf, RKNN), not in whether the chip is fast enough — it is. De-risk by building one node to this checklist and measuring DDR bandwidth and A7 utilization on real hardware before fanning out.
