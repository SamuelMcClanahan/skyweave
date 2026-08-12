#include "sw_config.h"

#include <ctype.h>
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "sw_common.h"

void sw_config_defaults(sw_config_t *config)
{
    memset(config, 0, sizeof(*config));
    config->source = SW_SOURCE_INJECT_FILE;
#ifdef SKYWEAVE_HAVE_RKMPI
    config->detector_kind = SW_DETECTOR_IVE;
#else
    config->detector_kind = SW_DETECTOR_SOFT;
#endif
    config->inject_port = 5600;
    config->ram_loop_frames = 0;
    config->ram_loop_pts_stride_ns = 0;
    config->ram_loop_period_ns = 0;
    config->ram_budget_mb = SW_RAM_BUDGET_DEFAULT_MB;
    snprintf(config->jetson_host, sizeof(config->jetson_host), "127.0.0.1");
    config->measurement_port = 5601;
    config->control_port = 5602;
    config->health_period_ms = 1000; /* the brief: 1 Hz health packet */
    config->camera_id = 0;

    /* Every default below is DetectorConfig's, verbatim. */
    config->detector.gmm2.model_num = 3;
    config->detector.gmm2.var_init = 225.0f;   /* (15 DN)^2 */
    config->detector.gmm2.var_min = 25.0f;     /* (5 DN)^2 */
    config->detector.gmm2.learn_rate = 0.005f;
    config->detector.gmm2.bg_ratio = 0.9f;
    config->detector.gmm2.match_sigmas = 2.5f;
    config->detector.gmm2.weight_init = 0.05f;
    config->detector.proc_width = 1536;
    config->detector.proc_height = 864;
    config->detector.warmup_frames = 90;
    config->detector.open_radius_px = 1;
    config->detector.min_area_px = 2;
    config->detector.max_area_px = 10000;
    config->detector.persistence_frames = 2;
    config->detector.persistence_gate_px = 12.0f;
    config->detector.max_components_per_frame = 7;
    config->detector.centroid_cov_floor_px2 = 0.25;
    config->detector.exposure_us = 3000.0f;
    config->detector.fps = 30.0f;
    snprintf(config->detector.detector_rev, sizeof(config->detector.detector_rev),
             "d8-edge/1");
    snprintf(config->detector.calibration_rev, sizeof(config->detector.calibration_rev),
             "uncalibrated");
}

const char *sw_source_name(sw_source_kind_t source)
{
    /* No `default:` arm, deliberately. -Wswitch under -Werror then turns a
     * fifth source kind into a COMPILE error here, instead of a run that
     * silently records the wrong mode in a collected artifact. */
    switch (source) {
    case SW_SOURCE_INJECT_FILE:
        return "inject-file";
    case SW_SOURCE_INJECT_TCP:
        return "inject-tcp";
    case SW_SOURCE_INJECT_RAM:
        return "inject-ram";
    case SW_SOURCE_VI:
        return "capture-vi";
    }
    return "unknown";
}

