/* The deployment detector: RK_MPI_IVE_GMM2 + RK_MPI_IVE_CCL on RV1106.
 *
 * Compiled only where the Luckfox SDK's IVE headers and libraries exist
 * (SKYWEAVE_HAVE_RKMPI). Without them this file provides a refusal, not a
 * fallback: a build that silently substituted the portable detector would
 * make every number in the D8 tolerance table unattributable.
 *
 * FOUR STRUCTURAL DIVERGENCES from the host `ive_approx` oracle, declared in
 * the D8 report before the original board run from exposed SDK interfaces and
 * representations. They are diagnostics, not defects or claims about opaque
 * vendor-library internals:
 *
 *   1. CONNECTIVITY. `rk_mpi_ive.h` documents IVE_CCL as "Only 8-Connected
 *      method is supported", while the host uses cv2 connectivity=4. Blobs
 *      that touch only diagonally are ONE component on the board and TWO on
 *      the host. The daemon therefore selects IVE_CCL_MODE_8C explicitly;
 *      the host retains both its authoritative 4C and diagnostic 8C views.
 *
 *   2. NO CENTROID FROM HARDWARE. IVE_REGION_S carries u32Area and the four
 *      bbox edges and nothing else. The centroid is a first moment over the
 *      preserved post-morph BINARY MASK, computed on the A7 inside each
 *      accepted bbox. The centroid is ours and the area remains the
 *      hardware's CCL/flood-fill area; overlapping bboxes can therefore
 *      measure some of the same nonzero mask pixels.
 *
 *   3. REPORTED AREA THRESHOLD. IVE_CCL_CTRL_S carries u16InitAreaThr and
 *      u16Step, while IVE_CCBLOB_S reports u32CurAreaThr. The daemon retains
 *      that SDK telemetry every frame and counts the frames where it exceeds
 *      the configured floor. The pinned headers do not prove the vendor
 *      library's internal threshold-selection algorithm, so this diagnostic
 *      must not be treated as a causal explanation by itself.
 *
 *   4. FIXED-POINT CONTROLS. GMM2's knobs are u8q2 / u10q0 fixed point, so
 *      the host's float learn_rate, bg_ratio and weights are QUANTISED on
 *      the way in. The quantised values the daemon actually programmed are
 *      logged at startup, so the report compares what the board ran against
 *      what the oracle ran instead of against what was asked for.
 */

#include <float.h>
#include <inttypes.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sw_ccl_measure.h"
#include "sw_common.h"
#include "sw_detect.h"

#if FLT_RADIX != 2 || DBL_MANT_DIG != 53 || DBL_MAX_EXP != 1024
#error "CCL centroid provenance requires IEEE 754 binary64 double"
#endif

SW_STATIC_ASSERT(sizeof(double) == sizeof(uint64_t),
                 centroid_binary64_storage_width);

#ifndef SKYWEAVE_HAVE_RKMPI

