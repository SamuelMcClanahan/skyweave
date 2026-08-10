/* skyweave-edge: the RV1106 edge daemon.
 *
 * One process, one thread, no allocation in the frame loop. The RV1106 has
 * no SMP to exploit and threads would only add context-switch and lock cost
 * (RV1106_EDGE_NODE.md section 4).
 *
 * The loop, per frame:
 *   1. get a PROC-resolution luma frame (injection stream, or VI capture);
 *   2. detector -> components at proc resolution;
 *   3. persistence -> per-frame cap -> capture event (sw_pipeline.c);
 *   4. nanopb encode -> one UDP datagram, or a LOUD refusal;
 *   5. service the control plane and the 1 Hz health packet.
 *
 * What it never does: retry a measurement, truncate a packet, split a
 * capture event across datagrams, or drop a component without counting it.
 */

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "sw_capture.h"
#include "sw_common.h"
#include "sw_config.h"
#include "sw_detect.h"
#include "sw_inject.h"
#include "sw_net.h"
#include "sw_pipeline.h"
#include "sw_wire.h"

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int signum)
{
    SW_UNUSED(signum);
    g_stop = 1;
}

typedef struct {
    uint64_t frames_in;
    uint64_t frames_detector_failed;
    uint64_t capture_events;
    uint64_t observations_sent;
    uint64_t events_unencodable;
    /* Measurement and health both leave through the same socket, so the
     * SENDER's counters are a socket total and cannot answer "how many
     * measurements went out". These two can. Conflating them made a replay
     * report one more datagram than it had capture events, which is exactly
     * the kind of off-by-one that reads as a lost measurement. */
    uint64_t measurement_datagrams_sent;
    uint64_t measurement_bytes_sent;
    uint64_t health_sent;
} sw_run_stats_t;

/* HealthPacket.drops, whose D8 semantics are declared in codec.py's Health
 * docstring: measurement items the node DID NOT SEND since boot. Frames the
 * detector could not process, components the cap removed, capture events
 * that could not be encoded, and datagrams the socket refused.
 *
 * It is a total, not a breakdown, because HealthPacket has one counter field
 * and adding another would be a wire change this phase is not sanctioned to
 * make. The breakdown lives in --stats and in the log; the total is what the
 * Jetson sees every second. Finding D8-F2 records the consequence. */
static uint64_t total_drops(const sw_run_stats_t *stats, const sw_pipeline_t *pipeline,
                            const sw_udp_sender_t *sender,
                            const sw_detector_t *detector)
{
    sw_detector_losses_t losses;
    losses.components_shed = 0;
    losses.frames_shed = 0;
    if (detector != NULL && detector->losses != NULL) {
        detector->losses(detector, &losses);
    }
    /* Four causes, one total. `send_failures` belongs here as much as the
     * others: a datagram the socket refused is a measurement that did not
     * arrive, and counting it only in a socket statistic would leave the
     * Jetson's drop total silent about the one failure that happens on the
     * link it is watching. */
    return stats->frames_detector_failed + pipeline->components_dropped_over_cap +
           stats->events_unencodable + sender->send_failures + losses.components_shed;
}

