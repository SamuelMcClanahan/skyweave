# Skyweave — Hitting 30 fps+ on Many Cameras on the Jetson

**The question:** the Rubik Pi MVP topped out near 20 fps; how do we comfortably hold 30 fps+ with many cameras on the Jetson while it ingests motion packets from the edge nodes?

**The one-sentence answer:** the 20 fps ceiling was a *Rubik-Pi-doing-everything* problem, not a *scoring-is-slow* problem — and the final architecture deletes most of that work from the central node, so the Jetson's real limits are three concrete, fixable things (a per-camera dense grid that scales as O(cameras × volume), a single GIL-bound Python loop, and per-frame pydantic), none of which require CUDA to solve.

This study explains where the time actually goes, builds the throughput budget, and ranks the fixes. It complements `docs/CODEBASE_ANALYSIS.md` (architecture/quality) and `docs/CUDA_DETECTOR_DESIGN.md` (the GPU scorer); read this one first, because it shows that **most of the 30-fps-many-cam goal is won on the CPU**, and CUDA is the *last* lever, not the first.

---

## 1. Why the Rubik Pi was at 20 fps — and why that number doesn't transfer

The operator runs one synchronous thread that, every frame, does **all** of this in sequence ([runtime.py:172](../src/skyweave/operator/runtime.py)):

```
grab + retrieve each camera  →  MJPEG/UVC decode  →  BGR→gray
  →  frame-diff motion extraction (per camera)
  →  preview: resize + ChArUco detect + JPEG encode (per camera)
  →  align → score → peaks → kalman
  →  pydantic model_dump of tracks/measurements/volume
  →  sleep the remainder of the 33 ms budget
```

20 fps means the loop took ~50 ms. Crucially, **almost none of that 50 ms is the voxel scorer.** On a Pi-class CPU the dominant costs are the per-pixel, per-camera jobs:

- **MJPEG decode of OV9281 USB frames** — UVC delivers MJPEG; decoding 3× 1280×800 streams is tens of ms of CPU on its own.
- **Grayscale conversion + frame-diff + connected components** for every camera, every frame.
- **Preview encoding** (JPEG + ChArUco detection) sharing the same thread — the spec explicitly says debug video must never block tracking, but here it does ([runtime.py:432](../src/skyweave/operator/runtime.py)).
- **Python/pydantic overhead** sprinkled through all of it.

The benchmark harness already proves the scorer is cheap: `skyweave-benchmark` breaks every stage out by p50/p95 and reports `scoring` as a small share ([benchmark.py:64](../src/skyweave/sim/benchmark.py)). The Pi was I/O- and pixel-bound, not fusion-bound.

**Why this matters for the Jetson:** the final architecture moves *every per-pixel job above the fold line to the edge nodes.* The RV1106s do capture, decode, frame-diff, and motion extraction; the Jetson receives a few-KB `MotionPacket` per camera per frame and only aligns, scores, tracks, and serves. The Jetson's per-camera cost is therefore ~100× lighter than the Pi's was, **and the heaviest work is now parallelized across N physical edge CPUs by construction.** That is the whole point of the thin-edge/fat-center split (`docs/CODEBASE_ANALYSIS.md` §7.1), and it is why "30 fps on many cams" is fundamentally achievable — *if* the central code is shaped to exploit it. The rest of this document is about that "if" — but it depends on a symmetric assumption that deserves its own scrutiny: **that a single RV1106 can actually hold 30 fps of motion extraction.** §7 addresses that head-on.

---

## 2. The Jetson throughput budget

30 fps = **33.3 ms per frame**. Split the central work into what scales with camera count `N` and what doesn't:

| Stage | Cost order | Per-frame at N=30 (target) | Notes |
|---|---|---|---|
| UDP receive + packet decode | O(N) | < 1 ms | ~30 small datagrams; trivial if decoded into SoA, not pydantic (§4) |
| Time alignment | O(N) | < 0.5 ms | watermark/window bookkeeping |
| **Ray construction + scoring** | **O(rays) + O(N × volume)** | **the swing factor** | the O(N × volume) term is the problem — §3 |
| Peak extraction | O(touched voxels) | < 1 ms | sparse, small |
| Triangulation | O(N) | < 0.5 ms | 3×3 solve |
| Kalman + association | O(tracks) | < 0.5 ms | tiny matrices |
| Serialize/record/viz | O(output size) | **can blow the budget** | per-frame pydantic `model_dump` — §4 |

