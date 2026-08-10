"""Per-frame component cap and the drop bookkeeping that makes it loud.

One capture event is one datagram and measurement data never splits, so a
frame that produces more components than the wire bound admits is an event
that cannot be sent at all (D7-F1). The D8 opening closed that with a cap:
at most ``DetectorConfig.max_components_per_frame`` components become
observations, and everything the cap removes is COUNTED.

Three properties this module exists to guarantee:

1. **The cap is shared.** It lives in ``DetectorConfig``, which the host
   oracle and the RV1106 daemon both read, so the host stays the oracle for
   the D8 frame->packet fixtures instead of predicting a different frame.

2. **The ranking is declared, not emergent.** The D8 opening says "keeping
   the top components by descending confidence". The D8.0 amendment says
   what that confidence IS: :func:`component_confidence`, area-derived and
   saturating. A bare confidence sort would still be a partial order — every
   component at or above :data:`CONFIDENCE_SATURATION_AREA_PX` ties at 1.0 —
   so the full ranking key is written down here (:func:`rank_key`) and
   tested, with the tie-breakers in priority order. See finding D8-F1 in the
   report: confidence is a monotone function of area, so the SELECTION the
   cap makes is the same one area alone would make, and that is a statement
   about the detector, not about this cap.

3. **Nothing is silent.** :func:`apply_component_cap` returns the drop count
   beside the survivors; :class:`DetectorStats` aggregates it across a clip;
   the daemon's health packet carries it. A cap that quietly discarded
   measurements would be strictly worse than the loud ``TooManyObservations``
   failure it replaces.

Survivors keep their ORIGINAL order (raster order from ``find_components``),
so ``obs_id`` assignment is unchanged for any frame at or under the cap. The
cap changes which components are emitted, never how the emitted ones are
numbered.
"""

from __future__ import annotations

from dataclasses import dataclass

from skyweave2.detector.components import MaskComponent

# The component area, at PROC resolution, at which the wire confidence
# saturates. `confidence = min(1.0, area_px / 50.0)`: the D0 "D8.0 amendment"
# entry, Samuel's call after the D8.0 hand-back surfaced the runner/cap/daemon
# disagreement. Fifty proc-resolution pixels is the area the pre-D8 runner
# already treated as "as much evidence as this detector can offer"; adopting
# the formula it carried keeps the number a recorded decision rather than a
# fresh invention. It is a saturation point, not a threshold — nothing is
# dropped at 49 px and nothing is promoted at 51.
CONFIDENCE_SATURATION_AREA_PX = 50.0

# (component, persistence_count, local_blob_id) as `PersistenceFilter.update`
# returns it.
Emitted = tuple[MaskComponent, int, int]


def component_confidence(component: MaskComponent, persistence_count: int) -> float:
    """The detector's confidence in one component, 0..1.

    ``min(1.0, area_px / CONFIDENCE_SATURATION_AREA_PX)``, per the D0 "D8.0
    amendment" entry. Area is the only evidence a background model plus
    connected components has: there is no appearance model and no
    likelihood, so this is a monotone restatement of "how much of the frame
    said something happened", not a probability. It is not calibrated and
    the Jetson must not treat it as one; the NPU appearance gate
    (RV1106_EDGE_NODE.md section 9, phase 2) is what would make it a real
    number.

    ``persistence_count`` is deliberately NOT in the formula. It is already
    on the wire as its own field, and folding it in would double-count the
    same evidence in a value the fusion side may later weight by.

    THE definition, singular. The runner reports what this returns, the cap
    ranks by what this returns, and ``sw_pipeline.c`` mirrors it — the D8.0
    hand-back found those three disagreeing (D8-F6), and one function is the
    only arrangement in which they cannot.

    ``area_px`` is at PROC resolution, so the value is resolution-dependent
    by construction: the same object at 1536x864 and at 1152x648 does not
    get the same confidence. That is honest for a per-node quantity — it
    says how much evidence THIS node saw — and it is one more reason the
    number is not a probability.
    """
    del persistence_count  # already its own wire field; see above
    return min(1.0, component.area_px / CONFIDENCE_SATURATION_AREA_PX)


