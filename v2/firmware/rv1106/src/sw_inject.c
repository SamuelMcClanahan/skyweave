#include "sw_inject.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "sw_common.h"

/* Layout from `_SESSION_FIXED` / `_FRAME_FIXED` / `_END_FIXED` in
 * skyweave2/edge/injection.py, offsets written out one by one. */
#define SESSION_FIXED_LEN 56
#define SE_OFF_MAGIC 0
#define SE_OFF_VERSION 4
#define SE_OFF_FLAGS 5
#define SE_OFF_RESERVED 6
#define SE_OFF_CAMERA_ID 8
#define SE_OFF_FULL_W 12
#define SE_OFF_FULL_H 16
#define SE_OFF_PROC_W 20
#define SE_OFF_PROC_H 24
#define SE_OFF_FRAME_COUNT 28
#define SE_OFF_FPS 32
#define SE_OFF_EXPOSURE 40
#define SE_OFF_GAIN 44
#define SE_OFF_LINE_READOUT 48
#define SE_OFF_DOMAIN 52
#define SE_OFF_UUID_LEN 53
#define SE_OFF_CAL_LEN 54
#define SE_OFF_DET_LEN 55

#define FRAME_FIXED_LEN 36
#define FR_OFF_MAGIC 0
#define FR_OFF_SEQ 4
#define FR_OFF_TS 12
#define FR_OFF_SYNC 20
#define FR_OFF_WIDTH 24
#define FR_OFF_HEIGHT 28
#define FR_OFF_PAYLOAD 32

#define FLAG_DECLARATION_OVERRIDDEN 0x01

SW_STATIC_ASSERT(SESSION_FIXED_LEN == SE_OFF_DET_LEN + 1, session_len_matches);
SW_STATIC_ASSERT(FRAME_FIXED_LEN == FR_OFF_PAYLOAD + 4, frame_len_matches);

static int read_text(int fd, uint8_t len, char *dst, size_t dst_size, const char *name)
{
    uint8_t scratch[256];
    if ((size_t)len > dst_size - 1) {
        SW_LOG_ERR("injection %s is %u bytes, over the declared bound of %zu", name, len,
                   dst_size - 1);
        return -1;
    }
    if (len > 0 && sw_read_exact(fd, scratch, len) != 0) {
        return -1;
    }
    memcpy(dst, scratch, len);
    dst[len] = '\0';
    return 0;
}