Read the table: the *arithmetic* of fusion is milliseconds even at N=30. The things that can actually miss 33 ms are (a) the O(N × volume) memory term hiding inside "scoring," (b) the per-frame pydantic/serialization, and (c) doing it all in one GIL-locked thread so you only get one of the Jetson's six cores. Those are §3, §4, §5.

---

## 3. The #1 many-camera bottleneck: the per-camera dense grid

This is the single most important finding for your question. The scorer allocates, every frame:

```python
score_by_camera = np.zeros((len(self._camera_ids), *self.grid.dims), dtype=np.float32)
```

— [scorer.py:139](../src/skyweave/rayweave/scorer.py) (and the NumPy backend keeps a separate full grid per camera too, [scorer.py:65](../src/skyweave/rayweave/scorer.py)). The combine step then loops over **every camera** at each touched voxel ([numba_scorer.py:157](../src/skyweave/rayweave/numba_scorer.py)).

The cost scales as **O(cameras × grid volume)**. Concretely, at the room preset 96×96×64 = 589,824 voxels × 4 bytes = **2.36 MB per camera**:

| Cameras | `score_by_camera` size | Combine cost (per touched voxel) |
|---|---|---|
| 3 | 7 MB | loop 3 |
| 10 | 24 MB | loop 10 |
| 30 | 71 MB | loop 30 |
| 50 | 118 MB | loop 50 |

Two things degrade as `N` grows: the per-frame allocation/free churn of an `N×volume` array (allocator pressure, page faults), and the combine loop's **camera-strided memory access** — reading `score_by_camera[c, voxel]` for all `c` jumps `volume×4` bytes between cameras, so cache misses grow linearly with `N`. This is exactly the "works fine at 3 cams, falls over at 30" scaling cliff.

**The fix is pure CPU and removes the camera dimension entirely:** accumulate all cameras into **one** score grid plus **one `uint32` camera-bitmask grid**. Each ray adds its weight to the score grid and ORs its camera bit into the mask grid. Sparsify by keeping voxels where `popcount(mask) >= min_support`:

```
# instead of N grids + a combine loop over cameras:
score[voxel] += weight                 # one grid, all cameras
cam_mask[voxel] |= (1 << camera_slot)  # one uint32 grid, up to 32 cameras
# keep voxel iff popcount(cam_mask[voxel]) >= min_support
```

Now scoring memory is **two grids, independent of camera count** (6 MB total at room resolution, forever), the combine loop over cameras disappears, and support counting is exact and free. This is `O(volume + rays)` instead of `O(N × volume)`. For 32+ cameras use `uint64` (64 cameras); beyond that, partition into camera groups. It is the same bitmask trick the CUDA design proposes (`docs/CUDA_DETECTOR_DESIGN.md` §5.2), but **you get the many-camera win on the CPU today without any GPU.**

(The deeper reformulation — back-projection instead of forward-scatter — also eliminates per-camera grids and is the GPU-native path, but the bitmask change is a few hours and unblocks many-camera scaling immediately.)

---

## 4. The #2 bottleneck: per-frame pydantic and object churn

Per frame, the hot path currently:

- builds up to **5000 pydantic `SparseVoxel` objects** ([scorer.py:255](../src/skyweave/rayweave/scorer.py)) that the peak extractor immediately turns back into numpy ([peaks.py:22](../src/skyweave/rayweave/peaks.py));
- in the operator, does `model_dump(mode="json")` of tracks, measurements, and the full volume every frame, plus `model_copy(update=...)` per packet ([runtime.py:684](../src/skyweave/operator/runtime.py));
- flushes the recorder and logger to disk per write ([recorder.py:81](../src/skyweave/recording/recorder.py), [log.py:28](../src/skyweave/log.py)).

At 30 fps × 30 cameras this is hundreds of thousands of validated-object constructions per second whose only purpose is to be reconverted to arrays — pydantic is ~µs/object versus ~ns/array-element, and the allocation churn defeats the cache. This is the "validate at the boundary, arrays inside" rule (`docs/CODEBASE_ANALYSIS.md` §6.5):

