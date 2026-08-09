from __future__ import annotations

import numpy as np
import pytest

from skyweave2.faults.config import load_manifest
from skyweave2.fusion.config import FusionConfig
from tests.fusion.conftest import look_at_camera, observe_point, target_position

RANGE_M, ALT_M, BASELINE_M = 243.84, 20.0, 20.0
FPS = 30.0


@pytest.fixture(scope="session")
def manifest():
    return load_manifest()


@pytest.fixture()
def cameras():
    aim = (0.0, RANGE_M, ALT_M)
    return {
        i: look_at_camera(i, (x, 0.0, 1.5), aim)
        for i, x in enumerate((-BASELINE_M / 2, 0.0, BASELINE_M / 2))
    }


@pytest.fixture()
def config():
    return FusionConfig()


def clean_stream(cameras, frames=range(0, 90), noise_px=0.1, seed=7):
    """A clean 3-camera observation stream on the manifest trajectory."""
    rng = np.random.default_rng(seed)
    out = []
    for frame in frames:
        out += observe_point(cameras, target_position(frame), frame_seq=frame,
                             noise_px=noise_px, rng=rng)
    return out


def truth_lookup(frames=range(0, 90)):
    positions = {f: target_position(f) for f in frames}
    return lambda batch_index: positions.get(batch_index)
