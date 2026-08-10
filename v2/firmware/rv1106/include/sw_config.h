/* Daemon configuration. Every knob, no constants buried in the code.
 *
 * The detector block MIRRORS `skyweave2/detector/config.py` field for field
 * and default for default. That is not tidiness: the host detector is the
 * ORACLE for the D8 frame->packet fixtures, so a default that differs here
 * would make the daemon predict a different frame than the fixture recorded,
 * and the tolerance table would be measuring a config drift.
 *
 * Values that ARE the freeze (the datagram ceiling, the magic bytes, the
 * declared field bounds) are NOT here — they live in sw_wire.h and
 * skyweave.pb.h as constants, because a config field implies "tune me".
 * Same split as the host's wire.py vs TransportConfig.
 */

#ifndef SW_CONFIG_H
#define SW_CONFIG_H

#include "sw_obs.h"

typedef enum {
    SW_SOURCE_INJECT_FILE = 0, /* C1: a SWIJ stream from local storage */
    SW_SOURCE_INJECT_TCP = 1,  /* C1: a SWIJ stream over Ethernet */
    SW_SOURCE_VI = 2           /* real CSI capture through RKMPI VI */
} sw_source_kind_t;

typedef enum {
    SW_DETECTOR_SOFT = 0, /* portable C GMM2+CCL; host and QEMU */
    SW_DETECTOR_IVE = 1   /* RK_MPI_IVE_GMM2 + RK_MPI_IVE_CCL; the board */
} sw_detector_kind_t;

/* Mirrors DetectorConfig.ive_approx (IveApproxParams), whose names in turn
 * mirror the RK_MPI_IVE_GMM2 control set. The model-count bound is the
 * host's (`model_num: int = Field(default=3, ge=1, le=5)`), so a config that
 * one side accepts the other cannot refuse. */
#define SW_GMM2_MAX_MODELS 5

typedef struct {
    int model_num;      /* u8MaxModelNum */
    float var_init;     /* u10q0InitVar, as a variance in DN^2 */
    float var_min;      /* u10q0MinVar */
    float learn_rate;   /* u8GlobalLearningRateMode */
    float bg_ratio;     /* u8q2BgRatio */
    float match_sigmas; /* u8VarThreshGen, as a Mahalanobis gate */
    float weight_init;  /* u8q2WeightInitVal */
} sw_gmm2_params_t;

typedef struct {
    sw_gmm2_params_t gmm2;

    int proc_width;
    int proc_height;
    int warmup_frames;

    int open_radius_px; /* morphological opening; 0 = off */
    int min_area_px;    /* at proc resolution */
    int max_area_px;

    int persistence_frames;
    float persistence_gate_px;

    /* D0 section 10, "D8 opening". Must never exceed SW_OBSERVATIONS_MAX;
     * sw_config_validate() refuses to start otherwise. */
    int max_components_per_frame;

    double centroid_cov_floor_px2; /* FULL-resolution px^2, never zero */
    float exposure_us;
    float fps;
    char detector_rev[SW_DETECTOR_REV_MAX];
    char calibration_rev[SW_CALIBRATION_REV_MAX];
} sw_detector_config_t;

typedef struct {
    sw_source_kind_t source;
    sw_detector_kind_t detector_kind;

    char inject_path[512];  /* SW_SOURCE_INJECT_FILE */
    int inject_port;        /* SW_SOURCE_INJECT_TCP */

    char jetson_host[64];
    int measurement_port;
    int control_port;
    int health_period_ms;

    uint32_t camera_id;
    char session_uuid[SW_SESSION_UUID_MAX]; /* empty: generated at boot */

    /* Where the daemon writes the datagrams it sent, as hex, one per line.
     * The fixture-replay gate (E5/E6) compares this against the host's
     * expected packet bytes. Empty disables it; it is never on the
     * measurement path's critical section on a deployed node. */
    char packet_log_path[512];
    char stats_path[512];

    sw_detector_config_t detector;
} sw_config_t;

void sw_config_defaults(sw_config_t *config);

/* Refuses on any inconsistency rather than clamping. Returns 0 or -1. */
int sw_config_validate(const sw_config_t *config);

/* argv parsing. Returns 0 on success, 1 when --help was asked for, -1 on a
 * bad argument (already logged). */
int sw_config_parse_args(sw_config_t *config, int argc, char **argv);

void sw_config_usage(const char *argv0);

#endif /* SW_CONFIG_H */
