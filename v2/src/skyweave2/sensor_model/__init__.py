"""Stage B sensor model: linear radiance in, realistic-wrong U8 Y out.

Implements the frozen chain of SYNTHETIC_PIPELINE_DESIGN §5.1 exactly, in
that order, every stage toggleable through `SensorModelSpec`. Renderer
agnostic: the input is a linear radiance array, never an image with a view
transform baked in.

``SENSOR_MODEL_VERSION`` participates in the dataset ID hash (design §9);
bump it on any change that alters output bytes.
"""

from skyweave2.sensor_model.config import SensorModelSpec, TruthCameraOptics
from skyweave2.sensor_model.pipeline import AeState, fpn_rng, frame_rng, render_frame

SENSOR_MODEL_VERSION = "d3-sensor-model/1"

__all__ = [
    "SENSOR_MODEL_VERSION",
    "AeState",
    "SensorModelSpec",
    "TruthCameraOptics",
    "fpn_rng",
    "frame_rng",
    "render_frame",
]
