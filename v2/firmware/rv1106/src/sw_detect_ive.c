/* The deployment detector: RK_MPI_IVE_GMM2 + RK_MPI_IVE_CCL on RV1106.
 *
 * Compiled only where the Luckfox SDK's IVE headers and libraries exist
 * (SKYWEAVE_HAVE_RKMPI). Without them this file provides a refusal, not a
 * fallback: a build that silently substituted the portable detector would
 * make every number in the D8 tolerance table unattributable.
 *
 * FOUR STRUCTURAL DIVERGENCES from the host `ive_approx` oracle, all read
 * out of the SDK headers rather than guessed, all declared in the D8 report
 * BEFORE any board run. They are properties of the hardware, not defects:
 *
 *   1. CONNECTIVITY. `rk_mpi_ive.h` documents IVE_CCL as "Only 8-Connected
 *      method is supported", while the host uses cv2 connectivity=4. Blobs
 *      that touch only diagonally are ONE component on the board and TWO on
 *      the host. The daemon therefore requests IVE_CCL_MODE_4C and reports
 *      what the hardware actually did, rather than assuming it honoured it.
 *
 *   2. NO CENTROID FROM HARDWARE. IVE_REGION_S carries u32Area and the four
 *      bbox edges and nothing else. The centroid is a first moment over the
 *      LABEL IMAGE, computed on the A7 inside each region's bbox — which is
 *      exactly the "bytes proportional to motion" rule, but it means the
 *      centroid is ours and the area is the hardware's, and the two can
 *      disagree about a pixel on the boundary.
 *
 *   3. ADAPTIVE AREA THRESHOLD. IVE_CCL_CTRL_S carries u16InitAreaThr and
 *      u16Step and the hardware RAISES the threshold until the region count
 *      fits IVE_MAX_REGION_NUM (254), reporting the value it settled on in
 *      u32CurAreaThr. The effective min_area_px on the board is therefore
 *      max(config, u32CurAreaThr) and it MOVES on cluttered frames. The
 *      daemon reads it back every frame and counts the frames where it rose
 *      above the configured floor, because a silently-raised threshold looks
 *      exactly like a quiet sky.
 *
 *   4. FIXED-POINT CONTROLS. GMM2's knobs are u8q2 / u10q0 fixed point, so
 *      the host's float learn_rate, bg_ratio and weights are QUANTISED on
 *      the way in. The quantised values the daemon actually programmed are
 *      logged at startup, so the report compares what the board ran against
 *      what the oracle ran instead of against what was asked for.
 */

#include <stdlib.h>
#include <string.h>

#include "sw_common.h"
#include "sw_detect.h"

#ifndef SKYWEAVE_HAVE_RKMPI

sw_detector_t *sw_detect_open_ive(const sw_detector_config_t *config)
{
    SW_UNUSED(config);
    SW_LOG_ERR("this build has no RKMPI/IVE SDK, so the hardware GMM2 detector "
               "does not exist in it. Refusing rather than substituting the "
               "portable detector: the D8 tolerance table is only meaningful if "
               "every number says which detector produced it. Build inside the "
               "pinned container (firmware/rv1106/docker) to get this path.");
    return NULL;
}

#else /* SKYWEAVE_HAVE_RKMPI */

#include "rk_comm_ive.h"
#include "rk_mpi_ive.h"
#include "rk_mpi_mb.h"
#include "rk_mpi_sys.h"

typedef struct {
    sw_detector_config_t config;
    int width;
    int height;
    bool initialised;

    MB_BLK src_blk;
    MB_BLK fg_blk;
    MB_BLK bg_blk;
    MB_BLK match_blk;
    MB_BLK model_blk;
    MB_BLK blob_blk;

    IVE_SRC_IMAGE_S src;
    IVE_DST_IMAGE_S fg;
    IVE_DST_IMAGE_S bg;
    IVE_DST_IMAGE_S match;
    IVE_MEM_INFO_S model;
    IVE_DST_MEM_INFO_S blob;

    IVE_GMM2_CTRL_S gmm2_ctrl;
    IVE_CCL_CTRL_S ccl_ctrl;

    /* Divergence bookkeeping, reported at the end of a run. */
    uint64_t frames;
    uint64_t frames_area_threshold_raised;
    uint32_t max_area_threshold;
    uint64_t frames_label_failed;
} ive_state_t;