static int read_session(sw_inject_t *inject)
{
    uint8_t header[SESSION_FIXED_LEN];
    uint8_t flags;
    uint8_t domain;
    if (sw_read_exact(inject->fd, header, sizeof(header)) != 0) {
        return -1;
    }
    if (memcmp(header + SE_OFF_MAGIC, "SWIJ", 4) != 0) {
        SW_LOG_ERR("bad injection magic; this is not a SWIJ stream");
        return -1;
    }
    if (header[SE_OFF_VERSION] != 1) {
        SW_LOG_ERR("injection stream version %u, this build speaks 1",
                   header[SE_OFF_VERSION]);
        return -1;
    }
    if (sw_be16(header + SE_OFF_RESERVED) != 0) {
        SW_LOG_ERR("injection reserved field is not zero");
        return -1;
    }
    flags = header[SE_OFF_FLAGS];
    if ((flags & (uint8_t)~FLAG_DECLARATION_OVERRIDDEN) != 0) {
        SW_LOG_ERR("unknown injection flags 0x%02x", flags);
        return -1;
    }
    inject->declaration_overridden = (flags & FLAG_DECLARATION_OVERRIDDEN) != 0;
    if (inject->declaration_overridden) {
        /* The stream itself says its declared sync error is not the
         * perturbation it applied. Announced loudly at every startup: a
         * known-lie fixture that ran quietly would eventually be mistaken
         * for evidence. */
        SW_LOG_WARN("this injection stream declares OVERRIDDEN time_sync_error_ms: "
                    "its declaration is NOT the fabrication it applied. Known-lie "
                    "fixture only — never a source of a reported number.");
    }

    domain = header[SE_OFF_DOMAIN];
    /* C1 fabricates the PTS from scene time and has no board clock. A stream
     * claiming NODE_MONO or NODE_PTP would be presenting a fabricated
     * timestamp as a real capture clock, which is the one thing this stage
     * must not do (SYNTHETIC_PIPELINE_DESIGN.md section 6). */
    if (domain != 1) {
        SW_LOG_ERR("injected frames claim clock domain %u; C1 may only declare "
                   "SYNTHETIC (1). Refusing the stream rather than relabelling it.",
                   domain);
        return -1;
    }
    inject->clock_domain = skyweave_v2_ClockDomain_CLOCK_DOMAIN_SYNTHETIC;
    inject->camera_id = sw_be32(header + SE_OFF_CAMERA_ID);
    inject->full_width = sw_be32(header + SE_OFF_FULL_W);
    inject->full_height = sw_be32(header + SE_OFF_FULL_H);
    inject->proc_width = sw_be32(header + SE_OFF_PROC_W);
    inject->proc_height = sw_be32(header + SE_OFF_PROC_H);
    inject->frame_count = sw_be32(header + SE_OFF_FRAME_COUNT);
    inject->fps = sw_be_double(header + SE_OFF_FPS);
    inject->exposure_us = sw_be_float(header + SE_OFF_EXPOSURE);
    inject->gain_db = sw_be_float(header + SE_OFF_GAIN);
    inject->line_readout_us = sw_be_float(header + SE_OFF_LINE_READOUT);

    if (read_text(inject->fd, header[SE_OFF_UUID_LEN], inject->session_uuid,
                  sizeof(inject->session_uuid), "session_uuid") != 0 ||
        read_text(inject->fd, header[SE_OFF_CAL_LEN], inject->calibration_rev,
                  sizeof(inject->calibration_rev), "calibration_rev") != 0 ||
        read_text(inject->fd, header[SE_OFF_DET_LEN], inject->detector_rev,
                  sizeof(inject->detector_rev), "detector_rev") != 0) {
        return -1;
    }
    /* Bounded, not merely non-zero. size_t is 32 bits on this ARM, so a
     * declared 65536x65536 grid would overflow the frame-size arithmetic to
     * a small number, malloc that, and then accept a payload whose declared
     * length overflowed to match — a buffer overrun reached entirely through
     * a header. The bound is far above any real sensor and the refusal is
     * loud. */
    if (inject->proc_width == 0 || inject->proc_height == 0 ||
        inject->proc_width > SW_INJECT_MAX_DIMENSION ||
        inject->proc_height > SW_INJECT_MAX_DIMENSION) {
        SW_LOG_ERR("injection declares a %ux%u processing grid; each side must be "
                   "1..%d",
                   inject->proc_width, inject->proc_height, SW_INJECT_MAX_DIMENSION);
        return -1;
    }
    inject->luma_capacity = (size_t)inject->proc_width * (size_t)inject->proc_height;
    inject->luma = (uint8_t *)malloc(inject->luma_capacity);
    if (inject->luma == NULL) {
        SW_LOG_ERR("out of memory for a %zu B luma frame", inject->luma_capacity);
        return -1;
    }
    SW_LOG_INFO("injection: camera %u, %ux%u proc from a %ux%u full grid, %u frames "
                "at %.3f fps, session %s",
                inject->camera_id, inject->proc_width, inject->proc_height,
                inject->full_width, inject->full_height, inject->frame_count,
                inject->fps, inject->session_uuid);
    return 0;
}

int sw_inject_open_file(sw_inject_t *inject, const char *path)
{
    memset(inject, 0, sizeof(*inject));
    inject->listen_fd = -1;
    inject->fd = open(path, O_RDONLY);
    if (inject->fd < 0) {
        SW_LOG_ERR("cannot open injection stream %s: %s", path, strerror(errno));
        return -1;
    }
    if (read_session(inject) != 0) {
        sw_inject_close(inject);
        return -1;
    }
    return 0;
}

