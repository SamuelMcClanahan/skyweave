#include "sw_ccl_measure.h"

#include <limits.h>
#include <string.h>

size_t sw_collect_nonzero_u32_slots(const void *records, size_t record_stride,
                                    size_t area_offset, size_t slot_count,
                                    uint16_t *out_indices,
                                    size_t out_capacity)
{
    const uint8_t *bytes = (const uint8_t *)records;
    size_t count = 0;
    size_t slot;

    if (records == NULL || record_stride < sizeof(uint32_t) ||
        area_offset > record_stride - sizeof(uint32_t) ||
        slot_count > (size_t)UINT16_MAX + 1u ||
        (out_capacity > 0 && out_indices == NULL)) {
        return 0;
    }
    for (slot = 0; slot < slot_count; ++slot) {
        uint32_t area;
        memcpy(&area, bytes + slot * record_stride + area_offset, sizeof(area));
        if (area == 0) {
            continue;
        }
        if (count < out_capacity) {
            out_indices[count] = (uint16_t)slot;
        }
        count++;
    }
    return count;
}

static int bbox_is_in_frame(const sw_bbox_t *bbox, int width, int height)
{
    int64_t right;
    int64_t bottom;
    if (bbox == NULL || width <= 0 || height <= 0 || bbox->x < 0 || bbox->y < 0 ||
        bbox->w == 0 || bbox->h == 0) {
        return 0;
    }
    right = (int64_t)bbox->x + (int64_t)bbox->w;
    bottom = (int64_t)bbox->y + (int64_t)bbox->h;
    return right <= (int64_t)width && bottom <= (int64_t)height;
}

int sw_mask_moment_nonzero(const uint8_t *mask, int width, int height,
                           size_t stride, const sw_bbox_t *bbox,
                           sw_mask_moment_t *out)
{
    uint64_t count = 0;
    uint64_t sum_u = 0;
    uint64_t sum_v = 0;
    int64_t right;
    int64_t bottom;
    int y;

    if (out != NULL) {
        memset(out, 0, sizeof(*out));
    }
    if (mask == NULL || out == NULL || stride < (size_t)width ||
        !bbox_is_in_frame(bbox, width, height)) {
        return -1;
    }
    right = (int64_t)bbox->x + (int64_t)bbox->w;
    bottom = (int64_t)bbox->y + (int64_t)bbox->h;
    for (y = bbox->y; (int64_t)y < bottom; ++y) {
        int x;
        const uint8_t *row = mask + (size_t)y * stride;
        for (x = bbox->x; (int64_t)x < right; ++x) {
            if (row[x] != 0) {
                count++;
                sum_u += (uint64_t)x;
                sum_v += (uint64_t)y;
            }
        }
    }
    if (count == 0) {
        return 0;
    }
    out->pixel_count = count;
    out->centroid_u = (double)sum_u / (double)count;
    out->centroid_v = (double)sum_v / (double)count;
    return 1;
}

int sw_bbox_overlaps(const sw_bbox_t *a, const sw_bbox_t *b)
{
    int64_t a_right;
    int64_t a_bottom;
    int64_t b_right;
    int64_t b_bottom;
    if (a == NULL || b == NULL || a->w == 0 || a->h == 0 || b->w == 0 ||
        b->h == 0) {
        return 0;
    }
    a_right = (int64_t)a->x + (int64_t)a->w;
    a_bottom = (int64_t)a->y + (int64_t)a->h;
    b_right = (int64_t)b->x + (int64_t)b->w;
    b_bottom = (int64_t)b->y + (int64_t)b->h;
    return (int64_t)a->x < b_right && (int64_t)b->x < a_right &&
           (int64_t)a->y < b_bottom && (int64_t)b->y < a_bottom;
}

uint64_t sw_count_overlapping_bbox_pairs(const sw_bbox_t *boxes, size_t count)
{
    uint64_t pairs = 0;
    size_t i;
    if (boxes == NULL) {
        return 0;
    }
    for (i = 0; i < count; ++i) {
        size_t j;
        for (j = i + 1; j < count; ++j) {
            if (sw_bbox_overlaps(&boxes[i], &boxes[j])) {
                pairs++;
            }
        }
    }
    return pairs;
}

int sw_ccl_measure_selftest(void)
{
    typedef struct {
        uint16_t tag;
        uint32_t area;
        uint8_t padding[3];
    } fake_region_t;
    fake_region_t regions[254] = {{0}};
    uint16_t indices[254] = {0};
    const uint8_t mask[7 * 9] = {
        0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 1, 1, 1, 1, 1, 0, 0, 0,
        0, 7, 0, 0, 0, 255, 0, 0, 0,
        0, 1, 0, 0, 0, 1, 0, 0, 0,
        0, 1, 0, 255, 0, 1, 0, 0, 0,
        0, 1, 1, 1, 1, 1, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0,
    };
    const sw_bbox_t moment_bbox = {1, 1, 5, 5};
    const sw_bbox_t overlap_boxes[] = {
        {0, 0, 4, 4},
        {2, 2, 4, 4},
        {6, 2, 2, 2},
        {1, 1, 1, 1},
        {0, 0, 0, 8},
    };
    sw_mask_moment_t moment;
    size_t count;

    regions[1].area = 11;
    regions[253].area = 29;
    count = sw_collect_nonzero_u32_slots(
        regions, sizeof(regions[0]), offsetof(fake_region_t, area), 254,
        indices, sizeof(indices) / sizeof(indices[0]));
    if (count != 2 || indices[0] != 1 || indices[1] != 253) {
        return 11;
    }
    if (sw_mask_moment_nonzero(mask, 7, 7, 9, &moment_bbox, &moment) != 1 ||
        moment.pixel_count != 17 || moment.centroid_u != 51.0 / 17.0 ||
        moment.centroid_v != 52.0 / 17.0) {
        return 12;
    }
    if (sw_count_overlapping_bbox_pairs(
            overlap_boxes, sizeof(overlap_boxes) / sizeof(overlap_boxes[0])) != 2) {
        return 13;
    }
    return 0;
}
