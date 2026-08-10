#include "sw_pipeline.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "sw_common.h"

/* The detector's confidence in a component, mirroring
 * `detector/cap.py::component_confidence`: `min(1.0, area_px / 50.0)` with
 * `area_px` at PROC resolution, per the D0 "D8.0 amendment" entry. Area is
 * the only evidence a background model plus connected components has; the
 * NPU appearance gate (phase 2) is what would make this a real number. It is
 * a function here for the same reason it is one there: the value the cap
 * ranks by and the value that goes on the wire must have ONE definition.
 *
 * INTEGER-SAFE, in three specific senses, because this value sits inside the
 * absolute byte-identity gate (E2/E5) where a one-ULP difference from the
 * host is a failed gate:
 *
 *   1. the saturation test is an INTEGER comparison. `area_px >= 50` says
 *      what it means — the clamp is a property of the integer area — and no
 *      rounding mode, FP evaluation width or compiler flag can reach it.
 *   2. `(double)area_px` is EXACT for every uint32_t (53-bit mantissa), so
 *      the dividend is the same real number on both sides. This is the one
 *      that is genuinely load-bearing above 2^24, where `(float)area_px`
 *      is not exact.
 *   3. the division happens in DOUBLE and is narrowed once, at the
 *      assignment to the `float` wire field — the same two roundings, in the
 *      same order, that Python's double division plus protobuf's binary32
 *      store performs.
 *
 * 1 and 3 are construction, not differences you can measure here, and saying
 * so is the honest version. Checked exhaustively: `(float)a >= 50.0f` agrees
 * with `a >= 50u` over all 2^32, and `(float)a / 50.0f` agrees bit-for-bit
 * with `(float)((double)a / 50.0)` over the whole 0..49 the clamp admits —
 * the two first diverge at a = 2^24 + 1, which is reason 2's territory and
 * which the clamp puts out of reach. An all-float form would therefore also
 * be correct today. This one is written because it mirrors the host's SHAPE
 * rather than its current output, and so stays right if the divisor or the
 * saturation area ever moves.
 *
 * Returns double, not float, for the reason spelled out at the ranking
 * comparator below: the CAP must rank on the unnarrowed value the host ranks
 * on. That, too, cannot move a byte today — area breaks any tie the
 * narrowing could create — and is written for the same reason. */
#define SW_CONFIDENCE_SATURATION_AREA_PX 50.0
#define SW_CONFIDENCE_SATURATION_AREA_INT 50u

static double component_confidence(const sw_component_t *component, int persistence)
{
    SW_UNUSED(persistence); /* already its own wire field; see cap.py */
    if (component->area_px >= SW_CONFIDENCE_SATURATION_AREA_INT) {
        return 1.0;
    }
    return (double)component->area_px / SW_CONFIDENCE_SATURATION_AREA_PX;
}

void sw_pipeline_init(sw_pipeline_t *pipeline, const sw_detector_config_t *config)
{
    memset(pipeline, 0, sizeof(*pipeline));
    pipeline->config = *config;
}

/* D0 section 2: u_full = (u_proc + 0.5) * s - 0.5. */
static double proc_to_full(double value, double scale)
{
    return (value + 0.5) * scale - 0.5;
}

typedef struct {
    int component_index;
    int persistence;
    uint32_t blob_id;
} emitted_t;

/* Persistence: globally nearest-pair-first association inside the gate.
 *
 * All (component, track) pairs inside the gate are collected, sorted by
 * distance, and claimed closest-first. This is the fix a host-side
 * adversarial review forced: raster-order greedy let a one-frame flicker
 * that happened to sort earlier steal an adjacent track and inherit its
 * count, emitting a single-frame blob as persistent. */
typedef struct {
    double distance;
    int component;
    int track;
} pair_t;

static int compare_pairs(const void *lhs, const void *rhs)
{
    const pair_t *a = (const pair_t *)lhs;
    const pair_t *b = (const pair_t *)rhs;
    if (a->distance < b->distance) {
        return -1;
    }
    if (a->distance > b->distance) {
        return 1;
    }
    /* Python's `sort` on (distance, ci, ti) tuples breaks ties by component
     * then track index. Reproduced, not left to qsort's discretion: an
     * unstable tie-break here is a different detector on a symmetric frame. */
    if (a->component != b->component) {
        return a->component < b->component ? -1 : 1;
    }
    if (a->track != b->track) {
        return a->track < b->track ? -1 : 1;
    }
    return 0;
}