int sw_config_validate(const sw_config_t *config)
{
    const sw_detector_config_t *d = &config->detector;
    if (d->max_components_per_frame < 1) {
        SW_LOG_ERR("component cap must be at least 1, got %d",
                   d->max_components_per_frame);
        return -1;
    }
    /* THE invariant of the D8 capacity decision. The wire bound is what the
     * peer's nanopb allocates from; a cap above it would build capture
     * events this daemon can encode and the host cannot decode. Refused at
     * startup, not discovered on a cluttered frame at 3 a.m. */
    if ((size_t)d->max_components_per_frame > SW_OBSERVATIONS_MAX) {
        SW_LOG_ERR("component cap %d exceeds the declared wire bound %zu "
                   "(proto/skyweave.options ObservationPacket.observations); "
                   "the cap may never exceed what the peer allocates",
                   d->max_components_per_frame, (size_t)SW_OBSERVATIONS_MAX);
        return -1;
    }
    if (d->proc_width < 1 || d->proc_height < 1) {
        SW_LOG_ERR("proc resolution %dx%d is not a grid", d->proc_width,
                   d->proc_height);
        return -1;
    }
    if (d->gmm2.model_num < 1 || d->gmm2.model_num > SW_GMM2_MAX_MODELS) {
        SW_LOG_ERR("GMM2 model_num %d outside 1..%d", d->gmm2.model_num,
                   SW_GMM2_MAX_MODELS);
        return -1;
    }
    if (d->warmup_frames < 0) {
        SW_LOG_ERR("warmup_frames %d is negative", d->warmup_frames);
        return -1;
    }
    if (d->min_area_px < 1 || d->max_area_px < d->min_area_px) {
        SW_LOG_ERR("area gate [%d, %d] is empty", d->min_area_px, d->max_area_px);
        return -1;
    }
    if (d->persistence_frames < 1) {
        SW_LOG_ERR("persistence_frames %d is below 1", d->persistence_frames);
        return -1;
    }
    if (!(d->centroid_cov_floor_px2 > 0.0)) {
        SW_LOG_ERR("centroid covariance floor must be positive (D0: never zero)");
        return -1;
    }
    if (config->source == SW_SOURCE_VI) {
#ifndef SKYWEAVE_HAVE_RKMPI
        SW_LOG_ERR("--capture-vi needs the RKMPI SDK; this build has none. "
                   "Refusing rather than silently falling back to injection: a "
                   "node that thinks it is capturing and is not would report "
                   "clean health forever");
        return -1;
#endif
    }
    if (config->detector_kind == SW_DETECTOR_IVE) {
#ifndef SKYWEAVE_HAVE_RKMPI
        SW_LOG_ERR("--detector ive needs the RKMPI IVE SDK; this build has none. "
                   "Refusing rather than substituting the portable detector: the "
                   "whole point of the D8 tolerance table is knowing WHICH "
                   "detector produced a number");
        return -1;
#endif
    }
    if (config->source == SW_SOURCE_INJECT_FILE && config->inject_path[0] == '\0') {
        SW_LOG_ERR("--inject-file needs a path");
        return -1;
    }
    if (config->source == SW_SOURCE_INJECT_RAM) {
        if (config->inject_path[0] == '\0') {
            SW_LOG_ERR("--inject-ram needs a path to the SWIJ clip to preload");
            return -1;
        }
        if (config->ram_loop_frames < 1) {
            SW_LOG_ERR("--inject-ram needs --ram-loop-frames N: a RAM loop has no "
                       "trailer, and a run bounded by anything but a frame count "
                       "cannot be compared with == on the deterministic counters "
                       "(E8)");
            return -1;
        }
        /* A repeated warm-up is not the hazard here — frame_seq is continuous
         * across wraps, so warm-up happens once — but a warm-up at or beyond
         * the budget means the run ends still warming. */
        if ((uint32_t)d->warmup_frames >= config->ram_loop_frames) {
            SW_LOG_ERR("--warmup %d against --ram-loop-frames %u: the whole run "
                       "would be warm-up and nothing would be scored",
                       d->warmup_frames, config->ram_loop_frames);
            return -1;
        }
        if (config->ram_loop_pts_stride_ns <= 0) {
            SW_LOG_ERR("--ram-loop-pts-stride-ns must be positive; the daemon adds "
                       "the harness's declared per-pass advance and computes no "
                       "timestamp of its own");
            return -1;
        }
        if (config->ram_budget_mb < 1 || config->ram_budget_mb > 2000) {
            SW_LOG_ERR("--ram-budget-mb %d outside 1..2000 (decimal MB, daemon-only)",
                       config->ram_budget_mb);
            return -1;
        }
    }
    if (config->ram_loop_period_ns < 0) {
        SW_LOG_ERR("--ram-loop-period-ns %lld is negative",
                   (long long)config->ram_loop_period_ns);
        return -1;
    }
    if (config->ram_loop_period_ns != 0 && config->source != SW_SOURCE_INJECT_RAM) {
        SW_LOG_ERR("--ram-loop-period-ns applies to --inject-ram only; the file and "
                   "TCP sources are paced by whoever writes them");
        return -1;
    }
    if (config->ram_loop_period_ns > (int64_t)config->health_period_ms * 1000000LL) {
        SW_LOG_ERR("--ram-loop-period-ns %lld exceeds the %d ms health cadence: a "
                   "pace period longer than the health period would delay every "
                   "health packet by more than a cadence, because the pace sleep "
                   "does not service the health plane inside a slice",
                   (long long)config->ram_loop_period_ns, config->health_period_ms);
        return -1;
    }
    return 0;
}

