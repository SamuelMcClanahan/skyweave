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
 *
 * ONE NAMED EXCEPTION, and it is narrow. Under a RAM loop (--inject-ram) the
 * daemon numbers frame_seq continuously across wraps and advances
 * capture_ts_ns by the harness's DECLARED --ram-loop-pts-stride-ns once per
 * pass. That reproduces, locally, the looping rule the harness's own TCP
 * feeder already applies (benchmark._feed_stream_over_tcp: continuous
 * frame_seq, pixels from seq % N), so the bytes served are the bytes an
 * unrolled feed would have written. The daemon still reads no clock, still
 * computes no timestamp, and still copies time_sync_error_ms untouched; and
 * it REFUSES any clip on which that advance would stop reproducing the
 * harness rule — frames not numbered 0..N-1, non-increasing capture times, a
 * stride no longer than the clip it advances, or an OVERRIDDEN declaration.
 */

#ifndef SW_INJECT_H
#define SW_INJECT_H

#include "sw_obs.h"

/* Upper bound on either side of an injected frame. Comfortably above the
 * SC3336's 2304x1296 and above anything the D4 sweep uses, and low enough
 * that width*height cannot overflow 32-bit size arithmetic on the node. */
#define SW_INJECT_MAX_DIMENSION 8192

/* A STRUCTURAL bound on the per-frame metadata arrays the RAM loop keeps,
 * independent of the byte budget: a 1x1 clip fits millions of frames inside
 * any byte budget and would still buy millions of records. */
#define SW_INJECT_RAM_MAX_FRAMES 4096

/* What the loop must remember per clip frame. The pixels live in one
 * contiguous arena; these two are the declaration that rides with them. */
typedef struct {
    int64_t capture_ts_ns;
    float time_sync_error_ms;
} sw_inject_ram_meta_t;

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

    /* SW_SOURCE_INJECT_RAM. ORDERING IS LOAD-BEARING: `ram_active` stays
     * false until the preload has finished, so the preloader itself drives
     * sw_inject_next down the ordinary FILE path and the clip goes through
     * exactly ONE parser with every existing validation applied verbatim.
     * Setting it early would recurse into the loop and read an arena that has
     * not been filled. */
    bool ram_active;
    uint8_t *ram_arena;
    size_t ram_arena_bytes;
    size_t ram_frame_bytes;
    uint32_t ram_frames;
    sw_inject_ram_meta_t *ram_meta;
    uint64_t ram_total_frames;
    uint64_t ram_served;
    int64_t ram_pts_stride_ns;
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

/* Preload the already-opened clip into one contiguous arena and serve it in a
 * loop. Called AFTER sw_inject_open_file has read the session header and
 * AFTER the detector has allocated, so the RAM budget check sees the
 * detector's real footprint; always BEFORE the frame loop, because there is
 * no allocation in the frame loop. Every parameter is a DECLARATION the
 * harness made. The file descriptor is CLOSED before this returns, so storage
 * leaves the measured path entirely. Returns 0 on success. */
int sw_inject_preload_ram(sw_inject_t *inject, uint64_t total_frames,
                          int64_t pts_stride_ns, uint64_t byte_budget);

/* 0: a frame was read; 1: end of stream (a trailer, or the RAM loop's
 * declared frame budget); -1: error. */
int sw_inject_next(sw_inject_t *inject, sw_inject_frame_t *frame);

void sw_inject_close(sw_inject_t *inject);

#endif /* SW_INJECT_H */