sw_detector_t *sw_detect_open_ive(const sw_detector_config_t *config,
                                  const char *ccl_log_path,
                                  const char *fg_mask_log_path,
                                  uint32_t fg_mask_limit)
{
    SW_UNUSED(config);
    SW_UNUSED(ccl_log_path);
    SW_UNUSED(fg_mask_log_path);
    SW_UNUSED(fg_mask_limit);
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
#include "rk_mpi_mmz.h"

SW_STATIC_ASSERT(SW_MAX_COMPONENTS == IVE_MAX_REGION_NUM,
                 component_bound_matches_ive_table);

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
    MB_BLK ccl_mem_blk;

    IVE_SRC_IMAGE_S src;
    IVE_DST_IMAGE_S fg;
    IVE_DST_IMAGE_S bg;
    IVE_DST_IMAGE_S match;
    IVE_MEM_INFO_S model;
    IVE_DST_MEM_INFO_S blob;
    IVE_MEM_INFO_S ccl_mem;

    IVE_GMM2_CTRL_S gmm2_ctrl;
    IVE_CCL_CTRL_S ccl_ctrl;
    IVE_ERODE_CTRL_S erode_ctrl;
    IVE_DILATE_CTRL_S dilate_ctrl;

    FILE *ccl_log;
    FILE *fg_mask_log;
    uint32_t fg_mask_limit;

    /* Divergence bookkeeping, reported at the end of a run. */
    uint64_t frames;
    sw_detector_diagnostics_t diagnostics;

    /* What ive_alloc actually asked MMZ for, summed from the same
     * expressions the alloc_block calls use. The RAM-budget check reads this
     * rather than re-deriving four-planes-plus-12-B/model/px elsewhere. Note
     * it is MMZ/CMA memory, so it does NOT appear in the process RSS the
     * harness samples: the two numbers are different quantities and must
     * never be added. */
    size_t footprint_bytes;

    /* Intra-frame timing, accumulated on EVERY frame whether or not
     * --profile-stats will emit it: the whole cost is 16-18 clock_gettime
     * calls per post-warm ~69 ms frame (morphology off/on) — each recorded
     * span pays an explicit start read plus the read inside
     * sw_profile_record, and on this vDSO-less uClibc each is a real
     * syscall (~1-5 us on the 1 GHz A7), an honest ~20-90 us, still
     * <= 0.13% of the frame — and a profiler that only runs when watched
     * measures a different detector than the one that runs unwatched. The
     * stage indices come from sw_profile_stage at open() and are stored
     * rather than assumed, so registration order and emission order cannot
     * drift apart. */
    sw_profile_t profile;
    int stage_src_copy;
    int stage_gmm2;
    int stage_erode;
    int stage_dilate;
    int stage_mask_preserve;
    int stage_ccl;
    int stage_region_scan;
    int stage_diag_io;
    int stage_frame_total;
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

/* The STANDALONE MMZ allocator, not rockit's. Rockit's RK_MPI_SYS_MmzAlloc
 * hands back blocks whose RK_MPI_MB_Handle2PhysAddr is 0 even after a
 * successful RK_MPI_SYS_Init (measured on a board, F-C1-5), and the RVE is a
 * no-IOMMU AXI master whose kernel driver programs whatever address
 * userspace supplies into the engine unvalidated — a DMA to physical 0 is
 * the bus wedge that killed two boards. Rockchip's own IVE sample allocates
 * every engine buffer exactly this way: CMA-typed, uncacheable, physical
 * address from RK_MPI_MMZ_Handle2PhysAddr. */
static int alloc_block(MB_BLK *blk, RK_U64 *phy, RK_U64 *vir, RK_U32 size)
{
    if (RK_MPI_MMZ_Alloc(blk, size,
                         RK_MMZ_ALLOC_TYPE_CMA | RK_MMZ_ALLOC_UNCACHEABLE) !=
        RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_MMZ_Alloc failed for %u B", size);
        *blk = NULL;  /* defensive: never leave a dirty handle for free_block */
        return -1;
    }
    *vir = (RK_U64)(uintptr_t)RK_MPI_MMZ_Handle2VirAddr(*blk);
    *phy = RK_MPI_MMZ_Handle2PhysAddr(*blk);
    /* F-C1-5 guard: an address the allocator cannot vouch for is a refusal,
     * never a submission — the engine would wedge the SoC, not error. */
    if (*phy == 0 || *vir == 0) {
        SW_LOG_ERR("MMZ block of %u B came back phys 0x%llx / virt 0x%llx: an "
                   "unverifiable address must not reach a no-IOMMU DMA engine "
                   "(F-C1-5). Refusing before the bus wedges.",
                   size, (unsigned long long)*phy, (unsigned long long)*vir);
        RK_MPI_MMZ_Free(*blk);
        *blk = NULL;
        return -1;
    }
    return 0;
}

static void free_block(MB_BLK *blk)
{
    if (*blk != NULL) {
        RK_MPI_MMZ_Free(*blk);
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
    free_block(&state->ccl_mem_blk);
}

/* The block sizes ive_alloc asks MMZ for, in ONE place. ive_alloc allocates
 * from it and ive_footprint_bytes answers with it for the CONFIGURED grid
 * before the first frame has allocated anything — the RAM budget check runs
 * before the frame loop. Two copies of the stride/plane/model arithmetic
 * would mean the budget check could pass against a formula that is not the
 * one that runs. */
typedef struct {
    RK_U32 stride;
    RK_U32 plane;
    RK_U32 model_bytes;
} ive_geometry_t;

static ive_geometry_t ive_geometry(const ive_state_t *state, int width, int height)
{
    ive_geometry_t geometry;
    geometry.stride = (RK_U32)((width + 15) & ~15); /* IVE wants 16B stride */
    geometry.plane = geometry.stride * (RK_U32)height;
    /* GMM2 model memory: model_num Gaussians per pixel, each carrying a
     * weight, a mean and a variance in the hardware's packed layout. The SDK
     * sample sizes it as 12 bytes per model per pixel; the daemon uses the
     * same figure and checks the call's return rather than trusting it. */
    geometry.model_bytes = geometry.plane * (RK_U32)state->config.gmm2.model_num * 12u;
    return geometry;
}

static size_t ive_footprint_for(const ive_geometry_t *geometry)
{
    /* Four U8 planes (src, fg, bg, match), the model store, the blob, and
     * CCL's staging memory. The staging term is the hardware's demand, not
     * ours: librve refuses CCL outright with "at least (w * h) bytes of
     * staging memory needed for ccl" (measured on a board, 2026-08-20), so
     * one more plane joins the arithmetic — which makes the real per-pixel
     * figure 41 B, not the 40 B the D8-F11 arithmetic declared. The
     * harness-side mirror still says 40 B/px; the delta is recorded in
     * D8_1_C1_FINDINGS.md for the planning session, and the daemon's budget
     * check uses THIS figure, the one that allocates. */
    return 5u * (size_t)geometry->plane + (size_t)geometry->model_bytes +
           sizeof(IVE_CCBLOB_S);
}

static int ive_alloc(ive_state_t *state, int width, int height)
{
    const ive_geometry_t geometry = ive_geometry(state, width, height);
    const RK_U32 stride = geometry.stride;
    const RK_U32 plane = geometry.plane;
    const RK_U32 model_bytes = geometry.model_bytes;

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
                    &state->blob.u64VirAddr, (RK_U32)sizeof(IVE_CCBLOB_S)) != 0 ||
        alloc_block(&state->ccl_mem_blk, &state->ccl_mem.u64PhyAddr,
                    &state->ccl_mem.u64VirAddr, plane) != 0) {
        SW_LOG_ERR("IVE allocation failed; releasing the blocks already taken "
                   "rather than orphaning them and retrying next frame");
        ive_release_blocks(state);
        state->width = 0;
        state->height = 0;
        return -1;
    }
    /* Only after all seven blocks succeeded: a partial allocation released
     * everything above and left the geometry at zero, so this stays zero too
     * rather than claiming memory nobody holds. */
    state->footprint_bytes = ive_footprint_for(&geometry);
    state->model.u32Size = model_bytes;
    state->blob.u32Size = (RK_U32)sizeof(IVE_CCBLOB_S);
    state->ccl_mem.u32Size = plane;
    memset((void *)(uintptr_t)state->model.u64VirAddr, 0, model_bytes);

    state->src.au32Stride[0] = stride;
    state->src.u32Width = (RK_U32)width;
    state->src.u32Height = (RK_U32)height;
    state->src.enType = IVE_IMAGE_TYPE_U8C1;
    state->fg = state->src;
    state->fg.au64PhyAddr[0] = RK_MPI_MMZ_Handle2PhysAddr(state->fg_blk);
    state->fg.au64VirAddr[0] =
        (RK_U64)(uintptr_t)RK_MPI_MMZ_Handle2VirAddr(state->fg_blk);
    state->bg = state->src;
    state->bg.au64PhyAddr[0] = RK_MPI_MMZ_Handle2PhysAddr(state->bg_blk);
    state->bg.au64VirAddr[0] =
        (RK_U64)(uintptr_t)RK_MPI_MMZ_Handle2VirAddr(state->bg_blk);
    state->match = state->src;
    state->match.au64PhyAddr[0] = RK_MPI_MMZ_Handle2PhysAddr(state->match_blk);
    state->match.au64VirAddr[0] =
        (RK_U64)(uintptr_t)RK_MPI_MMZ_Handle2VirAddr(state->match_blk);
    state->width = width;
    state->height = height;
    /* The addresses the engine will master, on the record BEFORE the first
     * submission: if a board still wedges, the last committed log line says
     * whether these looked like CMA or like garbage (F-C1-5 forensics). */
    SW_LOG_INFO("IVE MMZ blocks: src 0x%llx fg 0x%llx bg 0x%llx match 0x%llx "
                "model 0x%llx (%u B) blob 0x%llx ccl 0x%llx — physical, from "
                "RK_MPI_MMZ_Handle2PhysAddr; the RVE consumes these raw.",
                (unsigned long long)state->src.au64PhyAddr[0],
                (unsigned long long)state->fg.au64PhyAddr[0],
                (unsigned long long)state->bg.au64PhyAddr[0],
                (unsigned long long)state->match.au64PhyAddr[0],
                (unsigned long long)state->model.u64PhyAddr, model_bytes,
                (unsigned long long)state->blob.u64PhyAddr,
                (unsigned long long)state->ccl_mem.u64PhyAddr);
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
    /* The pinned SDK says only 8-connected CCL is supported. Select that
     * mode explicitly so Phase 1 never relies on an unsupported 4C request
     * being ignored by the implementation. */
    state->ccl_ctrl.enMode = IVE_CCL_MODE_8C;
    state->ccl_ctrl.u16InitAreaThr = (RK_U16)state->config.min_area_px;
    state->ccl_ctrl.u16Step = 1;
    /* CCL's staging memory is its OWN plane-sized block, never the blob:
     * stMem is the algorithm's working area (>= w*h bytes, enforced by
     * librve), while the blob is the region-table output. Handing the 3 KB
     * blob to both was the post-wedge failure mode of 2026-08-20. */
    state->ccl_ctrl.stMem = state->ccl_mem;

    /* OpenCV's 3x3 ellipse is this cross. IVE only accepts a 5x5 control
     * array, so embed the cross in its centre; every other tap stays zero. */
    memset(&state->erode_ctrl, 0, sizeof(state->erode_ctrl));
    memset(&state->dilate_ctrl, 0, sizeof(state->dilate_ctrl));
    state->erode_ctrl.au8Mask[1 * 5 + 2] = 255;
    state->erode_ctrl.au8Mask[2 * 5 + 1] = 255;
    state->erode_ctrl.au8Mask[2 * 5 + 2] = 255;
    state->erode_ctrl.au8Mask[2 * 5 + 3] = 255;
    state->erode_ctrl.au8Mask[3 * 5 + 2] = 255;
    memcpy(state->dilate_ctrl.au8Mask, state->erode_ctrl.au8Mask,
           sizeof(state->dilate_ctrl.au8Mask));
}