def rank_key(item: Emitted) -> tuple[float, float, float, float]:
    """Sort key for cap survival: earlier is kept.

    Priority order, each level only reached when the level above ties:

    1. ``-confidence`` — the D8 opening's rule, now an actual quantity
       (:func:`component_confidence`) rather than a constant.
    2. ``-area_px`` — the largest blob at proc resolution. Today's
       confidence is a monotone non-decreasing function of exactly this, so
       levels 1 and 2 never disagree and level 2 is what separates the
       components that saturate at 1.0. The SELECTION is therefore the one
       area alone would make (D8-F1) — which is the right one to make: area
       is the evidence the detector does have, a bigger foreground region is
       a better-conditioned centroid, and it matches the edge byte
       governor's "descending blob confidence" intent
       (RV1106_EDGE_NODE.md section 8) as closely as a model-free detector
       can. When a real confidence model lands, level 1 starts disagreeing
       with level 2 and outranks it, which is the point of the order.
    3. ``centroid_v`` then 4. ``centroid_u`` — raster order, purely to make
       the choice TOTAL. Without them two equal-area blobs would be ordered
       by list position, which is stable in CPython and meaningless as a
       rule; the C daemon sorts the same key and must not have to reproduce
       an accident of Python's sort.
    """
    component, persistence_count, _blob_id = item
    return (
        -component_confidence(component, persistence_count),
        -float(component.area_px),
        component.centroid_v,
        component.centroid_u,
    )


@dataclass(frozen=True)
class CapResult:
    """What survived one frame's cap, and what did not."""

    kept: list[Emitted]
    dropped: int
    offered: int

    @property
    def at_cap(self) -> bool:
        return self.dropped > 0


def apply_component_cap(emitted: list[Emitted], cap: int) -> CapResult:
    """Keep the top ``cap`` components by :func:`rank_key`; count the rest.

    Survivors are returned in the order they were offered, not in rank
    order: the caller assigns ``obs_id`` by position, and re-ordering would
    renumber observations on frames the cap never touched.
    """
    if cap < 1:
        raise ValueError(f"component cap must be at least 1, got {cap}")
    offered = len(emitted)
    if offered <= cap:
        return CapResult(kept=list(emitted), dropped=0, offered=offered)
    survivors = set(
        index
        for index, _ in sorted(
            ((i, item) for i, item in enumerate(emitted)),
            key=lambda pair: rank_key(pair[1]),
        )[:cap]
    )
    kept = [item for index, item in enumerate(emitted) if index in survivors]
    return CapResult(kept=kept, dropped=offered - cap, offered=offered)


@dataclass
class DetectorStats:
    """Clip-level detector bookkeeping. Counters only; no rates, no opinions.

    ``components_dropped_over_cap`` is the number the brief requires to be
    visible in "detector stats and the health path, never silent". It is a
    count of MEASUREMENTS THIS NODE CHOSE NOT TO SEND, which is exactly the
    quantity that has no other trace anywhere downstream: a dropped
    component leaves no gap in ``frame_seq``, no rejected datagram, and no
    aligner exclusion. If it is not counted here it does not exist.
    """

    # Frames the cap was APPLIED to, i.e. scored frames. Warm-up frames emit
    # nothing and are not counted here; `frames_at_cap / frames` is therefore
    # a rate over the frames where the question could arise.
    frames: int = 0
    frames_at_cap: int = 0
    components_offered: int = 0
    components_emitted: int = 0
    components_dropped_over_cap: int = 0
    max_components_offered: int = 0

    def record(self, result: CapResult) -> None:
        self.frames += 1
        self.components_offered += result.offered
        self.components_emitted += len(result.kept)
        self.components_dropped_over_cap += result.dropped
        self.max_components_offered = max(self.max_components_offered, result.offered)
        if result.at_cap:
            self.frames_at_cap += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "frames": self.frames,
            "frames_at_cap": self.frames_at_cap,
            "components_offered": self.components_offered,
            "components_emitted": self.components_emitted,
            "components_dropped_over_cap": self.components_dropped_over_cap,
            "max_components_offered": self.max_components_offered,
        }


__all__ = [
    "CONFIDENCE_SATURATION_AREA_PX",
    "CapResult",
    "DetectorStats",
    "apply_component_cap",
    "component_confidence",
    "rank_key",
]
