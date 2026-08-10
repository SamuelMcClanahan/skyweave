/* The daemon's in-memory capture event: one envelope, up to CAP observations.
 *
 * These structs mirror the frozen D0 contracts (`FrameEnvelope`,
 * `Observation2D`) field for field, with the same names, so a reviewer can
 * put `contracts/frames.py` beside this header and check them off. They are
 * NOT the nanopb structs: keeping them separate means the daemon's pipeline
 * never depends on a generated type, and `sw_wire.c` is the single place
 * where a contract field meets a wire field.
 *
 * Bounds come from `proto/skyweave.options` via the generated header, never
 * from a literal here — the whole point of the .options file is that one
 * declaration sizes both sides.
 */

#ifndef SW_OBS_H
#define SW_OBS_H

#include "skyweave.pb.h"
#include "sw_common.h"

/* Buffer sizes are nanopb max_size (INCLUDING the NUL), so the usable text
 * length is one less. Taken from the generated struct so they cannot drift. */
#define SW_SESSION_UUID_MAX sizeof(((skyweave_v2_FrameEnvelope *)0)->session_uuid)
#define SW_CALIBRATION_REV_MAX sizeof(((skyweave_v2_FrameEnvelope *)0)->calibration_rev)
#define SW_DETECTOR_REV_MAX sizeof(((skyweave_v2_FrameEnvelope *)0)->detector_rev)
#define SW_EVIDENCE_REF_MAX sizeof(((skyweave_v2_Observation2D *)0)->evidence_ref)

/* The declared repeated-field bound: what nanopb ALLOCATES from, and the
 * hard ceiling on the detector's per-frame cap. */
#define SW_OBSERVATIONS_MAX \
    (sizeof(((skyweave_v2_ObservationPacket *)0)->observations) / \
     sizeof(skyweave_v2_Observation2D))

typedef struct {
    uint32_t camera_id;
    char session_uuid[SW_SESSION_UUID_MAX];
    uint64_t frame_seq;
    int64_t capture_ts_ns; /* EXPOSURE MIDPOINT (D0 section 3) */
    skyweave_v2_ClockDomain clock_domain;
    float time_sync_error_ms; /* honest, may be large */
    float exposure_us;
    float gain_db;
    uint32_t full_width;
    uint32_t full_height;
    uint32_t proc_width;
    uint32_t proc_height;
    char calibration_rev[SW_CALIBRATION_REV_MAX];
    char detector_rev[SW_DETECTOR_REV_MAX];
    float line_readout_us;
} sw_envelope_t;

typedef struct {
    uint32_t obs_id;
    double u; /* FULL-resolution calibrated px (D0 section 2) */
    double v;
    double cov_uu; /* full-res px^2, never zero */
    double cov_uv;
    double cov_vv;
    int32_t bbox_x; /* full-res px; signed, a bbox may start off-grid */
    int32_t bbox_y;
    uint32_t bbox_w;
    uint32_t bbox_h;
    uint32_t area_px; /* at proc resolution */
    uint32_t persistence_count;
    float confidence;
    bool has_local_blob_id;
    uint32_t local_blob_id;
    bool has_evidence_ref;
    char evidence_ref[SW_EVIDENCE_REF_MAX];
} sw_observation_t;

typedef struct {
    sw_envelope_t envelope;
    sw_observation_t observations[SW_OBSERVATIONS_MAX];
    size_t count;
} sw_capture_event_t;

#endif /* SW_OBS_H */
