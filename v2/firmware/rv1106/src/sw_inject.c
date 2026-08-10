#include "sw_inject.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
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

int sw_inject_next(sw_inject_t *inject, sw_inject_frame_t *frame)
{
    uint8_t record[FRAME_FIXED_LEN];
    uint32_t width, height, payload_len;
    int status = sw_read_exact(inject->fd, record, 4);
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
}