void sw_config_usage(const char *argv0)
{
    fprintf(stderr,
            "usage: %s [options]\n"
            "\n"
            "  source (exactly one):\n"
            "    --inject-file PATH     replay a SWIJ injection stream from storage\n"
            "    --inject-listen PORT   accept a SWIJ injection stream over TCP\n"
            "    --inject-ram PATH      preload a SWIJ clip into DDR and loop it\n"
            "    --capture-vi           real CSI capture through RKMPI VI\n"
            "\n"
            "  RAM loop (--inject-ram only; every value is declared, none derived):\n"
            "    --ram-loop-frames N        total frames the run serves, then it ends\n"
            "    --ram-loop-pts-stride-ns S capture_ts_ns advance per pass\n"
            "    --ram-loop-period-ns P     per-frame pace (0 = unpaced)\n"
            "    --ram-budget-mb N          clip+detector+fixed ceiling, decimal MB\n"
            "\n"
            "  detector:\n"
            "    --detector soft|ive    portable C GMM2 (host) or RK_MPI_IVE_GMM2\n"
            "    --proc WxH             processing resolution\n"
            "    --warmup N             warm-up frames before any observation\n"
            "    --cap N                per-frame component cap (<= wire bound)\n"
            "\n"
            "  output:\n"
            "    --jetson HOST          measurement/health destination\n"
            "    --measurement-port P\n"
            "    --control-port P\n"
            "    --health-period-ms N\n"
            "    --packet-log PATH      hex datagrams, one per line (fixture gate)\n"
            "    --stats PATH           end-of-run counters as JSON\n"
            "\n"
            "  identity:\n"
            "    --camera-id N\n"
            "    --session-uuid U       declared session; else generated at boot\n"
            "    --detector-rev R\n"
            "    --calibration-rev R\n"
            "\n"
            "    -v                     more logging (repeatable)\n",
            argv0);
}

static int parse_wxh(const char *text, int *width, int *height)
{
    char *end = NULL;
    long w = strtol(text, &end, 10);
    long h;
    if (end == NULL || (*end != 'x' && *end != 'X')) {
        return -1;
    }
    h = strtol(end + 1, &end, 10);
    if (end == NULL || *end != '\0' || w < 1 || h < 1) {
        return -1;
    }
    *width = (int)w;
    *height = (int)h;
    return 0;
}

static int copy_arg(char *dst, size_t dst_size, const char *src, const char *name)
{
    size_t len = strlen(src);
    if (len > dst_size - 1) {
        SW_LOG_ERR("--%s is %zu bytes, over the %zu byte bound", name, len,
                   dst_size - 1);
        return -1;
    }
    memcpy(dst, src, len + 1);
    return 0;
}

/* The RAM-loop numbers are DECLARATIONS, so a value that was not understood
 * has to be refused rather than repaired.
 *
 * Finding "--ram-loop-frames is an unchecked (uint32_t)strtoul": the bare
 * cast was the very truncation the comment above it said it was chosen to
 * avoid. strtoul is DEFINED to return ULONG_MAX for "-1", which narrows to
 * 4294967295 and produces a run that never ends; "200,000" stops at the comma
 * and yields 200, a thousandth of the declared soak that still looks
 * declared; anything at or above 2^32 wraps modulo 2^32. sw_config_validate
 * cannot see any of it, because its only test is `< 1` on an unsigned field.
 *
 * So: the WHOLE string has to be consumed, the sign has to be absent, and the
 * value has to fit the field, or the flag is refused in the same voice as
 * every other refusal here. Never clamped — a clamp would put a number in the
 * artifact that no run carried out. */
