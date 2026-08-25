/* Intra-frame profiler: a fixed-capacity per-stage time accumulator over
 * CLOCK_MONOTONIC.
 *
 * The detectors accumulate into this on EVERY frame; --profile-stats gates
 * only whether write_stats emits the totals. Always-on is a deliberate
 * choice, not sloppiness: every recorded span costs two clock_gettime reads
 * (the explicit start plus the one inside sw_profile_record) — 10 per
 * post-warm frame for the portable detector's five stages, 16-18 for the
 * IVE detector's spans (morphology off/on). Each read is a real syscall on
 * the node's vDSO-less uClibc, ~1-5 us on the 1 GHz A7, so the honest total
 * is ~20-90 us per ~69 ms IVE frame — still <= 0.13% of the frame budget —
 * and a profiler that changes the timing it measures when switched on would
 * be measuring itself. The flag therefore never touches the frame loop.
 *
 * MONOTONIC ONLY, same rule as sw_common.h's sw_monotonic_ns: these numbers
 * are perf counters, never capture timestamps, and they never reach the
 * measurement wire. The reader is duplicated here rather than calling
 * sw_monotonic_ns so the host unit test links sw_profile.c ALONE, the same
 * shape as sw-ccl-measure-test — one translation unit under test, no
 * daemon-side logging pulled in with it.
 *
 * No locking. The daemon is one thread by design (RV1106_EDGE_NODE.md
 * section 4), and so is every test that uses this.
 */

#ifndef SW_PROFILE_H
#define SW_PROFILE_H

#include <stdint.h>

/* Fixed capacity, no allocation: the profiler obeys the same "no allocation
 * inside the frame loop" rule as everything it measures. The IVE detector
 * registers nine stages, the portable one five; 12 leaves headroom without
 * inviting an unbounded stage vocabulary. */
#define SW_PROFILE_MAX_STAGES 12

typedef struct {
    /* The registered name. NOT COPIED: sw_profile_stage keeps the pointer,
     * so the caller must pass a string that outlives the profile — in
     * practice a string literal. Copying defensively would need either
     * allocation (banned) or a truncating fixed buffer (a silently renamed
     * stage), and every caller here has a literal anyway. */
    const char *name;
    uint64_t count;
    uint64_t total_ns;
    /* 0 until the first record, which is also what write_stats emits for a
     * stage that never ran — no UINT64_MAX sentinel to leak into JSON. */
    uint64_t min_ns;
    uint64_t max_ns;
} sw_profile_stage_t;

typedef struct {
    sw_profile_stage_t stages[SW_PROFILE_MAX_STAGES];
    int stage_count;
} sw_profile_t;

/* CLOCK_MONOTONIC in nanoseconds via clock_gettime, the one timer interface
 * uclibc, glibc and macOS libc all declare under _POSIX_C_SOURCE=200809L.
 * Returns 0 if the clock fails, same convention as sw_monotonic_ns. */
uint64_t sw_profile_now_ns(void);

/* Zero every slot. Required before the first registration; NULL is a no-op. */
void sw_profile_init(sw_profile_t *profile);

/* Register a stage and return its index, in registration order — the order
 * write_stats emits. Returns -1 when the table is full or on a NULL profile
 * or name, and a refused registration changes nothing; sw_profile_record
 * ignores -1, so a caller may store the result unchecked. */
int sw_profile_stage(sw_profile_t *profile, const char *name);

/* Accumulate now() - start_ns against a stage. A NULL profile or an index
 * outside the registered range is ignored, and a start after now (a clock
 * that failed and read 0, or a bad caller) clamps the sample to 0 rather
 * than underflowing into a ~584-year total. */
void sw_profile_record(sw_profile_t *profile, int stage, uint64_t start_ns);

#endif /* SW_PROFILE_H */
