/* sw-fixture-tool — the daemon's encoder and decoder, exposed for test E2.
 *
 *   sw-fixture-tool encode <observations.swob>
 *       reads capture events, encodes each with nanopb, prints one lowercase
 *       hex datagram per line on stdout.
 *
 *   sw-fixture-tool decode <packets.hex>
 *       reads one hex datagram per line, decodes each with nanopb, prints a
 *       canonical one-line-per-observation text form on stdout.
 *
 * It links the SAME sw_wire.c the daemon links. That is the whole design:
 * E2 must gate the encoder that ships, not a test-only reimplementation of
 * it. The tool adds argument parsing and hex, nothing else.
 *
 * The decode direction prints a deliberately dull, fully-specified text form
 * (%.17g for doubles, %.9g for floats — round-trip exact for binary64 and
 * binary32) so the host can compare values rather than formatting.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>

#include "sw_common.h"
#include "sw_obsfixture.h"
#include "sw_wire.h"

static void print_hex(const uint8_t *data, size_t len)
{
    size_t i;
    for (i = 0; i < len; ++i) {
        printf("%02x", data[i]);
    }
    putchar('\n');
}

static int cmd_encode(const char *path)
{
    sw_obsfixture_t fixture;
    sw_capture_event_t event;
    uint8_t datagram[SW_DATAGRAM_CEILING_BYTES];
    int fd = open(path, O_RDONLY);
    int rc;
    if (fd < 0) {
        SW_LOG_ERR("cannot open %s", path);
        return 2;
    }
    if (sw_obsfixture_open(&fixture, fd) != 0) {
        close(fd);
        return 2;
    }
    while ((rc = sw_obsfixture_next(&fixture, &event)) == 0) {
        size_t len = 0;
        size_t would_be = 0;
        sw_wire_status_t status = sw_wire_encode_observation_packet(
            &event, datagram, sizeof(datagram), &len, &would_be);
        if (status != SW_WIRE_OK) {
            SW_LOG_ERR("event %u:%llu: %s (would have been %zu B)",
                       event.envelope.camera_id,
                       (unsigned long long)event.envelope.frame_seq,
                       sw_wire_strerror(status), would_be);
            close(fd);
            return 3;
        }
        print_hex(datagram, len);
    }
    close(fd);
    return rc < 0 ? 2 : 0;
}

static int hex_nibble(int c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static int parse_hex(const char *text, uint8_t *out, size_t out_capacity, size_t *out_len)
{
    size_t len = strlen(text);
    size_t i;
    while (len > 0 && (text[len - 1] == '\n' || text[len - 1] == '\r')) {
        len--;
    }
    if (len % 2 != 0) {
        SW_LOG_ERR("hex line has an odd length (%zu)", len);
        return -1;
    }
    if (len / 2 > out_capacity) {
        SW_LOG_ERR("hex line decodes to %zu B, over the %zu B buffer", len / 2,
                   out_capacity);
        return -1;
    }
    for (i = 0; i < len; i += 2) {
        int hi = hex_nibble((unsigned char)text[i]);
        int lo = hex_nibble((unsigned char)text[i + 1]);
        if (hi < 0 || lo < 0) {
            SW_LOG_ERR("non-hex character in input");
            return -1;
        }
        out[i / 2] = (uint8_t)((hi << 4) | lo);
    }
    *out_len = len / 2;
    return 0;
}

static void print_event(const sw_capture_event_t *event)
{
    size_t i;
    for (i = 0; i < event->count; ++i) {
        const sw_observation_t *o = &event->observations[i];
        printf("cam=%u seq=%llu ts=%lld domain=%d tse=%.9g exp=%.9g gain=%.9g "
               "full=%ux%u proc=%ux%u lro=%.9g cal=%s det=%s uuid=%s "
               "obs=%u u=%.17g v=%.17g cuu=%.17g cuv=%.17g cvv=%.17g "
               "bbox=%d,%d,%u,%u area=%u persist=%u conf=%.9g blob=%s%u "
               "evidence=%s%s\n",
               event->envelope.camera_id,
               (unsigned long long)event->envelope.frame_seq,
               (long long)event->envelope.capture_ts_ns, (int)event->envelope.clock_domain,
               (double)event->envelope.time_sync_error_ms,
               (double)event->envelope.exposure_us, (double)event->envelope.gain_db,
               event->envelope.full_width, event->envelope.full_height,
               event->envelope.proc_width, event->envelope.proc_height,
               (double)event->envelope.line_readout_us, event->envelope.calibration_rev,
               event->envelope.detector_rev, event->envelope.session_uuid, o->obs_id,
               o->u, o->v, o->cov_uu, o->cov_uv, o->cov_vv, o->bbox_x, o->bbox_y,
               o->bbox_w, o->bbox_h, o->area_px, o->persistence_count,
               (double)o->confidence, o->has_local_blob_id ? "" : "absent:",
               o->has_local_blob_id ? o->local_blob_id : 0,
               o->has_evidence_ref ? "" : "absent:",
               o->has_evidence_ref ? o->evidence_ref : "");
    }
}

static int cmd_decode(const char *path)
{
    char line[4 * SW_DATAGRAM_CEILING_BYTES];
    uint8_t datagram[SW_DATAGRAM_CEILING_BYTES];
    sw_capture_event_t event;
    FILE *fh = fopen(path, "r");
    if (fh == NULL) {
        SW_LOG_ERR("cannot open %s", path);
        return 2;
    }
    while (fgets(line, sizeof(line), fh) != NULL) {
        size_t len = 0;
        sw_payload_type_t type;
        const uint8_t *body = NULL;
        size_t body_len = 0;
        sw_wire_status_t status;
        if (line[0] == '\n' || line[0] == '\0') {
            continue;
        }
        if (parse_hex(line, datagram, sizeof(datagram), &len) != 0) {
            fclose(fh);
            return 2;
        }
        status = sw_wire_unframe(datagram, len, &type, &body, &body_len);
        if (status != SW_WIRE_OK) {
            SW_LOG_ERR("unframe: %s", sw_wire_strerror(status));
            fclose(fh);
            return 3;
        }
        if (type != SW_PAYLOAD_OBSERVATION) {
            SW_LOG_ERR("payload type %d is not an observation packet", (int)type);
            fclose(fh);
            return 3;
        }
        status = sw_wire_decode_observation_packet(body, body_len, &event);
        if (status != SW_WIRE_OK) {
            SW_LOG_ERR("decode: %s", sw_wire_strerror(status));
            fclose(fh);
            return 3;
        }
        print_event(&event);
    }
    fclose(fh);
    return 0;
}

int main(int argc, char **argv)
{
    if (getenv("SKYWEAVE_DEBUG") != NULL) {
        sw_log_set_level(SW_LOG_DEBUG);
    }
    if (argc != 3) {
        fprintf(stderr, "usage: %s encode <observations.swob>\n"
                        "       %s decode <packets.hex>\n",
                argv[0], argv[0]);
        return 1;
    }
    if (strcmp(argv[1], "encode") == 0) {
        return cmd_encode(argv[2]);
    }
    if (strcmp(argv[1], "decode") == 0) {
        return cmd_decode(argv[2]);
    }
    fprintf(stderr, "unknown command %s\n", argv[1]);
    return 1;
}
