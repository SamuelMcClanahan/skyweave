# D8.1 C1 findings — first daemon start on real hardware (node 1)

**Status: DRAFT for the planning session.** Written 2026-08-18 by the board
bring-up agent. C1 (provision + smoke + IVE budget) ran on node 1
(`192.168.10.101`) over Tailscale → Jetson jump → rig LAN. This records what
C1 established and the STOP condition it hit. Per the runbook the agent does
NOT edit the contracts doc; the decision below is the planning session's.

## What C1 established (works)

- **Daemon rebuilt from current source.** The Aug-9 `build-board/skyweave-edge`
  was stale (predated the D8.1 Phase A RAM-loop source + F10 fix). Rebuilt in
  the pinned container `skyweave-edge-build:d8.0` (Luckfox cross-toolchain,
  gcc 8.3.0, SDK commit 824b817f) via colima+Rosetta on the Mac. Output is a
  uClibc ARM EABI5 binary with the IVE/RGA arm compiled — the first real IVE
  compile (D8-F11).
- **Provision path works.** `provision_node()` over SSH (BatchMode key auth,
  ProxyJump through the Jetson) pushed the binary and verified it by SHA-256 on
  the node (`570a36f6…d4e2`).
- **IVE RAM-budget line confirmed (D8-F11).** First start printed:

  ```
  RAM budget: clip 35,831,808 B + detector 119,442,420 B + fixed 3,007,200 B
            = 158,281,428 B against a declared 160,000,000 B
  ```

  The IVE detector's **real** footprint (119,442,420 B) matches the pre-compile
  **arithmetic** (119,439,360 B) to within **3,060 B** — exactly the
  `IVE_CCBLOB_S` blob term the harness deliberately omits. The arithmetic is
  **validated**; the detector figure moves from Provisional toward Measured.

## Findings

### F-C1-1 — `provision.py` does not set `LD_LIBRARY_PATH` (fix needed)
The media libs (`librve.so`, `librockit*.so`, `librga.so`, `librkaiq.so`) live
in `/oem/usr/lib`, which is NOT on this uClibc board's default loader path
(there is no `ldconfig`/`ld.so.cache`, and `/etc/ld.so.conf` is not honored
without it). The daemon fails at the dynamic linker (`can't load library
'librve.so'`) unless launched with `LD_LIBRARY_PATH=/oem/usr/lib`.
`provision.py`'s `spawn()` builds `setsid nohup <argv>` with no environment, so
it cannot start the daemon on a real node as written. **Fix:** the daemon
launch must set `LD_LIBRARY_PATH=/oem/usr/lib` (e.g. `env LD_LIBRARY_PATH=… …`,
which the bring-up used via a transport subclass). Needs a test + review.

### F-C1-2 — RAM-loop OOM: the budget conflates two physically-separate pools (STOP)
The daemon printed the budget line (158.3 MB ≤ 160 MB → "fits"), then was
**OOM-killed** malloc'ing the clip:

```
Out of memory: Killed process 3430 (skyweave-edge) total-vm:43068kB, anon-rss:28144kB
```

Root cause: the clip arena is a plain heap `malloc` (`sw_inject.c:309`,
`sw_inject_preload_ram`), but the 160 MB budget counts the 119 MB detector,
which lives in **CMA / media-reserved** memory, not heap. On this board the
256 MB DDR splits roughly as **~54 MB Linux heap (~29 MB available) + ~66 MB
CMA + ~130 MB media-reserved**. So the clip competes only for the ~29 MB
available heap:

| Resolution | Derived clip | Clip bytes | vs ~29 MB heap |
| --- | --- | --- | --- |
| 2304×1296 | 12 frames | 35.8 MB | **OOM** (smallest clip, already over) |
| 1536×864 | (mid) | ~tens MB | over |
| 1152×648 | 171 frames | 127.7 MB | **2.3× total board RAM** |

The clip-length defaults (12/78/171) are **physically unrealizable**. The
budget model is arithmetically self-consistent but measures the wrong pool for
the clip term.

### F-C1-3 — node 1 wedges under the run (recurring media hang)
Node 1 went unreachable (100% loss, ARP INCOMPLETE, `:22` down) during the
inject-file follow-up run; the other four boards stayed up. Consistent with the
known recurring RKMedia/venc D-state hang under memory pressure. Node 1 is left
down by decision; recovery (power-cycle) is deferred.

## The decision (planning session)

C3 (the RAM-loop sweep) cannot run as specified. Options (a board-memory
analysis to quantify each is a noted TODO, not yet run — the ~256 MB DDR splits
~54 MB heap / ~66 MB CMA / ~130 MB media-reserved, confirmed on nodes 1 and 2):

1. **Reduce the image's media reservation** so more DDR falls to Linux heap —
   but the IVE detector still needs ~119 MB in that pool; show whether a 256 MB
   split exists that satisfies both.
2. **Lower `SW_RAM_BUDGET_DEFAULT_MB`** so derived clips fit real usable heap —
   report resulting clip lengths per resolution and whether they exceed warm-up.
3. **Allocate the clip from CMA/dma-heap** instead of `malloc` (sw_inject.c
   change) — needs free CMA (was 0).
4. **Switch C3's source to inject-file** (SD streaming, no preload) — revisits
   the F8 source decision.

Nothing here is a software regression: the git baseline and the (improved)
daemon rebuild are intact. This is a design/contract decision, not an agent
call.

## Reproduce

Bring-up driver (host, scratch): builds the clip with the harness helpers and
runs `provision_node()` over the Jetson ProxyJump with `LD_LIBRARY_PATH` set.
RAM-loop worst case at the smallest clip:
`board_run.py 2304 1296 <work> <out> 8 ram`. Inject-file pipeline smoke:
`board_run.py 1152 648 <work> <out> 10 file`.