static int persistence_update(sw_pipeline_t *pipeline, const sw_component_t *components,
                              int component_count, emitted_t *emitted)
{
    /* Worst case is every component against every track: 254 x 254 pairs,
     * ~1.5 MB in .bss. Preallocated rather than sized to the frame, because
     * the frame loop must not allocate (RV1106_EDGE_NODE.md section 4) and
     * 0.6% of a 256 MB node is a cheaper price than a malloc that can fail
     * on the one frame where the sky is full. */
    static pair_t pairs[SW_MAX_COMPONENTS * SW_MAX_COMPONENTS];
    static sw_track_t next_tracks[SW_MAX_COMPONENTS];
    static int component_to_track[SW_MAX_COMPONENTS];
    static bool track_used[SW_MAX_COMPONENTS];
    const double gate = pipeline->config.persistence_gate_px;
    size_t pair_count = 0;
    int emitted_count = 0;
    int i, t;

    for (i = 0; i < component_count; ++i) {
        component_to_track[i] = -1;
    }
    for (t = 0; t < pipeline->track_count; ++t) {
        track_used[t] = false;
    }
    for (i = 0; i < component_count; ++i) {
        for (t = 0; t < pipeline->track_count; ++t) {
            double du = components[i].centroid_u - pipeline->tracks[t].u;
            double dv = components[i].centroid_v - pipeline->tracks[t].v;
            double d = sqrt(du * du + dv * dv);
            if (d <= gate) {
                pairs[pair_count].distance = d;
                pairs[pair_count].component = i;
                pairs[pair_count].track = t;
                pair_count++;
            }
        }
    }
    qsort(pairs, pair_count, sizeof(pair_t), compare_pairs);
    for (i = 0; i < (int)pair_count; ++i) {
        int ci = pairs[i].component;
        int ti = pairs[i].track;
        if (component_to_track[ci] >= 0 || track_used[ti]) {
            continue;
        }
        component_to_track[ci] = ti;
        track_used[ti] = true;
    }

    for (i = 0; i < component_count; ++i) {
        sw_track_t track;
        if (component_to_track[i] >= 0) {
            const sw_track_t *prior = &pipeline->tracks[component_to_track[i]];
            track.count = prior->count + 1;
            track.blob_id = prior->blob_id;
        } else {
            track.count = 1;
            track.blob_id = pipeline->next_blob_id++;
        }
        track.u = components[i].centroid_u;
        track.v = components[i].centroid_v;
        next_tracks[i] = track;
        if (track.count >= pipeline->config.persistence_frames) {
            emitted[emitted_count].component_index = i;
            emitted[emitted_count].persistence = track.count;
            emitted[emitted_count].blob_id = track.blob_id;
            emitted_count++;
        }
    }
    /* Unmatched old tracks die immediately, exactly as on the host. */
    memcpy(pipeline->tracks, next_tracks, (size_t)component_count * sizeof(sw_track_t));
    pipeline->track_count = component_count;
    return emitted_count;
}

/* Cap ranking, mirroring `detector/cap.py::rank_key`:
 *   1. -confidence   (the D8 opening's rule, now an actual quantity)
 *   2. -area_px      (today's confidence is a monotone function of area, so
 *                     this level never contradicts level 1; it is what
 *                     separates the components that saturate at 1.0)
 *   3. centroid_v then 4. centroid_u  (raster order, purely to make the
 *                     choice TOTAL — without them two equal-area blobs
 *                     would be ordered by list position, which is an
 *                     accident of the host's sort and not a rule).
 *
 * `confidence` is the UNNARROWED double for the same reason the host ranks
 * on a Python float: ranking on the binary32 wire value could tie two
 * components the host separates. Level 2 would break that tie identically
 * today, but only because area happens to be the thing confidence is made
 * of, and this comparator must not depend on that staying true. */
typedef struct {
    int index;
    double confidence;
    uint32_t area_px;
    double centroid_v;
    double centroid_u;
} ranked_t;

