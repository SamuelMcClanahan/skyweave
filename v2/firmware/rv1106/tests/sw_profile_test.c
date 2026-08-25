/* Host test for the intra-frame profiler. Links src/sw_profile.c ALONE,
 * the same shape as sw-ccl-measure-test: one translation unit, no daemon.
 *
 * The clock is real, so most timing checks are inequalities against known
 * offsets. Two exact identities are used where the arithmetic allows them:
 * a start in the future clamps to a 0 ns sample (count/total/min/max all
 * become knowable), and with exactly two samples total_ns == min_ns +
 * max_ns whatever the clock did between reads. */

#include <stdint.h>
#include <stdio.h>

#include "sw_profile.h"

#define CHECK(cond)                                                           \
    do {                                                                      \
        if (!(cond)) {                                                        \
            fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, \
                    #cond);                                                   \
            return 1;                                                         \
        }                                                                     \
    } while (0)

static int test_now_is_monotonic_and_nonzero(void)
{
    uint64_t a = sw_profile_now_ns();
    uint64_t b = sw_profile_now_ns();
    CHECK(a > 0);
    CHECK(b >= a);
    return 0;
}

static int test_init_and_registration_order(void)
{
    sw_profile_t profile;
    sw_profile_init(&profile);
    CHECK(profile.stage_count == 0);
    CHECK(sw_profile_stage(&profile, "alpha") == 0);
    CHECK(sw_profile_stage(&profile, "beta") == 1);
    CHECK(profile.stage_count == 2);
    /* Names are kept by pointer, not copied (sw_profile.h). */
    CHECK(profile.stages[0].name != NULL);
    CHECK(profile.stages[0].name[0] == 'a');
    CHECK(profile.stages[1].name[0] == 'b');
    /* A fresh stage carries all zeros, min_ns included: that zero is what
     * write_stats emits for a stage that never ran. */
    CHECK(profile.stages[0].count == 0);
    CHECK(profile.stages[0].total_ns == 0);
    CHECK(profile.stages[0].min_ns == 0);
    CHECK(profile.stages[0].max_ns == 0);
    return 0;
}

static int test_capacity_refusal(void)
{
    static const char *const names[SW_PROFILE_MAX_STAGES] = {
        "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11",
    };
    sw_profile_t profile;
    int i;
    sw_profile_init(&profile);
    for (i = 0; i < SW_PROFILE_MAX_STAGES; ++i) {
        CHECK(sw_profile_stage(&profile, names[i]) == i);
    }
    /* The 13th is refused, and the refusal changes NOTHING: the count stays
     * put and the last accepted slot keeps its own name. */
    CHECK(sw_profile_stage(&profile, "one-too-many") == -1);
    CHECK(profile.stage_count == SW_PROFILE_MAX_STAGES);
    CHECK(profile.stages[SW_PROFILE_MAX_STAGES - 1].name ==
          names[SW_PROFILE_MAX_STAGES - 1]);
    /* And recording against the refused index is a tolerated no-op. */
    sw_profile_record(&profile, -1, sw_profile_now_ns());
    for (i = 0; i < SW_PROFILE_MAX_STAGES; ++i) {
        CHECK(profile.stages[i].count == 0);
    }
    return 0;
}

static int test_record_math(void)
{
    sw_profile_t profile;
    int stage;
    sw_profile_init(&profile);
    stage = sw_profile_stage(&profile, "stage");
    CHECK(stage == 0);

    /* A start in the FUTURE clamps to a 0 ns sample rather than wrapping,
     * which makes every field exact. */
    sw_profile_record(&profile, stage, sw_profile_now_ns() + 3600000000000ull);
    CHECK(profile.stages[stage].count == 1);
    CHECK(profile.stages[stage].total_ns == 0);
    CHECK(profile.stages[stage].min_ns == 0);
    CHECK(profile.stages[stage].max_ns == 0);

    /* A start 1 ms in the past measures at least 1 ms, and with the 0 ns
     * sample already in place: min stays 0, max carries the new sample, and
     * total == max exactly (0 + elapsed). */
    sw_profile_record(&profile, stage, sw_profile_now_ns() - 1000000ull);
    CHECK(profile.stages[stage].count == 2);
    CHECK(profile.stages[stage].min_ns == 0);
    CHECK(profile.stages[stage].max_ns >= 1000000ull);
    CHECK(profile.stages[stage].total_ns == profile.stages[stage].max_ns);

    /* A third sample makes the documented mean bounds non-trivial: whatever
     * the clock did between reads, an ACCUMULATED quadruple must satisfy
     * count*min <= total <= count*max. A block outside those bounds was
     * composed by hand, which is exactly what the host emission test screens
     * stats files for — this pins the same invariant at the source. */
    sw_profile_record(&profile, stage, sw_profile_now_ns() - 500000ull);
    CHECK(profile.stages[stage].count == 3);
    CHECK(profile.stages[stage].total_ns >=
          profile.stages[stage].count * profile.stages[stage].min_ns);
    CHECK(profile.stages[stage].total_ns <=
          profile.stages[stage].count * profile.stages[stage].max_ns);
    return 0;
}