- The aligner emits an **`EvidenceBatch`** (SoA arrays: `cam_slot[], u[], v[], weight[]`), not pydantic packets. The scorer consumes arrays; the Numba backend already builds exactly this internally ([scorer.py:190](../src/skyweave/rayweave/scorer.py)) — promote it to the boundary.
- The scorer returns `(indices: int32[], scores: float32[])`; pydantic `WeavefieldVolume`/`SparseVoxel` are constructed only for the *downsampled* subset that ships to the recorder/viz, off the hot path.
- The recorder gets a bounded queue + writer thread (the spec's async flight recorder, §11.5); the logger stops flushing per line.

This is independent of camera count but it is what keeps "serialize/record/viz" from silently eating the 33 ms budget once N grows.

---

## 5. The #3 bottleneck: one GIL-bound loop on a six-core machine

The operator is a single Python thread; the Jetson Orin has 6× Cortex-A78AE cores. As written, fusion uses ~one of them, and any CPU-bound Python (pydantic, the NumPy backend, preview) serializes against everything else. The standard shape — and the one the spec's distributed posture implies — is **process-per-concern with queues** (`docs/CODEBASE_ANALYSIS.md` §6.6, §7.6):

- **ingest** process(es): UDP sockets → decode → per-source ring buffers (shared memory). Network-bound, cheap.
- **fusion** process: aligner → scorer → peaks → tracker (owns the GPU if/when CUDA lands). Realtime priority, zero disk I/O.
- **ops** process: recorder, health, viz/HTTP server, preview/debug-video. Best-effort; allowed to lag; cannot stall tracking.

Two things make this concretely faster on the Jetson:

1. **Numba releases the GIL.** `@njit(nogil=True)` lets the scorer (and a thread-pool of per-source ray builders) run truly parallel. The codebase already demonstrates the pattern — `check_parallel` uses a `ThreadPoolExecutor` over motion sources ([live_benchmark.py:9](../src/skyweave/camera/live_benchmark.py)) — so the team has the idiom; it just isn't applied to the operator's fusion path.
2. **Decouple ingest rate from fusion rate.** With async UDP ingest into ring buffers, a slow frame doesn't back-pressure the cameras; the stateful watermark aligner (which must replace the current stateless one, `docs/CODEBASE_ANALYSIS.md` §3.7) closes windows on time and drops stragglers instead of blocking. This is what lets the Jetson hold 30 fps even when individual edge nodes jitter.

---

## 6. Do we even need CUDA to hit 30 fps on many cams?

**For the fusion work: almost certainly not.** With §3 (bitmask, camera-count-independent scoring), §4 (SoA, no per-frame pydantic), and §5 (multiprocess + nogil), the central arithmetic for dozens of cameras is single-digit milliseconds — because the per-pixel work lives on the edge nodes, and what remains (a few thousand rays into one grid, a sparse peak, a Kalman step) is genuinely small. The benchmark already shows scoring is a minor share even unoptimized.

**CUDA earns its place at the *next* scale, not this one:**

- hundreds of edge nodes, or much finer/larger grids (sky-scale);
- temporal track-before-detect integration over many frames (the Weavefield buffer becomes the working set);
- **sharing the GPU with the 100 fps turret YOLO** — once the turret detector is GPU-resident, putting the scorer on the GPU too lets them interleave instead of fighting for CPU cores.

So the honest sequence for *your* stated goal is: **fix the three CPU bottlenecks, benchmark on the actual Orin, and only reach for the CUDA scorer when the profiler says the CPU path is the wall** — consistent with the README's own "benchmark target hardware before optimizing" and with `docs/CUDA_DETECTOR_DESIGN.md` §12. The CUDA work is real and worth doing for the sky-scale endgame; it is not the lever that gets you from 20 fps on a Pi to 30 fps on many cams on a Jetson.

---

## 7. Can the RV1106 edge node actually feed 30 fps?

The whole thin-edge bet collapses if a single RV1106 can't hold 30 fps of motion extraction. The honest answer: **yes — but only if it is programmed like the camera SoC it is, not like a tiny Linux PC running the Python pipeline.**

**The RV1106 is purpose-built for this.** It is a Rockchip IP-camera SoC; Rockchip's design target for it was "capture a multi-megapixel sensor and H.265-encode it at 30 fps in real time on one Cortex-A7 plus hardware blocks." Capturing the SC3336 at 30 fps over MIPI-CSI is the chip's *core competency*, not a stretch. Motion extraction is added work on top of a capture path that is already hardware.

**The Rubik Pi comparison argues *for* feasibility.** The Pi's 20 fps was dominated by jobs the RV1106 simply does not have: USB **MJPEG decode** (the big one — CSI capture is raw, zero decode), preview JPEG encoding, pydantic, Python interpreter overhead, and *three cameras' worth* of it in one process. The RV1106 handles one camera, raw-captured, no preview, no fusion. The A7 is a much weaker core than a Rubik-Pi core, but the job is a small fraction of the size and most of it is offloaded to fixed-function hardware.

**The cost budget.** Frame-diff + threshold on a 3 MP frame is ~90 Mpix/s — memory-bound, not compute-bound. On NEON (16 uint8/instruction, `vabdq_u8` + `vcgeq_u8`) it is a couple percent of the A7's cycles. Connected components is cheap because the thresholded foreground is *sparse* (a few small blobs). **The binding resource is DDR2L memory bandwidth, not FLOPS** — 256 MB DDR2L at ~1–2 GB/s is shared by capture DMA, diff reads, and the encoder — and **256 MB total RAM** means you cannot hoard frames.

**What it requires (non-negotiable):**

- **ISP for capture and downscale, not the A7.** The ISP delivers NV12; the **Y plane *is* the grayscale image** — zero conversion cost (versus `cvtColor` per frame in the current path). The ISP can also emit a downscaled stream for detection.
- **C + NEON, not Python/OpenCV.** The current pure-Python BFS connected-components fallback ([motion.py:357](../src/skyweave/camera/motion.py)) would be catastrophic on an A7. The edge is a separate, tight C program; the Python motion extractor is its **reference/oracle**, validated against it with golden fixtures (`docs/CODEBASE_ANALYSIS.md` §7.2).
- **Detect coarse, sample fine.** Run detection at quarter-res (e.g. 1152×648) to cut per-pixel cost and bandwidth 4×, then crop patches from the full-res Y plane only where blobs are found. Bearing precision stays high exactly where it matters.
- **Hardware VENC for the debug-video stream**, in parallel, at zero A7 cost — never on the measurement path.

**The real risks, in order:** (1) DDR2L bandwidth contention between capture, diff, and encode; (2) sustained thermals in a sealed waterproof enclosure running A7+ISP+VENC continuously; (3) the single core — no SMP, so if diff + components + patch-encode + UDP + housekeeping exceed one core's budget there is no second core to spill onto. The **network is not a risk**: a ~2–6 KB `MotionPacket` at 30 fps is ~1 Mbit/s, trivial on the node's 100M link (`docs/CODEBASE_ANALYSIS.md` §7.3).

**The fallback ladder** if a single A7 cannot hold full-res 30 fps: drop detection resolution → drop framerate → push more onto the ISP/NPU. So the failure mode is graceful degradation, not a wall — and the RV1106's 0.5-TOPS NPU is a future home for a tiny patch classifier without changing the architecture.

**Bottom line:** feasible and well-matched to the silicon, but it is the part of the system with the *least* headroom and the most demanding embedded engineering. Prototype it **first** — one node at full-res 30 fps, measuring A7 utilization, DDR bandwidth, and enclosure temperature — before committing to 3 MP @ 30 fps. It is emphatically not a "run the Python stack on the edge" job.

---

## 8. The plan, ranked by fps-per-effort

| # | Change | fps impact at many cameras | Effort |
|---|---|---|---|
| 1 | **Bitmask support: one score grid + one `uint32` mask, drop the per-camera grids** (§3) | Removes the O(cameras × volume) term — the actual many-cam cliff | Low |
| 2 | **`EvidenceBatch` SoA in, raw arrays out; pydantic only at recorder/viz boundary** (§4) | Deletes hundreds of K object-constructions/s; keeps serialize off the hot path | Medium |
| 3 | **Async UDP ingest + stateful watermark aligner; decouple ingest from fusion** (§5) | Stops edge jitter and slow frames from capping the loop | Medium |
| 4 | **Process split (ingest / fusion / ops) + `@njit(nogil=True)`** (§5) | Uses all six Orin cores instead of one; isolates preview/disk from tracking | Medium |
| 5 | **Async flight recorder + non-flushing logger** (§4) | Removes disk stalls from the frame budget | Low |
| 6 | **Move preview/JPEG/ChArUco off the fusion thread** (§1) | Reclaims the per-camera preview cost the Pi loop paid inline | Low |
| 7 | **Instrument the live path end-to-end on the Orin** (extend `skyweave-benchmark`) | Tells you which of the above actually bind before you optimize further | Low |
| 8 | CUDA back-projection scorer behind `ScorerBackend` | Only after 1–7 and a profile prove the CPU path is the wall; scales to sky/turret | High |

**Bottom line:** the 20-fps number was the Rubik Pi carrying the whole per-pixel pipeline on one weak CPU. The Jetson, in the edge-node architecture, barely touches pixels — so 30 fps+ on many cameras is an architecture problem with three CPU-side answers (kill the per-camera grid, get pydantic off the hot loop, use more than one core), all cheaper and higher-leverage than a GPU rewrite. Do those, measure on the real Orin, and bring in CUDA for the sky-scale and turret-sharing future rather than for this milestone.
