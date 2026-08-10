#include "sw_net.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <string.h>
#include <sys/socket.h>
/* SO_RCVTIMEO takes a `struct timeval`, which lives here. Included
 * explicitly: under a strict _POSIX_C_SOURCE it does NOT arrive via
 * <sys/socket.h> on every libc, and the omission compiled on macOS's
 * default headers while failing under the daemon's declared feature set. */
#include <sys/time.h>
#include <unistd.h>

#include "pb_decode.h"
#include "pb_encode.h"
#include "sw_common.h"
#include "sw_wire.h"

/* ------------------------------------------------------------------------ */
/* Measurement plane                                                          */
/* ------------------------------------------------------------------------ */

int sw_udp_open(sw_udp_sender_t *sender, const char *host, int port)
{
    struct sockaddr_in address;
    memset(sender, 0, sizeof(*sender));
    sender->fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sender->fd < 0) {
        SW_LOG_ERR("measurement socket: %s", strerror(errno));
        return -1;
    }
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &address.sin_addr) != 1) {
        SW_LOG_ERR("measurement destination %s is not a dotted-quad address; the "
                   "daemon does no name resolution on purpose (a DNS lookup in the "
                   "frame loop is a stall waiting to happen)",
                   host);
        close(sender->fd);
        sender->fd = -1;
        return -1;
    }
    if (connect(sender->fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        SW_LOG_ERR("measurement connect to %s:%d: %s", host, port, strerror(errno));
        close(sender->fd);
        sender->fd = -1;
        return -1;
    }
    SW_LOG_INFO("measurement plane -> %s:%d", host, port);
    return 0;
}

int sw_udp_send(sw_udp_sender_t *sender, const uint8_t *datagram, size_t len)
{
    ssize_t sent;
    if (sender->fd < 0) {
        return -1;
    }
    sent = send(sender->fd, datagram, len, 0);
    if (sent < 0 || (size_t)sent != len) {
        /* Counted, never retried. A retransmitted measurement arrives after
         * the batch it belonged to and the host's aligner rejects it as
         * late; the honest record is that this one did not go. */
        sender->send_failures++;
        SW_LOG_WARN("measurement send failed (%zd of %zu B): %s", sent, len,
                    strerror(errno));
        return -1;
    }
    sender->datagrams_sent++;
    sender->bytes_sent += len;
    return 0;
}

void sw_udp_close(sw_udp_sender_t *sender)
{
    if (sender->fd >= 0) {
        close(sender->fd);
        sender->fd = -1;
    }
}

/* ------------------------------------------------------------------------ */
/* Control plane                                                              */
/* ------------------------------------------------------------------------ */

#define CONTROL_LENGTH_PREFIX_LEN 4
#define CONTROL_FRAME_MAX_BYTES (64 * 1024)
/* Total wall-clock budget for reading ONE control frame, well under a 33 ms
 * frame period at 30 fps. */
#define SW_CONTROL_FRAME_BUDGET_MS 5

static int set_nonblocking(int fd)
{
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
        SW_LOG_ERR("fcntl O_NONBLOCK: %s", strerror(errno));
        return -1;
    }
    return 0;
}