/* --ccl-log JSONL schema. Every actual post-warm CCL call gets one row.
 * Completed calls carry the hardware s8/u8/u32 fields; an API/query failure
 * has api_failure=true and a null s8 label status because no hardware label
 * result exists. Successful rows retain the exhaustive count of nonzero table
 * slots, the raw u8RegionNum telemetry, their explicit disagreement bit, and
 * the area-gated accepted component list. */
static void ive_log_ccl_attempt(ive_state_t *state, uint64_t frame_seq,
                                bool api_failure, int label_status,
                                uint32_t region_num,
                                uint32_t area_threshold,
                                uint32_t nonzero_region_slots,
                                const sw_component_t *components,
                                int accepted_count,
                                uint64_t overlap_pairs)
{
    int i;
    if (state->ccl_log == NULL) {
        return;
    }
    fprintf(state->ccl_log,
            "{\"frame_seq\":%llu,\"api_failure\":%s,"
            "\"s8_label_status\":",
            (unsigned long long)frame_seq, api_failure ? "true" : "false");
    if (api_failure) {
        fputs("null", state->ccl_log);
    } else {
        fprintf(state->ccl_log, "%d", label_status);
    }
    fprintf(state->ccl_log,
            ",\"u8_region_num\":%u,\"u32_cur_area_thr\":%u,"
            "\"nonzero_region_slots\":%u,\"region_count_mismatch\":%s,"
            "\"accepted_components\":%d,"
            "\"overlap_pairs\":%llu",
            region_num, area_threshold,
            (!api_failure && label_status == 0) ? nonzero_region_slots : 0,
            (!api_failure && label_status == 0 &&
             region_num != nonzero_region_slots) ? "true" : "false",
            (!api_failure && label_status == 0) ? accepted_count : 0,
            (unsigned long long)overlap_pairs);
    if (!api_failure && label_status == 0) {
        fputs(",\"components\":[", state->ccl_log);
        for (i = 0; i < accepted_count; ++i) {
            uint64_t centroid_u_bits;
            uint64_t centroid_v_bits;
            if (i > 0) {
                fputc(',', state->ccl_log);
            }
            memcpy(&centroid_u_bits, &components[i].centroid_u,
                   sizeof(centroid_u_bits));
            memcpy(&centroid_v_bits, &components[i].centroid_v,
                   sizeof(centroid_v_bits));
            fprintf(state->ccl_log,
                    "{\"centroid_u\":%.17g,"
                    "\"centroid_u_bits\":\"%016" PRIx64 "\","
                    "\"centroid_v\":%.17g,"
                    "\"centroid_v_bits\":\"%016" PRIx64 "\","
                    "\"bbox_x\":%d,\"bbox_y\":%d,\"bbox_w\":%u,"
                    "\"bbox_h\":%u,\"area_px\":%u}",
                    components[i].centroid_u, centroid_u_bits,
                    components[i].centroid_v, centroid_v_bits,
                    components[i].bbox_x, components[i].bbox_y,
                    components[i].bbox_w, components[i].bbox_h,
                    components[i].area_px);
        }
        fputc(']', state->ccl_log);
    }
    fputs("}\n", state->ccl_log);
    if (fflush(state->ccl_log) != 0 || ferror(state->ccl_log)) {
        SW_LOG_ERR("write failed for --ccl-log; disabling the diagnostic stream");
        fclose(state->ccl_log);
        state->ccl_log = NULL;
    }
}

