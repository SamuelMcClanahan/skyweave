/* RKMPI VI capture and RGA crop: the board's real pixel paths.
 *
 * Present but BEHIND the injection source this phase, per the D8 brief. The
 * declared scored path is C1 app-layer Y injection; real CSI capture gets a
 * smoke test only, and full CSI/ISP/PTS characterisation is deferred (C2/C3
 * are parked).
 *
 * The interface is shaped by the design's governing rule: the A7 touches as
 * few pixels as possible. `sw_capture_next` hands back a POINTER into the
 * DMA buffer for the downscaled Y plane and never copies the full-resolution
 * frame; `sw_rga_crop` asks the RGA to cut a patch out of the full-res frame
 * by handle. Nothing here memcpy's a 3 MP image.
 *
 * The timestamp comes from the V4L2/VI buffer, i.e. the KERNEL, not from
 * userspace at dequeue time (RV1106_EDGE_NODE.md section 10). What the
 * hardware PTS actually names relative to D0's exposure midpoint is exactly
 * what D8's coded-LED bench test has to measure; until it does, the daemon
 * declares the mapping Provisional by stamping an honest
 * `time_sync_error_ms` from the configured sync uncertainty rather than
 * claiming zero.
 */

#ifndef SW_CAPTURE_H
#define SW_CAPTURE_H

#include "sw_config.h"

typedef struct sw_capture sw_capture_t;

typedef struct {
    const uint8_t *luma; /* proc-resolution Y, DMA memory, do not free */
    int width;
    int height;
    uint64_t frame_seq;
    int64_t capture_ts_ns; /* kernel buffer timestamp */
    int full_frame_fd;     /* dma-buf handle for the full-res frame, or -1 */
} sw_capture_frame_t;

/* Returns NULL and logs when the SDK is absent. There is no software
 * fallback: a daemon that thinks it is capturing and is not would report
 * clean health forever. */
sw_capture_t *sw_capture_open(const sw_config_t *config);

/* 0: a frame; 1: end of stream; -1: error. */
int sw_capture_next(sw_capture_t *capture, sw_capture_frame_t *frame);

void sw_capture_close(sw_capture_t *capture);

/* RGA crop of the FULL-resolution frame at a full-res bbox, into `out`.
 * Evidence-plane only; never on the measurement path. Returns 0 or -1. */
int sw_rga_crop(int full_frame_fd, int32_t x, int32_t y, uint32_t w, uint32_t h,
                uint8_t *out, size_t out_capacity, size_t *out_len);

#endif /* SW_CAPTURE_H */
