#include <stdio.h>
#include <string.h>

#include "sw_pipeline.h"

#define CHECK(cond)                                                            \
    do {                                                                       \
        if (!(cond)) {                                                         \
            fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, \
                    #cond);                                                    \
            return 1;                                                          \
        }                                                                      \
    } while (0)

static void run_frame(sw_pipeline_t *pipeline, const sw_component_t *component,
                      uint64_t frame_seq, sw_capture_event_t *event)
{
    sw_envelope_t envelope;
    int dropped = -1;
    memset(&envelope, 0, sizeof(envelope));
    envelope.frame_seq = frame_seq;
    sw_pipeline_frame(pipeline, component, 1, &envelope, 2.0, 2.0, event,
                      &dropped);
    if (dropped != 0) {
        event->count = SW_OBSERVATIONS_MAX + 1u;
    }
}

int main(void)
{
    sw_detector_config_t config;
    sw_component_t component;
    sw_component_t tied_components[8];
    sw_pipeline_t pipeline;
    sw_capture_event_t event;
    sw_envelope_t envelope;
    int dropped;
    int index;

    memset(&config, 0, sizeof(config));
    config.persistence_frames = 2;
    config.persistence_gate_px = 12.0;
    config.max_components_per_frame = 7;
    config.centroid_cov_floor_px2 = 0.25;
    memset(&component, 0, sizeof(component));
    component.centroid_u = 20.0;
    component.centroid_v = 10.0;
    component.area_px = 25;
    component.bbox_x = 18;
    component.bbox_y = 8;
    component.bbox_w = 5;
    component.bbox_h = 5;

    sw_pipeline_init(&pipeline, &config);
    run_frame(&pipeline, &component, 30, &event);
    CHECK(event.count == 0);

    /* A detector-failed frame terminates the chain without becoming a scored
     * pipeline frame. */
    sw_pipeline_reset_persistence(&pipeline);
    CHECK(pipeline.frames == 1);

    run_frame(&pipeline, &component, 32, &event);
    CHECK(event.count == 0);
    run_frame(&pipeline, &component, 33, &event);
    CHECK(event.count == 1);
    CHECK(event.observations[0].persistence_count == 2);
    CHECK(pipeline.frames == 3);

    /* Python's cap sort is stable. C qsort is not, so a frame whose complete
     * rank key ties must make offered index the final selection rule. */
    memset(tied_components, 0, sizeof(tied_components));
    for (index = 0; index < 8; ++index) {
        tied_components[index].centroid_u = 5.0;
        tied_components[index].centroid_v = 5.0;
        tied_components[index].area_px = 50;
        tied_components[index].bbox_x = index;
        tied_components[index].bbox_y = index;
        tied_components[index].bbox_w = 1;
        tied_components[index].bbox_h = 1;
    }
    config.persistence_frames = 1;
    sw_pipeline_init(&pipeline, &config);
    memset(&envelope, 0, sizeof(envelope));
    envelope.frame_seq = 40;
    dropped = -1;
    sw_pipeline_frame(&pipeline, tied_components, 8, &envelope, 1.0, 1.0,
                      &event, &dropped);
    CHECK(dropped == 1);
    CHECK(event.count == 7);
    for (index = 0; index < 7; ++index) {
        CHECK(event.observations[index].local_blob_id == (uint32_t)index);
    }
    return 0;
}
