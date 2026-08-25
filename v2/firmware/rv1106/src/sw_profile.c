/* Fixed-capacity per-stage time accumulator. See sw_profile.h for why it is
 * always-on, why the clock reader is its own function, and why stage names
 * are kept by pointer.
 *
 * Deliberately free of sw_common.h: no logging (a profiler that logs from
 * inside the frame loop perturbs the frame it measures) and no daemon
 * dependencies, so the host unit test links this file alone. */

#include "sw_profile.h"

#include <string.h>
#include <time.h>

uint64_t sw_profile_now_ns(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        /* Same convention as sw_monotonic_ns: a clock that fails reads as 0.
         * sw_profile_record clamps the resulting backwards interval to 0, so
         * a broken clock produces zero-length samples, never garbage. */
        return 0;
    }
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

void sw_profile_init(sw_profile_t *profile)
{
    if (profile == NULL) {
        return;
    }
    memset(profile, 0, sizeof(*profile));
}

int sw_profile_stage(sw_profile_t *profile, const char *name)
{
    sw_profile_stage_t *stage;
    if (profile == NULL || name == NULL) {
        return -1;
    }
    if (profile->stage_count >= SW_PROFILE_MAX_STAGES) {
        /* Refused, never wrapped or overwritten: a 13th stage silently
         * folded into the 12th would attribute one stage's time to another,
         * which is worse than no measurement. The caller stores -1 and
         * sw_profile_record ignores it. */
        return -1;
    }
    stage = &profile->stages[profile->stage_count];
    /* Zeroed here as well as in init, so a stage registered into a REUSED
     * profile starts from zero instead of inheriting the previous owner's
     * totals. The name is kept by pointer — sw_profile.h documents that the
     * caller passes a literal. */
    stage->name = name;
    stage->count = 0;
    stage->total_ns = 0;
    stage->min_ns = 0;
    stage->max_ns = 0;
    return profile->stage_count++;
}

void sw_profile_record(sw_profile_t *profile, int stage, uint64_t start_ns)
{
    sw_profile_stage_t *slot;
    uint64_t now;
    uint64_t elapsed;
    if (profile == NULL || stage < 0 || stage >= profile->stage_count) {
        /* Index-tolerant by contract: -1 is what a refused registration
         * returned, and ignoring it here is what lets every call site stay
         * unconditional — no instrumentation branches in the frame loop. */
        return;
    }
    now = sw_profile_now_ns();
    /* Clamp, don't wrap: start_ns > now means the clock failed on one of the
     * two reads (both return 0 then) or the caller passed a foreign value,
     * and an unsigned underflow would poison total/max for the whole run. */
    elapsed = now > start_ns ? now - start_ns : 0;
    slot = &profile->stages[stage];
    slot->count++;
    slot->total_ns += elapsed;
    if (slot->count == 1 || elapsed < slot->min_ns) {
        slot->min_ns = elapsed;
    }
    if (elapsed > slot->max_ns) {
        slot->max_ns = elapsed;
    }
}
