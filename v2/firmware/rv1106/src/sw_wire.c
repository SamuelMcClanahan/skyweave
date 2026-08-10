#include "sw_wire.h"

#include <string.h>

#include "pb_decode.h"
#include "pb_encode.h"
#include "sw_common.h"

/* The generated message must be able to hold what the ceiling permits, and
 * the ceiling must be able to hold what the detector cap permits. Both are
 * checked at COMPILE time here and again at run time on the host (test E1),
 * because a mismatch between the two sides is exactly the failure the
 * .options file exists to prevent and it must not wait for a board. */
SW_STATIC_ASSERT(SW_OBSERVATIONS_MAX >= 1, observations_bound_is_positive);
SW_STATIC_ASSERT(SW_DATAGRAM_CEILING_BYTES > SW_HEADER_LEN, ceiling_exceeds_header);

const char *sw_wire_strerror(sw_wire_status_t status)
{
    switch (status) {
    case SW_WIRE_OK:
        return "ok";
    case SW_WIRE_ERR_TOO_MANY_OBSERVATIONS:
        return "capture event carries more observations than the declared bound; "
               "measurement data never splits across datagrams";
    case SW_WIRE_ERR_TOO_LARGE:
        return "datagram over the channel ceiling; never truncated, never split";
    case SW_WIRE_ERR_ENCODE:
        return "nanopb encode failed";
    case SW_WIRE_ERR_DECODE:
        return "nanopb decode failed";
    case SW_WIRE_ERR_PROTOCOL:
        return "decoded content breaks a D0/D7 rule";
    case SW_WIRE_ERR_BOUNDS:
        return "a string exceeds its declared nanopb max_size";
    }
    return "unknown";
}

/* ------------------------------------------------------------------------ */
/* Bounds                                                                     */
/* ------------------------------------------------------------------------ */

/* codec.py `_check_text`: the bound is on UTF-8 BYTES and max_size includes
 * the NUL terminator. A C string that exactly fills the buffer has no room
 * for its terminator and nanopb would read past it. */
static sw_wire_status_t copy_text(char *dst, size_t dst_size, const char *src,
                                  const char *name)
{
    size_t len = strlen(src);
    if (len > dst_size - 1) {
        SW_LOG_ERR("%s is %zu bytes, over the declared nanopb bound of %zu", name, len,
                   dst_size - 1);
        return SW_WIRE_ERR_BOUNDS;
    }
    memcpy(dst, src, len);
    dst[len] = '\0';
    return SW_WIRE_OK;
}

/* ------------------------------------------------------------------------ */
/* Encode                                                                     */
/* ------------------------------------------------------------------------ */

static sw_wire_status_t fill_envelope(skyweave_v2_FrameEnvelope *target,
                                      const sw_envelope_t *envelope)
{
    sw_wire_status_t status;
    memset(target, 0, sizeof(*target));
    status = copy_text(target->session_uuid, sizeof(target->session_uuid),
                       envelope->session_uuid, "session_uuid");
    if (status != SW_WIRE_OK) {
        return status;
    }
    status = copy_text(target->calibration_rev, sizeof(target->calibration_rev),
                       envelope->calibration_rev, "calibration_rev");
    if (status != SW_WIRE_OK) {
        return status;
    }
    status = copy_text(target->detector_rev, sizeof(target->detector_rev),
                       envelope->detector_rev, "detector_rev");
    if (status != SW_WIRE_OK) {
        return status;
    }
    if (envelope->clock_domain == skyweave_v2_ClockDomain_CLOCK_DOMAIN_UNSPECIFIED) {
        SW_LOG_ERR("refusing to send a timestamp with no clock domain (D0 section 3)");
        return SW_WIRE_ERR_PROTOCOL;
    }
    target->camera_id = envelope->camera_id;
    target->frame_seq = envelope->frame_seq;
    target->capture_ts_ns = envelope->capture_ts_ns;
    target->clock_domain = envelope->clock_domain;
    target->time_sync_error_ms = envelope->time_sync_error_ms;
    target->exposure_us = envelope->exposure_us;
    target->gain_db = envelope->gain_db;
    target->full_width = envelope->full_width;
    target->full_height = envelope->full_height;
    target->proc_width = envelope->proc_width;
    target->proc_height = envelope->proc_height;
    target->line_readout_us = envelope->line_readout_us;
    return SW_WIRE_OK;
}