static void write_stats(const char *path, const sw_config_t *config,
                        const sw_run_stats_t *stats, const sw_pipeline_t *pipeline,
                        const sw_udp_sender_t *sender, const sw_control_t *control,
                        const sw_detector_t *detector, const char *detector_name)
{
    sw_detector_losses_t losses;
    FILE *fh;
    losses.components_shed = 0;
    losses.frames_shed = 0;
    if (detector != NULL && detector->losses != NULL) {
        detector->losses(detector, &losses);
    }
    if (path == NULL || path[0] == '\0') {
        return;
    }
    fh = fopen(path, "w");
    if (fh == NULL) {
        SW_LOG_ERR("cannot write stats to %s", path);
        return;
    }
    fprintf(fh,
            "{\n"
            "  \"camera_id\": %u,\n"
            "  \"detector\": \"%s\",\n"
            "  \"proc_width\": %d,\n"
            "  \"proc_height\": %d,\n"
            "  \"max_components_per_frame\": %d,\n"
            "  \"wire_observations_max_count\": %zu,\n"
            "  \"frames_in\": %llu,\n"
            "  \"frames_detector_failed\": %llu,\n"
            "  \"frames_at_cap\": %llu,\n"
            "  \"frames_scored\": %llu,\n"
            "  \"components_offered\": %llu,\n"
            "  \"components_emitted\": %llu,\n"
            "  \"components_dropped_over_cap\": %llu,\n"
            "  \"max_components_offered\": %u,\n"
            "  \"capture_events\": %llu,\n"
            "  \"observations_sent\": %llu,\n"
            "  \"events_unencodable\": %llu,\n"
            "  \"datagrams_sent\": %llu,\n"
            "  \"bytes_sent\": %llu,\n"
            "  \"socket_datagrams_sent\": %llu,\n"
            "  \"socket_bytes_sent\": %llu,\n"
            "  \"send_failures\": %llu,\n"
            "  \"health_sent\": %llu,\n"
            "  \"control_frames_in\": %llu,\n"
            "  \"control_frames_out\": %llu,\n"
            "  \"control_rejected\": %llu,\n"
            "  \"components_shed_by_detector\": %llu,\n"
            "  \"frames_shed_by_detector\": %llu,\n"
            "  \"health_drops_total\": %llu\n"
            "}\n",
            config->camera_id, detector_name, config->detector.proc_width,
            config->detector.proc_height, config->detector.max_components_per_frame,
            (size_t)SW_OBSERVATIONS_MAX, (unsigned long long)stats->frames_in,
            (unsigned long long)stats->frames_detector_failed,
            (unsigned long long)pipeline->frames_at_cap,
            (unsigned long long)pipeline->frames,
            (unsigned long long)pipeline->components_offered,
            (unsigned long long)pipeline->components_emitted,
            (unsigned long long)pipeline->components_dropped_over_cap,
            pipeline->max_components_offered,
            (unsigned long long)stats->capture_events,
            (unsigned long long)stats->observations_sent,
            (unsigned long long)stats->events_unencodable,
            (unsigned long long)stats->measurement_datagrams_sent,
            (unsigned long long)stats->measurement_bytes_sent,
            (unsigned long long)sender->datagrams_sent,
            (unsigned long long)sender->bytes_sent,
            (unsigned long long)sender->send_failures,
            (unsigned long long)stats->health_sent,
            (unsigned long long)control->frames_in,
            (unsigned long long)control->frames_out,
            (unsigned long long)control->rejected,
            (unsigned long long)losses.components_shed,
            (unsigned long long)losses.frames_shed,
            (unsigned long long)total_drops(stats, pipeline, sender, detector));
    fclose(fh);
}

static void log_packet(FILE *packet_log, const uint8_t *datagram, size_t len)
{
    size_t i;
    if (packet_log == NULL) {
        return;
    }
    for (i = 0; i < len; ++i) {
        fprintf(packet_log, "%02x", datagram[i]);
    }
    fputc('\n', packet_log);
}

static void send_health(const sw_config_t *config, const sw_envelope_t *envelope,
                        double fps, uint64_t drops, sw_udp_sender_t *sender,
                        sw_run_stats_t *stats)
{
    uint8_t datagram[SW_DATAGRAM_CEILING_BYTES];
    size_t len = 0;
    sw_health_t health;
    memset(&health, 0, sizeof(health));
    health.camera_id = config->camera_id;
    snprintf(health.session_uuid, sizeof(health.session_uuid), "%s",
             envelope->session_uuid);
    /* The health packet's own clock is the node's, and it says so. The
     * measurement envelope's domain belongs to the measurement. */
    health.ts_ns = sw_monotonic_ns();
    health.clock_domain = skyweave_v2_ClockDomain_CLOCK_DOMAIN_NODE_MONO;
    health.fps = fps;
    health.drops = drops;
    health.time_sync_error_ms = envelope->time_sync_error_ms;
    /* No IMU wired yet: the identity rotation, sent explicitly rather than
     * omitted, so "no IMU" and "IMU reading zero" stay distinguishable by
     * the fact that the field is always present. */
    health.imu_w = 1.0;
    if (sw_wire_encode_health(&health, datagram, sizeof(datagram), &len) != SW_WIRE_OK) {
        SW_LOG_WARN("health encode failed");
        return;
    }
    if (sw_udp_send(sender, datagram, len) == 0) {
        stats->health_sent++;
    }
}

