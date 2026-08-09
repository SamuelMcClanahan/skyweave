"""Per-layer accept/reject bookkeeping — the D6 core deliverable.

Seven defense layers (brief §"The layer table"):

  1 mask cleanup + persistence   2 epipolar prefilter   3 consensus
  4 residual/status gates        5 leave-one-out        6 NIS gate
  7 lifecycle incl. suppression

For every injected item we record the layer that TERMINATED it (the first
layer that rejected or excluded it), or ``accepted`` if it survived into a
published track. Attribution reads the engine's own audit records — the
association/engine RejectionRecords, the aligner exclusions, and the
tracker's TrackUpdateRecords — so bookkeeping cannot claim a layer the
pipeline did not actually exercise.

False accept = a planted (false) item that reached a published track.
False reject = a TRUE item that some layer threw away.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skyweave2.faults.injectors import PlantLedger

LAYERS = {
    1: "mask_cleanup_persistence",
    2: "epipolar_prefilter",
    3: "consensus",
    4: "residual_status_gates",
    5: "leave_one_out",
    6: "nis_gate",
    7: "lifecycle_suppression",
}

# How each recorded rejection reason maps onto a layer number.
REASON_TO_LAYER = {
    # aligner exclusions are pre-layer-1 stream hygiene; the brief's layer 1
    # is the detector-side cleanup that owns them at the fusion boundary.
    "late": 1,
    "stale_or_duplicate": 1,
    "sequence_anomaly": 1,
    # association-stage rejections
    "epipolar": 2,              # pair failed the epipolar/ray-geometry gate
    "unassociated": 3,          # never won consensus / never paired
    "weak_geometry": 4,
    "residual": 4,
    "pruned_member": 5,
    # tracker
    "nis_gate": 6,
    "lifecycle": 7,
}


@dataclass
class LayerTally:
    accepted_true: int = 0
    accepted_false: int = 0        # false accepts: planted items published
    rejected_true: int = 0         # false rejects: true items thrown away
    rejected_false: int = 0        # correct rejections of planted items


@dataclass
class LayerTable:
    axis: str
    magnitude: str
    per_layer: dict[int, LayerTally] = field(default_factory=dict)
    terminating_layer: dict[str, int] = field(default_factory=dict)  # obs_key -> layer
    accepted_keys: set = field(default_factory=set)
    total_true: int = 0
    total_planted: int = 0

    def tally(self, layer: int) -> LayerTally:
        return self.per_layer.setdefault(layer, LayerTally())

    @property
    def false_accepts(self) -> int:
        return sum(t.accepted_false for t in self.per_layer.values())

    @property
    def false_rejects(self) -> int:
        return sum(t.rejected_true for t in self.per_layer.values())

    @property
    def false_accept_rate(self) -> float:
        return self.false_accepts / self.total_planted if self.total_planted else 0.0

    @property
    def false_reject_rate(self) -> float:
        return self.false_rejects / self.total_true if self.total_true else 0.0


def build_layer_table(
    axis: str,
    magnitude: str,
    ledger: PlantLedger,
    run,
    all_input_keys: set[str],
) -> LayerTable:
    """Attribute every observation in the faulted stream to a layer.

    ``run`` is a fusion ``RunResult``; ``all_input_keys`` is every obs_key
    that entered the aligner (post-injection).
    """
    table = LayerTable(axis=axis, magnitude=magnitude)
    planted = set(ledger.planted)
    table.total_planted = len(planted)
    table.total_true = len(all_input_keys - planted)

    # 1. Aligner exclusions (layer 1 hygiene).
    for entry in run.excluded:
        from skyweave2.fusion.aligner import obs_key

        key = obs_key(entry.obs)
        layer = REASON_TO_LAYER.get(entry.reason, 1)
        table.terminating_layer.setdefault(key, layer)

    # 2/3/4/5. Association + engine rejections.
    for batch in run.batches:
        for rejection in batch.rejections:
            layer = REASON_TO_LAYER.get(rejection.reason, 4)
            for key in rejection.obs_ids:
                table.terminating_layer.setdefault(key, layer)

    # 6. NIS-gated updates (tracker rejected the measurement).
    for record in run.records:
        if not record.accepted:
            for key in record.obs_ids:
                table.terminating_layer.setdefault(key, REASON_TO_LAYER["nis_gate"])

    # Accepted: observations consumed by an ACCEPTED tracker update.
    accepted: set[str] = set()
    for record in run.records:
        if record.accepted:
            accepted.update(record.obs_ids)
    table.accepted_keys = accepted

    # Anything that entered but neither reached an accepted update nor was
    # explicitly rejected was dropped for want of association support.
    for key in all_input_keys:
        if key in accepted or key in table.terminating_layer:
            continue
        table.terminating_layer[key] = REASON_TO_LAYER["unassociated"]

    for key in all_input_keys:
        is_planted = key in planted
        if key in accepted:
            layer = 7  # survived every layer, published by lifecycle
            # Record it: leaving accepted items out of terminating_layer
            # made consumers KeyError exactly when a false accept occurred
            # (D6 review) — the one case the table exists to show.
            table.terminating_layer[key] = layer
            tally = table.tally(layer)
            if is_planted:
                tally.accepted_false += 1
            else:
                tally.accepted_true += 1
        else:
            layer = table.terminating_layer.get(key, 3)
            tally = table.tally(layer)
            if is_planted:
                tally.rejected_false += 1
            else:
                tally.rejected_true += 1
    return table