/* u8q2: unsigned, two fractional bits. Rounds to nearest, saturates at the
 * representable maximum, and the CALLER is told when a value did not survive
 * the conversion — a quietly quantised learning rate is a detector nobody
 * configured. */
static uint16_t to_u8q2(float value, const char *name)
{
    float scaled = value * 4.0f;
    long rounded = (long)(scaled + 0.5f);
    if (rounded < 0) {
        rounded = 0;
    }
    if (rounded > 1023) {
        rounded = 1023;
    }
    if ((float)rounded != scaled) {
        SW_LOG_INFO("GMM2 %s: %.6f quantised to %ld/4 = %.6f (u8q2 fixed point)", name,
                    (double)value, rounded, rounded / 4.0);
    }
    return (uint16_t)rounded;
}

static uint16_t to_u10q0(float value, const char *name)
{
    long rounded = (long)(value + 0.5f);
    if (rounded < 0) {
        rounded = 0;
    }
    if (rounded > 1023) {
        SW_LOG_WARN("GMM2 %s: %.3f saturates the u10q0 control at 1023", name,
                    (double)value);
        rounded = 1023;
    }
    return (uint16_t)rounded;
}

static int alloc_block(MB_BLK *blk, RK_U64 *phy, RK_U64 *vir, RK_U32 size)
{
    MB_EXT_CONFIG_S ext;
    SW_UNUSED(ext);
    if (RK_MPI_SYS_MmzAlloc(blk, NULL, NULL, size) != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_SYS_MmzAlloc failed for %u B", size);
        return -1;
    }
    *vir = (RK_U64)(uintptr_t)RK_MPI_MB_Handle2VirAddr(*blk);
    *phy = RK_MPI_MB_Handle2PhysAddr(*blk);
    return 0;
}

static void free_block(MB_BLK *blk)
{
    if (*blk != NULL) {
        RK_MPI_SYS_MmzFree(*blk);
        *blk = NULL;
    }
}

/* Release every MMZ block this detector holds. Called on teardown AND on a
 * partial allocation failure — without the second call, a half-successful
 * ive_alloc orphaned the blocks it had already taken, and because
 * `state->initialised` is only set after success the next frame retried and
 * orphaned another set. On a 256 MB node with DMA memory that is a leak that
 * ends in a dead node, reached by nothing worse than a busy moment. */
static void ive_release_blocks(ive_state_t *state)
{
    free_block(&state->src_blk);
    free_block(&state->fg_blk);
    free_block(&state->bg_blk);
    free_block(&state->match_blk);
    free_block(&state->model_blk);
    free_block(&state->blob_blk);
}

