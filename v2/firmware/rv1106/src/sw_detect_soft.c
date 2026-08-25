/* Portable GMM2 + morphological opening + 4-connected CCL.
 *
 * A C transcription of the host's `ive_approx` backend
 * (`skyweave2/detector/backends.py::IveApproxBackend`) with the same seven
 * knobs, the same Stauffer-Grimson update order and the same
 * background-support rule. State is float32 on both sides, which is what
 * makes the two comparable at all.
 *
 * It is an APPROXIMATION and is labelled one everywhere it produces a
 * number. Two sources of divergence are known and are NOT bugs:
 *
 *   - summation and division order differ from numpy's vectorised form, so
 *     float32 rounding differs in the last bits and a pixel sitting exactly
 *     on the match gate can fall either way;
 *   - numpy's `argmax`/`argmin` tie-breaking (lowest index) is reproduced
 *     here explicitly, because getting it wrong would be a systematic
 *     divergence rather than a rounding one.
 *
 * The consequence is measured, not assumed: E5 scores this against the
 * committed oracle observations under the tolerances the D8 report declares
 * BEFORE any board run.
 *
 * Memory: every buffer is allocated once at open() and reused. The frame
 * loop allocates nothing (RV1106_EDGE_NODE.md section 4).
 */

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "sw_ccl_measure.h"
#include "sw_common.h"
#include "sw_detect.h"

typedef struct {
    sw_detector_config_t config;
    int width;
    int height;
    size_t pixels;
    bool initialised;

    float *weight; /* (K, H, W) */
    float *mean;
    float *var;

    uint8_t *mask;    /* foreground, 0/1 */
    uint8_t *opened;  /* after morphological opening */
    int32_t *labels;  /* CCL label image, 0 = background */
    int32_t *stack;   /* flood-fill frontier */

    /* Components the output list could not hold. Counted, not merely
     * logged: "nothing is dropped silently" is a rule about COUNTERS, and a
     * log line scrolls away while a counter reaches the health packet. */
    uint64_t components_over_list_bound;
    uint64_t frames_over_list_bound;

    uint64_t overlapping_bbox_pairs;
    uint64_t frames_with_overlapping_bboxes;

    /* What soft_alloc actually asked for, summed from the same expressions
     * the callocs use. The RAM-budget check reads this rather than
     * re-deriving 46 B/px somewhere else, because two copies of one formula
     * means one of them is not the one that runs. */
    size_t footprint_bytes;

    /* Intra-frame timing, accumulated on EVERY frame whether or not
     * --profile-stats will emit it: the whole cost is 10 clock_gettime calls
     * per post-warm ~69 ms frame — each of the five stages pays an explicit
     * start read plus the read inside sw_profile_record, and on the node's
     * vDSO-less uClibc each is a real syscall (~1-5 us on the 1 GHz A7), so
     * ~10-50 us, still under 0.1% of the frame — and a profiler that only
     * runs when watched measures a different detector than the one that runs
     * unwatched. The stage indices come from sw_profile_stage at open() and
     * are stored rather than assumed, so registration order and emission
     * order cannot drift apart. */
    sw_profile_t profile;
    int stage_gmm2;
    int stage_morph;
    int stage_occupancy_scan;
    int stage_ccl_label;
    int stage_frame_total;
} soft_state_t;

static void soft_free(soft_state_t *state)
{
    if (state == NULL) {
        return;
    }
    free(state->weight);
    free(state->mean);
    free(state->var);
    free(state->mask);
    free(state->opened);
    free(state->labels);
    free(state->stack);
    free(state);
}

/* THE sum of what soft_alloc asks for, in one place and one place only.
 * soft_alloc records it; soft_footprint_bytes answers with it for the
 * CONFIGURED grid before the first frame has arrived to allocate anything.
 * Two copies of this expression would mean the RAM-budget check could pass
 * against a formula that is not the one that runs. */
static size_t soft_footprint_for(int model_num, size_t pixels)
{
    size_t models = (size_t)model_num;
    return models * pixels * sizeof(float) * 3 + pixels * 1 * 2 +
           pixels * sizeof(int32_t) * 2;
}

