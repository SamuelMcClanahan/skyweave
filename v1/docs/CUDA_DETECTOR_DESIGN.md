# Skyweave — C++/CUDA Core Detector: Design & Migration Study

**Scope.** How to build an optimized, *elegant* C++/CUDA implementation of the core detector — the Rayweave scoring engine plus peak extraction — what it should look like, and exactly what must change in the existing Python codebase to make it a clean drop-in. This builds on `docs/CODEBASE_ANALYSIS.md`; read §5 (voxel-scoring audit) and §6–7 (code quality + scaling) there first, because several conclusions here depend on them.

**Companion fact this document keeps returning to:** the final architecture is *RV1106 edge nodes sending motion patches over Ethernet to a Jetson*. The CUDA detector runs **only on the Jetson**, consuming `MotionPacket`s (or their decoded SoA form), never frames. So "the detector" we are porting is the central scorer, not the edge motion extractor — the edge stays C-on-NEON (`docs/CODEBASE_ANALYSIS.md` §7.2). Every design choice below is made for an integrated-GPU Ampere part (Orin Nano Super, compute capability 8.7, ~1024 CUDA cores, ~102 GB/s shared LPDDR5), which differs from desktop CUDA folklore in ways that matter (§6.4, §9).

---

## Table of contents

1. [What exactly we are porting](#1-what-exactly-we-are-porting)
2. [The ancestor and the current algorithm](#2-the-ancestor-and-the-current-algorithm)
3. [The central decision: scatter vs. gather](#3-the-central-decision-scatter-vs-gather)
4. [Recommended formulation: sparse coarse-to-fine back-projection](#4-recommended-formulation-sparse-coarse-to-fine-back-projection)
5. [Kernel designs](#5-kernel-designs)
6. [Memory architecture and data layout](#6-memory-architecture-and-data-layout)
7. [Determinism, reproducibility, and the atomics problem](#7-determinism-reproducibility-and-the-atomics-problem)
8. [The C++ side: how to make it elegant](#8-the-c-side-how-to-make-it-elegant)
9. [Jetson-specific optimization](#9-jetson-specific-optimization)
10. [What must change in the Python codebase](#10-what-must-change-in-the-python-codebase)
11. [Libraries to lean on (don't hand-roll)](#11-libraries-to-lean-on-dont-hand-roll)
12. [Performance expectations and when it's worth it](#12-performance-expectations-and-when-its-worth-it)
13. [Staged implementation plan](#13-staged-implementation-plan)

---

## 1. What exactly we are porting

Draw the boundary deliberately, because "rewrite the detector in CUDA" is under-specified and the wrong boundary produces a kernel that spends all its time on launch overhead and transfers.

**Inside the GPU boundary (the hot, parallel, scalable work):**

- Ray construction from evidence pixels (back-project `(u,v)` through `K`, undistort, rotate to world) — [numba_scorer.py:40](../src/skyweave/rayweave/numba_scorer.py).
- The scoring accumulation (DDA traversal *or* back-projection) into a voxel field — [numba_scorer.py:106](../src/skyweave/rayweave/numba_scorer.py).
- Cross-camera combination + support gating — [numba_scorer.py:157](../src/skyweave/rayweave/numba_scorer.py).
- Temporal decay accumulation (the 4D Weavefield that does not yet exist — `docs/CODEBASE_ANALYSIS.md` §5-A7).
- Peak extraction: threshold → connected components → soft-argmax — [peaks.py:18](../src/skyweave/rayweave/peaks.py).

**Outside the GPU boundary (small, sequential, branchy — keep on the CPU):**

- Time alignment / watermarking (control logic, not arithmetic).
- Triangulation refinement (a 3×3 solve — trivial on CPU; copy out a handful of candidates).
- Kalman filter + association (tiny matrices; launch latency would exceed compute).
- All pydantic / serialization / recording / viz.

This is the standard "GPU does the wide arithmetic, CPU does the decisions" split. The single Protocol that makes the swap clean already exists: `ScorerBackend` ([scorer.py:29](../src/skyweave/rayweave/scorer.py)). The CUDA detector is a third implementation behind it, exactly as `NumbaScorerBackend` is the second.

**The function signature we are implementing** (conceptually):

```
score(EvidenceBatch, CameraParams, GridSpec, WeavefieldState)
    -> (sparse_voxels[], peaks[], updated WeavefieldState)
```

where `EvidenceBatch` is the structure-of-arrays form recommended in `docs/CODEBASE_ANALYSIS.md` §6.5 — not a list of pydantic packets. That prerequisite (§10) is what makes the boundary a single `memcpy`/`frombuffer`, not a marshalling exercise.

---

## 2. The ancestor and the current algorithm

It is worth being concrete about lineage, because the CUDA version should *not* resurrect the prototype's mistakes.

**The reference prototype** ([reference/pixel-to-voxel-projector/ray_voxel.cpp](../reference/pixel-to-voxel-projector/ray_voxel.cpp)) is a single-threaded CPU program: it loads frames, frame-diffs, builds rays from **Euler yaw/pitch/roll + FOV** (`rotation_matrix_yaw_pitch_roll`, line 84; `focal_len = w/2 / tan(fov/2)`, line 491), DDA-walks a **hardcoded 500³ grid at 6 m voxels centered at z=500** (line 434), and accumulates `diff` with an optional distance attenuation that the author's own comment admits is wrong (line 526). It is a forward-scatter accumulator with no multi-camera consensus — every ray just adds.

**The current Numba scorer** is the cleaned-up descendant: it replaces Euler+FOV with a proper pinhole `K` and `T_world_cam` rotation ([numba_scorer.py:34](../src/skyweave/rayweave/numba_scorer.py)), keeps the Amanatides–Woo DDA, and — crucially — adds the thing the prototype lacked: **per-camera grids with a support-count gate** ([numba_scorer.py:157](../src/skyweave/rayweave/numba_scorer.py)) so a voxel only survives if ≥K cameras illuminate it. It is still forward-scatter and still additive.

So the CUDA port inherits an algorithm that is (a) forward-scatter, (b) additive, (c) per-camera-grid-then-AND-gate. Sections 3–4 argue that the port is the right moment to change (a) and (b), because the GPU rewards a different formulation *and* that formulation is the one the math audit independently recommended. Two birds.

---

## 3. The central decision: scatter vs. gather

This is the architecturally load-bearing choice. Everything else (memory, occupancy, determinism) follows from it.

### 3.1 Forward scatter (faithful port)

**One GPU thread per ray.** Each thread does ray–AABB, then DDA-walks the grid, doing `atomicAdd` into the voxel field at each step.

Why the GPU dislikes this:

- **Warp divergence from variable ray length.** Threads in a warp execute in lockstep; a warp finishes only when its *longest* ray finishes. Ray lengths here span ~3 to ~300 voxels (corner-grazer vs. body-diagonal). A warp with 31 short rays and 1 long ray runs at the speed of the long one — up to ~30× waste in the tail. This is intrinsic to DDA, not fixable by tuning.
- **Atomic contention at convergence.** The *entire point* of the algorithm is that rays converge on the target voxel — which means many threads `atomicAdd` the same address simultaneously, and atomics to a contended address serialize. The hotspot is exactly where the work concentrates. (Modern L2 atomics are far faster than the Kepler-era reputation, but contention on a single cell still hurts.)
- **Uncoalesced writes.** Consecutive threads (rays) write scattered voxel addresses; no memory coalescing, so every step is a near-cache-line-granularity transaction against the bandwidth-bound integrated memory (§9).

Why it is nonetheless *viable*: work is proportional to evidence (sparse — a few hundred to a few thousand rays/frame at current data sizes), and a faithful port preserves bit-for-bit comparability with the Numba oracle (modulo float-atomic ordering, §7). For the MVP data regime, a forward-scatter kernel may simply *be fast enough*, and it is the lowest-risk first port. **Do not dismiss it; benchmark it.**

### 3.2 Back-projection gather (visual-hull / plane-sweep direction)

**One GPU thread per candidate voxel.** Each thread projects its voxel center into every camera, samples that camera's motion mask (resident in texture memory), combines the per-camera evidence, and writes its own single result.

Why the GPU loves this:

- **No atomics.** Each thread owns one output cell — pure scatter-free writes.
- **Uniform work.** Every thread loops over the same N cameras; near-zero divergence.
- **Coalesced everything.** Neighboring threads → neighboring voxels → neighboring output addresses; mask reads go through the texture cache, which is built for exactly this 2D-locality sampling pattern.
- **"Seen by K of N" is free.** Support counting is a per-thread integer, no separate combine pass.

Why it is not a free lunch: cost is proportional to **voxel count**, not evidence. Running it dense over a 96³ grid is ~885k threads doing camera projections whether or not anything is there — wasteful. It is only efficient when the voxel set is *small*, which forces the coarse-to-fine discipline of §4. This is exactly how **plane-sweep stereo** and **GPU visual hull** work, and why those methods always operate over a bounded sweep volume, never the whole world.

### 3.3 The verdict

The math audit (`docs/CODEBASE_ANALYSIS.md` §5-A2) already concluded back-projection + log-odds is the *statistically* better fusion rule (genuine consensus, negative evidence, no ray-smear bias). The GPU analysis concludes it is also the *computationally* better fit. When the correctness argument and the hardware argument point the same way, that is the formulation to build. **But** it requires the candidate-region machinery of §4 to be affordable. So:

- **Port forward-scatter first** as the oracle-matching baseline and risk-reducer (it reuses the exact algorithm, so parity testing is straightforward).
- **Ship back-projection** as the production kernel, over local candidate grids.

---

## 4. Recommended formulation: sparse coarse-to-fine back-projection

The elegant version exploits a fact the current code ignores: **the problem is sparse in both evidence and space.** There are few rays and one (or few) small regions where they agree. Spending a dense grid on this is the original sin. The pipeline:

```
EvidenceBatch (rays, SoA)
   │
   ▼  Stage 0 — candidate generation (cheap, coarse)
   │   either: coarse back-projection on a low-res grid (e.g. 24³)
   │   or:     pairwise ray closest-points (triangulation-style) → seed points
   │   → a handful of candidate centers
   ▼  Stage 1 — local refinement (back-projection, fine)
   │   for each candidate: a small local grid (e.g. 16³–32³) at fine voxel size,
   │   one thread per local voxel, project into all cameras, log-odds + support
   ▼  Stage 2 — temporal accumulation
   │   decay-add this frame's local field into the persistent Weavefield buffer
   ▼  Stage 3 — peak extraction (on the small touched set)
   │   block-reduce argmax + soft-argmax centroid + geometry-derived covariance
   ▼  measurement(s) → CPU (Kalman/association)
```

This is the structure the architecture-review note sketched ("detect → rays → candidate → local grid → refine") and the one large-scale systems converge on. Its properties:

- **Work is bounded by candidates, not by world volume** — the only way to ever reach sky scale (`docs/CODEBASE_ANALYSIS.md` §5-A1/A8).
- **Each stage is a clean kernel** with uniform work — no divergence, no atomics in the hot Stage 1.
- **It naturally hosts inverse-depth bins** for far targets: Stage 1's "local grid" can be a `(az, el, 1/depth)` slab per camera cluster instead of metric XYZ, without changing the kernel shape.
- **It composes with the turret:** a turret YOLO detection is just another candidate seed for Stage 1.

For the MVP/room regime where a single 96³ grid is cheap, you can collapse Stages 0–1 into one full-grid back-projection and still be fine; the staged form is what unlocks scale.

---

## 5. Kernel designs

Illustrative sketches (not compile-ready), chosen to show the *shape* and the two non-obvious tricks.

### 5.1 Shared device math (one source of truth)

Mark the math `__host__ __device__` so the *same* code compiles into the CPU oracle build and the CUDA build. This is how you guarantee the backends agree and avoid the current situation where `geom.py` and `numba_scorer.py` re-derive the same ray math twice.

```cpp
// rayweave_math.cuh  — compiled for host AND device
struct Vec3 { float x, y, z; };

__host__ __device__ inline Vec3 ray_dir_world(
    float u, float v, const CamIntrinsics& k, const Mat3& R)
{
    // undistortion folded in via precomputed LUT lookup before this call (§10),
    // or analytic Brown-Conrady here if preferred.
    float x = (u - k.cx) / k.fx;
    float y = (v - k.cy) / k.fy;
    float inv = rsqrtf(x*x + y*y + 1.f);           // device fast inverse sqrt
    Vec3 d_cam { x*inv, y*inv, inv };
    return normalize(R * d_cam);                    // R is T_world_cam rotation
}
```

### 5.2 Forward-scatter kernel (baseline) — with the bitmask support trick

The current CPU code keeps **one full grid per camera** ([scorer.py:139](../src/skyweave/rayweave/scorer.py)) just to count support, costing `n_cameras × grid` memory. On the GPU there is a much tighter idiom: **one score grid + one `uint32` camera-bitmask grid.** Each ray `atomicOr`s its camera bit and `atomicAdd`s its weight; the combine pass keeps a voxel iff `__popc(bitmask) >= min_support`. This halves-to-thirds the memory, makes support exact, and scales to 32 cameras for free (64 with `uint64`).

```cpp
__global__ void score_rays_scatter(
    const int*   cam_slot, const float* u, const float* v, const float* w, int n_rays,
    const CamIntrinsics* K, const Mat3* R, const Vec3* C,   // __constant__ in practice
    GridSpec g, float* score, unsigned int* cam_mask)        // persistent device buffers
{
    int r = blockIdx.x * blockDim.x + threadIdx.x;
    if (r >= n_rays) return;
    int s = cam_slot[r];
    Vec3 o = C[s];
    Vec3 d = ray_dir_world(u[r], v[r], K[s], R[s]);

    float t0, t1;
    if (!ray_aabb(o, d, g, t0, t1)) return;          // early-out, like the CPU code
    DDA dda = dda_init(o, d, t0, g);
    unsigned int bit = 1u << s;
    while (dda_in_bounds(dda, g) && dda.t <= t1) {
        int idx = flat(dda.ix, dda.iy, dda.iz, g);
        atomicAdd(&score[idx], w[r]);                // contended at convergence
        atomicOr (&cam_mask[idx], bit);              // cheap, idempotent
        dda_step(dda);
    }
}
```

The divergence and contention caveats of §3.1 apply; this kernel exists to match the oracle and to be the fallback, not to be the showpiece.

### 5.3 Back-projection kernel (production) — the showpiece

One thread per local-grid voxel; no atomics; log-odds fusion; support counting inline.

```cpp
__global__ void backproject_local(
    GridSpec local,                                  // small grid around a candidate
    const CamIntrinsics* K, const Mat3* Rinv, const Vec3* C, int n_cam,
    cudaTextureObject_t* mask,                       // per-camera motion mask (R8)
    const int* img_w, const int* img_h,
    float* out_logodds, unsigned char* out_support)
{
    int lin = blockIdx.x * blockDim.x + threadIdx.x;
    if (lin >= local.nx*local.ny*local.nz) return;
    Vec3 X = voxel_center_world(lin, local);

    float acc = 0.f; int support = 0;
    #pragma unroll 4
    for (int c = 0; c < n_cam; ++c) {
        Vec3 Xc = Rinv[c] * (X - C[c]);              // world→camera
        if (Xc.z <= 0.f) continue;                   // behind camera
        float u = K[c].fx * Xc.x / Xc.z + K[c].cx;
        float v = K[c].fy * Xc.y / Xc.z + K[c].cy;
        if (u < 0 || v < 0 || u >= img_w[c] || v >= img_h[c]) continue;
        float m = tex2D<float>(mask[c], u + .5f, v + .5f);  // bilinear, cached
        if (m > 0.f) {
            support++;
            acc += log_odds(m);                      // p(occupied|evidence)
        } else {
            acc += log_odds_miss();                  // negative evidence (optional)
        }
    }
    out_logodds[lin] = (support >= MIN_SUPPORT) ? acc : NEG_INF;
    out_support[lin] = (unsigned char)support;
}
```

Why this is the elegant core: every thread does identical, branch-light work; reads are coalesced and texture-cached; the output is the field *and* its support in one pass; and switching XYZ for inverse-depth changes only `voxel_center_world`. This single kernel subsumes scoring, combination, and gating that currently take three CPU passes.

### 5.4 Temporal decay (the 4D Weavefield, finally)

A persistent device buffer `W` updated in place each frame — one fused multiply-add over the touched set:

```cpp
__global__ void decay_accumulate(float* W, const float* frame, float lambda, const int* touched, int n) {
    int i = blockIdx.x*blockDim.x + threadIdx.x;
    if (i >= n) return;
    int idx = touched[i];
    W[idx] = lambda * W[idx] + frame[idx];           // exp-decay TBD integration
}
```

This is the cheapest possible track-before-detect accumulator (`docs/CODEBASE_ANALYSIS.md` §5-A7) and is essentially free on the GPU.

### 5.5 Peak extraction

For a *small* touched set (which §4 guarantees), two viable paths:

- **GPU:** CUB `DeviceReduce::ArgMax` for the peak, then a one-block kernel doing the soft-argmax centroid + covariance over a fixed neighborhood. Returns a few floats. Keeps everything on-device, avoids a copy of the whole field.
- **CPU:** copy back only the sparse touched voxels (already tiny) and reuse the existing, tested `peaks.py` logic. Lower engineering cost; a single small `cudaMemcpy`. **Recommended first** — peak extraction is not the bottleneck, and reusing the oracle keeps parity trivial.

---

## 6. Memory architecture and data layout

The kernels above are 20% of the work; the data layout is the other 80% and is where elegance and speed actually live.

- **Structure-of-Arrays everywhere.** `EvidenceBatch` is parallel arrays `cam_slot[], u[], v[], weight[]` (§10, and `docs/CODEBASE_ANALYSIS.md` §6.5). This is what coalesced loads require and what the Numba backend already builds internally ([scorer.py:190](../src/skyweave/rayweave/scorer.py)) — promote it to the canonical form.
- **Camera parameters in `__constant__` memory.** `K`, `Rinv`, `C` for ≤~32 cameras are a few KB, read identically by every thread → constant cache broadcasts them in one transaction. Textbook use of constant memory.
- **Motion masks as `cudaTextureObject_t`.** Back-projection samples masks at arbitrary `(u,v)` with 2D locality and wants bilinear + border-clamp for free — precisely what texture units do. One R8 texture per camera, uploaded per frame (or written directly by the ingest decode, §9).
- **Persistent device buffers, never per-frame `cudaMalloc`.** Allocate score/mask/Weavefield grids once at pipeline build; clear via the touched-list stamp trick the Numba code already uses ([numba_scorer.py:108](../src/skyweave/rayweave/numba_scorer.py)), or `cudaMemsetAsync` only the touched range. The current per-frame dense allocation ([scorer.py:139](../src/skyweave/rayweave/scorer.py)) is the first thing to delete.
- **Undistortion LUT in a texture.** Since the runtime currently *drops distortion* (`docs/CODEBASE_ANALYSIS.md` §3.1), the CUDA port is the natural place to add it correctly and for free: precompute a per-camera `(u,v) → undistorted (x,y)` map once with `cv::initUndistortRectifyMap`, store as a 2-channel texture, sample per ray. Distortion correction becomes one texture fetch.
- **Pinned host staging** for the `EvidenceBatch` upload so transfers overlap compute (less relevant on Orin's unified memory — §9 — but still the right default for portability).

---

## 7. Determinism, reproducibility, and the atomics problem

A subtlety that "make it fast" usually ignores but Skyweave specifically must not: **`atomicAdd` on floats is non-deterministic in summation order, so the forward-scatter kernel produces bitwise-different grids run-to-run.** That collides with two project commitments: the oracle-parity testing discipline (the whole `ScorerBackend` value proposition) and spec acceptance #10 ("replay reproduces the same Weavefield and track").

Options, in increasing order of effort:

1. **Tolerance-based parity** (easiest): assert the CUDA result matches the Numba oracle within a float epsilon, not bitwise. Fine for accuracy validation; does **not** give run-to-run reproducibility.
2. **Fixed-point atomics** (elegant): scale weights to integers and `atomicAdd` on `int`/`unsigned long long`. Integer atomic addition *is* associative, so the result is order-independent and exactly reproducible. Costs a scale factor and a final int→float pass. This is the standard trick for deterministic GPU reductions and is worth it here.
3. **The back-projection kernel sidesteps the problem entirely** — each voxel is written by exactly one thread, so there are no atomics and the output is deterministic by construction. This is *another* reason to make back-projection the production path: it is reproducible for free, while forward-scatter has to work for it.

Recommendation: tolerance parity for the scatter baseline, and lean on back-projection's intrinsic determinism for production. If a deterministic scatter path is ever needed, use fixed-point atomics.

---

## 8. The C++ side: how to make it elegant

The CUDA kernels are the easy part; the surrounding C++ is where projects turn into unmaintainable build swamps. The disciplines that keep it clean:

- **Library-first, binary-never.** Build `libskyweave_score` (a `.so`) with a narrow C-ABI or pybind11 surface. No `main()`, no file I/O, no JSON in the core (the prototype's sin — `ray_voxel.cpp` does all three). The library takes arrays in, returns arrays out. This is the same "pipeline is a library, processes are thin" rule from `docs/CODEBASE_ANALYSIS.md` §6.2.
- **One math header, two targets.** `rayweave_math.cuh` with `__host__ __device__` functions compiles into both an x86 CPU build (so the oracle and the GPU share *literally the same* ray/DDA code) and the CUDA build. This kills the geom/numba math duplication permanently.
- **RAII for device memory.** A `DeviceBuffer<T>` with `cudaMalloc`/`cudaFree` in ctor/dtor; no raw `cudaFree` calls scattered around. Or use Thrust's `device_vector`. Leaks and double-frees in CUDA are silent corruption — RAII is not optional.
- **pybind11 behind the existing Protocol.** The Python side gets a `CudaScorerBackend` whose `.score()` calls the binding; it satisfies `ScorerBackend` ([scorer.py:29](../src/skyweave/rayweave/scorer.py)) exactly like the Numba backend, so `_build_backend` ([scorer.py:216](../src/skyweave/rayweave/scorer.py)) gains one `elif config.backend == "cuda"`. Zero changes upstream of the scorer.
- **Build with `scikit-build-core` + CMake**, not a hand `setup.py` with `nvcc` flags (the prototype's `setup.py` is a pybind11 one-off). CMake's `CUDA` language + `CMAKE_CUDA_ARCHITECTURES=87` (Orin) handles the toolchain; scikit-build makes `pip install -e .[cuda]` work. Gate the CUDA extra so non-Jetson dev machines still install.
- **`zero-copy` numpy interop.** Accept `EvidenceBatch` arrays as buffers (no copy into C++ containers); return results as numpy via pybind11's buffer protocol. The data should cross the language boundary as a pointer + shape, never element-by-element.
- **Test as an oracle pair.** Reuse the existing python↔numba parity test pattern: same `EvidenceBatch` in, assert CUDA ≈ Numba ≈ NumPy within tolerance, across the perturbation configs. The infrastructure exists ([tests/](../tests/)); add a `cuda` parametrization that skips when no GPU is present.

A clean directory shape:

```
src/skyweave_cuda/
├── CMakeLists.txt
├── rayweave_math.cuh        # __host__ __device__ shared math (single source of truth)
├── grid.cuh                 # GridSpec, flat indexing, voxel↔world
├── score_scatter.cu         # baseline kernel (oracle-matching)
├── backproject.cu           # production kernel
├── weavefield.cu            # decay/accumulate + peak reduction
├── device_buffer.hpp        # RAII wrappers
└── bindings.cpp             # pybind11 → CudaScorerBackend
```

---

## 9. Jetson-specific optimization

Desktop CUDA advice is actively wrong on Orin in several places; these are the deltas that matter:

- **Unified physical memory.** CPU and GPU share the same LPDDR5. "Minimize host↔device transfers" becomes "avoid *copies*, not *transfers*": use **managed/pinned allocations** (`cudaMallocManaged` or zero-copy mapped pinned) so the ingest thread writes `EvidenceBatch` and motion masks straight into memory the kernel reads — no `cudaMemcpy` at all. This is the biggest single Orin-specific win and it simplifies the code.
- **Bandwidth-bound, not compute-bound.** ~102 GB/s shared between CPU and GPU is the ceiling for grid work. The lever is *touching less memory* — i.e., the sparse local-grid formulation (§4), not faster math. A dense 96³×float per frame is ~3.5 MB read+write per pass; at 100 Hz that pass alone is ~700 MB/s before anything else. Sparse touched-set processing keeps this negligible.
- **Streams to overlap the turret.** The 100 fps turret camera's MJPEG decode should run on **NVJPEG/NVDEC** (hardware engines, ~free) on one stream while the edge-node scorer runs on another; a CPU MJPEG decode of 100 fps would eat a whole Orin core (`docs/CODEBASE_ANALYSIS.md` §7.5). The GPU is shared, so structure work as streams + events, not blocking calls.
- **Right-size launches for SM 8.7.** ~1024 CUDA cores across few SMs means small grids under-occupy. The local-grid kernels (16³ = 4096 threads) are small; batch *all candidates' local grids into one launch* (one block per candidate, or a 1D grid over concatenated local voxels) so the GPU stays busy rather than launching many tiny kernels with launch-latency between them.
- **Power modes matter.** `nvpmodel`/`jetson_clocks` change clocks by ~2×; benchmark in the deployment power mode, and budget thermals for a sealed outdoor enclosure.
- **CUDA graphs** for the steady-state per-frame sequence (upload → score → decay → reduce): capture once, replay per frame, eliminating per-launch CPU overhead that otherwise dominates when kernels are this small.

---

## 10. What must change in the Python codebase

The good news from `docs/CODEBASE_ANALYSIS.md`: the seam (`ScorerBackend`) is already there, so the CUDA backend slots in without touching the aligner, peaks-consumer, or tracker call sites. The changes are about *feeding* it well:

**Prerequisites (do these regardless of CUDA — they're already recommended):**

1. **`EvidenceBatch` SoA form** (`docs/CODEBASE_ANALYSIS.md` §6.5, rec #6). Today the scorer takes `AlignedEvidence` (pydantic packets) and the Numba path rebuilds SoA arrays internally via Python lists ([scorer.py:190](../src/skyweave/rayweave/scorer.py)). Make SoA the *input* the aligner produces, so the CUDA boundary is a buffer pointer, not a marshalling loop. **This is the single most important change** — without it, the GPU port spends its time in Python list-building.
2. **Stop minting pydantic `SparseVoxel` per frame** ([scorer.py:255](../src/skyweave/rayweave/scorer.py)). The CUDA backend returns `(indices: int32[], scores: float32[])`; the pydantic `WeavefieldVolume` is constructed only at the recorder/viz boundary, for the downsampled subset. Up to 5000 validated objects/frame disappear.
3. **Persistent grid ownership.** The scorer should own preallocated score/mask/Weavefield buffers across frames (CPU and GPU), replacing per-frame `grid.zeros()` ([scorer.py:65](../src/skyweave/rayweave/scorer.py), [scorer.py:139](../src/skyweave/rayweave/scorer.py)). The CUDA buffers live for the pipeline's lifetime.

**CUDA-specific additions:**

4. **Undistortion LUT** in `CameraCalib` (precomputed once), consumed by the kernel — closes the §3.1 distortion gap as a side effect.
5. **`config.backend == "cuda"`** branch in `_build_backend` ([scorer.py:216](../src/skyweave/rayweave/scorer.py)) with graceful fallback to numba/numpy when no GPU (mirroring the existing import-error fallback at [runtime.py:277](../src/skyweave/operator/runtime.py)).
6. **A `decay`/Weavefield-state field** threaded through `score()` so the temporal buffer persists (currently `decay_s` is hardcoded 1.0, [scorer.py:92](../src/skyweave/rayweave/scorer.py)).

**Optional but synergistic:**

7. **Switch the production scorer to back-projection + log-odds** (the bigger math change, `docs/CODEBASE_ANALYSIS.md` §5-A2). Best done *as* the CUDA kernel, with the Numba additive scorer retained as the oracle for the scatter path and the NumPy scorer as the ground truth.

Nothing downstream of the scorer (peaks, triangulation, Kalman, viz, recorder) needs to change — that is the payoff of the Protocol seam.

---

## 11. Libraries to lean on (don't hand-roll)

Optimized CUDA, like optimized CPU code, is mostly *calling the right primitives* and writing glue:

- **CUB / Thrust** (ship with CUDA) for every reduction, scan, sort, and selection: `DeviceReduce::ArgMax` (peak), `DeviceSelect::Flagged` (sparsify touched voxels), `DeviceRadixSort` (if grouping by voxel for deterministic reduction), `DeviceScan` (compaction). **Do not hand-write top-K** — `cub::DeviceRadixSort` or a partial sort is faster and correct. (The current `_top_k_sparse` argpartition, [scorer.py:255](../src/skyweave/rayweave/scorer.py), maps directly to CUB select+sort.)
- **OpenCV CUDA module (`cv::cuda`)** if any image-space op is needed on the Jetson (it is built with CUDA in JetPack): GpuMat, `cv::cuda::remap` for undistortion, texture interop. Reuses the team's existing OpenCV familiarity.
- **NVJPEG / NVDEC / VPI / NPP** for the turret camera decode and any preprocessing — hardware engines, not CUDA cores (§9).
- **NVIDIA Warp** is worth a serious look as a *middle path*: it lets you write CUDA kernels in **Python** (decorated functions JIT-compiled to PTX), with structure-of-arrays, atomics, and — notably — **differentiability**. For Skyweave it offers (a) far lower authoring friction than C++/CUDA while keeping kernel-level control, (b) one language across oracle and GPU, and (c) a path to the "learned/auto-tuned scoring" idea in the high-level spec. The tradeoff vs. C++/CUDA: less control over the last 20% of performance and an extra dependency. **Recommendation: prototype the back-projection kernel in Warp first** — if it hits the latency budget (it likely will, given the work is tiny), you may never need the C++ build at all; if not, the Warp kernel is a precise executable spec for the C++ port.
- **CuPy** as the lightest entry: `cupy.RawKernel` runs the exact `.cu` source from Python, and CuPy's array ops cover candidate generation. Good for the first scatter port before committing to a build system.

The honest ladder of effort vs. control: **CuPy RawKernel → Warp → Numba-CUDA → C++/CUDA+pybind11.** Each step buys ~10–20% more performance ceiling for substantially more engineering. Given §12, start at the cheap end and only descend if the Orin profiler demands it.

---

## 12. Performance expectations and when it's worth it

A grounded reality check, because "rewrite in CUDA" can be premature optimization:

- **At current data sizes the Numba scorer is already sub-millisecond.** A few hundred rays into a 96³ grid is trivial. A CUDA launch has ~5–10 µs of overhead plus any transfer; for the *MVP room demo, the GPU may be slower than Numba* once launch latency is counted. CUDA is not justified by the room demo.
- **CUDA becomes justified when any of these scale up**, all of which are on the roadmap:
  - **Camera count** (10–50 edge nodes → 10–50× the rays).
  - **Temporal integration** (track-before-detect over many frames → the Weavefield buffer becomes the dominant working set).
  - **Volume/resolution** (sky-scale, finer voxels → orders of magnitude more cells).
  - **Concurrency with the turret detector** (sharing the GPU with a YOLO at 100 fps — you want the scorer to be GPU-resident so it interleaves rather than competing for the CPU).
- **The end-to-end budget is dominated elsewhere.** Per `docs/CODEBASE_ANALYSIS.md` §7.5, capture + decode + network + Python serialization will eat most of the <300 ms p95. A scorer going 2 ms → 0.2 ms is invisible if `model_dump` costs 8 ms/frame. **Fix the SoA/pydantic boundary (§10) and instrument the live path before writing a kernel** — you may find the scorer was never the problem, and the prerequisite changes (which the CUDA port needs anyway) deliver most of the win.

The disciplined sequence: profile → fix data layout → re-profile → *then* CUDA the stage that is actually hot, with the kernel formulation (§4) chosen so it also scales to the sky goal.

---

## 13. Staged implementation plan

Ordered so each step is independently valuable and de-risks the next:

| Stage | Deliverable | Why first |
|---|---|---|
| 0 | `EvidenceBatch` SoA + persistent buffers + kill per-frame `SparseVoxel` (§10.1–10.3) | Pure-Python win, prerequisite for any GPU work, fixes the real hot-loop cost |
| 1 | Live-path stage timing on the Orin (extend existing instrumentation) | Prove where the time actually goes before optimizing |
| 2 | **Warp** (or CuPy RawKernel) forward-scatter kernel behind `ScorerBackend`, tolerance-parity vs Numba | Lowest-friction GPU port; validates the boundary and the data layout end-to-end |
| 3 | **Back-projection + log-odds** kernel over a single full grid; bitmask support; determinism for free | The production formulation; fixes ray-smear bias (§5-A2) and is reproducible |
| 4 | Coarse-to-fine local grids (candidate gen → local back-projection) | Unlocks scale; bounds work by candidates, not volume |
| 5 | Persistent temporal Weavefield (decay-accumulate kernel) | The actual 4D novelty + track-before-detect sensitivity |
| 6 | Undistortion LUT in the kernel | Closes the §3.1 correctness gap as a texture fetch |
| 7 | C++/CUDA + pybind11 build *iff* Warp/CuPy misses budget on Orin | Only pay the build-complexity tax if profiling proves it necessary |
| 8 | Inverse-depth local grids for far targets | The sky-scale representational change, dropped into Stage 4's kernel shape |

**The throughline:** the elegant CUDA detector is not a transliteration of the Numba scorer — it is a *reformulation* (forward-scatter → coarse-to-fine back-projection) that happens to be simultaneously the GPU-natural shape, the statistically correct fusion, the deterministic-by-construction option, and the only form that reaches sky scale. The prerequisites that make it clean (SoA evidence, persistent buffers, no per-frame pydantic) are changes worth making even if the GPU is never used, and the `ScorerBackend` Protocol means the whole thing lands as one more backend behind an interface the codebase already has. Prototype in Warp, profile on the real Orin, and descend to hand-written C++/CUDA only at the exact point the numbers demand it.