static int ive_alloc(ive_state_t *state, int width, int height)
{
    const RK_U32 stride = (RK_U32)((width + 15) & ~15); /* IVE wants 16B stride */
    const RK_U32 plane = stride * (RK_U32)height;
    /* GMM2 model memory: model_num Gaussians per pixel, each carrying a
     * weight, a mean and a variance in the hardware's packed layout. The SDK
     * sample sizes it as 12 bytes per model per pixel; the daemon uses the
     * same figure and checks the call's return rather than trusting it. */
    const RK_U32 model_bytes = plane * (RK_U32)state->config.gmm2.model_num * 12u;

    if (alloc_block(&state->src_blk, &state->src.au64PhyAddr[0],
                    &state->src.au64VirAddr[0], plane) != 0 ||
        alloc_block(&state->fg_blk, &state->fg.au64PhyAddr[0],
                    &state->fg.au64VirAddr[0], plane) != 0 ||
        alloc_block(&state->bg_blk, &state->bg.au64PhyAddr[0],
                    &state->bg.au64VirAddr[0], plane) != 0 ||
        alloc_block(&state->match_blk, &state->match.au64PhyAddr[0],
                    &state->match.au64VirAddr[0], plane) != 0 ||
        alloc_block(&state->model_blk, &state->model.u64PhyAddr,
                    &state->model.u64VirAddr, model_bytes) != 0 ||
        alloc_block(&state->blob_blk, &state->blob.u64PhyAddr,
                    &state->blob.u64VirAddr, (RK_U32)sizeof(IVE_CCBLOB_S)) != 0) {
        SW_LOG_ERR("IVE allocation failed; releasing the blocks already taken "
                   "rather than orphaning them and retrying next frame");
        ive_release_blocks(state);
        state->width = 0;
        state->height = 0;
        return -1;
    }
    state->model.u32Size = model_bytes;
    state->blob.u32Size = (RK_U32)sizeof(IVE_CCBLOB_S);
    memset((void *)(uintptr_t)state->model.u64VirAddr, 0, model_bytes);

    state->src.au32Stride[0] = stride;
    state->src.u32Width = (RK_U32)width;
    state->src.u32Height = (RK_U32)height;
    state->src.enType = IVE_IMAGE_TYPE_U8C1;
    state->fg = state->src;
    state->fg.au64PhyAddr[0] = RK_MPI_MB_Handle2PhysAddr(state->fg_blk);
    state->fg.au64VirAddr[0] = (RK_U64)(uintptr_t)RK_MPI_MB_Handle2VirAddr(state->fg_blk);
    state->bg = state->src;
    state->bg.au64PhyAddr[0] = RK_MPI_MB_Handle2PhysAddr(state->bg_blk);
    state->bg.au64VirAddr[0] = (RK_U64)(uintptr_t)RK_MPI_MB_Handle2VirAddr(state->bg_blk);
    state->match = state->src;
    state->match.au64PhyAddr[0] = RK_MPI_MB_Handle2PhysAddr(state->match_blk);
    state->match.au64VirAddr[0] =
        (RK_U64)(uintptr_t)RK_MPI_MB_Handle2VirAddr(state->match_blk);
    state->width = width;
    state->height = height;
    return 0;
}

static void ive_configure(ive_state_t *state)
{
    const sw_gmm2_params_t *p = &state->config.gmm2;
    memset(&state->gmm2_ctrl, 0, sizeof(state->gmm2_ctrl));
    state->gmm2_ctrl.u8PicFormat = 0; /* U8C1 luma */
    state->gmm2_ctrl.u8FirstFrameFlag = 1;
    state->gmm2_ctrl.u8EnBgOut = 0; /* no background image; nothing reads it */
    state->gmm2_ctrl.u8MaxModelNum = (RK_U8)p->model_num;
    state->gmm2_ctrl.u8UseVarFactor = 0;
    state->gmm2_ctrl.u8GlobalLearningRateMode = 1;
    state->gmm2_ctrl.u8UpdateVar = 1;
    state->gmm2_ctrl.u8q2WeightInitVal = to_u8q2(p->weight_init, "weight_init");
    state->gmm2_ctrl.u8q2WeightAddFactor = to_u8q2(p->learn_rate, "learn_rate(add)");
    state->gmm2_ctrl.u8q2WeightReduFactor = to_u8q2(p->learn_rate, "learn_rate(reduce)");
    state->gmm2_ctrl.u8q2WeightThr = to_u8q2(p->weight_init, "weight_thr");
    state->gmm2_ctrl.u8VarThreshGen = (RK_U8)(p->match_sigmas * p->match_sigmas + 0.5f);
    state->gmm2_ctrl.u8q2BgRatio = to_u8q2(p->bg_ratio, "bg_ratio");
    state->gmm2_ctrl.u10q0InitVar = to_u10q0(p->var_init, "var_init");
    state->gmm2_ctrl.u10q0MinVar = to_u10q0(p->var_min, "var_min");
    state->gmm2_ctrl.u10q0MaxVar = 1023;
    state->gmm2_ctrl.u8VarThr = (RK_U8)(p->match_sigmas * p->match_sigmas + 0.5f);

    memset(&state->ccl_ctrl, 0, sizeof(state->ccl_ctrl));
    /* Asked for 4-connected to match the host oracle. The header says only
     * 8-connected is supported, so what the hardware DOES is recorded as a
     * declared divergence rather than assumed away. */
    state->ccl_ctrl.enMode = IVE_CCL_MODE_4C;
    state->ccl_ctrl.u16InitAreaThr = (RK_U16)state->config.min_area_px;
    state->ccl_ctrl.u16Step = 1;
    state->ccl_ctrl.stMem = state->blob;
}