static void store_be32(uint8_t *dst, uint32_t value)
{
    dst[0] = (uint8_t)(value >> 24);
    dst[1] = (uint8_t)(value >> 16);
    dst[2] = (uint8_t)(value >> 8);
    dst[3] = (uint8_t)value;
}

static void store_be64(uint8_t *dst, uint64_t value)
{
    store_be32(dst, (uint32_t)(value >> 32));
    store_be32(dst + 4, (uint32_t)value);
}

/* --fg-mask-log record (all integers big-endian):
 *   4 B "SWFM", u8 version=1, 3 B reserved,
 *   u64 frame_seq, u32 width, u32 height, u32 payload_len,
 *   then width*height raw final-mask bytes with stride padding removed.
 */
static void ive_log_failed_mask(ive_state_t *state, uint64_t frame_seq,
                                const uint8_t *mask, uint32_t stride)
{
    uint8_t header[28] = {'S', 'W', 'F', 'M', 1, 0, 0, 0};
    uint64_t payload;
    int row;
    if (state->fg_mask_log == NULL ||
        state->diagnostics.fg_masks_written >= state->fg_mask_limit) {
        return;
    }
    payload = (uint64_t)(uint32_t)state->width * (uint64_t)(uint32_t)state->height;
    if (payload > UINT32_MAX) {
        goto write_failed;
    }
    store_be64(header + 8, frame_seq);
    store_be32(header + 16, (uint32_t)state->width);
    store_be32(header + 20, (uint32_t)state->height);
    store_be32(header + 24, (uint32_t)payload);
    if (fwrite(header, 1, sizeof(header), state->fg_mask_log) != sizeof(header)) {
        goto write_failed;
    }
    for (row = 0; row < state->height; ++row) {
        if (fwrite(mask + (size_t)row * stride, 1, (size_t)state->width,
                   state->fg_mask_log) != (size_t)state->width) {
            goto write_failed;
        }
    }
    if (fflush(state->fg_mask_log) != 0 || ferror(state->fg_mask_log)) {
        goto write_failed;
    }
    state->diagnostics.fg_masks_written++;
    return;

write_failed:
    state->diagnostics.fg_mask_write_failures++;
    SW_LOG_ERR("write failed for --fg-mask-log; disabling the bounded mask stream");
    fclose(state->fg_mask_log);
    state->fg_mask_log = NULL;
}