static int soft_alloc(soft_state_t *state, int width, int height)
{
    size_t pixels = (size_t)width * (size_t)height;
    size_t models = (size_t)state->config.gmm2.model_num;

    state->weight = (float *)calloc(models * pixels, sizeof(float));
    state->mean = (float *)calloc(models * pixels, sizeof(float));
    state->var = (float *)calloc(models * pixels, sizeof(float));
    state->mask = (uint8_t *)calloc(pixels, 1);
    state->opened = (uint8_t *)calloc(pixels, 1);
    state->labels = (int32_t *)calloc(pixels, sizeof(int32_t));
    state->stack = (int32_t *)calloc(pixels, sizeof(int32_t));
    if (state->weight == NULL || state->mean == NULL || state->var == NULL ||
        state->mask == NULL || state->opened == NULL || state->labels == NULL ||
        state->stack == NULL) {
        SW_LOG_ERR("out of memory allocating detector state for %dx%d", width, height);
        /* Release what DID arrive and leave the geometry at zero. Committing
         * width/height before the allocations succeeded would make the
         * re-entry guard in soft_apply() read "already open at this size" on
         * the next frame, and the frame after an OOM would dereference NULL
         * buffers — a crash one frame downstream of the real failure, with
         * nothing in the log to connect them. */
        free(state->weight);
        free(state->mean);
        free(state->var);
        free(state->mask);
        free(state->opened);
        free(state->labels);
        free(state->stack);
        state->weight = NULL;
        state->mean = NULL;
        state->var = NULL;
        state->mask = NULL;
        state->opened = NULL;
        state->labels = NULL;
        state->stack = NULL;
        return -1;
    }
    state->width = width;
    state->height = height;
    state->pixels = pixels;
    /* Only on the success path, beside the geometry: a partial allocation
     * failure leaves this zero rather than claiming bytes nobody holds. */
    state->footprint_bytes = soft_footprint_for(state->config.gmm2.model_num, pixels);
    return 0;
}

static void soft_init_models(soft_state_t *state, const uint8_t *luma)
{
    const sw_gmm2_params_t *p = &state->config.gmm2;
    size_t pixels = state->pixels;
    size_t k, i;
    for (k = 0; k < (size_t)p->model_num; ++k) {
        for (i = 0; i < pixels; ++i) {
            size_t idx = k * pixels + i;
            state->weight[idx] = 0.0f;
            /* An unmatchable sentinel mean, not 0: a mean of 0 would let a
             * dark pixel "match" a zero-weight phantom mode and dodge the
             * replacement rule. Same value as the host backend. */
            state->mean[idx] = -1.0e4f;
            state->var[idx] = p->var_init;
        }
    }
    for (i = 0; i < pixels; ++i) {
        state->weight[i] = 1.0f;
        state->mean[i] = (float)luma[i];
    }
    state->initialised = true;
}