static int ive_apply(sw_detector_t *self, const uint8_t *luma, int width, int height,
                     bool warming_up, sw_component_t *out, int out_capacity,
                     double *occupancy)
{
    ive_state_t *state = (ive_state_t *)self->state;
    IVE_HANDLE handle = 0;
    bool finished = false;
    const IVE_CCBLOB_S *blob;
    const uint8_t *labels;
    RK_U32 stride;
    int count = 0;
    int row;
    unsigned region;
    uint64_t foreground = 0;

    if (!state->initialised) {
        if (ive_alloc(state, width, height) != 0) {
            return -1;
        }
        ive_configure(state);
        state->initialised = true;
    } else if (state->width != width || state->height != height) {
        SW_LOG_ERR("frame is %dx%d, IVE was opened at %dx%d", width, height,
                   state->width, state->height);
        return -1;
    }
    stride = state->src.au32Stride[0];

    /* Copy the luma into the DMA-visible source buffer, row by row because
     * the IVE stride is 16-byte aligned and the caller's is not. On the
     * capture path this copy does not exist: the VI hands over a dma-buf and
     * the source image points straight at it (sw_capture.c). */
    for (row = 0; row < height; ++row) {
        memcpy((uint8_t *)(uintptr_t)state->src.au64VirAddr[0] + (size_t)row * stride,
               luma + (size_t)row * width, (size_t)width);
    }

    if (RK_MPI_IVE_GMM2(&handle, &state->src, NULL, &state->fg, &state->bg,
                        &state->match, &state->model, &state->gmm2_ctrl, RK_TRUE) !=
        RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_IVE_GMM2 failed on frame %llu",
                   (unsigned long long)state->frames);
        return -1;
    }
    if (RK_MPI_IVE_Query(handle, &finished, RK_TRUE) != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_IVE_Query failed after GMM2");
        return -1;
    }
    state->gmm2_ctrl.u8FirstFrameFlag = 0;
    state->frames++;

    if (warming_up) {
        if (occupancy != NULL) {
            *occupancy = 0.0;
        }
        return 0;
    }

    /* CCL is IN PLACE on the foreground image: input binary mask, output
     * label image. The labels are what the centroid moment is taken over. */
    if (RK_MPI_IVE_CCL(&handle, &state->fg, &state->blob, &state->ccl_ctrl, RK_TRUE) !=
        RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_IVE_CCL failed on frame %llu",
                   (unsigned long long)state->frames);
        return -1;
    }
    if (RK_MPI_IVE_Query(handle, &finished, RK_TRUE) != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_IVE_Query failed after CCL");
        return -1;
    }

    blob = (const IVE_CCBLOB_S *)(uintptr_t)state->blob.u64VirAddr;
    labels = (const uint8_t *)(uintptr_t)state->fg.au64VirAddr[0];

    if (blob->s8LabelStatus != 0) {
        /* The hardware could not label this frame. Counted and reported as
         * a DROPPED FRAME, never as an empty one: "no blobs" and "the
         * labeller gave up" are different facts about the sky. */
        state->frames_label_failed++;
        SW_LOG_WARN("IVE CCL reported label failure on frame %llu; frame dropped",
                    (unsigned long long)state->frames);
        return -1;
    }
    if (blob->u32CurAreaThr > (RK_U32)state->config.min_area_px) {
        state->frames_area_threshold_raised++;
        if (blob->u32CurAreaThr > state->max_area_threshold) {
            state->max_area_threshold = blob->u32CurAreaThr;
        }
        SW_LOG_WARN("IVE CCL raised its area threshold to %u (configured floor %d) "
                    "on frame %llu: the frame was crowded enough that the hardware "
                    "discarded small regions to fit its 254-region table",
                    blob->u32CurAreaThr, state->config.min_area_px,
                    (unsigned long long)state->frames);
    }

    for (region = 0; region < blob->u8RegionNum && count < out_capacity; ++region) {
        const IVE_REGION_S *r = &blob->astRegion[region];
        double sum_u = 0.0;
        double sum_v = 0.0;
        uint32_t area = 0;
        int x, y;
        if (r->u32Area == 0) {
            continue; /* "non-continuous stored": empty slots are skipped */
        }
        if ((int)r->u32Area < state->config.min_area_px ||
            (int)r->u32Area > state->config.max_area_px) {
            continue;
        }
        /* First moment over the label image, inside the region's own bbox.
         * `label = ArrayIndex + 1` per the SDK header. */
        for (y = r->u16Top; y <= (int)r->u16Bottom; ++y) {
            for (x = r->u16Left; x <= (int)r->u16Right; ++x) {
                if (labels[(size_t)y * stride + (size_t)x] == region + 1) {
                    sum_u += (double)x;
                    sum_v += (double)y;
                    area++;
                }
            }
        }
        if (area == 0) {
            SW_LOG_WARN("region %u declares area %u but no pixel carries its label",
                        region, r->u32Area);
            continue;
        }
        if (area != r->u32Area) {
            /* The hardware's area and our moment disagree. Reported, not
             * reconciled: it is exactly the kind of divergence the D8
             * tolerance table exists to carry. */
            SW_LOG_DEBUG("region %u: hardware area %u, label-image area %u", region,
                         r->u32Area, area);
        }
        out[count].centroid_u = sum_u / (double)area;
        out[count].centroid_v = sum_v / (double)area;
        out[count].area_px = r->u32Area;
        out[count].bbox_x = r->u16Left;
        out[count].bbox_y = r->u16Top;
        out[count].bbox_w = (uint32_t)(r->u16Right - r->u16Left + 1);
        out[count].bbox_h = (uint32_t)(r->u16Bottom - r->u16Top + 1);
        count++;
        foreground += area;
    }
    if (occupancy != NULL) {
        *occupancy = (double)foreground / ((double)width * (double)height);
    }
    return count;
}