static int parse_u32_arg(const char *text, uint32_t *out, const char *flag)
{
    char *end = NULL;
    unsigned long long value;

    /* The sign is rejected BEFORE the parse, not after: strtoull accepts a
     * leading '-' and returns the wrapped value, so a post-hoc range check
     * would see 4294967295 and think it was asked for. */
    if (text[0] == '\0' || text[0] == '-' || text[0] == '+' ||
        isspace((unsigned char)text[0])) {
        SW_LOG_ERR("%s wants a plain decimal integer in 0..%lu, got \"%s\": "
                   "refusing rather than truncating a declaration into a "
                   "shorter run that still looks declared",
                   flag, (unsigned long)UINT32_MAX, text);
        return -1;
    }
    errno = 0;
    value = strtoull(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' ||
        value > (unsigned long long)UINT32_MAX) {
        SW_LOG_ERR("%s wants a plain decimal integer in 0..%lu, got \"%s\": "
                   "refusing rather than truncating a declaration into a "
                   "shorter run that still looks declared",
                   flag, (unsigned long)UINT32_MAX, text);
        return -1;
    }
    *out = (uint32_t)value;
    return 0;
}

/* Same rule for the nanosecond declarations. "33.3e6" parsed to 33 and ran a
 * "paced" soak at 30 million fps with every frame late; the sign is allowed
 * through here because sw_config_validate refuses negatives with a message
 * about what the flag MEANS, which is the better error of the two.
 *
 * No explicit int64 range check: `long long` is 64-bit on both targets (the
 * host builds and the RV1106's ARM32 ABI), so strtoll's own ERANGE is exactly
 * the field's range, and a written-out comparison would be a tautology that
 * -Wtype-limits refuses to compile. */
static int parse_i64_arg(const char *text, int64_t *out, const char *flag)
{
    char *end = NULL;
    long long value;

    if (text[0] == '\0' || isspace((unsigned char)text[0])) {
        SW_LOG_ERR("%s wants a plain decimal integer of nanoseconds, got "
                   "\"%s\": refusing rather than truncating a declaration",
                   flag, text);
        return -1;
    }
    errno = 0;
    value = strtoll(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0') {
        SW_LOG_ERR("%s wants a plain decimal integer of nanoseconds, got "
                   "\"%s\": refusing rather than truncating a declaration",
                   flag, text);
        return -1;
    }
    *out = (int64_t)value;
    return 0;
}

#define NEED_VALUE(flag)                                       \
    do {                                                       \
        if (i + 1 >= argc) {                                   \
            SW_LOG_ERR("%s needs a value", flag);              \
            return -1;                                         \
        }                                                      \
    } while (0)

