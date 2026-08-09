"""Fixtures for the D7 W-series.

Two stream sources, deliberately:

- a SYNTHETIC clean stream over the frozen exp001 geometry, which always
  runs, so the parity and fault tests can genuinely fail on any machine;
- the REAL golden gate-clip observations under ``output/``, which are
  machine-local data. Tests that need them skip when they are absent (the
  F9/F10 precedent) but hard-fail when they are present-but-incomplete.

The synthetic stream exists precisely so that "all W tests skipped" can never
be mistaken for "all W tests passed".
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from skyweave2.contracts import Observation2D
from skyweave2.fusion.config import FusionConfig
from tests.fusion.conftest import look_at_camera, observe_point, target_position

RANGE_M, ALT_M, BASELINE_M = 243.84, 20.0, 20.0
FPS = 30.0

V2 = Path(__file__).resolve().parents[2]
GATE_CLIPS = V2.parent / "output" / "exp001_clips" / "gate"
GATE_RENDER = V2.parent / "output" / "exp001_renders" / "gate"


@pytest.fixture()
def cameras():
    """The exp001 trio: 20 m east-west baseline aimed at the crossing point."""
    aim = (0.0, RANGE_M, ALT_M)
    return {
        i: look_at_camera(i, (x, 0.0, 1.5), aim)
        for i, x in enumerate((-BASELINE_M / 2, 0.0, BASELINE_M / 2))
    }


@pytest.fixture()
def config():
    return FusionConfig()


def synthetic_stream(cameras, frames=range(0, 120), noise_px=0.1, seed=7):
    """A clean 3-camera arrival-ordered stream on the manifest trajectory."""
    rng = np.random.default_rng(seed)
    out = []
    for frame in frames:
        out += observe_point(
            cameras, target_position(frame), frame_seq=frame, noise_px=noise_px, rng=rng
        )
    out.sort(key=lambda o: (o.envelope.capture_ts_ns, o.envelope.camera_id, o.obs_id))
    return out


@pytest.fixture()
def stream(cameras):
    return synthetic_stream(cameras)


def synthetic_truth(frames=range(0, 120)):
    positions = {f: target_position(f) for f in frames}
    return lambda batch_index: positions.get(batch_index)


@pytest.fixture()
def truth_at_batch():
    return synthetic_truth()


def gate_artifacts_present() -> bool:
    return GATE_CLIPS.exists() and GATE_RENDER.exists()


requires_gate_clip = pytest.mark.skipif(
    not gate_artifacts_present(),
    reason=(
        "golden gate artifacts absent under output/ (machine-local data). The "
        "synthetic-stream variant of this test still runs and still fails on a "
        "real defect."
    ),
)


@pytest.fixture(scope="session")
def gate_observations() -> list[Observation2D]:
    """Detector output over the real golden gate clip (slow; session-scoped)."""
    if not gate_artifacts_present():
        pytest.skip("golden gate artifacts absent under output/")
    for camera_id in (0, 1, 2):
        assert (GATE_CLIPS / f"cam{camera_id}" / "clip.json").exists(), (
            f"{GATE_CLIPS} incomplete: cam{camera_id}/clip.json missing"
        )
    from skyweave2.fusion.metrics import observation_stream
    from skyweave2.fusion.report import _detector_config

    return observation_stream(GATE_CLIPS, _detector_config())


@pytest.fixture(scope="session")
def gate_cameras_fixture():
    if not gate_artifacts_present():
        pytest.skip("golden gate artifacts absent under output/")
    from skyweave2.fusion.report import gate_cameras

    return gate_cameras(GATE_RENDER)


@pytest.fixture(scope="session")
def gate_truth():
    if not gate_artifacts_present():
        pytest.skip("golden gate artifacts absent under output/")
    from skyweave2.fusion.metrics import load_render_truth

    return load_render_truth(GATE_RENDER)


@pytest.fixture(scope="session")
def acceptance_manifest() -> dict:
    """The frozen scene manifest's acceptance block (never restated in code)."""
    import yaml

    manifest = yaml.safe_load((V2 / "configs" / "exp001_scene.yaml").read_text())
    return manifest["acceptance"]


def dump_observations(observations) -> str:
    """Canonical JSON for exact stream comparison in assertions."""
    return json.dumps(
        [o.model_dump(mode="json") for o in observations], sort_keys=True
    )


def as_wire(observations):
    """The stream as the FROZEN SCHEMA can represent it.

    D0's type column declares ``time_sync_error_ms``, ``exposure_us``,
    ``gain_db``, ``line_readout_us`` and ``confidence`` as ``float``, so a
    value like 0.1 comes back as its binary32 neighbour. Comparing a
    socket-delivered stream against the raw input would therefore fail for a
    reason that has nothing to do with the transport.

    This helper is used ONLY when the two sides of a comparison differ in
    whether they crossed the wire. Socket-vs-socket comparisons and every
    decision-level comparison use raw equality, so it can never be used to
    paper over a real asymmetry. ``u``, ``v``, the covariance terms and all
    timestamps are exact on the wire and are unaffected by it.
    """
    from skyweave2.transport.codec import wire_normalize

    return [wire_normalize(o) for o in observations]
