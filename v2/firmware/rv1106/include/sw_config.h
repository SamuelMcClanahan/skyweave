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
    SW_SOURCE_VI = 2,          /* real CSI capture through RKMPI VI */
    SW_SOURCE_INJECT_RAM = 3   /* C1: a SWIJ clip preloaded into DDR and looped */
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

/* The RAM loop's budget line, and the ONLY reading of it this daemon has.
 *
 * DECIMAL MB (10^6 B), and a DAEMON-ONLY budget: the clip arena plus the
 * detector's own allocation plus the daemon's fixed buffers. It is the upper
 * end of RV1106_EDGE_NODE.md section 6's with-NPU subtotal, "~120-160 MB".
 * That subtotal counts its own "~30-50 MB" kernel+rootfs row, which this
 * budget does NOT include, so the two are not the same quantity. Section 6
 * states no numeric margin at all — its Notes cells read "still fits with
 * margin" and "very comfortable", which is prose — so no margin figure is
 * derived from it here. Overridable with --ram-budget-mb, because which line
 * governs is an open question and a daemon that hid it would settle it. */
#define SW_RAM_BUDGET_DEFAULT_MB 160

typedef struct {
    sw_source_kind_t source;
    sw_detector_kind_t detector_kind;

    /* SW_SOURCE_INJECT_FILE, and SW_SOURCE_INJECT_RAM: the RAM loop
     * deliberately reuses this field because it is the same thing, a path to
     * a SWIJ stream, read by the same parser. */
    char inject_path[512];
    int inject_port;        /* SW_SOURCE_INJECT_TCP */

    /* SW_SOURCE_INJECT_RAM. Every value below is DECLARED by the harness on
     * the command line and the daemon derives NONE of them: a loop that
     * computed its own timestamp advance or its own run length would be the
     * daemon forming an opinion about capture time, which is the one thing
     * sw_inject.h's header rules out. sw_config_validate refuses each of them
     * rather than substituting a default. */
    uint32_t ram_loop_frames;      /* total frames the run serves; 0 = unset */
    int64_t ram_loop_pts_stride_ns; /* capture_ts_ns advance per pass */
    int64_t ram_loop_period_ns;    /* per-frame pace; 0 = unpaced */
    int ram_budget_mb;             /* SW_RAM_BUDGET_DEFAULT_MB unless overridden */
    /* Where the RAM loop's heap preflight reads MemAvailable. The budget
     * above is pool-blind — its detector term lives in the media heap while
     * the clip arena is a plain malloc — so before that malloc the daemon
     * reads the heap actually left and refuses loud rather than letting the
     * OOM killer refuse silently (F-C1-2). A path with no MemAvailable (any
     * non-Linux host build) skips the check, out loud. */
    char meminfo_path[256];        /* --meminfo-path; default /proc/meminfo */

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

/* The source mode as a string, for --stats. Returns exactly the spellings
 * `skyweave2.edge.benchmark.SOURCE_MODES` uses ("inject-file", "inject-tcp",
 * "inject-ram", "capture-vi"), because a collected node artifact whose source
 * mode does not match the harness's own vocabulary cannot be joined to it. */
const char *sw_source_name(sw_source_kind_t source);

/* Refuses on any inconsistency rather than clamping. Returns 0 or -1. */
int sw_config_validate(const sw_config_t *config);

/* argv parsing. Returns 0 on success, 1 when --help was asked for, -1 on a
 * bad argument (already logged). */
int sw_config_parse_args(sw_config_t *config, int argc, char **argv);

void sw_config_usage(const char *argv0);

#endif /* SW_CONFIG_H */
