/* SDK-independent measurements used around the IVE connected-component
 * table. Kept outside sw_detect_ive.c so the two failure-prone rules can be
 * exercised by host tests without Rockchip headers:
 *
 *   - a centroid is the first moment of every NONZERO final-mask pixel in
 *     the accepted component's bounding box;
 *   - overlap diagnostics count unordered pairs of non-empty rectangles.
 */

#ifndef SW_CCL_MEASURE_H
#define SW_CCL_MEASURE_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    int32_t x;
    int32_t y;
    uint32_t w;
    uint32_t h;
} sw_bbox_t;

typedef struct {
    double centroid_u;
    double centroid_v;
    uint64_t pixel_count;
} sw_mask_moment_t;

/* Scan a strided table whose records contain one uint32_t area field. Every
 * slot is inspected; a reported region count is never a dense-prefix bound.
 * Returns the total number of nonzero slots and writes their ascending
 * indices up to `out_capacity`. Zero is also returned for malformed input. */
size_t sw_collect_nonzero_u32_slots(const void *records, size_t record_stride,
                                    size_t area_offset, size_t slot_count,
                                    uint16_t *out_indices,
                                    size_t out_capacity);

/* Returns 1 when at least one nonzero pixel was measured, 0 for an empty
 * bbox, and -1 for invalid geometry. `stride` is in bytes. */
int sw_mask_moment_nonzero(const uint8_t *mask, int width, int height,
                           size_t stride, const sw_bbox_t *bbox,
                           sw_mask_moment_t *out);

/* Edge-touching rectangles do not overlap. Empty rectangles never overlap. */
int sw_bbox_overlaps(const sw_bbox_t *a, const sw_bbox_t *b);

/* Counts each overlapping pair exactly once: (i, j) for i < j. */
uint64_t sw_count_overlapping_bbox_pairs(const sw_bbox_t *boxes, size_t count);

/* Production-linked structural self-test. The daemon exposes this through
 * `--self-test-ccl-measure`, so BUG A/B evidence can execute the exact board
 * binary that will run the campaign rather than a look-alike test binary. */
int sw_ccl_measure_selftest(void);

#endif /* SW_CCL_MEASURE_H */
