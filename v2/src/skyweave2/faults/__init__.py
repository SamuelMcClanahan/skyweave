"""D6 fault injection: measure what every defense layer does on bad data.

The campaign manifest ``v2/configs/d6_faults.yaml`` is FROZEN. This package
READS it and never restates its values: any magnitude appearing in code
would be a second source of truth, and the brief forbids that.

Layout:
  config.py      manifest loader + typed accessors (no magnitudes in code)
  injectors.py   Tier II (observations), III (calibration), IV (stream)
  image.py       Tier I: faulted U8 regenerated from kept radiance
  bookkeeping.py per-layer accept/reject attribution (7 layers)
  honesty.py     honesty gates incl. the 5-sigma overconfidence detector
  campaign.py    single-axis + held-out combined runners
  report.py      D6_FAULT_REPORT.md generator (seeded, byte-deterministic)
"""

from skyweave2.faults.config import FaultManifest, load_manifest

FAULTS_VERSION = "d6-faults/1"

__all__ = ["FAULTS_VERSION", "FaultManifest", "load_manifest"]