static int ive_prepare_ccl_mask(ive_state_t *state, uint64_t frame_seq)
{
    IVE_HANDLE handle = 0;
    bool finished = false;
    size_t plane = (size_t)state->src.au32Stride[0] * (size_t)state->height;
    uint64_t stage_start_ns;
    if (state->config.open_radius_px == 1) {
        /* Each morphology stage records only when its op AND its Query
         * completed; a failed op returns unrecorded, and frame_total (in the
         * ive_apply wrapper) still carries the frame. No control flow, error
         * path or counter changes hands here — the timers are bystanders. */
        stage_start_ns = sw_profile_now_ns();
        if (RK_MPI_IVE_Erode(&handle, &state->fg, &state->bg,
                             &state->erode_ctrl, RK_TRUE) != RK_SUCCESS) {
            SW_LOG_ERR("RK_MPI_IVE_Erode failed on frame %llu",
                       (unsigned long long)frame_seq);
            return -1;
        }
        if (RK_MPI_IVE_Query(handle, &finished, RK_TRUE) != RK_SUCCESS) {
            SW_LOG_ERR("RK_MPI_IVE_Query failed after Erode on frame %llu",
                       (unsigned long long)frame_seq);
            return -1;
        }
        sw_profile_record(&state->profile, state->stage_erode, stage_start_ns);
        stage_start_ns = sw_profile_now_ns();
        if (RK_MPI_IVE_Dilate(&handle, &state->bg, &state->match,
                              &state->dilate_ctrl, RK_TRUE) != RK_SUCCESS) {
            SW_LOG_ERR("RK_MPI_IVE_Dilate failed on frame %llu",
                       (unsigned long long)frame_seq);
            return -1;
        }
        if (RK_MPI_IVE_Query(handle, &finished, RK_TRUE) != RK_SUCCESS) {
            SW_LOG_ERR("RK_MPI_IVE_Query failed after Dilate on frame %llu",
                       (unsigned long long)frame_seq);
            return -1;
        }
        sw_profile_record(&state->profile, state->stage_dilate, stage_start_ns);
    } else {
        /* Morphology off still preserves the GMM2 final mask in `match` so
         * in-place CCL can never destroy the pixels used for the moment. */
        stage_start_ns = sw_profile_now_ns();
        memcpy((void *)(uintptr_t)state->match.au64VirAddr[0],
               (const void *)(uintptr_t)state->fg.au64VirAddr[0], plane);
        sw_profile_record(&state->profile, state->stage_mask_preserve,
                          stage_start_ns);
    }
    /* mask_preserve counts each fg/match plane copy as its own sample, so
     * its count is 2x the frame count with morphology off and 1x with it on
     * — the copies are what is being measured, not the frames. */
    stage_start_ns = sw_profile_now_ns();
    memcpy((void *)(uintptr_t)state->fg.au64VirAddr[0],
           (const void *)(uintptr_t)state->match.au64VirAddr[0], plane);
    sw_profile_record(&state->profile, state->stage_mask_preserve, stage_start_ns);
    return 0;
}

