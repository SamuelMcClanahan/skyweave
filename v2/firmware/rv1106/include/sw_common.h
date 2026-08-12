/* Types, logging and small helpers shared by every translation unit.
 *
 * Rules this file exists to enforce, from RV1106_EDGE_NODE.md:
 *   - no allocation inside the frame loop (nothing here allocates at all);
 *   - no silent failure (SW_LOG_ERR goes to stderr and the caller returns);
 *   - the A7 is the scarce resource, so helpers stay branch-light and inline.
 *
 * The daemon targets C99 with the Luckfox SDK's gcc 8.3.0 (uclibc). Nothing
 * here may need a newer standard: a build that only works on the developer's
 * clang is not a build.
 */

#ifndef SW_COMMON_H
#define SW_COMMON_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#define SW_UNUSED(x) ((void)(x))

/* Compile-time assertion without C11's _Static_assert (gcc 8.3 in C99 mode
 * has it as a GNU extension, but the SDK toolchain is not the only compiler
 * this has to survive). */
#define SW_STATIC_ASSERT(cond, tag) \
    typedef char sw_static_assert_##tag[(cond) ? 1 : -1]

typedef enum {
    SW_LOG_ERROR = 0,
    SW_LOG_WARN = 1,
    SW_LOG_INFO = 2,
    SW_LOG_DEBUG = 3
} sw_log_level_t;

void sw_log_set_level(sw_log_level_t level);
sw_log_level_t sw_log_level(void);
void sw_log(sw_log_level_t level, const char *fmt, ...);

#define SW_LOG_ERR(...) sw_log(SW_LOG_ERROR, __VA_ARGS__)
#define SW_LOG_WARN(...) sw_log(SW_LOG_WARN, __VA_ARGS__)
#define SW_LOG_INFO(...) sw_log(SW_LOG_INFO, __VA_ARGS__)
#define SW_LOG_DEBUG(...) sw_log(SW_LOG_DEBUG, __VA_ARGS__)

/* Monotonic nanoseconds. Used for pacing, health cadence and perf counters
 * ONLY. It never becomes a capture timestamp: capture_ts_ns comes from the
 * kernel buffer stamp on the board and from the injection header under C1,
 * and a wall-clock value has no business in a scored artifact (the repo's
 * determinism rule). */
int64_t sw_monotonic_ns(void);

/* Sleep for at most `ns` nanoseconds on the same monotonic clock
 * sw_monotonic_ns reads. PACING ONLY, exactly as scoped above: a deadline
 * never becomes a capture timestamp and never reaches --stats, the packet log
 * or any scored artifact. Returns immediately if `ns` <= 0.
 *
 * Does NOT resume after a signal. The only signals this daemon installs are
 * SIGINT/SIGTERM (which mean leave) and SIGPIPE (ignored), so the caller
 * re-tests its stop flag at the top of the next slice and a SIGTERM is never
 * slept through. nanosleep (<time.h>) rather than usleep, which
 * _POSIX_C_SOURCE=200809L does not declare, and rather than
 * clock_nanosleep, which the macOS host build does not have. */
void sw_sleep_ns(int64_t ns);

/* Read exactly n bytes from a file descriptor, or fail. Returns 0 on success,
 * -1 on error, 1 on clean EOF before any byte was read (the only case a
 * caller is allowed to treat as "the stream ended"). */
int sw_read_exact(int fd, void *buffer, size_t n);

/* Same, but the WHOLE read is bounded by a monotonic deadline.
 *
 * SO_RCVTIMEO bounds one syscall, not a loop: a peer that dribbles one byte
 * per timeout keeps sw_read_exact resident forever. On a single-threaded
 * daemon that is the frame loop stopping — the control plane starving the
 * measurement plane, which is precisely backwards. Anything reading from a
 * peer that is not trusted to be prompt uses this. */
int sw_read_exact_by(int fd, void *buffer, size_t n, int64_t deadline_ns);

/* Big-endian ("network order") readers. The injection plane and the
 * measurement wire share one byte-order rule so the daemon needs only one. */
uint16_t sw_be16(const uint8_t *p);
uint32_t sw_be32(const uint8_t *p);
uint64_t sw_be64(const uint8_t *p);
float sw_be_float(const uint8_t *p);
double sw_be_double(const uint8_t *p);

#endif /* SW_COMMON_H */
