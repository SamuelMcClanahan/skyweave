/* SWIJ injection reader: the daemon's C1 input source.
 *
 * The C twin of `skyweave2/edge/injection.py`, reading the same bytes from a
 * file or a TCP connection — "over Ethernet or from local storage" is one
 * format and one parser, so a file replay and a wire replay cannot diverge.
 *
 * The daemon does NOT invent timestamps. Each frame record carries the
 * capture_ts_ns the harness fabricated and the time_sync_error_ms that
 * declares the fabrication's magnitude, and both are copied into the
 * envelope untouched. If the daemon recomputed either one, the honesty the
 * harness built in would end at this boundary.
 */

#ifndef SW_INJECT_H
#define SW_INJECT_H

#include "sw_obs.h"

/* Upper bound on either side of an injected frame. Comfortably above the
 * SC3336's 2304x1296 and above anything the D4 sweep uses, and low enough
 * that width*height cannot overflow 32-bit size arithmetic on the node. */
#define SW_INJECT_MAX_DIMENSION 8192

typedef struct {
    int fd;
    int listen_fd; /* -1 for a file source */
    uint32_t camera_id;
    char session_uuid[SW_SESSION_UUID_MAX];
    uint32_t full_width;
    uint32_t full_height;
    uint32_t proc_width;
    uint32_t proc_height;
    uint32_t frame_count;
    double fps;
    float exposure_us;
    float gain_db;
    float line_readout_us;
    skyweave_v2_ClockDomain clock_domain;
    char calibration_rev[SW_CALIBRATION_REV_MAX];
    char detector_rev[SW_DETECTOR_REV_MAX];
    bool declaration_overridden;

    uint32_t frames_read;
    uint8_t *luma; /* proc_width * proc_height, allocated once */
    size_t luma_capacity;
} sw_inject_t;

typedef struct {
    uint64_t frame_seq;
    int64_t capture_ts_ns;
    float time_sync_error_ms;
    uint32_t width;
    uint32_t height;
    const uint8_t *luma;
} sw_inject_frame_t;

/* Open a stream from a file, or accept ONE connection on `port`. Returns 0
 * on success. */
int sw_inject_open_file(sw_inject_t *inject, const char *path);
int sw_inject_open_listen(sw_inject_t *inject, int port);

/* 0: a frame was read; 1: the trailer; -1: error. */
int sw_inject_next(sw_inject_t *inject, sw_inject_frame_t *frame);

void sw_inject_close(sw_inject_t *inject);

#endif /* SW_INJECT_H */