static sw_wire_status_t fill_observation(skyweave_v2_Observation2D *target,
                                         const sw_observation_t *obs)
{
    memset(target, 0, sizeof(*target));
    /* Field 1 stays UNSET: inside an ObservationPacket the envelope rides
     * once at packet level, and a per-observation envelope is a hard decode
     * error on the host, never a silent override. */
    target->has_envelope = false;
    target->obs_id = obs->obs_id;
    target->u = obs->u;
    target->v = obs->v;
    target->cov_uu = obs->cov_uu;
    target->cov_uv = obs->cov_uv;
    target->cov_vv = obs->cov_vv;
    /* bbox presence is set even when every component is zero: the host's
     * codec always sets the submessage, so an all-zero bbox is an EMPTY
     * PRESENT submessage on the wire, not an absent one. Getting this wrong
     * is a byte difference that only shows up on a blob at the origin. */
    target->has_bbox = true;
    target->bbox.x = obs->bbox_x;
    target->bbox.y = obs->bbox_y;
    target->bbox.w = obs->bbox_w;
    target->bbox.h = obs->bbox_h;
    target->area_px = obs->area_px;
    target->persistence_count = obs->persistence_count;
    target->confidence = obs->confidence;
    target->has_local_blob_id = obs->has_local_blob_id;
    target->local_blob_id = obs->has_local_blob_id ? obs->local_blob_id : 0;
    target->has_evidence_ref = obs->has_evidence_ref;
    if (obs->has_evidence_ref) {
        sw_wire_status_t status =
            copy_text(target->evidence_ref, sizeof(target->evidence_ref),
                      obs->evidence_ref, "evidence_ref");
        if (status != SW_WIRE_OK) {
            return status;
        }
    }
    return SW_WIRE_OK;
}

static sw_wire_status_t frame_datagram(sw_payload_type_t type, const uint8_t *body,
                                       size_t body_len, uint8_t *out,
                                       size_t out_capacity, size_t *out_len)
{
    size_t total = SW_HEADER_LEN + body_len;
    size_t ceiling = (type == SW_PAYLOAD_EVIDENCE) ? SW_EVIDENCE_CEILING_BYTES
                                                   : SW_DATAGRAM_CEILING_BYTES;
    if (total > ceiling) {
        return SW_WIRE_ERR_TOO_LARGE;
    }
    if (total > out_capacity) {
        return SW_WIRE_ERR_TOO_LARGE;
    }
    out[0] = SW_WIRE_MAGIC_0;
    out[1] = SW_WIRE_MAGIC_1;
    out[2] = SW_WIRE_VERSION;
    out[3] = (uint8_t)type;
    memmove(out + SW_HEADER_LEN, body, body_len);
    *out_len = total;
    return SW_WIRE_OK;
}

sw_wire_status_t sw_wire_encode_observation_packet(const sw_capture_event_t *event,
                                                   uint8_t *out, size_t out_capacity,
                                                   size_t *out_len,
                                                   size_t *would_be_bytes)
{
    /* One static packet, reused: the frame loop must not allocate
     * (RV1106_EDGE_NODE.md section 4). The daemon is single-threaded by
     * design — "one thread is sufficient and preferable" — so a file-scope
     * buffer is safe and is the cheapest thing on a 256 MB node. */
    static skyweave_v2_ObservationPacket packet;
    static uint8_t body[skyweave_v2_ObservationPacket_size];
    pb_ostream_t stream;
    size_t body_len = 0;
    size_t i;

    if (would_be_bytes != NULL) {
        *would_be_bytes = 0;
    }
    /* Count bound FIRST, before any bytes are weighed: it is what the peer's
     * nanopb allocates from, and it bites even when the bytes would fit. */
    if (event->count > SW_OBSERVATIONS_MAX) {
        SW_LOG_ERR("capture event %u:%llu carries %zu observations, declared bound is %zu",
                   event->envelope.camera_id,
                   (unsigned long long)event->envelope.frame_seq, event->count,
                   (size_t)SW_OBSERVATIONS_MAX);
        return SW_WIRE_ERR_TOO_MANY_OBSERVATIONS;
    }

    memset(&packet, 0, sizeof(packet));
    packet.has_envelope = true;
    {
        sw_wire_status_t status = fill_envelope(&packet.envelope, &event->envelope);
        if (status != SW_WIRE_OK) {
            return status;
        }
    }
    for (i = 0; i < event->count; ++i) {
        sw_wire_status_t status =
            fill_observation(&packet.observations[i], &event->observations[i]);
        if (status != SW_WIRE_OK) {
            return status;
        }
    }
    packet.observations_count = (pb_size_t)event->count;

    stream = pb_ostream_from_buffer(body, sizeof(body));
    if (!pb_encode(&stream, skyweave_v2_ObservationPacket_fields, &packet)) {
        SW_LOG_ERR("nanopb encode failed: %s", PB_GET_ERROR(&stream));
        return SW_WIRE_ERR_ENCODE;
    }
    body_len = stream.bytes_written;
    if (would_be_bytes != NULL) {
        *would_be_bytes = SW_HEADER_LEN + body_len;
    }
    return frame_datagram(SW_PAYLOAD_OBSERVATION, body, body_len, out, out_capacity,
                          out_len);
}