static void soft_gmm2(soft_state_t *state, const uint8_t *luma)
{
    const sw_gmm2_params_t *p = &state->config.gmm2;
    const size_t pixels = state->pixels;
    const int models = p->model_num;
    /* PRECISION, transcribed rather than chosen. The host backend's state is
     * float32, but numpy promotes wherever a Python float meets it, and the
     * promotion points are what decide a borderline pixel:
     *
     *   distance2 = (frame_f - mean) ** 2      float32 throughout
     *   matched   = distance2 <= (sigmas**2) * var
     *                                          float64 threshold, float32
     *                                          distance promoted to compare
     *   weight   += float32(lr) * (...)        float32 throughout
     *   mean     += lr * delta                 float64 product, stored float32
     *   var      += lr * (delta**2 - var)      float64 product, stored float32
     *
     * Matching those promotions here cut the measured centroid divergence
     * against the oracle by more than half. It does not make the two
     * IDENTICAL and is not meant to — E5's declared tolerance carries what
     * is left. */
    const double gate = (double)p->match_sigmas * (double)p->match_sigmas;
    const float weight_rho = p->learn_rate;
    const double rho = (double)p->learn_rate;
    size_t i;

    for (i = 0; i < pixels; ++i) {
        float value = (float)luma[i];
        int best = -1;
        float best_weight = -1.0f;
        int lowest = 0;
        float lowest_weight = 0.0f;
        float total = 0.0f;
        int k;

        /* Best matching mode = the matched mode with the highest weight;
         * ties go to the lowest index, matching numpy's argmax. */
        for (k = 0; k < models; ++k) {
            size_t idx = (size_t)k * pixels + i;
            float delta = value - state->mean[idx];
            float distance2 = delta * delta;
            if ((double)distance2 <= gate * (double)state->var[idx]) {
                if (state->weight[idx] > best_weight) {
                    best_weight = state->weight[idx];
                    best = k;
                }
            }
        }

        for (k = 0; k < models; ++k) {
            size_t idx = (size_t)k * pixels + i;
            float is_best = (k == best) ? 1.0f : 0.0f;
            /* delta comes from the OLD mean and feeds BOTH updates, exactly
             * as the host computes it once before either. */
            float delta = value - state->mean[idx];
            float distance2 = delta * delta;
            state->weight[idx] += weight_rho * (is_best - state->weight[idx]);
            if (k == best) {
                state->mean[idx] =
                    (float)((double)state->mean[idx] + rho * (double)delta);
                state->var[idx] =
                    (float)((double)state->var[idx] +
                            rho * ((double)distance2 - (double)state->var[idx]));
            }
            if (state->var[idx] < p->var_min) {
                state->var[idx] = p->var_min;
            }
        }

        if (best < 0) {
            /* Replace the LOWEST-weight mode; ties to the lowest index,
             * matching numpy's argmin. */
            lowest = 0;
            lowest_weight = state->weight[i];
            for (k = 1; k < models; ++k) {
                size_t idx = (size_t)k * pixels + i;
                if (state->weight[idx] < lowest_weight) {
                    lowest_weight = state->weight[idx];
                    lowest = k;
                }
            }
            {
                size_t idx = (size_t)lowest * pixels + i;
                state->mean[idx] = value;
                state->var[idx] = p->var_init;
                state->weight[idx] = p->weight_init;
            }
        }

        for (k = 0; k < models; ++k) {
            total += state->weight[(size_t)k * pixels + i];
        }
        if (total > 0.0f) {
            for (k = 0; k < models; ++k) {
                state->weight[(size_t)k * pixels + i] /= total;
            }
        }

        /* Background support: modes in descending weight until the
         * cumulative weight BEFORE the mode reaches bg_ratio. Selection sort
         * over at most five modes, which is cheaper than any real sort and
         * has the tie behaviour spelled out. */
        {
            int order[SW_GMM2_MAX_MODELS];
            bool used[SW_GMM2_MAX_MODELS];
            float cumulative = 0.0f;
            bool best_in_bg = false;
            int position;
            for (k = 0; k < models; ++k) {
                used[k] = false;
            }
            for (position = 0; position < models; ++position) {
                int pick = -1;
                float pick_weight = -1.0f;
                for (k = 0; k < models; ++k) {
                    float w = state->weight[(size_t)k * pixels + i];
                    if (!used[k] && w > pick_weight) {
                        pick_weight = w;
                        pick = k;
                    }
                }
                used[pick] = true;
                order[position] = pick;
                if (cumulative < p->bg_ratio && pick == best) {
                    best_in_bg = true;
                }
                cumulative += pick_weight;
            }
            SW_UNUSED(order);
            state->mask[i] = (best >= 0 && best_in_bg) ? 0 : 1;
        }
    }
}

/* Morphological opening with the structuring element OpenCV builds for
 * `getStructuringElement(MORPH_ELLIPSE, (3, 3))`.
 *
 * That element is a CROSS, not a 3x3 square:
 *
 *     0 1 0
 *     1 1 1
 *     0 1 0
 *
 * which is not what "ellipse" suggests and is not what this file assumed
 * first. The square version eroded one extra ring off every blob, and the
 * fixture caught it as a systematic ~1 px area deficit against the oracle on
 * every component (finding D8-F4). Element shape is a detector parameter,
 * not an implementation detail.
 *
 * Radii above 1 are refused rather than approximated, for the same reason:
 * a different element is a different detector, and OpenCV's ellipse for
 * larger radii is a genuine disc that this would have to reproduce exactly.
 */
