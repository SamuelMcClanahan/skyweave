#include "sw_obsfixture.h"

#include <string.h>

#include "sw_common.h"

/* Record layouts, byte by byte, from `_HEADER` / `_EVENT` / `_OBSERVATION`
 * in `skyweave2/edge/obsfixture.py`. Written out as explicit offsets rather
 * than a packed struct: `#pragma pack` and struct layout are the compiler's
 * business, and the fixture's business is that both sides read the same
 * bytes. A miscount fails the length assertion below, not silently later. */
#define FIXTURE_HEADER_LEN 8 /* magic(4) version(1) reserved(1) count(2) */
#define END_LEN 8            /* magic(4) count(4) */

/* Event record, offsets relative to the record start (magic included). */
#define EV_OFF_MAGIC 0
#define EV_OFF_CAMERA_ID 4
#define EV_OFF_FRAME_SEQ 8
#define EV_OFF_CAPTURE_TS 16
#define EV_OFF_DOMAIN 24
#define EV_OFF_TIME_SYNC 25
#define EV_OFF_EXPOSURE 29
#define EV_OFF_GAIN 33
#define EV_OFF_FULL_W 37
#define EV_OFF_FULL_H 41
#define EV_OFF_PROC_W 45
#define EV_OFF_PROC_H 49
#define EV_OFF_LINE_READOUT 53
#define EV_OFF_UUID_LEN 57
#define EV_OFF_CAL_LEN 58
#define EV_OFF_DET_LEN 59
#define EV_OFF_OBS_COUNT 60
#define EV_RECORD_LEN 61

/* Observation record, same convention. */
#define OBS_OFF_MAGIC 0
#define OBS_OFF_ID 4
#define OBS_OFF_U 8
#define OBS_OFF_V 16
#define OBS_OFF_COV_UU 24
#define OBS_OFF_COV_UV 32
#define OBS_OFF_COV_VV 40
#define OBS_OFF_BBOX_X 48
#define OBS_OFF_BBOX_Y 52
#define OBS_OFF_BBOX_W 56
#define OBS_OFF_BBOX_H 60
#define OBS_OFF_AREA 64
#define OBS_OFF_PERSIST 68
#define OBS_OFF_CONFIDENCE 72
#define OBS_OFF_HAS_BLOB 76
#define OBS_OFF_BLOB_ID 77
#define OBS_OFF_HAS_EVIDENCE 81
#define OBS_OFF_EVIDENCE_LEN 82
#define OBS_RECORD_LEN 83

SW_STATIC_ASSERT(EV_RECORD_LEN == EV_OFF_OBS_COUNT + 1, event_record_len_matches);
SW_STATIC_ASSERT(OBS_RECORD_LEN == OBS_OFF_EVIDENCE_LEN + 1, obs_record_len_matches);