int main(int argc, char **argv)
{
    sw_config_t config;
    sw_run_stats_t stats;
    sw_pipeline_t pipeline;
    sw_udp_sender_t sender;
    sw_control_t control;
    sw_inject_t inject;
    sw_detector_t *detector = NULL;
    sw_capture_t *capture = NULL;
    FILE *packet_log = NULL;
    sw_envelope_t envelope;
    static sw_component_t components[SW_MAX_COMPONENTS];
    static sw_capture_event_t event;
    uint8_t datagram[SW_DATAGRAM_CEILING_BYTES];
    double scale_x = 1.0;
    double scale_y = 1.0;
    int64_t started_ns;
    int64_t last_health_ns;
    int parse_status;
    int exit_code = 0;

    memset(&stats, 0, sizeof(stats));
    memset(&sender, 0, sizeof(sender));
    memset(&control, 0, sizeof(control));
    memset(&inject, 0, sizeof(inject));
    memset(&envelope, 0, sizeof(envelope));
    sender.fd = -1;
    control.listen_fd = -1;
    control.client_fd = -1;

    sw_config_defaults(&config);
    parse_status = sw_config_parse_args(&config, argc, argv);
    if (parse_status != 0) {
        sw_config_usage(argv[0]);
        return parse_status > 0 ? 0 : 1;
    }
    if (sw_config_validate(&config) != 0) {
        return 1;
    }
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    /* A failed measurement send must not kill the daemon. */
    signal(SIGPIPE, SIG_IGN);

    if (config.source == SW_SOURCE_VI) {
        capture = sw_capture_open(&config);
        if (capture == NULL) {
            return 1;
        }
        envelope.camera_id = config.camera_id;
        envelope.full_width = 2304;
        envelope.full_height = 1296;
        envelope.proc_width = (uint32_t)config.detector.proc_width;
        envelope.proc_height = (uint32_t)config.detector.proc_height;
        /* Real capture: the node's own clock, which is what NODE_MONO means.
         * Not NODE_PTP — this build disciplines nothing — and the sync error
         * is declared, not assumed zero. */
        envelope.clock_domain = skyweave_v2_ClockDomain_CLOCK_DOMAIN_NODE_MONO;
        envelope.time_sync_error_ms = 0.0f;
        snprintf(envelope.session_uuid, sizeof(envelope.session_uuid), "%s",
                 config.session_uuid);
    } else if (config.source == SW_SOURCE_INJECT_TCP) {
        if (sw_inject_open_listen(&inject, config.inject_port) != 0) {
            return 1;
        }
    } else {
        if (sw_inject_open_file(&inject, config.inject_path) != 0) {
            return 1;
        }
    }

    if (config.source != SW_SOURCE_VI) {
        /* The injection stream's declared identity is authoritative: the
         * harness knows which clip and which camera it is replaying and the
         * daemon does not get to relabel it. A --session-uuid on the command
         * line is only a fallback for real capture. */
        envelope.camera_id = inject.camera_id;
        snprintf(envelope.session_uuid, sizeof(envelope.session_uuid), "%s",
                 inject.session_uuid);
        envelope.full_width = inject.full_width;
        envelope.full_height = inject.full_height;
        envelope.proc_width = inject.proc_width;
        envelope.proc_height = inject.proc_height;
        envelope.clock_domain = inject.clock_domain;
        envelope.exposure_us = inject.exposure_us;
        envelope.gain_db = inject.gain_db;
        envelope.line_readout_us = inject.line_readout_us;
        snprintf(envelope.calibration_rev, sizeof(envelope.calibration_rev), "%s",
                 inject.calibration_rev);
        snprintf(envelope.detector_rev, sizeof(envelope.detector_rev), "%s",
                 inject.detector_rev);
        if ((int)inject.proc_width != config.detector.proc_width ||
            (int)inject.proc_height != config.detector.proc_height) {
            SW_LOG_INFO("adopting the injection stream's %ux%u processing grid "
                        "(command line asked for %dx%d)",
                        inject.proc_width, inject.proc_height,
                        config.detector.proc_width, config.detector.proc_height);
            config.detector.proc_width = (int)inject.proc_width;
            config.detector.proc_height = (int)inject.proc_height;
        }
        config.camera_id = inject.camera_id;
    } else {
        snprintf(envelope.calibration_rev, sizeof(envelope.calibration_rev), "%s",
                 config.detector.calibration_rev);
        snprintf(envelope.detector_rev, sizeof(envelope.detector_rev), "%s",
                 config.detector.detector_rev);
        envelope.exposure_us = config.detector.exposure_us;
    }

    scale_x = (double)envelope.full_width / (double)envelope.proc_width;
    scale_y = (double)envelope.full_height / (double)envelope.proc_height;

    detector = sw_detect_open(&config);
    if (detector == NULL) {
        exit_code = 1;
        goto cleanup;
    }
    sw_pipeline_init(&pipeline, &config.detector);

    if (sw_udp_open(&sender, config.jetson_host, config.measurement_port) != 0) {
        exit_code = 1;
        goto cleanup;
    }
    sw_control_open(&control, config.control_port);
    if (config.packet_log_path[0] != '\0') {
        packet_log = fopen(config.packet_log_path, "w");
        if (packet_log == NULL) {
            SW_LOG_ERR("cannot open packet log %s", config.packet_log_path);
            exit_code = 1;
            goto cleanup;
        }
    }

    SW_LOG_INFO("skyweave-edge: camera %u, detector %s, proc %dx%d, cap %d/frame, "
                "wire bound %zu/event",
                config.camera_id, detector->name, config.detector.proc_width,
                config.detector.proc_height, config.detector.max_components_per_frame,
                (size_t)SW_OBSERVATIONS_MAX);

    started_ns = sw_monotonic_ns();
    last_health_ns = started_ns;

    while (!g_stop && !control.stop_requested) {
        const uint8_t *luma = NULL;
        int width = 0;
        int height = 0;
        int component_count;
        int dropped = 0;
        bool warming;
        double occupancy = 0.0;
        int64_t now_ns;

        if (config.source == SW_SOURCE_VI) {
            sw_capture_frame_t frame;
            int status = sw_capture_next(capture, &frame);
            if (status != 0) {
                break;
            }
            luma = frame.luma;
            width = frame.width;
            height = frame.height;
            envelope.frame_seq = frame.frame_seq;
            envelope.capture_ts_ns = frame.capture_ts_ns;
        } else {
            sw_inject_frame_t frame;
            int status = sw_inject_next(&inject, &frame);
            if (status > 0) {
                break; /* clean trailer */
            }
            if (status < 0) {
                exit_code = 1;
                break;
            }
            luma = frame.luma;
            width = (int)frame.width;
            height = (int)frame.height;
            envelope.frame_seq = frame.frame_seq;
            /* Copied straight through: the harness fabricated this PTS and
             * declared its error, and the daemon carries both untouched. */
            envelope.capture_ts_ns = frame.capture_ts_ns;
            envelope.time_sync_error_ms = frame.time_sync_error_ms;
        }
        stats.frames_in++;

        warming = (int64_t)envelope.frame_seq < (int64_t)config.detector.warmup_frames;
        component_count = detector->apply(detector, luma, width, height, warming,
                                          components, SW_MAX_COMPONENTS, &occupancy);
        if (component_count < 0) {
            /* A failed frame still owes the Jetson a health packet. Skipping
             * housekeeping here meant that a detector failing on EVERY frame
             * — the loudest thing that can go wrong — produced total silence
             * on the health plane, which reads exactly like a dead node with
             * none of the information. */
            stats.frames_detector_failed++;
            goto housekeeping;
        }
        if (warming) {
            goto housekeeping;
        }

        sw_pipeline_frame(&pipeline, components, component_count, &envelope, scale_x,
                          scale_y, &event, &dropped);
        if (dropped > 0) {
            SW_LOG_DEBUG("frame %llu: cap kept %zu of %d components (%d dropped)",
                         (unsigned long long)envelope.frame_seq, event.count,
                         (int)event.count + dropped, dropped);
        }
        if (event.count > 0) {
            size_t len = 0;
            size_t would_be = 0;
            sw_wire_status_t status = sw_wire_encode_observation_packet(
                &event, datagram, sizeof(datagram), &len, &would_be);
            if (status != SW_WIRE_OK) {
                /* Loud, and counted as a drop: an event that cannot be sent
                 * whole is not sent at all, and the caller learns how far
                 * over it was. */
                stats.events_unencodable++;
                SW_LOG_ERR("frame %llu: %s (%zu observations, would have been %zu B "
                           "against a %d B ceiling)",
                           (unsigned long long)envelope.frame_seq,
                           sw_wire_strerror(status), event.count, would_be,
                           SW_DATAGRAM_CEILING_BYTES);
            } else {
                stats.capture_events++;
                stats.observations_sent += event.count;
                log_packet(packet_log, datagram, len);
                if (sw_udp_send(&sender, datagram, len) == 0) {
                    stats.measurement_datagrams_sent++;
                    stats.measurement_bytes_sent += len;
                }
            }
        }

    housekeeping:
        sw_control_poll(&control);
        now_ns = sw_monotonic_ns();
        if (now_ns - last_health_ns >= (int64_t)config.health_period_ms * 1000000LL) {
            double elapsed_s = (double)(now_ns - started_ns) / 1e9;
            double fps = elapsed_s > 0.0 ? (double)stats.frames_in / elapsed_s : 0.0;
            send_health(&config, &envelope, fps, total_drops(&stats, &pipeline, &sender, detector),
                        &sender, &stats);
            last_health_ns = now_ns;
        }
    }

    /* One last health packet so the final drop count reaches the Jetson even
     * when the run was shorter than a health period. */
    if (stats.frames_in > 0) {
        double elapsed_s = (double)(sw_monotonic_ns() - started_ns) / 1e9;
        double fps = elapsed_s > 0.0 ? (double)stats.frames_in / elapsed_s : 0.0;
        send_health(&config, &envelope, fps, total_drops(&stats, &pipeline, &sender, detector), &sender,
                    &stats);
    }

    SW_LOG_INFO("done: %llu frames in, %llu capture events, %llu observations sent, "
                "%llu components dropped over the cap, %llu events unencodable, "
                "%llu detector failures",
                (unsigned long long)stats.frames_in,
                (unsigned long long)stats.capture_events,
                (unsigned long long)stats.observations_sent,
                (unsigned long long)pipeline.components_dropped_over_cap,
                (unsigned long long)stats.events_unencodable,
                (unsigned long long)stats.frames_detector_failed);
    write_stats(config.stats_path, &config, &stats, &pipeline, &sender, &control,
                detector, detector != NULL ? detector->name : "none");

cleanup:
    if (packet_log != NULL) {
        fclose(packet_log);
    }
    if (detector != NULL) {
        detector->close(detector);
    }
    if (capture != NULL) {
        sw_capture_close(capture);
    }
    if (config.source != SW_SOURCE_VI) {
        sw_inject_close(&inject);
    }
    sw_control_close(&control);
    sw_udp_close(&sender);
    return exit_code;
}
