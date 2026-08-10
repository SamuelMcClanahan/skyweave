/* Datagram framing and nanopb encode/decode. The C twin of
 * `skyweave2/transport/wire.py` + `codec.py`.
 *
 * Everything in this header is a MIRROR, not a design. Each constant below
 * has exactly one counterpart on the host and test E2 fails if the two ever
 * produce different bytes for the same observations. Where the two files
 * disagree, the host is the contract and this file is wrong.
 *
 * The three asymmetries codec.py calls out are honoured here for the same
 * reasons, and each is a place a C encoder would otherwise lie quietly:
 *
 *   1. envelope-once: the packet-level envelope is set, and every
 *      observation's field 1 is NOT set. nanopb would happily emit both.
 *   2. binary32 narrowing: the D0 `float` fields are floats here, so the
 *      narrowing happens in the same place on both sides.
 *   3. absent != zero: local_blob_id and evidence_ref carry explicit
 *      presence flags, so "no local blob" and "blob 0" stay different.
 */

#ifndef SW_WIRE_H
#define SW_WIRE_H

#include "sw_obs.h"

#define SW_WIRE_MAGIC_0 'S'
#define SW_WIRE_MAGIC_1 'W'
#define SW_WIRE_VERSION 1
#define SW_HEADER_LEN 4

/* D0 section 10, "D8 opening": 1500 (untagged Ethernet MTU) - 20 (IPv4)
 * - 8 (UDP). A ceiling on the DATAGRAM, header included. */
#define SW_DATAGRAM_CEILING_BYTES 1472
#define SW_EVIDENCE_CEILING_BYTES (8 * 1024 + 256)

typedef enum {
    SW_PAYLOAD_OBSERVATION = 1,
    SW_PAYLOAD_HEALTH = 2,
    SW_PAYLOAD_EVIDENCE = 3,
    SW_PAYLOAD_CONTROL = 4
} sw_payload_type_t;

typedef enum {
    SW_WIRE_OK = 0,
    SW_WIRE_ERR_TOO_MANY_OBSERVATIONS = -1, /* count bound: what nanopb allocates */
    SW_WIRE_ERR_TOO_LARGE = -2,             /* byte ceiling: what the network carries */
    SW_WIRE_ERR_ENCODE = -3,
    SW_WIRE_ERR_DECODE = -4,
    SW_WIRE_ERR_PROTOCOL = -5, /* framing fine, content breaks a D0/D7 rule */
    SW_WIRE_ERR_BOUNDS = -6    /* a declared nanopb max_size would overflow */
} sw_wire_status_t;

const char *sw_wire_strerror(sw_wire_status_t status);

/* Encode one capture event into `out` (at least SW_DATAGRAM_CEILING_BYTES).
 * Writes the framed datagram (header + protobuf body) and its length.
 *
 * There is no truncating path and no splitting path, exactly as on the host:
 * an event that does not fit is refused whole, and `*would_be_bytes` reports
 * how large it would have been so the caller can say by how much. */
sw_wire_status_t sw_wire_encode_observation_packet(const sw_capture_event_t *event,
                                                   uint8_t *out, size_t out_capacity,
                                                   size_t *out_len,
                                                   size_t *would_be_bytes);

typedef struct {
    uint32_t camera_id;
    char session_uuid[SW_SESSION_UUID_MAX];
    int64_t ts_ns;
    skyweave_v2_ClockDomain clock_domain;
    double fps;
    uint64_t drops; /* measurement items NOT sent since boot; see sw_stats.h */
    double time_sync_error_ms;
    double imu_x, imu_y, imu_z, imu_w; /* xyzw, telemetry only (D0 section 1) */
} sw_health_t;

sw_wire_status_t sw_wire_encode_health(const sw_health_t *health, uint8_t *out,
                                       size_t out_capacity, size_t *out_len);

/* Decode direction, used by the control plane and by test E2's reverse leg.
 * `body`/`body_len` are the protobuf body WITHOUT the 4-byte header. */
sw_wire_status_t sw_wire_decode_observation_packet(const uint8_t *body, size_t body_len,
                                                   sw_capture_event_t *event);

/* Split a received datagram into its payload type and body, rejecting bad
 * magic, an unknown version, an unknown type, and anything over the ceiling.
 * An over-ceiling datagram is REJECTED rather than parsed: the size a
 * receiver saw is the only evidence the peer is not honouring the freeze. */
sw_wire_status_t sw_wire_unframe(const uint8_t *datagram, size_t len,
                                 sw_payload_type_t *type, const uint8_t **body,
                                 size_t *body_len);

#endif /* SW_WIRE_H */