static int ive_apply_frame(sw_detector_t *self, const uint8_t *luma, int width,
                           int height, bool warming_up, uint64_t frame_seq,
                           sw_component_t *out, int out_capacity,
                           double *occupancy)
{
    ive_state_t *state = (ive_state_t *)self->state;
    IVE_HANDLE handle = 0;
    bool finished = false;
    const IVE_CCBLOB_S *blob;
    const uint8_t *mask;
    RK_U32 stride;
    const int capacity = out_capacity < SW_MAX_COMPONENTS ? out_capacity
                                                          : SW_MAX_COMPONENTS;
    int count = 0;
    int row;
    uint64_t foreground = 0;
    uint64_t stage_start_ns;

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
    stage_start_ns = sw_profile_now_ns();
    for (row = 0; row < height; ++row) {
        memcpy((uint8_t *)(uintptr_t)state->src.au64VirAddr[0] + (size_t)row * stride,
               luma + (size_t)row * width, (size_t)width);
    }
    sw_profile_record(&state->profile, state->stage_src_copy, stage_start_ns);

    /* Each stage below records only when its work COMPLETED — a failed op
     * or Query returns unrecorded while frame_total (in the ive_apply
     * wrapper) still carries the frame, and diag_io still measures whichever
     * logging a failure path performs. The timers change no control flow,
     * error path or counter. */
    stage_start_ns = sw_profile_now_ns();
    if (RK_MPI_IVE_GMM2(&handle, &state->src, NULL, &state->fg, &state->bg,
                        &state->match, &state->model, &state->gmm2_ctrl, RK_TRUE) !=
        RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_IVE_GMM2 failed on frame %llu",
                   (unsigned long long)frame_seq);
        return -1;
    }
    if (RK_MPI_IVE_Query(handle, &finished, RK_TRUE) != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_IVE_Query failed after GMM2 on frame %llu",
                   (unsigned long long)frame_seq);
        return -1;
    }
    sw_profile_record(&state->profile, state->stage_gmm2, stage_start_ns);
    state->gmm2_ctrl.u8FirstFrameFlag = 0;
    state->frames++;

    if (warming_up) {
        if (occupancy != NULL) {
            *occupancy = 0.0;
        }
        return 0;
    }

    /* The ccl stage spans mask preparation through CCL's Query: erode,
     * dilate and mask_preserve are its interior stages (they run inside
     * ive_prepare_ccl_mask), so ccl deliberately OVERLAPS them the same way
     * frame_total overlaps everything — the stage table is a set of spans,
     * not a partition, and summing it double-counts by design. */
    stage_start_ns = sw_profile_now_ns();
    if (ive_prepare_ccl_mask(state, frame_seq) != 0) {
        return -1;
    }
    mask = (const uint8_t *)(uintptr_t)state->match.au64VirAddr[0];

    /* Empty every slot before CCL. Successful region records are documented
     * as non-contiguous; a full-table scan can only distinguish a genuine
     * empty slot from last frame's record if unused areas begin at zero. */
    memset((void *)(uintptr_t)state->blob.u64VirAddr, 0, sizeof(IVE_CCBLOB_S));
    state->diagnostics.ccl_attempts++;

    /* CCL is IN PLACE on fg. `match` remains the final binary mask. */
    if (RK_MPI_IVE_CCL(&handle, &state->fg, &state->blob, &state->ccl_ctrl, RK_TRUE) !=
        RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_IVE_CCL failed on frame %llu",
                   (unsigned long long)frame_seq);
        state->diagnostics.ccl_api_failures++;
        stage_start_ns = sw_profile_now_ns();
        ive_log_ccl_attempt(state, frame_seq, true, 0, 0, 0, 0, NULL, 0, 0);
        ive_log_failed_mask(state, frame_seq, mask, stride);
        sw_profile_record(&state->profile, state->stage_diag_io, stage_start_ns);
        return -1;
    }
    if (RK_MPI_IVE_Query(handle, &finished, RK_TRUE) != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_IVE_Query failed after CCL on frame %llu",
                   (unsigned long long)frame_seq);
        state->diagnostics.ccl_api_failures++;
        stage_start_ns = sw_profile_now_ns();
        ive_log_ccl_attempt(state, frame_seq, true, 0, 0, 0, 0, NULL, 0, 0);
        ive_log_failed_mask(state, frame_seq, mask, stride);
        sw_profile_record(&state->profile, state->stage_diag_io, stage_start_ns);
        return -1;
    }
    sw_profile_record(&state->profile, state->stage_ccl, stage_start_ns);

    blob = (const IVE_CCBLOB_S *)(uintptr_t)state->blob.u64VirAddr;

    /* Count a reported current threshold above the configured initial value
     * on every completed CCL attempt, including a label failure. Returning
     * first would make this aggregate contradict the retained per-attempt
     * u32CurAreaThr telemetry. */
    if (blob->u32CurAreaThr > (RK_U32)state->config.min_area_px) {
        state->diagnostics.frames_area_threshold_raised++;
        if (blob->u32CurAreaThr > state->diagnostics.max_area_threshold) {
            state->diagnostics.max_area_threshold = blob->u32CurAreaThr;
        }
        SW_LOG_WARN("IVE CCL reported area threshold %u above configured floor %d "
                    "on frame %llu",
                    blob->u32CurAreaThr, state->config.min_area_px,
                    (unsigned long long)frame_seq);
    }

    if (blob->s8LabelStatus != 0) {
        /* The hardware could not label this frame. Counted and reported as
         * a DROPPED FRAME, never as an empty one: "no blobs" and "the
         * labeller gave up" are different facts about the sky. */
        state->diagnostics.ccl_label_failures++;
        if (blob->u32CurAreaThr > (RK_U32)state->config.min_area_px) {
            state->diagnostics.ccl_label_failures_with_raised_threshold++;
        }
        if (blob->u8RegionNum == 0) {
            state->diagnostics.ccl_threshold_runaway_failures++;
        } else if (blob->u8RegionNum < SW_MAX_COMPONENTS) {
            state->diagnostics.ccl_sub_cap_failures++;
        } else {
            state->diagnostics.ccl_other_failures++;
        }
        /* Log the two fields that classify the reported failure mode
         * (F-C1-6). u8RegionNum is opaque vendor telemetry rather than a
         * proven table cardinality; u32CurAreaThr is SDK-reported threshold
         * telemetry. Diagnostics only — no tuning, no behaviour change. */
        SW_LOG_WARN("IVE CCL reported label failure on frame %llu; frame dropped "
                    "(u8RegionNum %u, u32CurAreaThr %u, configured floor %d)",
                    (unsigned long long)frame_seq, blob->u8RegionNum,
                    blob->u32CurAreaThr, state->config.min_area_px);
        stage_start_ns = sw_profile_now_ns();
        ive_log_ccl_attempt(state, frame_seq, false,
                            (int)blob->s8LabelStatus, blob->u8RegionNum,
                            blob->u32CurAreaThr, 0, NULL, 0, 0);
        ive_log_failed_mask(state, frame_seq, mask, stride);
        sw_profile_record(&state->profile, state->stage_diag_io, stage_start_ns);
        return -1;
    }
    /* BUG A: IVE says valid records are stored non-contiguously. Real RV1106
     * output proves u8RegionNum does not reliably equal the number of nonzero
     * records, so it is never a scan bound. Inspect all 254 slots and skip only
     * records whose area is zero, matching the pinned vendor samples. */
    stage_start_ns = sw_profile_now_ns();
    {
        uint16_t populated_slots[SW_MAX_COMPONENTS];
        const size_t populated_count = sw_collect_nonzero_u32_slots(
            blob->astRegion, sizeof(blob->astRegion[0]),
            offsetof(IVE_REGION_S, u32Area), SW_MAX_COMPONENTS,
            populated_slots, SW_MAX_COMPONENTS);
        const uint32_t nonzero_region_slots = (uint32_t)populated_count;
        if ((uint32_t)blob->u8RegionNum != nonzero_region_slots) {
            state->diagnostics.ccl_region_count_mismatch_frames++;
        }
        size_t populated;
        for (populated = 0; populated < populated_count; ++populated) {
            const uint32_t region = populated_slots[populated];
            const IVE_REGION_S *r = &blob->astRegion[region];
            sw_bbox_t bbox;
            sw_mask_moment_t moment;
            int measured;
            if ((int)r->u32Area < state->config.min_area_px ||
                (int)r->u32Area > state->config.max_area_px) {
                continue;
            }
            if (r->u16Right < r->u16Left || r->u16Bottom < r->u16Top) {
                SW_LOG_WARN("region slot %u has inverted bbox (%u,%u)-(%u,%u)",
                            region, r->u16Left, r->u16Top, r->u16Right,
                            r->u16Bottom);
                continue;
            }
            bbox.x = r->u16Left;
            bbox.y = r->u16Top;
            bbox.w = (uint32_t)(r->u16Right - r->u16Left + 1);
            bbox.h = (uint32_t)(r->u16Bottom - r->u16Top + 1);
            measured = sw_mask_moment_nonzero(mask, width, height, stride, &bbox,
                                              &moment);
            if (measured != 1) {
                SW_LOG_WARN("region slot %u declares area %u but its final-mask "
                            "bbox (%d,%d %ux%u) is invalid or empty",
                            region, r->u32Area, bbox.x, bbox.y, bbox.w, bbox.h);
                continue;
            }
            if (moment.pixel_count != r->u32Area) {
                SW_LOG_DEBUG("region slot %u: hardware area %u, nonzero pixels "
                             "in final-mask bbox %llu",
                             region, r->u32Area,
                             (unsigned long long)moment.pixel_count);
            }
            if (count >= capacity) {
                continue;
            }
            out[count].centroid_u = moment.centroid_u;
            out[count].centroid_v = moment.centroid_v;
            out[count].area_px = r->u32Area;
            out[count].bbox_x = bbox.x;
            out[count].bbox_y = bbox.y;
            out[count].bbox_w = bbox.w;
            out[count].bbox_h = bbox.h;
            count++;
            /* Occupancy keeps CCL/flood-fill area semantics too. */
            foreground += r->u32Area;
        }
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
            state->diagnostics.overlapping_bbox_pairs += pairs;
            if (pairs > 0) {
                state->diagnostics.frames_with_overlapping_bboxes++;
            }
            /* region_scan ends where the diagnostic FILE write begins:
             * diag_io covers only the --ccl-log/--fg-mask-log writes, the
             * split that says whether a slow frame was the scan or the SD
             * card. Honestly, though: the per-slot stderr WARN/DEBUG lines
             * above (inverted bbox, invalid final-mask bbox, the area
             * mismatch debug) are emitted DURING the scan, so on an
             * anomalous frame region_scan includes that stderr I/O, not
             * pure compute. */
            sw_profile_record(&state->profile, state->stage_region_scan,
                              stage_start_ns);
            stage_start_ns = sw_profile_now_ns();
            ive_log_ccl_attempt(state, frame_seq, false, 0,
                                blob->u8RegionNum, blob->u32CurAreaThr,
                                nonzero_region_slots, out, count, pairs);
            sw_profile_record(&state->profile, state->stage_diag_io,
                              stage_start_ns);
        }
    }
    if (occupancy != NULL) {
        *occupancy = (double)foreground / ((double)width * (double)height);
    }
    return count;
}