sw_wire_status_t sw_wire_encode_health(const sw_health_t *health, uint8_t *out,
                                       size_t out_capacity, size_t *out_len)
{
    static skyweave_v2_HealthPacket message;
    static uint8_t body[skyweave_v2_HealthPacket_size];
    pb_ostream_t stream;
    sw_wire_status_t status;

    memset(&message, 0, sizeof(message));
    status = copy_text(message.session_uuid, sizeof(message.session_uuid),
                       health->session_uuid, "session_uuid");
    if (status != SW_WIRE_OK) {
        return status;
    }
    message.camera_id = health->camera_id;
    message.ts_ns = health->ts_ns;
    message.clock_domain = health->clock_domain;
    message.fps = health->fps;
    message.drops = health->drops;
    message.time_sync_error_ms = health->time_sync_error_ms;
    /* Always present, matching the host encoder, which sets the quaternion
     * unconditionally. A node with no IMU sends the identity rotation and
     * says so by sending it, rather than by omitting the field. */
    message.has_imu_quaternion = true;
    message.imu_quaternion.x = health->imu_x;
    message.imu_quaternion.y = health->imu_y;
    message.imu_quaternion.z = health->imu_z;
    message.imu_quaternion.w = health->imu_w;

    stream = pb_ostream_from_buffer(body, sizeof(body));
    if (!pb_encode(&stream, skyweave_v2_HealthPacket_fields, &message)) {
        SW_LOG_ERR("nanopb health encode failed: %s", PB_GET_ERROR(&stream));
        return SW_WIRE_ERR_ENCODE;
    }
    return frame_datagram(SW_PAYLOAD_HEALTH, body, stream.bytes_written, out,
                          out_capacity, out_len);
}

/* ------------------------------------------------------------------------ */
/* Decode                                                                     */
/* ------------------------------------------------------------------------ */

sw_wire_status_t sw_wire_unframe(const uint8_t *datagram, size_t len,
                                 sw_payload_type_t *type, const uint8_t **body,
                                 size_t *body_len)
{
    size_t ceiling;
    if (len < SW_HEADER_LEN) {
        SW_LOG_ERR("datagram is %zu B, shorter than the %d B header", len,
                   SW_HEADER_LEN);
        return SW_WIRE_ERR_PROTOCOL;
    }
    if (datagram[0] != SW_WIRE_MAGIC_0 || datagram[1] != SW_WIRE_MAGIC_1) {
        SW_LOG_ERR("bad magic %02x%02x", datagram[0], datagram[1]);
        return SW_WIRE_ERR_PROTOCOL;
    }
    if (datagram[2] != SW_WIRE_VERSION) {
        SW_LOG_ERR("unsupported wire version %u, this build speaks %d", datagram[2],
                   SW_WIRE_VERSION);
        return SW_WIRE_ERR_PROTOCOL;
    }
    switch (datagram[3]) {
    case SW_PAYLOAD_OBSERVATION:
    case SW_PAYLOAD_HEALTH:
    case SW_PAYLOAD_EVIDENCE:
    case SW_PAYLOAD_CONTROL:
        *type = (sw_payload_type_t)datagram[3];
        break;
    default:
        SW_LOG_ERR("unknown payload type %u", datagram[3]);
        return SW_WIRE_ERR_PROTOCOL;
    }
    ceiling = (*type == SW_PAYLOAD_EVIDENCE) ? SW_EVIDENCE_CEILING_BYTES
                                             : SW_DATAGRAM_CEILING_BYTES;
    if (len > ceiling) {
        SW_LOG_ERR("received %zu B over the %zu B ceiling; rejected rather than parsed",
                   len, ceiling);
        return SW_WIRE_ERR_PROTOCOL;
    }
    *body = datagram + SW_HEADER_LEN;
    *body_len = len - SW_HEADER_LEN;
    return SW_WIRE_OK;
}