static void ive_losses(const sw_detector_t *self, sw_detector_losses_t *out)
{
    const ive_state_t *state = (const ive_state_t *)self->state;
    /* The hardware sheds by RAISING its area threshold, so what it dropped
     * is not individually countable — only the frames on which it happened
     * are. Reported as frames with a zero component count rather than as an
     * invented number: an estimate here would be a fabricated measurement.
     * Frames the labeller failed are counted too; those lost everything. */
    out->components_shed = 0;
    out->frames_shed = state != NULL
                           ? state->frames_area_threshold_raised +
                                 state->frames_label_failed
                           : 0;
}

static void ive_close(sw_detector_t *self)
{
    ive_state_t *state;
    if (self == NULL) {
        return;
    }
    state = (ive_state_t *)self->state;
    if (state != NULL) {
        SW_LOG_INFO("IVE detector: %llu frames, area threshold raised on %llu, "
                    "max threshold %u, label failures %llu",
                    (unsigned long long)state->frames,
                    (unsigned long long)state->frames_area_threshold_raised,
                    state->max_area_threshold,
                    (unsigned long long)state->frames_label_failed);
        ive_release_blocks(state);
        free(state);
    }
    free(self);
}

sw_detector_t *sw_detect_open_ive(const sw_detector_config_t *config)
{
    sw_detector_t *detector = (sw_detector_t *)calloc(1, sizeof(sw_detector_t));
    ive_state_t *state = (ive_state_t *)calloc(1, sizeof(ive_state_t));
    if (detector == NULL || state == NULL) {
        free(detector);
        free(state);
        SW_LOG_ERR("out of memory opening the IVE detector");
        return NULL;
    }
    state->config = *config;
    detector->apply = ive_apply;
    detector->close = ive_close;
    detector->losses = ive_losses;
    detector->name = "ive-gmm2";
    detector->state = state;
    return detector;
}

#endif /* SKYWEAVE_HAVE_RKMPI */