static int ive_apply(sw_detector_t *self, const uint8_t *luma, int width, int height,
                     bool warming_up, uint64_t frame_seq, sw_component_t *out,
                     int out_capacity, double *occupancy)
{
    /* Thin timing shell so frame_total covers EVERY exit of the real body —
     * failures included — without a record call before each return. The
     * inner function is the detector; this is a stopwatch around it. */
    ive_state_t *state = (ive_state_t *)self->state;
    const uint64_t frame_start_ns = sw_profile_now_ns();
    const int result = ive_apply_frame(self, luma, width, height, warming_up,
                                       frame_seq, out, out_capacity, occupancy);
    sw_profile_record(&state->profile, state->stage_frame_total, frame_start_ns);
    return result;
}

static void ive_losses(const sw_detector_t *self, sw_detector_losses_t *out)
{
    const ive_state_t *state = (const ive_state_t *)self->state;
    /* A reported current threshold above the configured initial value is
     * conservatively counted as a shed frame. The exact number excluded is
     * not available, so components_shed stays zero rather than inventing a
     * measurement. Frames the labeller failed are counted too; those lost
     * everything. */
    out->components_shed = 0;
    out->frames_shed =
        state != NULL
            ? state->diagnostics.frames_area_threshold_raised +
                  state->diagnostics.ccl_label_failures -
                  state->diagnostics.ccl_label_failures_with_raised_threshold
            : 0;
}

