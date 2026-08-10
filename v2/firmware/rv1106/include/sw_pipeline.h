/* Components -> persistence -> per-frame cap -> capture event.
 *
 * The C twin of `detector/persistence.py` + `detector/cap.py` + the
 * observation-building half of `detector/runner.py`. Everything after the
 * detector is here, in ONE place, because everything after the detector is
 * shared by both detectors and must not fork.
 *
 * The three rules it carries, each of which was learned the expensive way on
 * the host side:
 *
 *   - persistence association is globally nearest-pair-first, not raster
 *     order. Raster-order greedy let a one-frame flicker that happened to
 *     sort earlier STEAL an adjacent track and inherit its count.
 *   - the cap runs AFTER persistence, so a crowded frame does not make the
 *     detector amnesiac about the blobs it dropped.
 *   - centroids are reported in FULL-resolution coordinates through the D0
 *     scaling law, and the envelope declares the proc grid it was measured
 *     on.
 */

#ifndef SW_PIPELINE_H
#define SW_PIPELINE_H

#include "sw_detect.h"
#include "sw_obs.h"

typedef struct {
    double u;
    double v;
    int count;
    uint32_t blob_id;
} sw_track_t;

typedef struct {
    sw_detector_config_t config;
    sw_track_t tracks[SW_MAX_COMPONENTS];
    int track_count;
    uint32_t next_blob_id;

    /* Cap bookkeeping. The brief: dropped components are counted in detector
     * stats and the health path, NEVER silent. */
    uint64_t frames;
    uint64_t frames_at_cap;
    uint64_t components_offered;
    uint64_t components_emitted;
    uint64_t components_dropped_over_cap;
    uint32_t max_components_offered;
} sw_pipeline_t;

void sw_pipeline_init(sw_pipeline_t *pipeline, const sw_detector_config_t *config);

/* Turn one frame's components into a capture event.
 *
 * `scale_x`/`scale_y` are full/proc, per the D0 scaling law. `event->count`
 * is at most config->max_components_per_frame; whatever the cap removed is
 * added to the counters above and returned in `*dropped`. */
void sw_pipeline_frame(sw_pipeline_t *pipeline, const sw_component_t *components,
                       int component_count, const sw_envelope_t *envelope,
                       double scale_x, double scale_y, sw_capture_event_t *event,
                       int *dropped);

#endif /* SW_PIPELINE_H */
