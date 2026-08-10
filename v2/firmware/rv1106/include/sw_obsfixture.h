/* Reader for the `SWOB` observation fixture (skyweave2/edge/obsfixture.py).
 *
 * The dumbest possible loop over fixed-width big-endian records, on purpose.
 * Test E2 asks whether two ENCODERS agree for identical inputs; if this
 * reader were clever it could become the thing that differs, and a byte
 * mismatch would be a parser bug wearing an encoder bug's clothes.
 */

#ifndef SW_OBSFIXTURE_H
#define SW_OBSFIXTURE_H

#include "sw_obs.h"

typedef struct {
    int fd;
    uint16_t declared_events;
    uint16_t read_events;
} sw_obsfixture_t;

/* Returns 0 on success, -1 on any malformation. */
int sw_obsfixture_open(sw_obsfixture_t *fixture, int fd);

/* Returns 0 when an event was read, 1 at the trailer, -1 on error. */
int sw_obsfixture_next(sw_obsfixture_t *fixture, sw_capture_event_t *event);

#endif /* SW_OBSFIXTURE_H */
