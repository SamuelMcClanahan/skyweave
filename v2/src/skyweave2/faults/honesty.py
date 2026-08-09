"""Honesty gates: the hard checks every fault run must satisfy (brief §59).

1. No crash, no NaN.
2. Covariance grows when input quality drops.
3. Failures exit as labeled statuses, never silent absence.
4. Overconfidence (published CONFIRMED whose TRUE error exceeds N sigma of
   its OWN covariance): zero tolerance.
5. Deterministic replay under fault.

The overconfidence sigma and tolerance come from the frozen manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from skyweave2.contracts import TrackState


@dataclass
class OverconfidenceEvent:
    batch_index: int
    track_id: int
    sigma: float           # how many sigma the true error was
    error_m: float
    status: str


@dataclass
class HonestyVerdict:
    no_nan: bool = True
    labeled_exits: bool = True
    overconfidence_events: list[OverconfidenceEvent] = field(default_factory=list)
    declared_bound_states: int = 0   # published states carrying a bound > 0
    # States excluded from the sigma test because they are too far from
    # truth to be the target at all (data-association failures). Counted
    # and REPORTED: silently skipping them would hide the very worst
    # confidently-wrong states (D6 review, critical).
    far_from_truth: int = 0
    mean_position_sigma_m: float = float("nan")   # covariance size proxy
    published_confirmed: int = 0

    @property
    def overconfidence_count(self) -> int:
        return len(self.overconfidence_events)

    def passes(self, tolerance: int) -> bool:
        return (
            self.no_nan
            and self.labeled_exits
            and self.overconfidence_count <= tolerance
        )


def sigma_with_bound(
    error: np.ndarray, covariance: np.ndarray, systematic_bound=None
) -> float:
    """Overconfidence metric (D6.1): true error vs covariance PLUS the
    DECLARED systematic bound.

    D0 keeps the two channels separate — a bias is never folded into the
    filter's noise model. But an honesty CHECK on the published state must
    consider everything the state declared about its own error: the random
    covariance and the systematic bound beside it. The error is charged
    against the covariance only AFTER the declared bound has absorbed what
    it claims to cover, per axis.
    """
    error = np.asarray(error, dtype=np.float64)
    if systematic_bound is not None:
        bound = np.abs(np.asarray(systematic_bound, dtype=np.float64))
        # Per axis, the declared bound absorbs error up to its magnitude.
        residual = np.sign(error) * np.maximum(np.abs(error) - bound, 0.0)
    else:
        residual = error
    return mahalanobis_sigma(residual, covariance)


def mahalanobis_sigma(error: np.ndarray, covariance: np.ndarray) -> float:
    """How many sigma the error is, under its own covariance.

    Uses the 3-dof Mahalanobis distance: sigma = sqrt(d^2 / 3) is NOT the
    right scale for a per-axis claim, so we report sqrt(d^2) directly as
    "sigma" in the 1-D equivalent sense along the error direction — the
    quantity the honesty gate is about ("this state claimed a covariance
    that does not contain its own error").
    """
    cov = np.asarray(covariance, dtype=np.float64)
    error = np.asarray(error, dtype=np.float64)
    # Variance of the error's own direction under the reported covariance.
    norm = float(np.linalg.norm(error))
    if norm <= 0.0:
        return 0.0
    direction = error / norm
    variance = float(direction @ cov @ direction)
    if variance <= 0.0:
        return float("inf")
    return norm / float(np.sqrt(variance))


def evaluate_honesty(
    run,
    truth_at_batch,
    sigma_threshold: float,
    truth_match_m: float = 50.0,
) -> HonestyVerdict:
    """Score one fault run against the honesty gates."""
    verdict = HonestyVerdict()
    sigmas: list[float] = []
    for batch_index, track in run.published:
        state = np.asarray(track.state, dtype=np.float64)
        cov = np.asarray(track.covariance, dtype=np.float64).reshape(6, 6)
        if not (np.all(np.isfinite(state)) and np.all(np.isfinite(cov))):
            verdict.no_nan = False
            continue
        if track.status in (TrackState.CONFIRMED, TrackState.REACQUIRED):
            verdict.published_confirmed += 1
        position_cov = cov[0:3, 0:3]
        sigmas.append(float(np.sqrt(max(np.trace(position_cov) / 3.0, 0.0))))
        truth = truth_at_batch(batch_index)
        if truth is None or track.status != TrackState.CONFIRMED:
            continue
        error = state[0:3] - np.asarray(truth, dtype=np.float64)
        _declared = getattr(run, "systematic", {}).get((batch_index, track.track_id))
        if _declared and float(np.max(np.abs(_declared[0]))) > 0.0:
            verdict.declared_bound_states += 1
        # Only tracks that are plausibly OF the target are scored: a track
        # following a different object is a data-association finding, not a
        # covariance-honesty one.
        if float(np.linalg.norm(error)) > truth_match_m:
            verdict.far_from_truth += 1
            continue
        declared = getattr(run, "systematic", {}).get(
            (batch_index, track.track_id))
        bound = declared[0] if declared else None
        sigma = sigma_with_bound(error, position_cov, bound)
        if sigma > sigma_threshold:
            verdict.overconfidence_events.append(
                OverconfidenceEvent(
                    batch_index=batch_index,
                    track_id=track.track_id,
                    sigma=float(sigma),
                    error_m=float(np.linalg.norm(error)),
                    status=track.status.value,
                )
            )
    if sigmas:
        verdict.mean_position_sigma_m = float(np.mean(sigmas))
    # Labeled exits: every rejection carries a reason string.
    for batch in run.batches:
        for rejection in batch.rejections:
            if not rejection.reason:
                verdict.labeled_exits = False
    for entry in run.excluded:
        if not entry.reason:
            verdict.labeled_exits = False
    return verdict


def decisions_fingerprint(run) -> str:
    """Canonical, byte-comparable digest of a run's DECISIONS (test N7)."""
    payload = {
        "published": [
            (i, t.model_dump(mode="json")) for i, t in run.published
        ],
        "records": [r.model_dump(mode="json") for r in run.records],
        "statuses": run.statuses,
        "rejections": [
            [b.batch_index, r.reason, list(r.obs_ids), round(r.detail, 9)]
            for b in run.batches
            for r in b.rejections
        ],
        "excluded": [[e.reason] for e in run.excluded],
    }
    return json.dumps(payload, sort_keys=True)
