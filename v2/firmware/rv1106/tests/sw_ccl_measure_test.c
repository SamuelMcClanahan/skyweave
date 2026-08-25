#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "sw_ccl_measure.h"

#define CHECK(cond)                                                           \
    do {                                                                      \
        if (!(cond)) {                                                        \
            fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, \
                    #cond);                                                   \
            return 1;                                                         \
        }                                                                     \
    } while (0)

static int close_to(double actual, double expected)
{
    return fabs(actual - expected) < 1.0e-12;
}

static int test_full_254_slot_region_scan(void)
{
    typedef struct {
        uint16_t tag;
        uint32_t area;
        uint8_t padding[3];
    } fake_region_t;
    fake_region_t regions[254] = {{0}};
    uint16_t indices[254] = {0};
    size_t count;

    /* Slot 253 is deliberately beyond a hypothetical dense u8RegionNum=2
     * prefix. The production IVE path calls this same strided scanner. */
    regions[1].area = 11;
    regions[253].area = 29;
    count = sw_collect_nonzero_u32_slots(
        regions, sizeof(regions[0]), offsetof(fake_region_t, area), 254,
        indices, sizeof(indices) / sizeof(indices[0]));
    CHECK(count == 2);
    CHECK(indices[0] == 1);
    CHECK(indices[1] == 253);

    indices[0] = 0;
    count = sw_collect_nonzero_u32_slots(
        regions, sizeof(regions[0]), offsetof(fake_region_t, area), 254,
        indices, 1);
    CHECK(count == 2);
    CHECK(indices[0] == 1);
    return 0;
}

static int test_nonzero_mask_moment(void)
{
    /* The 5x5 bbox contains a border plus one off-centre interior pixel.
     * Values deliberately differ (1, 7, 255): the rule is nonzero final mask,
     * never an IVE label value. Pixel count is 17; sums are u=51, v=52. */
    const uint8_t mask[7 * 9] = {
        0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 1, 1, 1, 1, 1, 0, 0, 0,
        0, 7, 0, 0, 0, 255, 0, 0, 0,
        0, 1, 0, 0, 0, 1, 0, 0, 0,
        0, 1, 0, 255, 0, 1, 0, 0, 0,
        0, 1, 1, 1, 1, 1, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0,
    };
    const sw_bbox_t bbox = {1, 1, 5, 5};
    sw_mask_moment_t moment;
    CHECK(sw_mask_moment_nonzero(mask, 7, 7, 9, &bbox, &moment) == 1);
    CHECK(moment.pixel_count == 17);
    CHECK(close_to(moment.centroid_u, 51.0 / 17.0));
    CHECK(close_to(moment.centroid_v, 52.0 / 17.0));
    return 0;
}

static int test_empty_and_invalid_moments(void)
{
    const uint8_t mask[4 * 4] = {0};
    const sw_bbox_t empty = {1, 1, 2, 2};
    const sw_bbox_t outside = {3, 3, 2, 2};
    sw_mask_moment_t moment;
    CHECK(sw_mask_moment_nonzero(mask, 4, 4, 4, &empty, &moment) == 0);
    CHECK(moment.pixel_count == 0);
    CHECK(sw_mask_moment_nonzero(mask, 4, 4, 4, &outside, &moment) == -1);
    CHECK(sw_mask_moment_nonzero(mask, 4, 4, 3, &empty, &moment) == -1);
    return 0;
}

static int test_unordered_overlap_pairs(void)
{
    const sw_bbox_t boxes[] = {
        {0, 0, 4, 4}, /* overlaps 1 and 3 */
        {2, 2, 4, 4}, /* overlaps 0; edge-touches 2 and 3 */
        {6, 2, 2, 2}, /* edge-touches 1: no overlap */
        {1, 1, 1, 1}, /* nested in 0; edge-touches 1 */
        {0, 0, 0, 8}, /* empty */
    };
    CHECK(sw_bbox_overlaps(&boxes[0], &boxes[1]) == 1);
    CHECK(sw_bbox_overlaps(&boxes[1], &boxes[2]) == 0);
    CHECK(sw_bbox_overlaps(&boxes[0], &boxes[4]) == 0);
    CHECK(sw_count_overlapping_bbox_pairs(boxes,
                                           sizeof(boxes) / sizeof(boxes[0])) == 2);
    return 0;
}

int main(void)
{
    if (sw_ccl_measure_selftest() != 0 ||
        test_full_254_slot_region_scan() != 0 ||
        test_nonzero_mask_moment() != 0 ||
        test_empty_and_invalid_moments() != 0 ||
        test_unordered_overlap_pairs() != 0) {
        return 1;
    }
    return 0;
}