sw_wire_status_t sw_wire_decode_observation_packet(const uint8_t *body, size_t body_len,
                                                   sw_capture_event_t *event)
{
    static skyweave_v2_ObservationPacket packet;
    pb_istream_t stream = pb_istream_from_buffer(body, body_len);
    size_t i;

    memset(&packet, 0, sizeof(packet));
    /* pb_decode SKIPS unknown fields, which is the forward-compatibility half
     * of T5: a newer peer's extra fields must not break this parser. */
    if (!pb_decode(&stream, skyweave_v2_ObservationPacket_fields, &packet)) {
        SW_LOG_ERR("nanopb decode failed: %s", PB_GET_ERROR(&stream));
        return SW_WIRE_ERR_DECODE;
    }
    if (!packet.has_envelope) {
        SW_LOG_ERR("ObservationPacket carries no envelope (D0 section 4)");
        return SW_WIRE_ERR_PROTOCOL;
    }
    if (packet.envelope.clock_domain ==
        skyweave_v2_ClockDomain_CLOCK_DOMAIN_UNSPECIFIED) {
        SW_LOG_ERR("clock_domain is UNSPECIFIED; rejected rather than defaulted");
        return SW_WIRE_ERR_PROTOCOL;
    }
    if (packet.observations_count > SW_OBSERVATIONS_MAX) {
        return SW_WIRE_ERR_TOO_MANY_OBSERVATIONS;
    }

    memset(event, 0, sizeof(*event));
    event->envelope.camera_id = packet.envelope.camera_id;
    memcpy(event->envelope.session_uuid, packet.envelope.session_uuid,
           sizeof(event->envelope.session_uuid));
    event->envelope.frame_seq = packet.envelope.frame_seq;
    event->envelope.capture_ts_ns = packet.envelope.capture_ts_ns;
    event->envelope.clock_domain = packet.envelope.clock_domain;
    event->envelope.time_sync_error_ms = packet.envelope.time_sync_error_ms;
    event->envelope.exposure_us = packet.envelope.exposure_us;
    event->envelope.gain_db = packet.envelope.gain_db;
    event->envelope.full_width = packet.envelope.full_width;
    event->envelope.full_height = packet.envelope.full_height;
    event->envelope.proc_width = packet.envelope.proc_width;
    event->envelope.proc_height = packet.envelope.proc_height;
    memcpy(event->envelope.calibration_rev, packet.envelope.calibration_rev,
           sizeof(event->envelope.calibration_rev));
    memcpy(event->envelope.detector_rev, packet.envelope.detector_rev,
           sizeof(event->envelope.detector_rev));
    event->envelope.line_readout_us = packet.envelope.line_readout_us;

    for (i = 0; i < packet.observations_count; ++i) {
        const skyweave_v2_Observation2D *src = &packet.observations[i];
        sw_observation_t *dst = &event->observations[i];
        if (src->has_envelope) {
            SW_LOG_ERR("observation %u carries its own envelope inside a packet; "
                       "capture time is never overwritten (D0 section 3)",
                       src->obs_id);
            return SW_WIRE_ERR_PROTOCOL;
        }
        if (!src->has_bbox) {
            SW_LOG_ERR("observation %u carries no bbox (D0 field 8)", src->obs_id);
            return SW_WIRE_ERR_PROTOCOL;
        }
        memset(dst, 0, sizeof(*dst));
        dst->obs_id = src->obs_id;
        dst->u = src->u;
        dst->v = src->v;
        dst->cov_uu = src->cov_uu;
        dst->cov_uv = src->cov_uv;
        dst->cov_vv = src->cov_vv;
        dst->bbox_x = src->bbox.x;
        dst->bbox_y = src->bbox.y;
        dst->bbox_w = src->bbox.w;
        dst->bbox_h = src->bbox.h;
        dst->area_px = src->area_px;
        dst->persistence_count = src->persistence_count;
        dst->confidence = src->confidence;
        dst->has_local_blob_id = src->has_local_blob_id;
        dst->local_blob_id = src->local_blob_id;
        dst->has_evidence_ref = src->has_evidence_ref;
        if (src->has_evidence_ref) {
            memcpy(dst->evidence_ref, src->evidence_ref, sizeof(dst->evidence_ref));
        }
    }
    event->count = packet.observations_count;
    return SW_WIRE_OK;
}