static const int k_cross_dx[5] = {0, 1, -1, 0, 0};
static const int k_cross_dy[5] = {0, 0, 0, 1, -1};

static int soft_open(soft_state_t *state)
{
    const int w = state->width;
    const int h = state->height;
    int radius = state->config.open_radius_px;
    int x, y, n;
    if (radius <= 0) {
        memcpy(state->opened, state->mask, state->pixels);
        return 0;
    }
    if (radius != 1) {
        SW_LOG_ERR("open_radius_px %d is not supported by the portable detector; "
                   "only OpenCV's 3x3 ellipse (radius 1, a cross) is transcribed "
                   "exactly",
                   radius);
        return -1;
    }
    /* erode */
    for (y = 0; y < h; ++y) {
        for (x = 0; x < w; ++x) {
            uint8_t keep = 1;
            for (n = 0; n < 5; ++n) {
                int nx = x + k_cross_dx[n];
                int ny = y + k_cross_dy[n];
                /* OpenCV's default morphology border is BORDER_CONSTANT with
                 * the value that makes erosion ignore outside pixels, i.e.
                 * the frame edge does not erode. Treating outside as
                 * background would shave every edge-touching blob. */
                if (nx < 0 || ny < 0 || nx >= w || ny >= h) {
                    continue;
                }
                if (state->mask[(size_t)ny * w + nx] == 0) {
                    keep = 0;
                    break;
                }
            }
            state->opened[(size_t)y * w + x] = keep;
        }
    }
    /* dilate, back into mask */
    for (y = 0; y < h; ++y) {
        for (x = 0; x < w; ++x) {
            uint8_t hit = 0;
            for (n = 0; n < 5; ++n) {
                int nx = x + k_cross_dx[n];
                int ny = y + k_cross_dy[n];
                if (nx < 0 || ny < 0 || nx >= w || ny >= h) {
                    continue;
                }
                if (state->opened[(size_t)ny * w + nx]) {
                    hit = 1;
                    break;
                }
            }
            state->mask[(size_t)y * w + x] = hit;
        }
    }
    memcpy(state->opened, state->mask, state->pixels);
    return 0;
}

/* "Worse" in the cap's sense: smaller area first, then later in raster
 * order. Deliberately the same order `cap.py::rank_key` produces: its
 * confidence level is a monotone non-decreasing function of area
 * (`min(1.0, area_px / 50.0)`), so ranking by area and raster order is the
 * same ranking, and the component list and the cap agree about which
 * components matter. If confidence ever stops being a function of area, this
 * shedding order has to be revisited with it. */
static bool rank_worse_than(const sw_component_t *a, const sw_component_t *b)
{
    if (a->area_px != b->area_px) {
        return a->area_px < b->area_px;
    }
    if (a->centroid_v != b->centroid_v) {
        return a->centroid_v > b->centroid_v;
    }
    return a->centroid_u > b->centroid_u;
}

static int compare_components(const void *lhs, const void *rhs)
{
    const sw_component_t *a = (const sw_component_t *)lhs;
    const sw_component_t *b = (const sw_component_t *)rhs;
    if (a->centroid_v < b->centroid_v) {
        return -1;
    }
    if (a->centroid_v > b->centroid_v) {
        return 1;
    }
    if (a->centroid_u < b->centroid_u) {
        return -1;
    }
    if (a->centroid_u > b->centroid_u) {
        return 1;
    }
    return 0;
}

/* 4-connected CCL by iterative flood fill, then the same (centroid_v,
 * centroid_u) ordering the host applies in `find_components`. Iterative and
 * not recursive on purpose: a full-frame foreground would blow a 256 MB
 * node's stack, and "the frame was all white" is precisely when a node must
 * not crash. */