static int compare_ranked(const void *lhs, const void *rhs)
{
    const ranked_t *a = (const ranked_t *)lhs;
    const ranked_t *b = (const ranked_t *)rhs;
    if (a->confidence != b->confidence) {
        return a->confidence > b->confidence ? -1 : 1;
    }
    if (a->area_px != b->area_px) {
        return a->area_px > b->area_px ? -1 : 1;
    }
    if (a->centroid_v != b->centroid_v) {
        return a->centroid_v < b->centroid_v ? -1 : 1;
    }
    if (a->centroid_u != b->centroid_u) {
        return a->centroid_u < b->centroid_u ? -1 : 1;
    }
    return 0;
}

void sw_pipeline_frame(sw_pipeline_t *pipeline, const sw_component_t *components,
                       int component_count, const sw_envelope_t *envelope,
                       double scale_x, double scale_y, sw_capture_event_t *event,
                       int *dropped)
{
    static emitted_t emitted[SW_MAX_COMPONENTS];
    static ranked_t ranked[SW_MAX_COMPONENTS];
    static bool survives[SW_MAX_COMPONENTS];
    const int cap = pipeline->config.max_components_per_frame;
    int offered;
    int kept = 0;
    int i;

    memset(event, 0, sizeof(*event));
    event->envelope = *envelope;
    *dropped = 0;

    offered = persistence_update(pipeline, components, component_count, emitted);
    pipeline->frames++;
    pipeline->components_offered += (uint64_t)offered;
    if ((uint32_t)offered > pipeline->max_components_offered) {
        pipeline->max_components_offered = (uint32_t)offered;
    }

    for (i = 0; i < offered; ++i) {
        survives[i] = true;
    }
    if (offered > cap) {
        for (i = 0; i < offered; ++i) {
            const sw_component_t *c = &components[emitted[i].component_index];
            ranked[i].index = i;
            ranked[i].confidence = component_confidence(c, emitted[i].persistence);
            ranked[i].area_px = c->area_px;
            ranked[i].centroid_v = c->centroid_v;
            ranked[i].centroid_u = c->centroid_u;
        }
        qsort(ranked, (size_t)offered, sizeof(ranked_t), compare_ranked);
        for (i = 0; i < offered; ++i) {
            survives[i] = false;
        }
        for (i = 0; i < cap; ++i) {
            survives[ranked[i].index] = true;
        }
        *dropped = offered - cap;
        pipeline->frames_at_cap++;
        pipeline->components_dropped_over_cap += (uint64_t)(offered - cap);
    }

    /* Survivors keep the order they were OFFERED in, not rank order: obs_id
     * is assigned by position and re-ordering would renumber observations on
     * frames the cap never touched. */
    for (i = 0; i < offered; ++i) {
        const sw_component_t *c;
        sw_observation_t *obs;
        if (!survives[i]) {
            continue;
        }
        c = &components[emitted[i].component_index];
        obs = &event->observations[kept];
        memset(obs, 0, sizeof(*obs));
        obs->obs_id = (uint32_t)kept;
        obs->u = proc_to_full(c->centroid_u, scale_x);
        obs->v = proc_to_full(c->centroid_v, scale_y);
        obs->cov_uu = pipeline->config.centroid_cov_floor_px2;
        obs->cov_uv = 0.0;
        obs->cov_vv = pipeline->config.centroid_cov_floor_px2;
        obs->bbox_x = (int32_t)floor((double)c->bbox_x * scale_x);
        obs->bbox_y = (int32_t)floor((double)c->bbox_y * scale_y);
        obs->bbox_w = (uint32_t)ceil((double)c->bbox_w * scale_x);
        obs->bbox_h = (uint32_t)ceil((double)c->bbox_h * scale_y);
        if (obs->bbox_w < 1u) {
            obs->bbox_w = 1u;
        }
        if (obs->bbox_h < 1u) {
            obs->bbox_h = 1u;
        }
        obs->area_px = c->area_px;
        obs->persistence_count = (uint32_t)emitted[i].persistence;
        /* The one narrowing, here: the host's double goes through binary32
         * at the protobuf boundary, so this side must round exactly once
         * too. */
        obs->confidence = (float)component_confidence(c, emitted[i].persistence);
        obs->has_local_blob_id = true;
        obs->local_blob_id = emitted[i].blob_id;
        /* The measurement path carries no patches, so evidence_ref stays
         * ABSENT rather than empty (T7/W3: absent != ""). RGA crops feed the
         * separate, droppable evidence plane. */
        obs->has_evidence_ref = false;
        kept++;
    }
    event->count = (size_t)kept;
    pipeline->components_emitted += (uint64_t)kept;
}
