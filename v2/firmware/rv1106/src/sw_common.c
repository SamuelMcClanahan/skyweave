#include "sw_common.h"

#include <errno.h>
#include <stdarg.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static sw_log_level_t g_level = SW_LOG_INFO;

static const char *const k_level_names[] = {"ERROR", "WARN", "INFO", "DEBUG"};

void sw_log_set_level(sw_log_level_t level) { g_level = level; }

sw_log_level_t sw_log_level(void) { return g_level; }

void sw_log(sw_log_level_t level, const char *fmt, ...)
{
    va_list args;
    if (level > g_level) {
        return;
    }
    /* stderr, unbuffered by default, and never stdout: stdout carries the
     * tools' machine-readable output and a stray log line there would be
     * parsed as data. */
    fprintf(stderr, "[skyweave-edge %s] ", k_level_names[level]);
    va_start(args, fmt);
    vfprintf(stderr, fmt, args);
    va_end(args);
    fputc('\n', stderr);
}

int64_t sw_monotonic_ns(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0;
    }
    return (int64_t)ts.tv_sec * 1000000000LL + (int64_t)ts.tv_nsec;
}

void sw_sleep_ns(int64_t ns)
{
    struct timespec req;
    if (ns <= 0) {
        return;
    }
    req.tv_sec = (time_t)(ns / 1000000000LL);
    req.tv_nsec = (long)(ns % 1000000000LL);
    /* The return value and the EINTR remainder are discarded deliberately.
     * Every caller sleeps towards an ABSOLUTE deadline it recomputes on the
     * next slice, so a short sleep is absorbed there instead of accumulating
     * the loop's own cost as pace drift. */
    (void)nanosleep(&req, NULL);
}

int sw_read_exact_by(int fd, void *buffer, size_t n, int64_t deadline_ns)
{
    uint8_t *out = (uint8_t *)buffer;
    size_t done = 0;
    while (done < n) {
        ssize_t got;
        if (deadline_ns > 0 && sw_monotonic_ns() > deadline_ns) {
            SW_LOG_ERR("read deadline expired after %zu of %zu B; abandoning the "
                       "peer rather than letting it hold this loop",
                       done, n);
            return -1;
        }
        got = read(fd, out + done, n - done);
        if (got == 0) {
            if (done == 0) {
                return 1; /* clean EOF on a record boundary */
            }
            SW_LOG_ERR("short read: wanted %zu B, stream ended after %zu B", n, done);
            return -1;
        }
        if (got < 0) {
            if (errno == EINTR) {
                continue;
            }
            if ((errno == EAGAIN || errno == EWOULDBLOCK) && deadline_ns > 0) {
                continue; /* the deadline check above is what ends this */
            }
            SW_LOG_ERR("read failed: %s", strerror(errno));
            return -1;
        }
        done += (size_t)got;
    }
    return 0;
}

int sw_read_exact(int fd, void *buffer, size_t n)
{
    /* No deadline: the injection stream and the fixture files are local and
     * trusted, and a deadline there would turn a slow disk into a lost clip. */
    return sw_read_exact_by(fd, buffer, n, 0);
}

uint16_t sw_be16(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

uint32_t sw_be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) |
           (uint32_t)p[3];
}

uint64_t sw_be64(const uint8_t *p)
{
    return ((uint64_t)sw_be32(p) << 32) | (uint64_t)sw_be32(p + 4);
}

float sw_be_float(const uint8_t *p)
{
    /* memcpy, not a pointer cast: the RV1106's A7 faults on unaligned VFP
     * loads and a cast here would be a latent, data-dependent crash. */
    uint32_t bits = sw_be32(p);
    float out;
    memcpy(&out, &bits, sizeof(out));
    return out;
}

double sw_be_double(const uint8_t *p)
{
    uint64_t bits = sw_be64(p);
    double out;
    memcpy(&out, &bits, sizeof(out));
    return out;
}