int sw_control_open(sw_control_t *control, int port)
{
    struct sockaddr_in address;
    int reuse = 1;
    memset(control, 0, sizeof(*control));
    control->client_fd = -1;
    control->listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (control->listen_fd < 0) {
        SW_LOG_ERR("control socket: %s", strerror(errno));
        return -1;
    }
    setsockopt(control->listen_fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_ANY);
    address.sin_port = htons((uint16_t)port);
    if (bind(control->listen_fd, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        listen(control->listen_fd, 1) != 0) {
        SW_LOG_ERR("control listen on port %d: %s", port, strerror(errno));
        close(control->listen_fd);
        control->listen_fd = -1;
        return -1;
    }
    if (set_nonblocking(control->listen_fd) != 0) {
        close(control->listen_fd);
        control->listen_fd = -1;
        return -1;
    }
    SW_LOG_INFO("control plane listening on port %d", port);
    return 0;
}

static int send_ack(sw_control_t *control, uint64_t control_seq, bool accepted,
                    const char *detail)
{
    static skyweave_v2_ControlMessage message;
    static uint8_t body[skyweave_v2_ControlMessage_size];
    uint8_t datagram[CONTROL_FRAME_MAX_BYTES];
    uint8_t prefix[CONTROL_LENGTH_PREFIX_LEN];
    pb_ostream_t stream;
    size_t total;
    size_t detail_len = strlen(detail);

    memset(&message, 0, sizeof(message));
    message.type = skyweave_v2_ControlType_CONTROL_TYPE_ACK;
    message.control_seq = control_seq;
    message.has_ack = true;
    message.ack.control_seq = control_seq;
    message.ack.accepted = accepted;
    if (detail_len > sizeof(message.ack.detail) - 1) {
        /* The host makes the same check for the same reason: a NACK the peer
         * cannot decode is worse than no NACK at all. */
        detail_len = sizeof(message.ack.detail) - 1;
    }
    memcpy(message.ack.detail, detail, detail_len);
    message.ack.detail[detail_len] = '\0';

    stream = pb_ostream_from_buffer(body, sizeof(body));
    if (!pb_encode(&stream, skyweave_v2_ControlMessage_fields, &message)) {
        SW_LOG_ERR("control ack encode: %s", PB_GET_ERROR(&stream));
        return -1;
    }
    if (SW_HEADER_LEN + stream.bytes_written > sizeof(datagram)) {
        return -1;
    }
    datagram[0] = SW_WIRE_MAGIC_0;
    datagram[1] = SW_WIRE_MAGIC_1;
    datagram[2] = SW_WIRE_VERSION;
    datagram[3] = (uint8_t)SW_PAYLOAD_CONTROL;
    memcpy(datagram + SW_HEADER_LEN, body, stream.bytes_written);
    total = SW_HEADER_LEN + stream.bytes_written;
    prefix[0] = (uint8_t)(total >> 24);
    prefix[1] = (uint8_t)(total >> 16);
    prefix[2] = (uint8_t)(total >> 8);
    prefix[3] = (uint8_t)total;
    if (write(control->client_fd, prefix, sizeof(prefix)) != (ssize_t)sizeof(prefix) ||
        write(control->client_fd, datagram, total) != (ssize_t)total) {
        SW_LOG_WARN("control ack write failed: %s", strerror(errno));
        return -1;
    }
    control->frames_out++;
    return 0;
}

/* Read one whole control frame from a socket already known to have data,
 * bounded by a total deadline (see below). */
static int read_frame(int fd, uint8_t *out, size_t capacity, size_t *out_len)
{
    uint8_t prefix[CONTROL_LENGTH_PREFIX_LEN];
    uint32_t length;
    /* The WHOLE frame, prefix included, must land inside this budget. A
     * per-syscall timeout is not a bound on a loop: a peer sending one byte
     * per second would hold this single-threaded daemon indefinitely, and
     * the measurement plane would stop because someone opened a socket.
     * SW_CONTROL_FRAME_BUDGET_MS is well under a frame period, so the worst
     * a hostile control peer costs is one late frame, once. */
    int64_t deadline = sw_monotonic_ns() +
                       (int64_t)SW_CONTROL_FRAME_BUDGET_MS * 1000000LL;
    int status = sw_read_exact_by(fd, prefix, sizeof(prefix), deadline);
    if (status != 0) {
        return status;
    }
    length = sw_be32(prefix);
    if (length > CONTROL_FRAME_MAX_BYTES || length > capacity) {
        /* Bounded BEFORE allocating or reading: a hostile or corrupt length
         * must not make the node reserve memory. */
        SW_LOG_ERR("control frame declares %u B; refusing to allocate", length);
        return -1;
    }
    if (sw_read_exact_by(fd, out, length, deadline) != 0) {
        return -1;
    }
    *out_len = length;
    return 0;
}

static void handle_message(sw_control_t *control, const uint8_t *body, size_t body_len)
{
    static skyweave_v2_ControlMessage message;
    pb_istream_t stream = pb_istream_from_buffer(body, body_len);
    memset(&message, 0, sizeof(message));
    if (!pb_decode(&stream, skyweave_v2_ControlMessage_fields, &message)) {
        control->rejected++;
        SW_LOG_WARN("control decode failed: %s", PB_GET_ERROR(&stream));
        return;
    }
    control->control_seq_seen = message.control_seq;
    switch (message.type) {
    case skyweave_v2_ControlType_CONTROL_TYPE_START:
        control->start_seen = true;
        send_ack(control, message.control_seq, true, "start");
        break;
    case skyweave_v2_ControlType_CONTROL_TYPE_STOP:
        control->stop_requested = true;
        send_ack(control, message.control_seq, true, "stop");
        break;
    case skyweave_v2_ControlType_CONTROL_TYPE_SET_CONFIG:
        /* NACKed, deliberately. Accepting a live config change would mean
         * the observations before and after it were measured by different
         * detectors under one detector_rev, and every scored artifact in
         * this project is identified by its configuration. The lever exists
         * on the wire; the daemon declines to pull it this phase. */
        send_ack(control, message.control_seq, false,
                 "SET_CONFIG refused: a live detector change would break "
                 "detector_rev traceability (D8)");
        break;
    case skyweave_v2_ControlType_CONTROL_TYPE_CALIBRATION_REVISION:
        if (message.has_calibration) {
            send_ack(control, message.control_seq, true, "calibration noted");
        } else {
            control->rejected++;
            send_ack(control, message.control_seq, false,
                     "CALIBRATION_REVISION carries no calibration payload");
        }
        break;
    case skyweave_v2_ControlType_CONTROL_TYPE_ACK:
        control->rejected++;
        SW_LOG_WARN("a peer sent the node an ACK; ignored");
        break;
    default:
        control->rejected++;
        send_ack(control, message.control_seq, false, "control message has no type");
        break;
    }
}

int sw_control_poll(sw_control_t *control)
{
    uint8_t frame[CONTROL_FRAME_MAX_BYTES];
    size_t frame_len = 0;
    sw_payload_type_t type;
    const uint8_t *body = NULL;
    size_t body_len = 0;

    if (control->listen_fd < 0) {
        return 0;
    }
    if (control->client_fd < 0) {
        int fd = accept(control->listen_fd, NULL, NULL);
        if (fd < 0) {
            return 0; /* EAGAIN: nobody is calling */
        }
        {
            struct timeval timeout;
            timeout.tv_sec = 1;
            timeout.tv_usec = 0;
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        }
        control->client_fd = fd;
        SW_LOG_INFO("control plane: peer connected");
        return 0;
    }
    if (set_nonblocking(control->client_fd) != 0) {
        return 0;
    }
    {
        uint8_t peek;
        ssize_t got = recv(control->client_fd, &peek, 1, MSG_PEEK);
        if (got == 0) {
            SW_LOG_INFO("control plane: peer disconnected");
            close(control->client_fd);
            control->client_fd = -1;
            return 0;
        }
        if (got < 0) {
            return 0; /* EAGAIN */
        }
    }
    /* The fd stays NON-BLOCKING; the deadline inside read_frame is what
     * bounds the read now. Switching to blocking here was the old approach
     * and it relied on SO_RCVTIMEO, which bounds a syscall rather than the
     * loop around it. */
    if (read_frame(control->client_fd, frame, sizeof(frame), &frame_len) != 0) {
        control->rejected++;
        close(control->client_fd);
        control->client_fd = -1;
        return 0;
    }
    control->frames_in++;
    if (sw_wire_unframe(frame, frame_len, &type, &body, &body_len) != SW_WIRE_OK ||
        type != SW_PAYLOAD_CONTROL) {
        control->rejected++;
        SW_LOG_WARN("control frame rejected at the framing layer");
        return 0;
    }
    handle_message(control, body, body_len);
    return 0;
}

void sw_control_close(sw_control_t *control)
{
    if (control->client_fd >= 0) {
        close(control->client_fd);
        control->client_fd = -1;
    }
    if (control->listen_fd >= 0) {
        close(control->listen_fd);
        control->listen_fd = -1;
    }
}