static void ive_diagnostics(const sw_detector_t *self,
                            sw_detector_diagnostics_t *out)
{
    const ive_state_t *state = (const ive_state_t *)self->state;
    memset(out, 0, sizeof(*out));
    if (state != NULL) {
        *out = state->diagnostics;
    }
}

static const sw_profile_t *ive_profile(const sw_detector_t *self)
{
    const ive_state_t *state = (const ive_state_t *)self->state;
    return state != NULL ? &state->profile : NULL;
}

static size_t ive_footprint_bytes(const sw_detector_t *self)
{
    const ive_state_t *state = (const ive_state_t *)self->state;
    ive_geometry_t geometry;
    if (state == NULL) {
        return 0;
    }
    if (state->footprint_bytes != 0) {
        return state->footprint_bytes;
    }
    /* Nothing is allocated until the first frame arrives (ive_apply's
     * initialised guard), and the RAM budget check deliberately runs BEFORE
     * the frame loop — so answer for the grid the detector was opened at. */
    geometry = ive_geometry(state, state->config.proc_width, state->config.proc_height);
    return ive_footprint_for(&geometry);
}

static void ive_close(sw_detector_t *self)
{
    ive_state_t *state;
    if (self == NULL) {
        return;
    }
    state = (ive_state_t *)self->state;
    if (state != NULL) {
        SW_LOG_INFO("IVE detector: %llu frames, %llu post-warm CCL attempts, "
                    "%llu CCL API failures, %llu label failures (%llu "
                    "threshold-runaway, %llu sub-cap, %llu other), %llu "
                    "successful region-count mismatches, %llu "
                    "overlapping bbox pairs across %llu frames, area threshold "
                    "reported above floor on %llu, max threshold %u",
                    (unsigned long long)state->frames,
                    (unsigned long long)state->diagnostics.ccl_attempts,
                    (unsigned long long)state->diagnostics.ccl_api_failures,
                    (unsigned long long)state->diagnostics.ccl_label_failures,
                    (unsigned long long)
                        state->diagnostics.ccl_threshold_runaway_failures,
                    (unsigned long long)state->diagnostics.ccl_sub_cap_failures,
                    (unsigned long long)state->diagnostics.ccl_other_failures,
                    (unsigned long long)
                        state->diagnostics.ccl_region_count_mismatch_frames,
                    (unsigned long long)state->diagnostics.overlapping_bbox_pairs,
                    (unsigned long long)
                        state->diagnostics.frames_with_overlapping_bboxes,
                    (unsigned long long)
                        state->diagnostics.frames_area_threshold_raised,
                    state->diagnostics.max_area_threshold);
        if (state->ccl_log != NULL) {
            fclose(state->ccl_log);
        }
        if (state->fg_mask_log != NULL) {
            fclose(state->fg_mask_log);
        }
        ive_release_blocks(state);
        free(state);
    }
    free(self);
}

sw_detector_t *sw_detect_open_ive(const sw_detector_config_t *config,
                                  const char *ccl_log_path,
                                  const char *fg_mask_log_path,
                                  uint32_t fg_mask_limit)
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
    state->fg_mask_limit = fg_mask_limit;
    if (ccl_log_path != NULL && ccl_log_path[0] != '\0') {
        state->ccl_log = fopen(ccl_log_path, "w");
        if (state->ccl_log == NULL) {
            SW_LOG_ERR("cannot open CCL log %s", ccl_log_path);
            free(detector);
            free(state);
            return NULL;
        }
    }
    if (fg_mask_log_path != NULL && fg_mask_log_path[0] != '\0') {
        state->fg_mask_log = fopen(fg_mask_log_path, "wb");
        if (state->fg_mask_log == NULL) {
            SW_LOG_ERR("cannot open failed-mask log %s", fg_mask_log_path);
            if (state->ccl_log != NULL) {
                fclose(state->ccl_log);
            }
            free(detector);
            free(state);
            return NULL;
        }
    }
    /* Stage registration order is emission order in --stats. Capacity is 12
     * and this registers 9, so none of these can return -1; the indices are
     * stored anyway because sw_profile_record tolerates -1 and an assumed
     * index is exactly the kind of drift the accumulator exists to rule out. */
    sw_profile_init(&state->profile);
    state->stage_src_copy = sw_profile_stage(&state->profile, "src_copy");
    state->stage_gmm2 = sw_profile_stage(&state->profile, "gmm2");
    state->stage_erode = sw_profile_stage(&state->profile, "erode");
    state->stage_dilate = sw_profile_stage(&state->profile, "dilate");
    state->stage_mask_preserve =
        sw_profile_stage(&state->profile, "mask_preserve");
    state->stage_ccl = sw_profile_stage(&state->profile, "ccl");
    state->stage_region_scan = sw_profile_stage(&state->profile, "region_scan");
    state->stage_diag_io = sw_profile_stage(&state->profile, "diag_io");
    state->stage_frame_total = sw_profile_stage(&state->profile, "frame_total");
    detector->apply = ive_apply;
    detector->close = ive_close;
    detector->losses = ive_losses;
    detector->diagnostics = ive_diagnostics;
    detector->footprint_bytes = ive_footprint_bytes;
    detector->name = "ive-gmm2";
    detector->state = state;
    detector->profile = ive_profile;
    return detector;
}

#endif /* SKYWEAVE_HAVE_RKMPI */
