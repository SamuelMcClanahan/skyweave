"""Stage D: truth labels, the detector scorecard, and hybrid clip generation."""

from skyweave2.eval.labels import TruthLabel, read_labels, write_labels
from skyweave2.eval.scorecard import ScorecardConfig, score_clip

__all__ = [
    "ScorecardConfig",
    "TruthLabel",
    "read_labels",
    "score_clip",
    "write_labels",
]