static int soft_label(soft_state_t *state, sw_component_t *out, int out_capacity)
{
    static const int dx4[4] = {1, -1, 0, 0};
    static const int dy4[4] = {0, 0, 1, -1};
    const int w = state->width;
    const int h = state->height;
    const uint8_t *mask = state->opened;
    int32_t *labels = state->labels;
    const int capacity = out_capacity < SW_MAX_COMPONENTS ? out_capacity
                                                          : SW_MAX_COMPONENTS;
    int32_t next_label = 1;
    int count = 0;
    int overflow = 0;
    int x, y;

    memset(labels, 0, state->pixels * sizeof(int32_t));
    for (y = 0; y < h; ++y) {
        for (x = 0; x < w; ++x) {
            size_t seed = (size_t)y * w + x;
            int32_t top = 0;
            uint32_t area = 0;
            int32_t min_x = x, max_x = x, min_y = y, max_y = y;
            sw_component_t candidate;
            if (!mask[seed] || labels[seed] != 0) {
                continue;
            }
            /* The blob is ALWAYS walked, whatever the list holds. Walking is
             * what marks it visited and what measures it, and skipping the
             * walk to save work on a crowded frame is how the old version
             * ended up keeping whatever happened to be near the top of the
             * frame. `next_label` is independent of `count` so a component
             * that is later displaced does not free its label for reuse. */
            labels[seed] = next_label++;
            state->stack[top++] = (int32_t)seed;
            while (top > 0) {
                int32_t index = state->stack[--top];
                int px = index % w;
                int py = index / w;
                int n;
                area++;
                if (px < min_x) min_x = px;
                if (px > max_x) max_x = px;
                if (py < min_y) min_y = py;
                if (py > max_y) max_y = py;
                for (n = 0; n < 4; ++n) {
                    int nx = px + dx4[n];
                    int ny = py + dy4[n];
                    size_t nidx;
                    if (nx < 0 || ny < 0 || nx >= w || ny >= h) {
                        continue;
                    }
                    nidx = (size_t)ny * w + nx;
                    if (mask[nidx] && labels[nidx] == 0) {
                        labels[nidx] = labels[seed];
                        state->stack[top++] = (int32_t)nidx;
                    }
                }
            }
            if ((int)area < state->config.min_area_px ||
                (int)area > state->config.max_area_px) {
                continue; /* area-gated, exactly as the host does */
            }
            candidate.area_px = area;
            candidate.bbox_x = min_x;
            candidate.bbox_y = min_y;
            candidate.bbox_w = (uint32_t)(max_x - min_x + 1);
            candidate.bbox_h = (uint32_t)(max_y - min_y + 1);
            {
                const sw_bbox_t bbox = {candidate.bbox_x, candidate.bbox_y,
                                        candidate.bbox_w, candidate.bbox_h};
                sw_mask_moment_t moment;
                int measured = sw_mask_moment_nonzero(
                    mask, w, h, (size_t)w, &bbox, &moment);
                if (measured != 1) {
                    SW_LOG_ERR("portable CCL produced an invalid or empty bbox "
                               "(%d,%d %ux%u)",
                               candidate.bbox_x, candidate.bbox_y,
                               candidate.bbox_w, candidate.bbox_h);
                    continue;
                }
                /* Deliberately NOT area/pixel_count. area_px is the CCL
                 * flood-fill area; the centroid is the first moment of every
                 * nonzero final-mask pixel in this accepted bbox. */
                candidate.centroid_u = moment.centroid_u;
                candidate.centroid_v = moment.centroid_v;
            }

            if (count < capacity) {
                out[count++] = candidate;
                continue;
            }
            /* The list is full. What must NOT happen is keeping the first
             * `capacity` in RASTER order: the scan runs top to bottom, so a
             * saturated frame would go blind below a scanline and the cap's
             * declared ranking ("largest areas survive") would be silently
             * pre-empted by scan position. Measured before this was fixed:
             * three large blobs emitted on a 211-component frame vanished on
             * a 1095-component one, replaced by tiny components from the top
             * rows — the cap ranking undone by the layer beneath it.
             *
             * So the list keeps the BEST `capacity` by the same order the cap
             * ranks by, and a newcomer displaces the current worst. That is
             * also what the IVE hardware does — it raises its area threshold,
             * i.e. sheds the SMALLEST regions — which is what sw_detect.h
             * promises about the two detectors on a crowded frame.
             *
             * Everything shed is COUNTED, per frame and per run. */
            {
                int worst = 0;
                int k;
                for (k = 1; k < count; ++k) {
                    if (rank_worse_than(&out[k], &out[worst])) {
                        worst = k;
                    }
                }
                state->components_over_list_bound++;
                overflow++;
                if (rank_worse_than(&out[worst], &candidate)) {
                    out[worst] = candidate;
                }
            }
        }
    }
    if (overflow > 0) {
        state->frames_over_list_bound++;
        SW_LOG_WARN("component list full at %d; %d further blob(s) on this frame "
                    "were shed by rank and counted",
                    count, overflow);
    }
    qsort(out, (size_t)count, sizeof(sw_component_t), compare_components);
    {
        sw_bbox_t boxes[SW_MAX_COMPONENTS];
        uint64_t pairs;
        int i;
        for (i = 0; i < count; ++i) {
            boxes[i].x = out[i].bbox_x;
            boxes[i].y = out[i].bbox_y;
            boxes[i].w = out[i].bbox_w;
            boxes[i].h = out[i].bbox_h;
        }
        pairs = sw_count_overlapping_bbox_pairs(boxes, (size_t)count);
        state->overlapping_bbox_pairs += pairs;
        if (pairs > 0) {
            state->frames_with_overlapping_bboxes++;
        }
    }
    return count;
}