static int test_min_max_ordering(void)
{
    sw_profile_t profile;
    int stage;
    sw_profile_init(&profile);
    stage = sw_profile_stage(&profile, "stage");

    /* First sample: min == max == total, whatever the clock measured. */
    sw_profile_record(&profile, stage, sw_profile_now_ns() - 1000ull);
    CHECK(profile.stages[stage].count == 1);
    CHECK(profile.stages[stage].min_ns >= 1000ull);
    CHECK(profile.stages[stage].min_ns == profile.stages[stage].max_ns);
    CHECK(profile.stages[stage].min_ns == profile.stages[stage].total_ns);

    /* Second sample, 10 s in the past: unless two adjacent clock reads sat
     * 10 s apart, this is the max and the first sample is the min. Either
     * way total == min + max holds EXACTLY at count 2. */
    sw_profile_record(&profile, stage, sw_profile_now_ns() - 10000000000ull);
    CHECK(profile.stages[stage].count == 2);
    CHECK(profile.stages[stage].max_ns >= 10000000000ull);
    CHECK(profile.stages[stage].min_ns < 10000000000ull);
    CHECK(profile.stages[stage].min_ns <= profile.stages[stage].max_ns);
    CHECK(profile.stages[stage].total_ns ==
          profile.stages[stage].min_ns + profile.stages[stage].max_ns);
    return 0;
}

static int test_registered_but_never_recorded_stage_stays_all_zeros(void)
{
    /* write_stats lives in main.c with the whole daemon behind it, so it is
     * not linkable here; what CAN be asserted is the accumulator state it
     * prints verbatim. A stage that was registered but never recorded —
     * while its neighbours were — must read count 0 with min_ns 0, because
     * that zero is exactly what write_stats emits for a stage that never
     * ran (sw_profile.h: no UINT64_MAX sentinel to leak into JSON). */
    sw_profile_t profile;
    int recorded;
    int idle;
    sw_profile_init(&profile);
    recorded = sw_profile_stage(&profile, "recorded");
    idle = sw_profile_stage(&profile, "idle");
    sw_profile_record(&profile, recorded, sw_profile_now_ns() - 2000ull);
    sw_profile_record(&profile, recorded, sw_profile_now_ns() - 4000ull);
    CHECK(profile.stages[recorded].count == 2);
    CHECK(profile.stages[idle].count == 0);
    CHECK(profile.stages[idle].total_ns == 0);
    CHECK(profile.stages[idle].min_ns == 0);
    CHECK(profile.stages[idle].max_ns == 0);
    return 0;
}

static int test_ive_stage_list_leaves_registration_headroom(void)
{
    /* Nine is not a random number: it is the IVE detector's whole stage list
     * (src_copy, gmm2, erode, dilate, mask_preserve, ccl, region_scan,
     * diag_io, frame_total — sw_detect_ive.c), the table's largest consumer.
     * The capacity of 12 promises it three stages of headroom, so nine plus
     * three must all register and a 13th must be refused. A future 13th IVE
     * stage would eat that headroom silently until a registration returned
     * -1 on a board; this test is where the arithmetic breaks FIRST, in
     * review, instead. */
    static const char *const ive_stages[9] = {
        "src_copy", "gmm2",        "erode",   "dilate",      "mask_preserve",
        "ccl",      "region_scan", "diag_io", "frame_total",
    };
    static const char *const headroom[3] = {"spare0", "spare1", "spare2"};
    sw_profile_t profile;
    int i;
    sw_profile_init(&profile);
    for (i = 0; i < 9; ++i) {
        CHECK(sw_profile_stage(&profile, ive_stages[i]) == i);
    }
    for (i = 0; i < 3; ++i) {
        CHECK(sw_profile_stage(&profile, headroom[i]) == 9 + i);
    }
    CHECK(sw_profile_stage(&profile, "a-13th-stage") == -1);
    CHECK(profile.stage_count == SW_PROFILE_MAX_STAGES);
    return 0;
}

static int test_stage_independence(void)
{
    sw_profile_t profile;
    int first;
    int second;
    sw_profile_init(&profile);
    first = sw_profile_stage(&profile, "first");
    second = sw_profile_stage(&profile, "second");
    sw_profile_record(&profile, second, sw_profile_now_ns() - 5000ull);
    CHECK(profile.stages[first].count == 0);
    CHECK(profile.stages[first].total_ns == 0);
    CHECK(profile.stages[second].count == 1);
    CHECK(profile.stages[second].total_ns >= 5000ull);
    return 0;
}

static int test_null_and_index_tolerance(void)
{
    sw_profile_t profile;
    /* Every operation shrugs at NULL instead of dereferencing it. */
    sw_profile_init(NULL);
    CHECK(sw_profile_stage(NULL, "stage") == -1);
    sw_profile_record(NULL, 0, sw_profile_now_ns());

    sw_profile_init(&profile);
    CHECK(sw_profile_stage(&profile, NULL) == -1);
    CHECK(profile.stage_count == 0);
    CHECK(sw_profile_stage(&profile, "only") == 0);

    /* Out-of-range indices — negative, past the registered count but inside
     * capacity, and past capacity — are all ignored without touching the
     * one registered stage. */
    sw_profile_record(&profile, -1, sw_profile_now_ns());
    sw_profile_record(&profile, 1, sw_profile_now_ns());
    sw_profile_record(&profile, SW_PROFILE_MAX_STAGES, sw_profile_now_ns());
    sw_profile_record(&profile, SW_PROFILE_MAX_STAGES + 7, sw_profile_now_ns());
    CHECK(profile.stages[0].count == 0);
    CHECK(profile.stages[1].count == 0);
    return 0;
}

int main(void)
{
    if (test_now_is_monotonic_and_nonzero() != 0 ||
        test_init_and_registration_order() != 0 ||
        test_capacity_refusal() != 0 ||
        test_record_math() != 0 ||
        test_min_max_ordering() != 0 ||
        test_registered_but_never_recorded_stage_stays_all_zeros() != 0 ||
        test_ive_stage_list_leaves_registration_headroom() != 0 ||
        test_stage_independence() != 0 ||
        test_null_and_index_tolerance() != 0) {
        return 1;
    }
    return 0;
}