static int read_text(int fd, uint8_t len, char *dst, size_t dst_size, const char *name)
{
    uint8_t scratch[256];
    if ((size_t)len > dst_size - 1) {
        SW_LOG_ERR("fixture %s is %u bytes, over the declared bound of %zu", name, len,
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

static skyweave_v2_ClockDomain domain_from_wire(uint8_t value)
{
    switch (value) {
    case 1:
        return skyweave_v2_ClockDomain_CLOCK_DOMAIN_SYNTHETIC;
    case 2:
        return skyweave_v2_ClockDomain_CLOCK_DOMAIN_NODE_MONO;
    case 3:
        return skyweave_v2_ClockDomain_CLOCK_DOMAIN_NODE_PTP;
    case 4:
        return skyweave_v2_ClockDomain_CLOCK_DOMAIN_JETSON_RX;
    default:
        return skyweave_v2_ClockDomain_CLOCK_DOMAIN_UNSPECIFIED;
    }
}

int sw_obsfixture_open(sw_obsfixture_t *fixture, int fd)
{
    uint8_t header[FIXTURE_HEADER_LEN];
    memset(fixture, 0, sizeof(*fixture));
    if (sw_read_exact(fd, header, sizeof(header)) != 0) {
        return -1;
    }
    if (memcmp(header, "SWOB", 4) != 0) {
        SW_LOG_ERR("bad observation fixture magic");
        return -1;
    }
    if (header[4] != 1) {
        SW_LOG_ERR("observation fixture version %u, this build speaks 1", header[4]);
        return -1;
    }
    if (header[5] != 0) {
        SW_LOG_ERR("observation fixture reserved byte is %u, must be 0", header[5]);
        return -1;
    }
    fixture->fd = fd;
    fixture->declared_events = sw_be16(header + 6);
    return 0;
}

static int read_observations(sw_obsfixture_t *fixture, sw_capture_event_t *event,
                             uint8_t obs_count)
{
    size_t i;
    for (i = 0; i < obs_count; ++i) {
        uint8_t buf[OBS_RECORD_LEN];
        sw_observation_t *obs = &event->observations[i];
        if (sw_read_exact(fixture->fd, buf, sizeof(buf)) != 0) {
            return -1;
        }
        if (memcmp(buf + OBS_OFF_MAGIC, "SWOO", 4) != 0) {
            SW_LOG_ERR("bad observation magic in fixture event %u",
                       fixture->read_events);
            return -1;
        }
        memset(obs, 0, sizeof(*obs));
        obs->obs_id = sw_be32(buf + OBS_OFF_ID);
        obs->u = sw_be_double(buf + OBS_OFF_U);
        obs->v = sw_be_double(buf + OBS_OFF_V);
        obs->cov_uu = sw_be_double(buf + OBS_OFF_COV_UU);
        obs->cov_uv = sw_be_double(buf + OBS_OFF_COV_UV);
        obs->cov_vv = sw_be_double(buf + OBS_OFF_COV_VV);
        obs->bbox_x = (int32_t)sw_be32(buf + OBS_OFF_BBOX_X);
        obs->bbox_y = (int32_t)sw_be32(buf + OBS_OFF_BBOX_Y);
        obs->bbox_w = sw_be32(buf + OBS_OFF_BBOX_W);
        obs->bbox_h = sw_be32(buf + OBS_OFF_BBOX_H);
        obs->area_px = sw_be32(buf + OBS_OFF_AREA);
        obs->persistence_count = sw_be32(buf + OBS_OFF_PERSIST);
        obs->confidence = sw_be_float(buf + OBS_OFF_CONFIDENCE);
        obs->has_local_blob_id = buf[OBS_OFF_HAS_BLOB] != 0;
        obs->local_blob_id = sw_be32(buf + OBS_OFF_BLOB_ID);
        obs->has_evidence_ref = buf[OBS_OFF_HAS_EVIDENCE] != 0;
        if (read_text(fixture->fd, buf[OBS_OFF_EVIDENCE_LEN], obs->evidence_ref,
                      sizeof(obs->evidence_ref), "evidence_ref") != 0) {
            return -1;
        }
    }
    return 0;
}

int sw_obsfixture_next(sw_obsfixture_t *fixture, sw_capture_event_t *event)
{
    uint8_t record[EV_RECORD_LEN];
    uint8_t obs_count;

    if (sw_read_exact(fixture->fd, record, 4) != 0) {
        return -1;
    }
    if (memcmp(record, "SWOZ", 4) == 0) {
        uint8_t tail[END_LEN - 4];
        if (sw_read_exact(fixture->fd, tail, sizeof(tail)) != 0) {
            return -1;
        }
        if (sw_be32(tail) != fixture->read_events ||
            fixture->read_events != fixture->declared_events) {
            SW_LOG_ERR("fixture trailer declares %u events, %u were read "
                       "(header said %u)",
                       sw_be32(tail), fixture->read_events, fixture->declared_events);
            return -1;
        }
        return 1;
    }
    if (memcmp(record, "SWOE", 4) != 0) {
        SW_LOG_ERR("expected an event or trailer magic in the fixture");
        return -1;
    }
    if (sw_read_exact(fixture->fd, record + 4, EV_RECORD_LEN - 4) != 0) {
        return -1;
    }

    memset(event, 0, sizeof(*event));
    event->envelope.camera_id = sw_be32(record + EV_OFF_CAMERA_ID);
    event->envelope.frame_seq = sw_be64(record + EV_OFF_FRAME_SEQ);
    event->envelope.capture_ts_ns = (int64_t)sw_be64(record + EV_OFF_CAPTURE_TS);
    event->envelope.clock_domain = domain_from_wire(record[EV_OFF_DOMAIN]);
    event->envelope.time_sync_error_ms = sw_be_float(record + EV_OFF_TIME_SYNC);
    event->envelope.exposure_us = sw_be_float(record + EV_OFF_EXPOSURE);
    event->envelope.gain_db = sw_be_float(record + EV_OFF_GAIN);
    event->envelope.full_width = sw_be32(record + EV_OFF_FULL_W);
    event->envelope.full_height = sw_be32(record + EV_OFF_FULL_H);
    event->envelope.proc_width = sw_be32(record + EV_OFF_PROC_W);
    event->envelope.proc_height = sw_be32(record + EV_OFF_PROC_H);
    event->envelope.line_readout_us = sw_be_float(record + EV_OFF_LINE_READOUT);
    obs_count = record[EV_OFF_OBS_COUNT];

    if (read_text(fixture->fd, record[EV_OFF_UUID_LEN], event->envelope.session_uuid,
                  sizeof(event->envelope.session_uuid), "session_uuid") != 0 ||
        read_text(fixture->fd, record[EV_OFF_CAL_LEN], event->envelope.calibration_rev,
                  sizeof(event->envelope.calibration_rev), "calibration_rev") != 0 ||
        read_text(fixture->fd, record[EV_OFF_DET_LEN], event->envelope.detector_rev,
                  sizeof(event->envelope.detector_rev), "detector_rev") != 0) {
        return -1;
    }
    if (obs_count > SW_OBSERVATIONS_MAX) {
        SW_LOG_ERR("fixture event carries %u observations, over the declared bound %zu",
                   obs_count, (size_t)SW_OBSERVATIONS_MAX);
        return -1;
    }
    if (read_observations(fixture, event, obs_count) != 0) {
        return -1;
    }
    event->count = obs_count;
    fixture->read_events++;
    return 0;
}