static int soft_apply_frame(sw_detector_t *self, const uint8_t *luma, int width,
                            int height, bool warming_up, uint64_t frame_seq,
                            sw_component_t *out, int out_capacity,
                            double *occupancy)
{
    soft_state_t *state = (soft_state_t *)self->state;
    uint64_t stage_start_ns;
    size_t i;
    uint32_t foreground = 0;

    SW_UNUSED(frame_seq);

    if (state->width != width || state->height != height) {
        if (state->width != 0) {
            SW_LOG_ERR("frame is %dx%d, detector was opened at %dx%d; a resolution "
                       "change mid-stream would silently reset the background model",
                       width, height, state->width, state->height);
            return -1;
        }
        if (soft_alloc(state, width, height) != 0) {
            return -1;
        }
    }
    if (!state->initialised) {
        soft_init_models(state, luma);
        if (occupancy != NULL) {
            *occupancy = 0.0;
        }
        return 0; /* the host emits nothing on the model-init frame either */
    }
    /* Each stage below records only when its work COMPLETED; a stage that
     * refused partway is absent from the profile while frame_total (in the
     * soft_apply wrapper) still carries the frame. The instrumentation adds
     * no branch and changes no control flow, error path or counter. */
    stage_start_ns = sw_profile_now_ns();
    soft_gmm2(state, luma);
    sw_profile_record(&state->profile, state->stage_gmm2, stage_start_ns);
    if (warming_up) {
        if (occupancy != NULL) {
            *occupancy = 0.0;
        }
        return 0;
    }
    stage_start_ns = sw_profile_now_ns();
    if (soft_open(state) != 0) {
        return -1;
    }
    sw_profile_record(&state->profile, state->stage_morph, stage_start_ns);
    stage_start_ns = sw_profile_now_ns();
    for (i = 0; i < state->pixels; ++i) {
        foreground += state->opened[i] ? 1u : 0u;
    }
    sw_profile_record(&state->profile, state->stage_occupancy_scan, stage_start_ns);
    if (occupancy != NULL) {
        *occupancy = (double)foreground / (double)state->pixels;
    }
    {
        int count;
        stage_start_ns = sw_profile_now_ns();
        count = soft_label(state, out, out_capacity);
        sw_profile_record(&state->profile, state->stage_ccl_label, stage_start_ns);
        return count;
    }
}

static int soft_apply(sw_detector_t *self, const uint8_t *luma, int width, int height,
                      bool warming_up, uint64_t frame_seq, sw_component_t *out,
                      int out_capacity, double *occupancy)
{
    /* Thin timing shell so frame_total covers EVERY exit of the real body —
     * failures included — without a record call before each return. The
     * inner function is the detector; this is a stopwatch around it. */
    soft_state_t *state = (soft_state_t *)self->state;
    const uint64_t frame_start_ns = sw_profile_now_ns();
    const int result = soft_apply_frame(self, luma, width, height, warming_up,
                                        frame_seq, out, out_capacity, occupancy);
    sw_profile_record(&state->profile, state->stage_frame_total, frame_start_ns);
    return result;
}

