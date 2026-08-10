#include "sw_capture.h"

#include <stdlib.h>
#include <string.h>

#include "sw_common.h"

#ifndef SKYWEAVE_HAVE_RKMPI

sw_capture_t *sw_capture_open(const sw_config_t *config)
{
    SW_UNUSED(config);
    SW_LOG_ERR("this build has no RKMPI SDK, so there is no VI capture path in it. "
               "Refusing rather than pretending: a node that reports frames it "
               "never captured is the worst failure this system can have.");
    return NULL;
}

int sw_capture_next(sw_capture_t *capture, sw_capture_frame_t *frame)
{
    SW_UNUSED(capture);
    SW_UNUSED(frame);
    return -1;
}

void sw_capture_close(sw_capture_t *capture) { SW_UNUSED(capture); }

int sw_rga_crop(int full_frame_fd, int32_t x, int32_t y, uint32_t w, uint32_t h,
                uint8_t *out, size_t out_capacity, size_t *out_len)
{
    SW_UNUSED(full_frame_fd);
    SW_UNUSED(x);
    SW_UNUSED(y);
    SW_UNUSED(w);
    SW_UNUSED(h);
    SW_UNUSED(out);
    SW_UNUSED(out_capacity);
    SW_UNUSED(out_len);
    SW_LOG_ERR("this build has no librga; evidence crops are unavailable");
    return -1;
}

#else /* SKYWEAVE_HAVE_RKMPI */

#include "im2d.h"
#include "rga.h"
#include "rk_mpi_mb.h"
#include "rk_mpi_sys.h"
#include "rk_mpi_vi.h"

/* Two VI channels, exactly as RV1106_EDGE_NODE.md section 3 requires:
 *   ch0  full-resolution NV12, stays in DMA, RGA crops patches out of it;
 *   ch1  the downscaled detection stream, the ONLY plane the A7 reads.
 * Running the detector on the full 3 MP frame would add ~270 MB/s of DDR
 * traffic and push the bus past 50%; this split is the core performance
 * lever of the whole node. */
#define SW_VI_DEV 0
#define SW_VI_CHN_FULL 0
#define SW_VI_CHN_PROC 1

struct sw_capture {
    sw_config_t config;
    bool started;
    uint64_t frame_seq;
    VIDEO_FRAME_INFO_S proc_frame;
    VIDEO_FRAME_INFO_S full_frame;
    bool proc_held;
    bool full_held;
};

static int configure_channel(int channel, int width, int height)
{
    VI_CHN_ATTR_S attr;
    memset(&attr, 0, sizeof(attr));
    attr.stIspOpt.u32BufCount = 4; /* ~18 MB at full res, per the memory budget */
    attr.stIspOpt.enMemoryType = VI_V4L2_MEMORY_TYPE_DMABUF;
    attr.stSize.u32Width = (RK_U32)width;
    attr.stSize.u32Height = (RK_U32)height;
    attr.enPixelFormat = RK_FMT_YUV420SP;
    attr.enCompressMode = COMPRESS_MODE_NONE;
    attr.u32Depth = 2;
    if (RK_MPI_VI_SetChnAttr(SW_VI_DEV, channel, &attr) != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_VI_SetChnAttr(ch%d) failed", channel);
        return -1;
    }
    if (RK_MPI_VI_EnableChn(SW_VI_DEV, channel) != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_VI_EnableChn(ch%d) failed", channel);
        return -1;
    }
    return 0;
}

sw_capture_t *sw_capture_open(const sw_config_t *config)
{
    sw_capture_t *capture = (sw_capture_t *)calloc(1, sizeof(sw_capture_t));
    VI_DEV_ATTR_S dev_attr;
    VI_DEV_BIND_PIPE_S bind_pipe;
    if (capture == NULL) {
        return NULL;
    }
    capture->config = *config;
    if (RK_MPI_SYS_Init() != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_SYS_Init failed");
        free(capture);
        return NULL;
    }
    memset(&dev_attr, 0, sizeof(dev_attr));
    if (RK_MPI_VI_SetDevAttr(SW_VI_DEV, &dev_attr) != RK_SUCCESS ||
        RK_MPI_VI_EnableDev(SW_VI_DEV) != RK_SUCCESS) {
        SW_LOG_ERR("VI device setup failed");
        free(capture);
        return NULL;
    }
    memset(&bind_pipe, 0, sizeof(bind_pipe));
    bind_pipe.u32Num = 1;
    bind_pipe.PipeId[0] = SW_VI_DEV;
    if (RK_MPI_VI_SetDevBindPipe(SW_VI_DEV, &bind_pipe) != RK_SUCCESS) {
        SW_LOG_ERR("RK_MPI_VI_SetDevBindPipe failed");
        free(capture);
        return NULL;
    }
    /* Full-resolution channel: the SC3336's native 2304x1296. */
    if (configure_channel(SW_VI_CHN_FULL, 2304, 1296) != 0 ||
        configure_channel(SW_VI_CHN_PROC, config->detector.proc_width,
                          config->detector.proc_height) != 0) {
        free(capture);
        return NULL;
    }
    SW_LOG_WARN("VI capture is a SMOKE TEST this phase: frames flow and can be "
                "eyeballed, but nothing here is scored and no PTS claim is made "
                "(C2/C3 parked, D8 brief scope item 6)");
    capture->started = true;
    return capture;
}