int sw_config_parse_args(sw_config_t *config, int argc, char **argv)
{
    int i;
    int verbosity = 0;
    int sources = 0;
    for (i = 1; i < argc; ++i) {
        const char *arg = argv[i];
        if (strcmp(arg, "--help") == 0 || strcmp(arg, "-h") == 0) {
            return 1;
        } else if (strcmp(arg, "--inject-file") == 0) {
            NEED_VALUE(arg);
            config->source = SW_SOURCE_INJECT_FILE;
            sources++;
            if (copy_arg(config->inject_path, sizeof(config->inject_path), argv[++i],
                         "inject-file") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--inject-listen") == 0) {
            NEED_VALUE(arg);
            config->source = SW_SOURCE_INJECT_TCP;
            sources++;
            config->inject_port = atoi(argv[++i]);
        } else if (strcmp(arg, "--inject-ram") == 0) {
            NEED_VALUE(arg);
            config->source = SW_SOURCE_INJECT_RAM;
            sources++;
            if (copy_arg(config->inject_path, sizeof(config->inject_path), argv[++i],
                         "inject-ram") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--ram-loop-frames") == 0) {
            NEED_VALUE(arg);
            /* A CHECKED parse and not atoi, and not a bare cast either: a
             * frame budget and a nanosecond stride both run past what an int
             * holds, and both of the cheaper spellings truncate a soak's
             * declaration into a shorter run that still looks declared. */
            if (parse_u32_arg(argv[++i], &config->ram_loop_frames,
                              "--ram-loop-frames") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--ram-loop-pts-stride-ns") == 0) {
            NEED_VALUE(arg);
            if (parse_i64_arg(argv[++i], &config->ram_loop_pts_stride_ns,
                              "--ram-loop-pts-stride-ns") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--ram-loop-period-ns") == 0) {
            NEED_VALUE(arg);
            if (parse_i64_arg(argv[++i], &config->ram_loop_period_ns,
                              "--ram-loop-period-ns") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--ram-budget-mb") == 0) {
            NEED_VALUE(arg);
            config->ram_budget_mb = atoi(argv[++i]);
        } else if (strcmp(arg, "--capture-vi") == 0) {
            config->source = SW_SOURCE_VI;
            sources++;
        } else if (strcmp(arg, "--detector") == 0) {
            NEED_VALUE(arg);
            ++i;
            if (strcmp(argv[i], "soft") == 0) {
                config->detector_kind = SW_DETECTOR_SOFT;
            } else if (strcmp(argv[i], "ive") == 0) {
                config->detector_kind = SW_DETECTOR_IVE;
            } else {
                SW_LOG_ERR("--detector must be soft or ive, got %s", argv[i]);
                return -1;
            }
        } else if (strcmp(arg, "--proc") == 0) {
            NEED_VALUE(arg);
            if (parse_wxh(argv[++i], &config->detector.proc_width,
                          &config->detector.proc_height) != 0) {
                SW_LOG_ERR("--proc wants WxH, got %s", argv[i]);
                return -1;
            }
        } else if (strcmp(arg, "--warmup") == 0) {
            NEED_VALUE(arg);
            config->detector.warmup_frames = atoi(argv[++i]);
        } else if (strcmp(arg, "--cap") == 0) {
            NEED_VALUE(arg);
            config->detector.max_components_per_frame = atoi(argv[++i]);
        } else if (strcmp(arg, "--jetson") == 0) {
            NEED_VALUE(arg);
            if (copy_arg(config->jetson_host, sizeof(config->jetson_host), argv[++i],
                         "jetson") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--measurement-port") == 0) {
            NEED_VALUE(arg);
            config->measurement_port = atoi(argv[++i]);
        } else if (strcmp(arg, "--control-port") == 0) {
            NEED_VALUE(arg);
            config->control_port = atoi(argv[++i]);
        } else if (strcmp(arg, "--health-period-ms") == 0) {
            NEED_VALUE(arg);
            config->health_period_ms = atoi(argv[++i]);
        } else if (strcmp(arg, "--packet-log") == 0) {
            NEED_VALUE(arg);
            if (copy_arg(config->packet_log_path, sizeof(config->packet_log_path),
                         argv[++i], "packet-log") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--stats") == 0) {
            NEED_VALUE(arg);
            if (copy_arg(config->stats_path, sizeof(config->stats_path), argv[++i],
                         "stats") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--camera-id") == 0) {
            NEED_VALUE(arg);
            config->camera_id = (uint32_t)strtoul(argv[++i], NULL, 10);
        } else if (strcmp(arg, "--session-uuid") == 0) {
            NEED_VALUE(arg);
            if (copy_arg(config->session_uuid, sizeof(config->session_uuid), argv[++i],
                         "session-uuid") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--detector-rev") == 0) {
            NEED_VALUE(arg);
            if (copy_arg(config->detector.detector_rev,
                         sizeof(config->detector.detector_rev), argv[++i],
                         "detector-rev") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "--calibration-rev") == 0) {
            NEED_VALUE(arg);
            if (copy_arg(config->detector.calibration_rev,
                         sizeof(config->detector.calibration_rev), argv[++i],
                         "calibration-rev") != 0) {
                return -1;
            }
        } else if (strcmp(arg, "-v") == 0) {
            verbosity++;
        } else {
            SW_LOG_ERR("unknown argument %s", arg);
            return -1;
        }
    }
    if (sources > 1) {
        SW_LOG_ERR("choose exactly one source: --inject-file, --inject-listen, "
                   "--inject-ram or --capture-vi");
        return -1;
    }
    if (verbosity == 1) {
        sw_log_set_level(SW_LOG_DEBUG);
    } else if (verbosity > 1) {
        sw_log_set_level(SW_LOG_DEBUG);
    }
    return 0;
}
