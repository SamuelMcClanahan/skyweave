/* UDP measurement plane and TCP control plane, per the D7 freeze.
 *
 * Measurement: one capture event per datagram, never retransmitted, never
 * split. The socket is connect()ed once so the frame loop is a bare send()
 * with no address resolution, and a send failure is COUNTED rather than
 * retried — a retransmitted measurement arrives after the batch it belonged
 * to and the aligner rejects it as late anyway.
 *
 * Control: TCP, the same protobuf, a 4-byte big-endian length prefix in
 * front of the same 4-byte framing header. Non-blocking accept and read, so
 * the control plane can never stall a frame: a node that stops detecting
 * because someone opened a socket is worse than a node that ignores them.
 */

#ifndef SW_NET_H
#define SW_NET_H

#include "sw_obs.h"

typedef struct {
    int fd;
    uint64_t datagrams_sent;
    uint64_t bytes_sent;
    uint64_t send_failures;
} sw_udp_sender_t;

int sw_udp_open(sw_udp_sender_t *sender, const char *host, int port);
int sw_udp_send(sw_udp_sender_t *sender, const uint8_t *datagram, size_t len);
void sw_udp_close(sw_udp_sender_t *sender);

typedef struct {
    int listen_fd;
    int client_fd;
    uint64_t frames_in;
    uint64_t frames_out;
    uint64_t rejected;
    uint64_t control_seq_seen;
    bool stop_requested;
    bool start_seen;
} sw_control_t;

int sw_control_open(sw_control_t *control, int port);

/* Service the control plane without blocking. Returns 0 always; every
 * failure is logged and counted, because a control-plane error must not take
 * the measurement plane down with it. */
int sw_control_poll(sw_control_t *control);

void sw_control_close(sw_control_t *control);

#endif /* SW_NET_H */
