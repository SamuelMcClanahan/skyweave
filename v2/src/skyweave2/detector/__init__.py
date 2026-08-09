"""D4 host detector: U8 luma clips in, frozen-contract Observation2D out.

Backends: ``fixed_bg`` (sanity baseline), ``mog2`` (OpenCV, the PRIMARY host
baseline for all D4 scorecards), ``ive_approx`` (numpy GMM whose knobs mirror
the RK_MPI_IVE_GMM2 controls so D8 board behavior can be predicted).

Dependency rule (finding F-D0-8): OpenCV is REQUIRED. If cv2 is missing the
detector refuses to run — no silent pure-python fallback on the
authoritative path, ever.
"""

from skyweave2.detector.config import DetectorConfig
from skyweave2.detector.runner import detect_clip, run_detector

DETECTOR_VERSION = "d4-detector/1"

__all__ = ["DETECTOR_VERSION", "DetectorConfig", "detect_clip", "run_detector"]