static void soft_losses(const sw_detector_t *self, sw_detector_losses_t *out)
{
    const soft_state_t *state = (const soft_state_t *)self->state;
    out->components_shed = state != NULL ? state->components_over_list_bound : 0;
    out->frames_shed = state != NULL ? state->frames_over_list_bound : 0;
}

static void soft_diagnostics(const sw_detector_t *self,
                             sw_detector_diagnostics_t *out)
{
    const soft_state_t *state = (const soft_state_t *)self->state;
    memset(out, 0, sizeof(*out));
    if (state != NULL) {
        out->overlapping_bbox_pairs = state->overlapping_bbox_pairs;
        out->frames_with_overlapping_bboxes =
            state->frames_with_overlapping_bboxes;
    }
}

static const sw_profile_t *soft_profile(const sw_detector_t *self)
{
    const soft_state_t *state = (const soft_state_t *)self->state;
    return state != NULL ? &state->profile : NULL;
}

static size_t soft_footprint_bytes(const sw_detector_t *self)
{
    const soft_state_t *state = (const soft_state_t *)self->state;
    if (state == NULL) {
        return 0;
    }
    if (state->footprint_bytes != 0) {
        return state->footprint_bytes;
    }
    /* Nothing is allocated until the first frame arrives (soft_apply's
     * re-entry guard), and the RAM budget check deliberately runs BEFORE the
     * frame loop — so answer for the grid the detector was opened at. Same
     * expression, so the two answers cannot disagree. */
    return soft_footprint_for((int)state->config.gmm2.model_num,
                              (size_t)state->config.proc_width *
                                  (size_t)state->config.proc_height);
}

static void soft_close(sw_detector_t *self)
{
    soft_state_t *state;
    if (self == NULL) {
        return;
    }
    state = (soft_state_t *)self->state;
    if (state != NULL && state->components_over_list_bound > 0) {
        SW_LOG_WARN("portable detector: %llu component(s) over the %d-component "
                    "list bound across %llu frame(s)",
                    (unsigned long long)state->components_over_list_bound,
                    SW_MAX_COMPONENTS,
                    (unsigned long long)state->frames_over_list_bound);
    }
    soft_free(state);
    free(self);
}

sw_detector_t *sw_detect_open_soft(const sw_detector_config_t *config)
{
    sw_detector_t *detector = (sw_detector_t *)calloc(1, sizeof(sw_detector_t));
    soft_state_t *state = (soft_state_t *)calloc(1, sizeof(soft_state_t));
    if (detector == NULL || state == NULL) {
        free(detector);
        free(state);
        SW_LOG_ERR("out of memory opening the portable detector");
        return NULL;
    }
    state->config = *config;
    /* Stage registration order is emission order in --stats. Capacity is 12
     * and this registers 5, so none of these can return -1; the indices are
     * stored anyway because sw_profile_record tolerates -1 and an assumed
     * index is exactly the kind of drift the accumulator exists to rule out. */
    sw_profile_init(&state->profile);
    state->stage_gmm2 = sw_profile_stage(&state->profile, "gmm2");
    state->stage_morph = sw_profile_stage(&state->profile, "morph");
    state->stage_occupancy_scan =
        sw_profile_stage(&state->profile, "occupancy_scan");
    state->stage_ccl_label = sw_profile_stage(&state->profile, "ccl_label");
    state->stage_frame_total = sw_profile_stage(&state->profile, "frame_total");
    detector->apply = soft_apply;
    detector->close = soft_close;
    detector->losses = soft_losses;
    detector->diagnostics = soft_diagnostics;
    detector->footprint_bytes = soft_footprint_bytes;
    detector->name = "soft-gmm2";
    detector->state = state;
    detector->profile = soft_profile;
    return detector;
}

sw_detector_t *sw_detect_open(const sw_config_t *config)
{
    if (config->detector_kind == SW_DETECTOR_IVE) {
        return sw_detect_open_ive(&config->detector, config->ccl_log_path,
                                  config->fg_mask_log_path,
                                  config->fg_mask_limit);
    }
    return sw_detect_open_soft(&config->detector);
}