int sw_inject_open_listen(sw_inject_t *inject, int port)
{
    struct sockaddr_in address;
    int reuse = 1;
    memset(inject, 0, sizeof(*inject));
    inject->fd = -1;
    inject->listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (inject->listen_fd < 0) {
        SW_LOG_ERR("injection listen socket: %s", strerror(errno));
        return -1;
    }
    setsockopt(inject->listen_fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_ANY);
    address.sin_port = htons((uint16_t)port);
    if (bind(inject->listen_fd, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        listen(inject->listen_fd, 1) != 0) {
        SW_LOG_ERR("injection listen on port %d: %s", port, strerror(errno));
        sw_inject_close(inject);
        return -1;
    }
    SW_LOG_INFO("waiting for an injection connection on port %d", port);
    inject->fd = accept(inject->listen_fd, NULL, NULL);
    if (inject->fd < 0) {
        SW_LOG_ERR("injection accept: %s", strerror(errno));
        sw_inject_close(inject);
        return -1;
    }
    if (read_session(inject) != 0) {
        sw_inject_close(inject);
        return -1;
    }
    return 0;
}

/* Serve one frame out of the preloaded arena.
 *
 * The whole D1 transform is the four assignments below, and every input to
 * them was declared by the harness and checked at preload. */
static int ram_next(sw_inject_t *inject, sw_inject_frame_t *frame)
{
    uint64_t index;
    uint64_t pass;
    uint64_t slot;

    if (inject->ram_served >= inject->ram_total_frames) {
        /* Return 1, the SAME clean end a trailer gives, so the frame loop
         * needs no new case. Said out loud so it is never mistaken for one. */
        SW_LOG_INFO("RAM loop served its declared budget of %llu frames; ending "
                    "the run the way a trailer would (there is no trailer)",
                    (unsigned long long)inject->ram_total_frames);
        return 1;
    }
    index = inject->ram_served;
    pass = index / (uint64_t)inject->ram_frames;
    slot = index % (uint64_t)inject->ram_frames;

    /* Valid only because preload enforced stored seq == index: this IS the
     * harness's own numbering continued, not a renumbering. */
    frame->frame_seq = index;
    /* The addend is the harness's DECLARED per-pass stride. The daemon adds
     * it and derives nothing — no fps, no division, no clock. */
    frame->capture_ts_ns =
        inject->ram_meta[slot].capture_ts_ns + (int64_t)pass * inject->ram_pts_stride_ns;
    /* Copied, never recomputed: the harness declared this and the daemon's
     * job is to carry the declaration, not to have an opinion about it. */
    frame->time_sync_error_ms = inject->ram_meta[slot].time_sync_error_ms;
    frame->width = inject->proc_width;
    frame->height = inject->proc_height;
    frame->luma = inject->ram_arena + (size_t)slot * inject->ram_frame_bytes;
    inject->ram_served++;
    inject->frames_read++;
    return 0;
}

/* MemAvailable in BYTES from a meminfo-format file, 0 on success. Kept
 * separate from the decision so the preflight reads as one block above the
 * malloc it guards. */
static int read_mem_available(const char *path, uint64_t *out_bytes)
{
    FILE *fp;
    char line[128];
    char *end;
    unsigned long long kib;

    if (path == NULL || path[0] == '\0') {
        return -1;
    }
    fp = fopen(path, "r");
    if (fp == NULL) {
        return -1;
    }
    /* strtoull rather than sscanf's %llu: printf's ll qualifier is proven on
     * this uClibc (the budget INFO line printed on a real board), scanf's is
     * not, and a scanf that assigned 32 of these 64 bits would produce a
     * wrong verdict rather than a loud failure. strtoull skips the leading
     * whitespace itself. */
    while (fgets(line, sizeof(line), fp) != NULL) {
        if (strncmp(line, "MemAvailable:", 13) != 0) {
            continue;
        }
        end = NULL;
        kib = strtoull(line + 13, &end, 10);
        if (end == line + 13) {
            break; /* a label with no number is "no MemAvailable" */
        }
        fclose(fp);
        *out_bytes = (uint64_t)kib * 1024ULL;
        return 0;
    }
    fclose(fp);
    return -1;
}

int sw_inject_preload_ram(sw_inject_t *inject, uint64_t total_frames,
                          int64_t pts_stride_ns, uint64_t byte_budget,
                          const char *meminfo_path)
{
    uint64_t frame_bytes;
    uint64_t arena_bytes;
    uint64_t meta_bytes;
    uint64_t heap_available;
    uint64_t clip_span_ns;
    uint64_t passes;
    uint8_t *arena;
    sw_inject_ram_meta_t *meta;
    sw_inject_frame_t f;
    uint32_t i;
    int status;

    if (inject->declaration_overridden) {
        SW_LOG_ERR("this clip declares OVERRIDDEN time_sync_error_ms: a known-lie "
                   "fixture may not become a looped sweep source. Refusing.");
        return -1;
    }
    if (inject->frame_count == 0 || inject->frame_count > SW_INJECT_RAM_MAX_FRAMES) {
        SW_LOG_ERR("RAM clip declares %u frames, outside 1..%d "
                   "(SW_INJECT_RAM_MAX_FRAMES bounds the per-frame metadata arrays "
                   "whatever the byte budget allows)",
                   inject->frame_count, SW_INJECT_RAM_MAX_FRAMES);
        return -1;
    }
    if (total_frames < 1) {
        SW_LOG_ERR("--ram-loop-frames must be at least 1");
        return -1;
    }
    if (pts_stride_ns <= 0) {
        SW_LOG_ERR("--ram-loop-pts-stride-ns must be positive");
        return -1;
    }

    /* OVERFLOW GUARD, BY DIVISION and never by multiplication. size_t is 32
     * bits on this ARM (see read_session's bound), so at 2304x1296 the arena
     * product wraps at 1439 frames — and a wrapped product would malloc a
     * small buffer and then be written past. Division cannot wrap. */
    frame_bytes = (uint64_t)inject->proc_width * (uint64_t)inject->proc_height;
    if ((uint64_t)inject->frame_count > byte_budget / frame_bytes) {
        SW_LOG_ERR("RAM clip is %u frames x %llu B, over the %llu B remaining "
                   "budget; checked by division before any malloc because size_t "
                   "is 32 bits on this target",
                   inject->frame_count, (unsigned long long)frame_bytes,
                   (unsigned long long)byte_budget);
        return -1;
    }
    arena_bytes = (uint64_t)inject->frame_count * frame_bytes;
    if (arena_bytes > (uint64_t)SIZE_MAX) {
        SW_LOG_ERR("RAM clip arena is %llu B, past what a size_t on this target "
                   "can address",
                   (unsigned long long)arena_bytes);
        return -1;
    }

    /* HEAP PREFLIGHT (F-C1-2). The arithmetic budget the caller enforced is
     * pool-blind: its detector term lives in the media heap, but the arena
     * below is a plain malloc against the Linux heap, a far smaller pool on
     * the node. The first board run passed 158.3 MB <= 160 MB and was then
     * OOM-killed at a 35.8 MB malloc against ~29 MB of MemAvailable. So read
     * the heap actually left and refuse LOUD, before the OOM killer refuses
     * silently. No margin is applied: MemAvailable is itself an estimate,
     * and this check catches pool-scale disconnects, not kilobyte edges. No
     * MemAvailable at the declared path (any non-Linux host build) skips the
     * check, out loud — the arithmetic budget still governs. */
    meta_bytes = (uint64_t)inject->frame_count * sizeof(sw_inject_ram_meta_t);
    if (read_mem_available(meminfo_path, &heap_available) == 0) {
        SW_LOG_INFO("heap preflight: MemAvailable %llu B read from %s against a "
                    "%llu B arena + %llu B metadata. Measured at preload, not "
                    "part of the arithmetic budget.",
                    (unsigned long long)heap_available, meminfo_path,
                    (unsigned long long)arena_bytes,
                    (unsigned long long)meta_bytes);
        if (arena_bytes + meta_bytes > heap_available) {
            SW_LOG_ERR("heap preflight refuses: the clip arena is a heap "
                       "allocation, and MemAvailable is %llu B (%s), under the "
                       "%llu B arena + %llu B metadata. The arithmetic budget "
                       "models the daemon's total across pools; this malloc "
                       "draws on the heap alone (F-C1-2).",
                       (unsigned long long)heap_available, meminfo_path,
                       (unsigned long long)arena_bytes,
                       (unsigned long long)meta_bytes);
            return -1;
        }
    } else {
        SW_LOG_INFO("heap preflight skipped: no MemAvailable at %s; the "
                    "arithmetic budget is the only gate on this host",
                    (meminfo_path == NULL || meminfo_path[0] == '\0')
                        ? "(no path)" : meminfo_path);
    }

    arena = (uint8_t *)malloc((size_t)arena_bytes);
    meta = (sw_inject_ram_meta_t *)malloc((size_t)inject->frame_count *
                                          sizeof(sw_inject_ram_meta_t));
    if (arena == NULL || meta == NULL) {
        SW_LOG_ERR("out of memory preloading a %llu B RAM clip",
                   (unsigned long long)arena_bytes);
        goto refuse;
    }

    for (i = 0; i < inject->frame_count; ++i) {
        /* The UNMODIFIED file path: ram_active is still false, so the clip
         * goes through exactly ONE parser and every existing validation — the
         * SWIF/SWIE magic, payload_len != width*height, the mid-stream
         * resolution refusal — applies verbatim. */
        status = sw_inject_next(inject, &f);
        if (status != 0) {
            SW_LOG_ERR("RAM clip is shorter than its header declares: %u of %u "
                       "frames arrived",
                       i, inject->frame_count);
            goto refuse;
        }
        if (f.frame_seq != (uint64_t)i) {
            SW_LOG_ERR("RAM-loop clip frame %u declares frame_seq %llu: a RAM-loop "
                       "clip must be numbered 0..N-1, because the loop continues "
                       "the harness's numbering across every wrap and a gap would "
                       "make the wrap arithmetic wrong. Refusing rather than "
                       "renumbering it.",
                       i, (unsigned long long)f.frame_seq);
            goto refuse;
        }
        if (i == 0 && f.capture_ts_ns < 0) {
            SW_LOG_ERR("RAM-loop clip starts at capture_ts_ns %lld: a negative base "
                       "would make the wrap overflow check itself overflow",
                       (long long)f.capture_ts_ns);
            goto refuse;
        }
        if (i > 0 && f.capture_ts_ns <= meta[i - 1].capture_ts_ns) {
            SW_LOG_ERR("RAM-loop clip capture time does not increase at frame %u "
                       "(%lld after %lld); a loop over it could not increase "
                       "either",
                       i, (long long)f.capture_ts_ns,
                       (long long)meta[i - 1].capture_ts_ns);
            goto refuse;
        }
        memcpy(arena + (size_t)i * (size_t)frame_bytes, f.luma, (size_t)frame_bytes);
        meta[i].capture_ts_ns = f.capture_ts_ns;
        meta[i].time_sync_error_ms = f.time_sync_error_ms;
    }
    /* One more read, which MUST be the trailer: "a truncated stream is an
     * ERROR, not a short clip" holds for a preload too. */
    if (sw_inject_next(inject, &f) != 1) {
        SW_LOG_ERR("RAM clip has no trailer after its %u declared frames",
                   inject->frame_count);
        goto refuse;
    }

    /* EXACT INTEGER cross-checks on the declared stride. The first is what
     * guarantees capture time increases ACROSS the wrap and not merely within
     * a pass; without it a mis-declared stride would fold the clip back onto
     * itself and nothing downstream would notice. */
    clip_span_ns = (uint64_t)(meta[inject->frame_count - 1].capture_ts_ns -
                              meta[0].capture_ts_ns);
    if ((uint64_t)pts_stride_ns <= clip_span_ns) {
        SW_LOG_ERR("--ram-loop-pts-stride-ns %lld is not longer than the %llu ns "
                   "clip it advances, so capture time would not increase across "
                   "the wrap",
                   (long long)pts_stride_ns, (unsigned long long)clip_span_ns);
        goto refuse;
    }
    passes = (total_frames - 1) / (uint64_t)inject->frame_count;
    if (passes > (uint64_t)((INT64_MAX - meta[inject->frame_count - 1].capture_ts_ns) /
                            pts_stride_ns)) {
        SW_LOG_ERR("%llu frames at a %lld ns per-pass stride would run capture time "
                   "past INT64_MAX",
                   (unsigned long long)total_frames, (long long)pts_stride_ns);
        goto refuse;
    }

    /* Storage leaves the measured path here: the descriptor and the
     * single-frame staging buffer are done, and one full frame of DDR is
     * reclaimed before the loop starts. */
    if (inject->fd >= 0) {
        close(inject->fd);
        inject->fd = -1;
    }
    free(inject->luma);
    inject->luma = NULL;
    inject->luma_capacity = 0;

    inject->ram_arena = arena;
    inject->ram_arena_bytes = (size_t)arena_bytes;
    inject->ram_frame_bytes = (size_t)frame_bytes;
    inject->ram_frames = inject->frame_count;
    inject->ram_meta = meta;
    inject->ram_total_frames = total_frames;
    inject->ram_served = 0;
    inject->ram_pts_stride_ns = pts_stride_ns;
    inject->frames_read = 0;
    /* LAST assignment, deliberately: set before the preload loop it would
     * recurse into ram_next and read an arena nothing had filled. */
    inject->ram_active = true;

    SW_LOG_INFO("RAM loop: %u frames of %ux%u preloaded into a %llu B arena, "
                "serving %llu frames",
                inject->ram_frames, inject->proc_width, inject->proc_height,
                (unsigned long long)arena_bytes, (unsigned long long)total_frames);
    /* Loud at every startup, in the same register as the OVERRIDDEN warning
     * above: this is the daemon's ONE sanctioned advance of a capture
     * timestamp, and a run that did it quietly would eventually be read as
     * evidence that the daemon never touches capture time. */
    SW_LOG_WARN("RAM loop ADVANCES capture time: frame_seq is numbered "
                "continuously across wraps and capture_ts_ns gains the harness's "
                "DECLARED %lld ns stride once per pass, reproducing the harness's "
                "own looping feed. The daemon reads no clock and computes no "
                "timestamp; time_sync_error_ms is the clip's, untouched. The DDR "
                "traffic profile differs from real ISP writes — a declared "
                "systematic, recorded beside every result.",
                (long long)pts_stride_ns);
    return 0;

refuse:
    /* One exit for the eight refusals above. Every one of them happens with
     * both blocks already taken and nothing published into `inject`, so the
     * daemon leaves with the arena released rather than holding a clip it
     * refused to serve. */
    free(arena);
    free(meta);
    return -1;
}

int sw_inject_next(sw_inject_t *inject, sw_inject_frame_t *frame)
{
    uint8_t record[FRAME_FIXED_LEN];
    uint32_t width, height, payload_len;
    int status;
    if (inject->ram_active) {
        return ram_next(inject, frame);
    }
    status = sw_read_exact(inject->fd, record, 4);
    if (status != 0) {
        SW_LOG_ERR("injection stream ended without a trailer after %u frames; a "
                   "truncated stream is an ERROR, not a short clip",
                   inject->frames_read);
        return -1;
    }
    if (memcmp(record + FR_OFF_MAGIC, "SWIE", 4) == 0) {
        uint8_t tail[4];
        if (sw_read_exact(inject->fd, tail, sizeof(tail)) != 0) {
            return -1;
        }
        if (sw_be32(tail) != inject->frames_read) {
            SW_LOG_ERR("injection trailer declares %u frames, %u arrived",
                       sw_be32(tail), inject->frames_read);
            return -1;
        }
        return 1;
    }
    if (memcmp(record + FR_OFF_MAGIC, "SWIF", 4) != 0) {
        SW_LOG_ERR("expected a frame or trailer magic; the stream is out of step "
                   "and the next bytes would be read as pixels");
        return -1;
    }
    if (sw_read_exact(inject->fd, record + 4, FRAME_FIXED_LEN - 4) != 0) {
        return -1;
    }
    width = sw_be32(record + FR_OFF_WIDTH);
    height = sw_be32(record + FR_OFF_HEIGHT);
    payload_len = sw_be32(record + FR_OFF_PAYLOAD);
    if (payload_len != width * height) {
        SW_LOG_ERR("injected frame payload is %u B for a %ux%u luma plane",
                   payload_len, width, height);
        return -1;
    }
    if (width != inject->proc_width || height != inject->proc_height) {
        SW_LOG_ERR("injected frame is %ux%u, the session declared %ux%u; a mid-stream "
                   "resolution change would silently reset the background model",
                   width, height, inject->proc_width, inject->proc_height);
        return -1;
    }
    if ((size_t)payload_len > inject->luma_capacity) {
        SW_LOG_ERR("injected frame is larger than the allocated buffer");
        return -1;
    }
    if (sw_read_exact(inject->fd, inject->luma, payload_len) != 0) {
        return -1;
    }
    frame->frame_seq = sw_be64(record + FR_OFF_SEQ);
    frame->capture_ts_ns = (int64_t)sw_be64(record + FR_OFF_TS);
    /* Copied, never recomputed: the harness declared this and the daemon's
     * job is to carry the declaration, not to have an opinion about it. */
    frame->time_sync_error_ms = sw_be_float(record + FR_OFF_SYNC);
    frame->width = width;
    frame->height = height;
    frame->luma = inject->luma;
    inject->frames_read++;
    return 0;
}

void sw_inject_close(sw_inject_t *inject)
{
    if (inject->fd >= 0) {
        close(inject->fd);
        inject->fd = -1;
    }
    if (inject->listen_fd >= 0) {
        close(inject->listen_fd);
        inject->listen_fd = -1;
    }
    free(inject->luma);
    inject->luma = NULL;
    inject->luma_capacity = 0;
    free(inject->ram_arena);
    inject->ram_arena = NULL;
    inject->ram_arena_bytes = 0;
    free(inject->ram_meta);
    inject->ram_meta = NULL;
    inject->ram_frames = 0;
    inject->ram_active = false;
}
