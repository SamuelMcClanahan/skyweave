"""Comparing a detector against the oracle, under DECLARED bounds.

The D8 fixture policy is split. The wire half is exact and has no machinery:
bytes match or they do not. This module is the other half — the toleranced
one — and it exists because "the board's GMM2 differs from ive_approx" is a
Measured result, not a failure, right up until it exceeds a bound somebody
wrote down first.

Two rules the design follows, both from the brief:

1. **Bounds are declared BEFORE the run.** :class:`DetectorTolerance` values
   are committed in ``v2/docs/D8_EDGE_REPORT.md`` and imported from
   :data:`DECLARED_TOLERANCE`; the comparison reads them, never fits them.
   A tolerance chosen after seeing the divergence is not a tolerance.

2. **The comparison reports before it judges.** :class:`Divergence` carries
   the full picture — matched, missed, extra, centroid statistics — and
   ``passes`` is a derived opinion about it. A bound that trips must be
   readable as a number, not as the word FAIL.

Matching is nearest-neighbour within a declared radius, per capture event.
Two detectors that find the same blob will agree to a fraction of a pixel;
one that finds a different blob will not be matched to it at any radius that
means anything, so the radius separates "moved slightly" from "found
something else" instead of blurring them together.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from skyweave2.contracts import Observation2D
from skyweave2.edge.obsfixture import group_by_capture_event


@dataclass(frozen=True)
class DetectorTolerance:
    """Bounds a candidate detector must hold against the oracle.

    Every field is a bound on a DIVERGENCE, never a quality target: the
    oracle's own accuracy is D4's business and is not re-litigated here.
    """

    #: Two observations are the same blob if their full-res centroids are
    #: within this radius. Above it they are different findings, not a
    #: displaced one.
    match_radius_px: float
    #: Mean and p95 centroid difference over MATCHED pairs, full-res px.
    centroid_mean_px: float
    centroid_p95_px: float
    #: Fraction of the oracle's observations with no match at all.
    missed_fraction: float
    #: Extra observations per capture event the candidate found and the
    #: oracle did not.
    extra_per_event: float
    #: Fraction of capture events where the two disagree about the count.
    count_mismatch_fraction: float


@dataclass
class Divergence:
    """What the comparison found. Numbers first; the verdict is derived."""

    oracle_observations: int = 0
    candidate_observations: int = 0
    oracle_events: int = 0
    candidate_events: int = 0
    common_events: int = 0
    matched: int = 0
    missed: int = 0
    extra: int = 0
    count_mismatch_events: int = 0
    centroid_deltas_px: list[float] = field(default_factory=list)

    @property
    def centroid_mean_px(self) -> float:
        if not self.centroid_deltas_px:
            return 0.0
        return sum(self.centroid_deltas_px) / len(self.centroid_deltas_px)

    @property
    def centroid_p95_px(self) -> float:
        if not self.centroid_deltas_px:
            return 0.0
        ordered = sorted(self.centroid_deltas_px)
        # Nearest-rank p95: no interpolation, so the reported number is a
        # value the detector actually produced.
        index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return ordered[index]

    @property
    def centroid_max_px(self) -> float:
        return max(self.centroid_deltas_px, default=0.0)

    @property
    def missed_fraction(self) -> float:
        if self.oracle_observations == 0:
            return 0.0
        return self.missed / self.oracle_observations

    @property
    def extra_per_event(self) -> float:
        if self.common_events == 0:
            return 0.0
        return self.extra / self.common_events

    @property
    def count_mismatch_fraction(self) -> float:
        if self.common_events == 0:
            return 0.0
        return self.count_mismatch_events / self.common_events

    def breaches(self, tolerance: DetectorTolerance) -> list[str]:
        """Every bound this divergence exceeds, named with both numbers."""
        out = []
        checks = (
            ("centroid mean", self.centroid_mean_px, tolerance.centroid_mean_px, "px"),
            ("centroid p95", self.centroid_p95_px, tolerance.centroid_p95_px, "px"),
            ("missed fraction", self.missed_fraction, tolerance.missed_fraction, ""),
            ("extra per event", self.extra_per_event, tolerance.extra_per_event, ""),
            (
                "count-mismatch fraction",
                self.count_mismatch_fraction,
                tolerance.count_mismatch_fraction,
                "",
            ),
        )
        for name, measured, bound, unit in checks:
            if measured > bound:
                out.append(f"{name}: {measured:.4f}{unit} > declared {bound:.4f}{unit}")
        return out

    def passes(self, tolerance: DetectorTolerance) -> bool:
        return not self.breaches(tolerance)

    def as_dict(self) -> dict:
        return {
            "oracle_observations": self.oracle_observations,
            "candidate_observations": self.candidate_observations,
            "oracle_events": self.oracle_events,
            "candidate_events": self.candidate_events,
            "common_events": self.common_events,
            "matched": self.matched,
            "missed": self.missed,
            "extra": self.extra,
            "count_mismatch_events": self.count_mismatch_events,
            "centroid_mean_px": self.centroid_mean_px,
            "centroid_p95_px": self.centroid_p95_px,
            "centroid_max_px": self.centroid_max_px,
            "missed_fraction": self.missed_fraction,
            "extra_per_event": self.extra_per_event,
            "count_mismatch_fraction": self.count_mismatch_fraction,
        }


def _event_key(envelope) -> tuple[int, int]:
    """A capture event's identity: camera and frame. NOT the session uuid —
    the daemon and the oracle legitimately carry different ones, and NOT the
    timestamp, which is what a clock divergence would corrupt."""
    return (envelope.camera_id, envelope.frame_seq)


def compare_to_oracle(
    oracle: list[Observation2D],
    candidate: list[Observation2D],
    tolerance: DetectorTolerance,
) -> Divergence:
    """Match a candidate detector's observations against the oracle's.

    Matching is global-nearest-first WITHIN one capture event, the same
    closest-pair-first rule the persistence filter uses, and for the same
    reason: greedy-in-list-order lets an early far pair claim a partner that
    a later near pair needed.
    """
    divergence = Divergence()
    oracle_events = {_event_key(env): obs for env, obs in group_by_capture_event(oracle)}
    candidate_events = {
        _event_key(env): obs for env, obs in group_by_capture_event(candidate)
    }
    divergence.oracle_observations = len(oracle)
    divergence.candidate_observations = len(candidate)
    divergence.oracle_events = len(oracle_events)
    divergence.candidate_events = len(candidate_events)

    for key, oracle_obs in oracle_events.items():
        candidate_obs = candidate_events.get(key)
        if candidate_obs is None:
            # The candidate produced no event at all here: every oracle
            # observation is missed, and the event is not "common" so it
            # cannot dilute the per-event rates.
            divergence.missed += len(oracle_obs)
            continue
        divergence.common_events += 1
        if len(candidate_obs) != len(oracle_obs):
            divergence.count_mismatch_events += 1

        pairs = []
        for i, a in enumerate(oracle_obs):
            for j, b in enumerate(candidate_obs):
                distance = math.hypot(a.u - b.u, a.v - b.v)
                if distance <= tolerance.match_radius_px:
                    pairs.append((distance, i, j))
        pairs.sort()
        used_oracle: set[int] = set()
        used_candidate: set[int] = set()
        for distance, i, j in pairs:
            if i in used_oracle or j in used_candidate:
                continue
            used_oracle.add(i)
            used_candidate.add(j)
            divergence.matched += 1
            divergence.centroid_deltas_px.append(distance)
        divergence.missed += len(oracle_obs) - len(used_oracle)
        divergence.extra += len(candidate_obs) - len(used_candidate)

    for key, candidate_obs in candidate_events.items():
        if key not in oracle_events:
            # An event the oracle never produced. Counted as extra, and the
            # event joins the common count so `extra_per_event` has an honest
            # denominator rather than a flattering one.
            divergence.common_events += 1
            divergence.count_mismatch_events += 1
            divergence.extra += len(candidate_obs)
    return divergence


# --------------------------------------------------------------------------
# The DECLARED bounds
# --------------------------------------------------------------------------

#: Portable C GMM2 (`sw_detect_soft.c`) vs the `ive_approx` host oracle, at
#: the fixture resolution. DECLARED 2026-08-09, before the first board run,
#: and recorded in `v2/docs/D8_EDGE_REPORT.md`. Rationale for each number is
#: in the report's tolerance section; changing one here without changing it
#: there is a documentation break, and test E5 reads BOTH.
HOST_SOFT_TOLERANCE = DetectorTolerance(
    match_radius_px=4.0,
    centroid_mean_px=0.50,
    centroid_p95_px=1.50,
    missed_fraction=0.10,
    extra_per_event=0.30,
    count_mismatch_fraction=0.35,
)

#: Hardware RK_MPI_IVE_GMM2 + IVE_CCL vs the same oracle. DECLARED
#: 2026-08-09, BEFORE any board exists to measure — which is the point of
#: the anti-tuning rule. Wider than the host bounds because four structural
#: divergences are known in advance and named in `sw_detect_ive.c`:
#: 8-connected CCL against the oracle's 4-connected, a centroid computed by
#: the A7 over the label image rather than by the labeller, an adaptive area
#: threshold the hardware may raise on crowded frames, and fixed-point
#: quantisation of every GMM2 control.
BOARD_IVE_TOLERANCE = DetectorTolerance(
    match_radius_px=6.0,
    centroid_mean_px=1.00,
    centroid_p95_px=3.00,
    missed_fraction=0.20,
    extra_per_event=0.50,
    count_mismatch_fraction=0.50,
)

DECLARED_TOLERANCE = {
    "soft": HOST_SOFT_TOLERANCE,
    "ive": BOARD_IVE_TOLERANCE,
}


# --------------------------------------------------------------------------
# E8: the benchmark's run-to-run bounds
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkTolerance:
    """How far two runs of the SAME benchmark configuration may differ.

    A sibling of :class:`DetectorTolerance` and declared under the same rule —
    before the run, in the report, read by a test — but it bounds a different
    kind of quantity. The detector bounds cover a DIVERGENCE between two
    detectors; these cover the REPEATABILITY of one measurement on one machine.

    Only three axes appear here, and the omissions are the design:

    - the deterministic counters (frames, capture events, observations, drops)
      are compared EXACTLY, not bounded. The same frames through the same
      detector produce the same components; a tolerance on that would be
      permission for a defect. ``benchmark.EXACT_COUNTERS`` is that list.
    - DDR bandwidth and thermals have no bound because they have no host
      measurement to bound. When the board produces them, a bound for them is a
      decision to record then — with the numbers still unseen, which is the
      only time a bound can honestly be set.

    Each value is a RELATIVE difference, |a - b| / mean, so one bound covers
    all three resolutions instead of three absolute numbers that would each
    need their own justification.
    """

    #: Sustained fps, taken from the daemon's own health-packet rate rather
    #: than the harness's wall clock (the harness figure includes process
    #: startup, which is not the node's).
    fps_relative: float
    #: Peak resident set. The daemon allocates everything at startup and never
    #: allocates in the frame loop, so two runs of one configuration should
    #: differ by page-granularity noise and nothing else.
    peak_rss_relative: float
    #: CPU seconds per wall second. A ratio of two measured quantities, so it
    #: carries both errors and gets the loosest bound of the three.
    cpu_utilisation_relative: float


#: DECLARED 2026-08-10, BEFORE any board has run the sweep — the same
#: anti-tuning rule the detector bounds were declared under, applied to E8
#: ("two runs, same config, stats within declared run-to-run bounds").
#:
#: The numbers, and why each is what it is:
#:
#: - **fps 10%.** The node is a fixed-clock single-core device with nothing
#:   else running on it, so the honest expectation is a few percent. Ten
#:   leaves room for scheduler and I/O noise while still failing on the things
#:   this bound exists to catch: thermal throttling between runs, a source that
#:   starved one run and not the other, and a background process that should
#:   not be on a sealed node at all.
#: - **peak RSS 5%.** Allocation happens once at startup. Five percent is
#:   already generous for a quantity that should be identical, and it is not
#:   zero only because the two mechanisms that can read it (a kernel high-water
#:   mark, a sampled maximum) do not have to agree to the page.
#: - **CPU utilisation 25%.** cpu_seconds / wall_seconds on a short run is a
#:   small number over a small number. The bound is wide because the estimator
#:   is, and it is still narrow enough to catch a run that spent a quarter more
#:   of the core than its twin.
#:
#: Exceeding one is a finding to investigate and record, not automatically a
#: failure — but the bound is what decides which conversation happens, and it
#: exists before the data. Changing a number here without changing it in
#: `v2/docs/D8_EDGE_REPORT.md` is a documentation break, and the E8 harness
#: test reads BOTH.
BENCHMARK_RUN_TO_RUN = BenchmarkTolerance(
    fps_relative=0.10,
    peak_rss_relative=0.05,
    cpu_utilisation_relative=0.25,
)

DECLARED_BENCHMARK_TOLERANCE = {
    "run_to_run": BENCHMARK_RUN_TO_RUN,
}

__all__ = [
    "BENCHMARK_RUN_TO_RUN",
    "BOARD_IVE_TOLERANCE",
    "BenchmarkTolerance",
    "DECLARED_BENCHMARK_TOLERANCE",
    "DECLARED_TOLERANCE",
    "DetectorTolerance",
    "Divergence",
    "HOST_SOFT_TOLERANCE",
    "compare_to_oracle",
]