int sw_capture_next(sw_capture_t *capture, sw_capture_frame_t *frame)
{
    if (!capture->started) {
        return -1;
    }
    if (capture->proc_held) {
        RK_MPI_VI_ReleaseChnFrame(SW_VI_DEV, SW_VI_CHN_PROC, &capture->proc_frame);
        capture->proc_held = false;
    }
    if (capture->full_held) {
        RK_MPI_VI_ReleaseChnFrame(SW_VI_DEV, SW_VI_CHN_FULL, &capture->full_frame);
        capture->full_held = false;
    }
    if (RK_MPI_VI_GetChnFrame(SW_VI_DEV, SW_VI_CHN_PROC, &capture->proc_frame, 1000) !=
        RK_SUCCESS) {
        SW_LOG_WARN("VI ch%d frame timeout", SW_VI_CHN_PROC);
        return -1;
    }
    capture->proc_held = true;
    if (RK_MPI_VI_GetChnFrame(SW_VI_DEV, SW_VI_CHN_FULL, &capture->full_frame, 1000) ==
        RK_SUCCESS) {
        capture->full_held = true;
    }

    frame->luma = (const uint8_t *)RK_MPI_MB_Handle2VirAddr(
        capture->proc_frame.stVFrame.pMbBlk);
    frame->width = (int)capture->proc_frame.stVFrame.u32Width;
    frame->height = (int)capture->proc_frame.stVFrame.u32Height;
    frame->frame_seq = capture->frame_seq++;
    /* The KERNEL's buffer timestamp, not a userspace clock read at dequeue:
     * it removes the largest jitter term for free. What it names relative to
     * D0's exposure midpoint is Provisional until the coded-LED bench test
     * measures the offset. */
    frame->capture_ts_ns = (int64_t)capture->proc_frame.stVFrame.u64PTS * 1000;
    frame->full_frame_fd =
        capture->full_held
            ? RK_MPI_MB_Handle2Fd(capture->full_frame.stVFrame.pMbBlk)
            : -1;
    return 0;
}

void sw_capture_close(sw_capture_t *capture)
{
    if (capture == NULL) {
        return;
    }
    if (capture->proc_held) {
        RK_MPI_VI_ReleaseChnFrame(SW_VI_DEV, SW_VI_CHN_PROC, &capture->proc_frame);
    }
    if (capture->full_held) {
        RK_MPI_VI_ReleaseChnFrame(SW_VI_DEV, SW_VI_CHN_FULL, &capture->full_frame);
    }
    RK_MPI_VI_DisableChn(SW_VI_DEV, SW_VI_CHN_PROC);
    RK_MPI_VI_DisableChn(SW_VI_DEV, SW_VI_CHN_FULL);
    RK_MPI_VI_DisableDev(SW_VI_DEV);
    RK_MPI_SYS_Exit();
    free(capture);
}

int sw_rga_crop(int full_frame_fd, int32_t x, int32_t y, uint32_t w, uint32_t h,
                uint8_t *out, size_t out_capacity, size_t *out_len)
{
    rga_buffer_t source;
    rga_buffer_t destination;
    im_rect rect;
    size_t needed = (size_t)w * (size_t)h;
    if (full_frame_fd < 0) {
        SW_LOG_WARN("no full-resolution frame handle; evidence crop skipped");
        return -1;
    }
    if (needed > out_capacity) {
        SW_LOG_ERR("evidence crop %ux%u needs %zu B, buffer is %zu B", w, h, needed,
                   out_capacity);
        return -1;
    }
    /* The RGA reads the full-res frame BY HANDLE and writes only the patch:
     * bytes proportional to motion, not to frame size. The A7 never touches
     * the full-resolution image. */
    source = wrapbuffer_fd(full_frame_fd, 2304, 1296, RK_FORMAT_YCbCr_420_SP);
    destination = wrapbuffer_virtualaddr(out, (int)w, (int)h, RK_FORMAT_YCbCr_400);
    rect.x = (int)x;
    rect.y = (int)y;
    rect.width = (int)w;
    rect.height = (int)h;
    if (imcheck(source, destination, rect, rect, IM_CROP) != IM_STATUS_NOERROR) {
        SW_LOG_ERR("RGA rejected the crop request");
        return -1;
    }
    if (imcrop(source, destination, rect) != IM_STATUS_SUCCESS) {
        SW_LOG_ERR("RGA crop failed");
        return -1;
    }
    *out_len = needed;
    return 0;
}

#endif /* SKYWEAVE_HAVE_RKMPI */
