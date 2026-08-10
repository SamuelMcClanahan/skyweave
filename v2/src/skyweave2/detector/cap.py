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
   the top components by descending confidence". The D4 detector has NO
   confidence model — ``runner.detect_clip`` emits ``confidence = 1.0`` for
   every observation — so a bare confidence sort is a total tie and the
   survivors would be whatever order the components arrived in. The full
   ranking key is therefore written down here (:func:`rank_key`) and tested,
   with the tie-breakers in priority order. See finding D8-F1 in the report:
   the tie-breaker is doing the real work today, and that is a statement
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

# The confidence the D4 detector assigns to every component. Stated as a
# constant here, and asserted against `runner`'s emitted observations by test
# E1, so that "rank by confidence" cannot silently become meaningful (or
# silently stop being meaningful) without the ranking being revisited.
DETECTOR_CONFIDENCE = 1.0

# (component, persistence_count, local_blob_id) as `PersistenceFilter.update`
# returns it.
Emitted = tuple[MaskComponent, int, int]


def component_confidence(component: MaskComponent, persistence_count: int) -> float:
    """The detector's confidence in one component, 0..1.

    Constant by construction: the D4 detector is a background-model +
    connected-components pipeline with no appearance model and no
    likelihood, so it has nothing to be more or less confident about. The
    NPU appearance gate (RV1106_EDGE_NODE.md section 9, phase 2) is what
    would make this a real number.

    It is a function rather than a literal so the cap's ranking has ONE
    place to change when that day comes, and so the tests can assert that
    what the runner puts on the wire and what the cap ranks by are the same
    quantity.
    """
    del component, persistence_count  # no evidence to condition on yet
    return DETECTOR_CONFIDENCE


def rank_key(item: Emitted) -> tuple[float, float, float, float]:
    """Sort key for cap survival: earlier is kept.

    Priority order, each level only reached when the level above ties:

    1. ``-confidence`` — the D8 opening's rule, and the only level that will
       matter once a confidence model exists.
    2. ``-area_px`` — the largest blob at proc resolution. Under a constant
       confidence this is the level that actually decides, and it is the
       right proxy: area is the evidence the detector does have, a bigger
       foreground region is a better-conditioned centroid, and it matches
       the edge byte governor's "descending blob confidence" intent
       (RV1106_EDGE_NODE.md section 8) as closely as a model-free detector
       can.
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
    "DETECTOR_CONFIDENCE",
    "CapResult",
    "DetectorStats",
    "apply_component_cap",
    "component_confidence",
    "rank_key",
]
