/* The detector interface: luma frame in, components out.
 *
 * Two implementations, chosen at run time and both always compiled where
 * their dependencies exist:
 *
 *   sw_detect_soft.c  a portable C GMM2 + 4-connected CCL whose knobs are
 *                     the same seven the host's `ive_approx` backend uses.
 *                     It is an APPROXIMATION of the host oracle, declared as
 *                     such, and it is what runs on the host and under QEMU.
 *   sw_detect_ive.c   RK_MPI_IVE_GMM2 + RK_MPI_IVE_CCL on the board's
 *                     hardware. This is the deployment detector.
 *
 * They are deliberately NOT interchangeable in the report: every number is
 * labelled with which one produced it. The D8 tolerance table exists exactly
 * because they differ, and a build that quietly substituted one for the
 * other would make that table meaningless (see sw_config_validate).
 *
 * Both return components at PROC resolution. Mapping to full-resolution
 * coordinates via the D0 scaling law happens once, in sw_pipeline.c, so
 * there is one place that law can be got wrong.
 */

#ifndef SW_DETECT_H
#define SW_DETECT_H

#include "sw_config.h"

/* IVE's CCL reports at most 254 regions (IVE_MAX_REGION_NUM) and raises its
 * own area threshold until the count fits. The portable detector uses the
 * same bound so a crowded frame behaves the same way on both. */
#define SW_MAX_COMPONENTS 254

typedef struct {
    double centroid_u; /* proc resolution */
    double centroid_v;
    uint32_t area_px;
    int32_t bbox_x;
    int32_t bbox_y;
    uint32_t bbox_w;
    uint32_t bbox_h;
} sw_component_t;

typedef struct sw_detector sw_detector_t;

/* Components the detector had to shed before the pipeline ever saw them —
 * the portable detector's list bound, or the IVE hardware raising its own
 * area threshold. It is a DIFFERENT loss from the per-frame cap (that one
 * is a policy; this one is a limit), and it must reach `--stats` and the
 * health total or it is a silent drop. */
typedef struct {
    uint64_t components_shed;
    uint64_t frames_shed;
} sw_detector_losses_t;

struct sw_detector {
    /* Feed one PROC-resolution luma frame. `warming_up` frames update the
     * background model and emit nothing, exactly as the host does.
     * Returns the component count, or -1. */
    int (*apply)(sw_detector_t *self, const uint8_t *luma, int width, int height,
                 bool warming_up, sw_component_t *out, int out_capacity,
                 double *occupancy);
    void (*close)(sw_detector_t *self);
    /* Losses so far. Every detector implements it; a detector with no bound
     * of its own returns zeros rather than leaving the caller to guess. */
    void (*losses)(const sw_detector_t *self, sw_detector_losses_t *out);
    /* The exact byte total this backend passed to its allocator for the
     * current grid. NOT an RSS reading and never a measurement — it is the
     * sum of the sizes the allocations actually asked for, which is what the
     * RAM-budget check compares a preloaded clip against. Re-deriving the
     * formula elsewhere would give two copies, one of which is not the one
     * that runs. Every backend implements it. */
    size_t (*footprint_bytes)(const sw_detector_t *self);
    const char *name;
    void *state;
};

/* Returns NULL and logs on failure. `config` must outlive the detector. */
sw_detector_t *sw_detect_open_soft(const sw_detector_config_t *config);
sw_detector_t *sw_detect_open_ive(const sw_detector_config_t *config);

/* Dispatch on config->detector_kind. */
sw_detector_t *sw_detect_open(const sw_config_t *config);

#endif /* SW_DETECT_H */
